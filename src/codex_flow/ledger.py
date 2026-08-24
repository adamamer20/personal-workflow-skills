"""The durable, local source of truth for Codex workflow state.

H2 deliberately keeps this module small and boring: the standard-library
``sqlite3`` connection is the only persistence boundary, writes are explicit
transactions, and every state mutation appends its event before commit.  No
SDK, subprocess, network, or worktree code belongs here.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeAlias

from .domain import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    DispatchClaim,
    DispatchId,
    EventId,
    EventRecord,
    EventSequence,
    Generation,
    JsonObject,
    LedgerSnapshot,
    MilestoneId,
    MilestoneRecord,
    PostIdentityExecutionFailure,
    PreIdentityTransportFailure,
    ReasonCode,
    RecoveryFact,
    ReviewRejected,
    RoleId,
    RunId,
    RunRecord,
    SchemaVersion,
    State,
    TerminalFailureAfterIdentity,
    TerminalOutcome,
    TransportFailureBeforeIdentity,
    WorkflowReason,
    WorkflowState,
    coerce_state,
)

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | dict[str, "JSONValue"] | list["JSONValue"]

CURRENT_SCHEMA_VERSION = SchemaVersion(2)
SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
SUPPORTED_SCHEMA_VERSIONS = frozenset({SchemaVersion(1), CURRENT_SCHEMA_VERSION})

_STATES_SQL = ", ".join(f"'{state.value}'" for state in WorkflowState)
_EVENT_TYPE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_FORBIDDEN_FIELD_PATTERN = re.compile(
    r"(?:secret|password|credential|token|prompt|payload|request|response|raw[_ -]?sdk|raw[_ -]?response|authorization)",
    re.IGNORECASE,
)


class LedgerError(RuntimeError):
    """Base class for durable-ledger failures."""


class LedgerClosedError(LedgerError):
    """An operation was attempted after ``close``."""


class SchemaError(LedgerError):
    """The SQLite file is corrupt or is not an owned ledger schema."""


class CorruptSchemaError(SchemaError):
    """Schema metadata or required tables/columns are invalid."""


class UnsupportedSchemaVersion(SchemaError):
    """The ledger was written by a newer or unsupported schema."""


class RecordNotFound(LedgerError):
    """A requested run, milestone, or dispatch does not exist."""


class InvalidTransition(LedgerError):
    """A state transition is not in the explicit H2 transition table."""


InvalidTransitionError = InvalidTransition


class StaleWriter(LedgerError):
    """The caller's expected predecessor is no longer current."""


StaleStateError = StaleWriter


class DispatchConflict(LedgerError):
    """A milestone/role is already owned by another logical dispatch."""


DispatchOwnershipConflict = DispatchConflict


FaultInjector = Callable[[str], None]


def utc_now() -> str:
    """Return a sortable, explicit UTC timestamp."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _identifier(value: str | Any, constructor: Callable[[str], Any], label: str) -> Any:
    try:
        return constructor(value if isinstance(value, str) else str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc


def _run_id(value: RunId | str) -> RunId:
    return _identifier(value, RunId, "run id")


def _milestone_id(value: MilestoneId | str) -> MilestoneId:
    return _identifier(value, MilestoneId, "milestone id")


def _role(value: RoleId | str) -> RoleId:
    return _identifier(value, RoleId, "role")


def _generation(value: Generation | int | str) -> Generation:
    try:
        return Generation(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid generation: {value!r}") from exc


def _safe_json_value(value: object, *, field_name: str = "metadata", depth: int = 0) -> JSONValue:
    """Validate the intentionally non-sensitive projection metadata contract.

    Ledger metadata is for small identifiers and diagnostics, never model
    inputs or SDK payloads.  Rejecting suspicious field names before SQL keeps
    secrets from being accidentally made durable and makes the boundary easy
    to audit.
    """

    if depth > 12:
        raise ValueError(f"{field_name} is nested too deeply")
    if isinstance(value, dict):
        output: dict[str, JSONValue] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key or len(key) > 96:
                raise ValueError(f"{field_name} keys must be bounded strings")
            if _FORBIDDEN_FIELD_PATTERN.search(key):
                raise ValueError(f"{field_name} cannot persist sensitive field {key!r}")
            output[key] = _safe_json_value(child, field_name=f"{field_name}.{key}", depth=depth + 1)
        return output
    if isinstance(value, list):
        return [_safe_json_value(child, field_name=field_name, depth=depth + 1) for child in value]
    if value is None or isinstance(value, str | int | float | bool):
        if isinstance(value, str) and ("\x00" in value or len(value) > 4096):
            raise ValueError(f"{field_name} contains an unsafe or oversized string")
        return value
    raise TypeError(f"{field_name} must be JSON-serializable")


def _json_object(value: Mapping[str, object] | None, *, field_name: str) -> tuple[JsonObject, str]:
    checked = _safe_json_value(dict(value or {}), field_name=field_name)
    assert isinstance(checked, dict)
    return checked, json.dumps(checked, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_object(raw: str | None, *, field_name: str) -> JsonObject:
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"invalid JSON in {field_name}") from exc
    if not isinstance(value, dict):
        raise SchemaError(f"{field_name} must contain a JSON object")
    checked = _safe_json_value(value, field_name=field_name)
    assert isinstance(checked, dict)
    return checked


def _reason(value: WorkflowReason | ReasonCode | str | BaseException | None) -> WorkflowReason | None:
    if value is None:
        return None
    if isinstance(value, WorkflowReason):
        if value.detail is not None and _FORBIDDEN_FIELD_PATTERN.search(value.detail):
            raise ValueError("reason detail cannot persist sensitive content")
        return value
    if isinstance(value, TransportFailureBeforeIdentity):
        return PreIdentityTransportFailure(str(value) or None)
    if isinstance(value, TerminalFailureAfterIdentity):
        return PostIdentityExecutionFailure(str(value) or None)
    if isinstance(value, ReasonCode):
        return WorkflowReason(value)
    try:
        return WorkflowReason(ReasonCode(value))
    except ValueError as exc:
        raise ValueError(f"unknown reason code: {value!r}") from exc


def _row_state(value: str) -> WorkflowState:
    try:
        return WorkflowState(value)
    except ValueError as exc:
        raise SchemaError(f"unknown persisted workflow state: {value!r}") from exc


def _event_type(value: str) -> str:
    if not isinstance(value, str) or _EVENT_TYPE_PATTERN.fullmatch(value) is None:
        raise ValueError("event type must be lowercase ASCII and at most 64 characters")
    return value


class Ledger:
    """One connection to one repository-local workflow ledger."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 5.0,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("SQLite busy timeout must be positive")
        self.path = Path(path)
        if self.path.is_symlink():
            raise SchemaError(f"refusing symlink workflow ledger path: {self.path}")
        self._timeout = timeout
        self._fault_injector = fault_injector
        self._connection: sqlite3.Connection | None = None
        self._open()

    def _open(self) -> None:
        if self._connection is not None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(self.path),
                timeout=self._timeout,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1000)}")
            self._connection = connection
            self._ensure_schema()
        except sqlite3.DatabaseError as exc:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise SchemaError(f"unable to open workflow ledger {self.path}") from exc

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise LedgerClosedError("workflow ledger is closed")
        return self._connection

    @property
    def schema_version(self) -> SchemaVersion:
        row = self.connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            raise CorruptSchemaError("schema_version marker is missing")
        try:
            return SchemaVersion(row[0])
        except ValueError as exc:
            raise CorruptSchemaError("schema_version marker is not an integer") from exc

    def _ensure_schema(self) -> None:
        connection = self.connection
        tables = {
            str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if not tables:
            self._create_schema()
            return
        if "schema_meta" not in tables:
            raise CorruptSchemaError("workflow ledger is missing schema metadata")
        try:
            version = self.schema_version
        except sqlite3.DatabaseError as exc:
            raise CorruptSchemaError("unable to read schema metadata") from exc
        if version > CURRENT_SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(f"ledger schema {version} is newer than supported {CURRENT_SCHEMA_VERSION}")
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersion(f"ledger schema {version} is unsupported")
        marker = connection.execute("SELECT value FROM schema_meta WHERE key = 'migration_marker'").fetchone()
        if marker is None and version == CURRENT_SCHEMA_VERSION:
            raise CorruptSchemaError("migration marker is missing")
        if marker is not None and marker[0] not in {"complete", f"v{int(version)}"}:
            raise CorruptSchemaError("invalid migration marker")
        if version < CURRENT_SCHEMA_VERSION:
            self._migrate(version)
        self._validate_shape()

    def _create_schema(self) -> None:
        sql = f"""
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY NOT NULL CHECK(length(key) > 0),
                value TEXT NOT NULL CHECK(length(value) > 0)
            );
            INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?);
            INSERT INTO schema_meta(key, value) VALUES ('migration_marker', 'complete');

            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY NOT NULL CHECK(length(run_id) BETWEEN 1 AND 128),
                created_at TEXT NOT NULL,
                closed_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{{}}'
                    CHECK(json_valid(metadata_json) AND json_type(metadata_json) = 'object')
            );

            CREATE TABLE milestones (
                run_id TEXT NOT NULL,
                milestone_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK(current_state IN ({_STATES_SQL})),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{{}}'
                    CHECK(json_valid(metadata_json) AND json_type(metadata_json) = 'object'),
                PRIMARY KEY(run_id, milestone_id),
                FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
            );

            CREATE TABLE dispatches (
                dispatch_id TEXT PRIMARY KEY NOT NULL CHECK(length(dispatch_id) BETWEEN 1 AND 512),
                run_id TEXT NOT NULL,
                milestone_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(length(role) BETWEEN 1 AND 128),
                generation INTEGER NOT NULL CHECK(generation > 0),
                claimed_at TEXT NOT NULL,
                UNIQUE(run_id, milestone_id, role),
                FOREIGN KEY(run_id, milestone_id) REFERENCES milestones(run_id, milestone_id) ON DELETE CASCADE
            );

            CREATE TABLE events (
                event_id TEXT PRIMARY KEY NOT NULL CHECK(length(event_id) BETWEEN 1 AND 256),
                run_id TEXT NOT NULL,
                milestone_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK(sequence > 0),
                from_state TEXT CHECK(from_state IS NULL OR from_state IN ({_STATES_SQL})),
                to_state TEXT NOT NULL CHECK(to_state IN ({_STATES_SQL})),
                event_type TEXT NOT NULL CHECK(length(event_type) BETWEEN 1 AND 64),
                reason_code TEXT,
                reason_detail TEXT,
                dispatch_id TEXT,
                data_json TEXT CHECK(data_json IS NULL OR (json_valid(data_json) AND json_type(data_json) = 'object')),
                occurred_at TEXT NOT NULL,
                UNIQUE(run_id, milestone_id, sequence),
                FOREIGN KEY(run_id, milestone_id) REFERENCES milestones(run_id, milestone_id) ON DELETE CASCADE,
                FOREIGN KEY(dispatch_id) REFERENCES dispatches(dispatch_id) ON DELETE RESTRICT
            );
        """
        try:
            with self._transaction():
                # executescript manages the DDL transaction itself when
                # isolation is disabled, so use individual statements inside
                # our explicit write transaction to retain rollback semantics.
                self.connection.execute(
                    "CREATE TABLE schema_meta (key TEXT PRIMARY KEY NOT NULL CHECK(length(key) > 0), "
                    "value TEXT NOT NULL CHECK(length(value) > 0))"
                )
                self.connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                    (str(int(CURRENT_SCHEMA_VERSION)),),
                )
                self.connection.execute("INSERT INTO schema_meta(key, value) VALUES ('migration_marker', 'complete')")
                statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
                # The first three statements above are intentionally skipped
                # from the generated block, leaving the four owned data tables.
                for statement in statements[3:]:
                    self.connection.execute(statement)
        except sqlite3.OperationalError as exc:
            # Two first openers may observe an empty file concurrently.  The
            # loser waits for the winner's transaction, then observes the
            # already committed schema and validates it read-only.
            if "already exists" not in str(exc).lower():
                raise
            self._ensure_schema()

    def _validate_shape(self) -> None:
        required: dict[str, set[str]] = {
            "schema_meta": {"key", "value"},
            "runs": {"run_id", "created_at", "metadata_json"},
            "milestones": {"run_id", "milestone_id", "current_state", "created_at", "updated_at", "metadata_json"},
            "dispatches": {"dispatch_id", "run_id", "milestone_id", "role", "generation", "claimed_at"},
            "events": {
                "event_id",
                "run_id",
                "milestone_id",
                "sequence",
                "from_state",
                "to_state",
                "event_type",
                "reason_code",
                "reason_detail",
                "dispatch_id",
                "data_json",
                "occurred_at",
            },
        }
        for table, expected_columns in required.items():
            columns = {str(row[1]) for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()}
            if not expected_columns.issubset(columns):
                raise CorruptSchemaError(f"ledger table {table!r} is missing required columns")
        foreign_keys = self.connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or int(foreign_keys[0]) != 1:
            raise CorruptSchemaError("foreign-key enforcement is disabled")

    def _migrate(self, version: SchemaVersion) -> None:
        if version != SchemaVersion(1):
            raise UnsupportedSchemaVersion(f"ledger schema {version} has no owned migration")
        with self._transaction():
            columns = {str(row[1]) for row in self.connection.execute("PRAGMA table_info(runs)").fetchall()}
            if "closed_at" not in columns:
                self.connection.execute("ALTER TABLE runs ADD COLUMN closed_at TEXT")
            self.connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('migration_marker', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"v{int(CURRENT_SCHEMA_VERSION)}",),
            )
            self.connection.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(int(CURRENT_SCHEMA_VERSION)),),
            )
            self._fault("after_migration")
            self.connection.execute("UPDATE schema_meta SET value = 'complete' WHERE key = 'migration_marker'")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise
        else:
            connection.execute("COMMIT")

    def _fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def reopen(self) -> Ledger:
        self.close()
        self._open()
        return self

    def open(self) -> Ledger:
        self._open()
        return self

    def __enter__(self) -> Ledger:
        self._open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Record creation and lookup
    # ------------------------------------------------------------------

    def create_run(self, run_id: RunId | str, *, metadata: Mapping[str, object] | None = None) -> RunRecord:
        run = _run_id(run_id)
        checked, encoded = _json_object(metadata, field_name="run metadata")
        created = utc_now()
        with self._transaction():
            existing = self.connection.execute("SELECT * FROM runs WHERE run_id = ?", (str(run),)).fetchone()
            if existing is not None:
                if _decode_object(existing["metadata_json"], field_name="run metadata") != checked:
                    raise LedgerError(f"run {run} already exists with different metadata")
                return self._run_from_row(existing)
            self.connection.execute(
                "INSERT INTO runs(run_id, created_at, closed_at, metadata_json) VALUES (?, ?, NULL, ?)",
                (str(run), created, encoded),
            )
            return RunRecord(run, created, None, checked)

    def get_run(self, run_id: RunId | str) -> RunRecord:
        run = _run_id(run_id)
        row = self.connection.execute("SELECT * FROM runs WHERE run_id = ?", (str(run),)).fetchone()
        if row is None:
            raise RecordNotFound(f"run {run} does not exist")
        return self._run_from_row(row)

    def close_run(self, run_id: RunId | str) -> RunRecord:
        run = _run_id(run_id)
        closed = utc_now()
        with self._transaction():
            self.get_run(run)
            self.connection.execute(
                "UPDATE runs SET closed_at = COALESCE(closed_at, ?) WHERE run_id = ?", (closed, str(run))
            )
            return self.get_run(run)

    def create_milestone(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> MilestoneRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        checked, encoded = _json_object(metadata, field_name="milestone metadata")
        now = utc_now()
        with self._transaction():
            self.get_run(run)
            existing = self.connection.execute(
                "SELECT * FROM milestones WHERE run_id = ? AND milestone_id = ?", (str(run), str(milestone))
            ).fetchone()
            if existing is not None:
                if _decode_object(existing["metadata_json"], field_name="milestone metadata") != checked:
                    raise LedgerError(f"milestone {milestone} already exists with different metadata")
                return self._milestone_from_row(existing)
            self.connection.execute(
                "INSERT INTO milestones(run_id, milestone_id, current_state, created_at, updated_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(run), str(milestone), WorkflowState.PLANNED.value, now, now, encoded),
            )
            return MilestoneRecord(run, milestone, WorkflowState.PLANNED, now, now, checked)

    def get_milestone(self, milestone_id: MilestoneId | str, *, run_id: RunId | str | None = None) -> MilestoneRecord:
        milestone = _milestone_id(milestone_id)
        if run_id is None:
            row = self.connection.execute(
                "SELECT * FROM milestones WHERE milestone_id = ? ORDER BY run_id LIMIT 1", (str(milestone),)
            ).fetchone()
        else:
            run = _run_id(run_id)
            row = self.connection.execute(
                "SELECT * FROM milestones WHERE run_id = ? AND milestone_id = ?", (str(run), str(milestone))
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"milestone {milestone} does not exist")
        return self._milestone_from_row(row)

    def current_state(self, milestone_id: MilestoneId | str, *, run_id: RunId | str | None = None) -> WorkflowState:
        return self.get_milestone(milestone_id, run_id=run_id).state

    # ------------------------------------------------------------------
    # Dispatch claims and state transitions
    # ------------------------------------------------------------------

    def claim_dispatch(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        role: RoleId | str,
        generation: Generation | int | str,
    ) -> DispatchClaim:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        role_value = _role(role)
        generation_value = _generation(generation)
        dispatch_id = DispatchId.from_parts(run, milestone, role_value, generation_value)
        now = utc_now()
        with self._transaction():
            self.get_run(run)
            existing = self.connection.execute(
                "SELECT * FROM dispatches WHERE dispatch_id = ?", (str(dispatch_id),)
            ).fetchone()
            if existing is not None:
                return self._dispatch_from_row(existing)
            owner = self.connection.execute(
                "SELECT dispatch_id FROM dispatches WHERE run_id = ? AND milestone_id = ? AND role = ?",
                (str(run), str(milestone), str(role_value)),
            ).fetchone()
            if owner is not None:
                raise DispatchConflict(f"milestone {milestone} role {role_value} is owned by {owner['dispatch_id']}")
            current = self.get_milestone(milestone, run_id=run)
            if current.state is not WorkflowState.PLANNED:
                raise InvalidTransition(f"dispatch claim requires PLANNED, found {current.state.value}")
            self.connection.execute(
                "INSERT INTO dispatches(dispatch_id, run_id, milestone_id, role, generation, claimed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(dispatch_id), str(run), str(milestone), str(role_value), int(generation_value), now),
            )
            self.connection.execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (WorkflowState.STARTING.value, now, str(run), str(milestone)),
            )
            self._fault("after_state_update")
            self._append_event_in_transaction(
                run,
                milestone,
                from_state=WorkflowState.PLANNED,
                to_state=WorkflowState.STARTING,
                event_type="dispatch_claimed",
                reason=WorkflowReason(ReasonCode.DISPATCH_CLAIMED),
                dispatch_id=dispatch_id,
                data=None,
            )
            self._fault("after_event_insert")
            return DispatchClaim(dispatch_id, run, milestone, role_value, generation_value, now)

    def get_dispatch(self, dispatch_id: DispatchId | str) -> DispatchClaim:
        dispatch = DispatchId(dispatch_id if isinstance(dispatch_id, str) else str(dispatch_id))
        row = self.connection.execute("SELECT * FROM dispatches WHERE dispatch_id = ?", (str(dispatch),)).fetchone()
        if row is None:
            raise RecordNotFound(f"dispatch {dispatch} does not exist")
        return self._dispatch_from_row(row)

    def claim(self, dispatch_id: DispatchId | str) -> DispatchClaim:
        """Claim from a prevalidated logical identity string."""

        dispatch = DispatchId(dispatch_id if isinstance(dispatch_id, str) else str(dispatch_id))
        run, milestone, role, generation = dispatch.parts
        return self.claim_dispatch(run, milestone, role, generation)

    claim_identity = claim

    def transition(
        self,
        run_or_milestone: RunId | MilestoneId | str,
        milestone_or_state: MilestoneId | WorkflowState | State | str,
        to_state: WorkflowState | State | str | None = None,
        *,
        expected_state: WorkflowState | State | str | None = None,
        expected: WorkflowState | State | str | None = None,
        reason: WorkflowReason | ReasonCode | str | BaseException | None = None,
        event_type: str | None = None,
        data: Mapping[str, object] | None = None,
        dispatch_id: DispatchId | str | None = None,
    ) -> EventRecord:
        """Advance a milestone and append its causal event atomically.

        Both ``transition(milestone_id, target, ...)`` and the explicit
        ``transition(run_id, milestone_id, target, ...)`` form are accepted.
        The latter is preferred when milestone ids are reused across runs.
        """

        explicit_run: RunId | None
        if to_state is None:
            explicit_run = None
            milestone = _milestone_id(run_or_milestone)
            target = coerce_state(milestone_or_state)
        else:
            explicit_run = _run_id(run_or_milestone)
            milestone = _milestone_id(milestone_or_state)
            target = coerce_state(to_state)
        if expected_state is not None and expected is not None:
            raise ValueError("pass only one of expected_state or expected")
        expected_predecessor = expected_state if expected_state is not None else expected
        expected_value = coerce_state(expected_predecessor) if expected_predecessor is not None else None
        normalized_reason = _reason(reason)
        encoded_data: JsonObject | None = None
        if data is not None:
            encoded_data, _ = _json_object(data, field_name="event data")
        dispatch = None
        if dispatch_id is not None:
            dispatch = DispatchId(dispatch_id if isinstance(dispatch_id, str) else str(dispatch_id))
        normalized_event_type = _event_type(event_type or "state_transition")
        with self._transaction():
            current = self.get_milestone(milestone, run_id=explicit_run)
            if expected_value is not None and current.state is not expected_value:
                raise StaleWriter(
                    f"stale milestone writer: expected {expected_value.value}, current is {current.state.value}"
                )
            if target not in ALLOWED_TRANSITIONS[current.state]:
                raise InvalidTransition(f"{current.state.value} -> {target.value} is not allowed")
            if dispatch is not None and (
                dispatch.parts[0] != current.run_id or dispatch.parts[1] != current.milestone_id
            ):
                raise DispatchConflict("event dispatch identity does not belong to the milestone")
            if dispatch is not None:
                dispatch_row = self.connection.execute(
                    "SELECT 1 FROM dispatches WHERE dispatch_id = ?", (str(dispatch),)
                ).fetchone()
                if dispatch_row is None:
                    raise RecordNotFound(f"dispatch {dispatch} does not exist")
            now = utc_now()
            self.connection.execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (target.value, now, str(current.run_id), str(current.milestone_id)),
            )
            self._fault("after_state_update")
            if normalized_reason is None and target in TERMINAL_STATES:
                normalized_reason = WorkflowReason(ReasonCode.TERMINAL_OUTCOME)
            event = self._append_event_in_transaction(
                current.run_id,
                current.milestone_id,
                from_state=current.state,
                to_state=target,
                event_type=normalized_event_type,
                reason=normalized_reason,
                dispatch_id=dispatch,
                data=encoded_data,
            )
            self._fault("after_event_insert")
            return event

    def transition_state(self, *args: Any, **kwargs: Any) -> EventRecord:
        return self.transition(*args, **kwargs)

    advance = transition
    append_event = transition

    def _append_event_in_transaction(
        self,
        run_id: RunId,
        milestone_id: MilestoneId,
        *,
        from_state: WorkflowState | None,
        to_state: WorkflowState,
        event_type: str,
        reason: WorkflowReason | None,
        dispatch_id: DispatchId | None,
        data: JsonObject | None,
    ) -> EventRecord:
        last = self.connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE run_id = ? AND milestone_id = ?",
            (str(run_id), str(milestone_id)),
        ).fetchone()
        sequence = EventSequence(int(last[0]) + 1)
        event_id = EventId(f"{run_id}/{milestone_id}/{int(sequence)}")
        encoded_data = None
        if data is not None:
            _, encoded_data = _json_object(data, field_name="event data")
        self.connection.execute(
            "INSERT INTO events(event_id, run_id, milestone_id, sequence, from_state, to_state, event_type, "
            "reason_code, reason_detail, dispatch_id, data_json, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(event_id),
                str(run_id),
                str(milestone_id),
                int(sequence),
                from_state.value if from_state is not None else None,
                to_state.value,
                event_type,
                reason.code.value if reason is not None else None,
                reason.detail if reason is not None else None,
                str(dispatch_id) if dispatch_id is not None else None,
                encoded_data,
                utc_now(),
            ),
        )
        row = self.connection.execute("SELECT * FROM events WHERE event_id = ?", (str(event_id),)).fetchone()
        assert row is not None
        return self._event_from_row(row)

    # ------------------------------------------------------------------
    # Read-only snapshots and recovery facts
    # ------------------------------------------------------------------

    def events(
        self,
        milestone_id: MilestoneId | str,
        *,
        run_id: RunId | str | None = None,
    ) -> tuple[EventRecord, ...]:
        milestone = _milestone_id(milestone_id)
        if run_id is None:
            rows = self.connection.execute(
                "SELECT * FROM events WHERE milestone_id = ? ORDER BY sequence", (str(milestone),)
            ).fetchall()
        else:
            run = _run_id(run_id)
            rows = self.connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND milestone_id = ? ORDER BY sequence",
                (str(run), str(milestone)),
            ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def list_events(
        self, milestone_id: MilestoneId | str, *, run_id: RunId | str | None = None
    ) -> tuple[EventRecord, ...]:
        return self.events(milestone_id, run_id=run_id)

    def recovery_facts(self, run_id: RunId | str | None = None) -> tuple[RecoveryFact, ...]:
        params: tuple[object, ...] = () if run_id is None else (str(_run_id(run_id)),)
        where = "" if run_id is None else "WHERE d.run_id = ?"
        rows = self.connection.execute(
            f"SELECT d.*, m.current_state FROM dispatches d JOIN milestones m "
            f"ON m.run_id = d.run_id AND m.milestone_id = d.milestone_id {where} "
            "ORDER BY d.run_id, d.milestone_id, d.role, d.generation",
            params,
        ).fetchall()
        facts: list[RecoveryFact] = []
        for row in rows:
            state = _row_state(row["current_state"])
            if state in TERMINAL_STATES:
                continue
            dispatch = self._dispatch_from_row(row)
            last = self.connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND milestone_id = ? ORDER BY sequence DESC LIMIT 1",
                (str(dispatch.run_id), str(dispatch.milestone_id)),
            ).fetchone()
            if last is None:
                raise SchemaError(f"dispatch {dispatch.dispatch_id} has no causal event")
            facts.append(RecoveryFact(dispatch, state, self._event_from_row(last)))
        return tuple(facts)

    non_terminal_dispatches = recovery_facts
    recovery = recovery_facts

    def snapshot(self, run_id: RunId | str) -> LedgerSnapshot:
        run = _run_id(run_id)
        # A read transaction gives the artifact projector one committed view
        # without allowing it to become an authority for future writes.
        with self._read_transaction():
            record = self.get_run(run)
            milestone_rows = self.connection.execute(
                "SELECT * FROM milestones WHERE run_id = ? ORDER BY milestone_id", (str(run),)
            ).fetchall()
            dispatch_rows = self.connection.execute(
                "SELECT * FROM dispatches WHERE run_id = ? ORDER BY milestone_id, role, generation", (str(run),)
            ).fetchall()
            event_rows = self.connection.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY milestone_id, sequence", (str(run),)
            ).fetchall()
            return LedgerSnapshot(
                record,
                tuple(self._milestone_from_row(row) for row in milestone_rows),
                tuple(self._dispatch_from_row(row) for row in dispatch_rows),
                tuple(self._event_from_row(row) for row in event_rows),
            )

    @contextmanager
    def _read_transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN")
        try:
            yield
        finally:
            try:
                self.connection.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass

    # ------------------------------------------------------------------
    # Row conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            RunId(row["run_id"]),
            str(row["created_at"]),
            str(row["closed_at"]) if row["closed_at"] is not None else None,
            _decode_object(row["metadata_json"], field_name="run metadata"),
        )

    @staticmethod
    def _milestone_from_row(row: sqlite3.Row) -> MilestoneRecord:
        return MilestoneRecord(
            RunId(row["run_id"]),
            MilestoneId(row["milestone_id"]),
            _row_state(row["current_state"]),
            str(row["created_at"]),
            str(row["updated_at"]),
            _decode_object(row["metadata_json"], field_name="milestone metadata"),
        )

    @staticmethod
    def _dispatch_from_row(row: sqlite3.Row) -> DispatchClaim:
        return DispatchClaim(
            DispatchId(row["dispatch_id"]),
            RunId(row["run_id"]),
            MilestoneId(row["milestone_id"]),
            RoleId(row["role"]),
            Generation(row["generation"]),
            str(row["claimed_at"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> EventRecord:
        reason_code = row["reason_code"]
        reason = None
        if reason_code is not None:
            try:
                code = ReasonCode(reason_code)
                reason_type: type[WorkflowReason] = {
                    ReasonCode.TRANSPORT_FAILURE: PreIdentityTransportFailure,
                    ReasonCode.EXECUTION_FAILURE: PostIdentityExecutionFailure,
                    ReasonCode.REVIEW_REJECTED: ReviewRejected,
                    ReasonCode.TERMINAL_OUTCOME: TerminalOutcome,
                }.get(code, WorkflowReason)
                if reason_type is WorkflowReason:
                    reason = WorkflowReason(code, row["reason_detail"])
                else:
                    reason = reason_type(row["reason_detail"])
            except ValueError as exc:
                raise SchemaError(f"unknown persisted reason code: {reason_code!r}") from exc
        data = _decode_object(row["data_json"], field_name="event data") if row["data_json"] is not None else None
        return EventRecord(
            EventId(row["event_id"]),
            RunId(row["run_id"]),
            MilestoneId(row["milestone_id"]),
            EventSequence(row["sequence"]),
            _row_state(row["from_state"]) if row["from_state"] is not None else None,
            _row_state(row["to_state"]),
            str(row["event_type"]),
            reason,
            str(row["occurred_at"]),
            DispatchId(row["dispatch_id"]) if row["dispatch_id"] is not None else None,
            data,
        )


# Public spelling aliases used by callers and focused tests.
SQLiteLedger = Ledger
WorkflowLedger = Ledger
LedgerSchemaError = SchemaError
NewerSchemaError = UnsupportedSchemaVersion
TransportFailureBeforeIdentityReason = PreIdentityTransportFailure
