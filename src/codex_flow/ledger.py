"""The durable, local source of truth for Codex workflow state.

H2 deliberately keeps this module small and boring: the standard-library
``sqlite3`` connection is the only persistence boundary, writes are explicit
transactions, and every state mutation appends its event before commit.  No
SDK, subprocess, network, or worktree code belongs here.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    TerminalFailureAfterIdentity,
    TerminalOutcome,
    TransportFailureBeforeIdentity,
    WorkflowReason,
    WorkflowState,
    coerce_state,
)

CURRENT_SCHEMA_VERSION = SchemaVersion(2)
SUPPORTED_SCHEMA_VERSIONS = frozenset({SchemaVersion(1), CURRENT_SCHEMA_VERSION})
_SCHEMA_IDENTITIES = {SchemaVersion(1): "codex_flow_h2_v1", CURRENT_SCHEMA_VERSION: "codex_flow_h2_v2"}
_SCHEMA_IDENTITY = _SCHEMA_IDENTITIES[CURRENT_SCHEMA_VERSION]

_STATES_SQL = ", ".join(f"'{state.value}'" for state in WorkflowState)
_REASON_CODES_SQL = ", ".join(f"'{reason.value}'" for reason in ReasonCode)
_EVENT_TYPES = frozenset({"dispatch_claimed", "state_transition"})
_EVENT_TYPES_SQL = ", ".join(f"'{event_type}'" for event_type in sorted(_EVENT_TYPES))


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


class StaleWriter(LedgerError):
    """The caller's expected predecessor is no longer current."""


class DispatchConflict(LedgerError):
    """A milestone/role is already owned by another logical dispatch."""


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


def _empty_object(value: Mapping[str, object] | None, *, field_name: str) -> tuple[JsonObject, str]:
    """Reject generic durable content; H2 persists identifiers and facts only."""

    if value is not None and not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    if value is not None and dict(value):
        raise ValueError(f"{field_name} does not accept arbitrary durable fields")
    return {}, "{}"


def _decode_object(raw: str | None, *, field_name: str) -> JsonObject:
    if raw in (None, "{}"):
        return {}
    raise SchemaError(f"{field_name} contains unsupported durable content")


def _reason(value: WorkflowReason | ReasonCode | str | BaseException | None) -> WorkflowReason | None:
    if value is None:
        return None
    if isinstance(value, WorkflowReason):
        if value.detail is not None:
            raise ValueError("durable workflow reasons do not carry free-form detail")
        return value
    if isinstance(value, TransportFailureBeforeIdentity):
        return PreIdentityTransportFailure()
    if isinstance(value, TerminalFailureAfterIdentity):
        return PostIdentityExecutionFailure()
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
    if value not in _EVENT_TYPES:
        raise ValueError(f"unsupported event type: {value!r}")
    return value


def _absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_components(path: Path) -> tuple[Path, ...]:
    absolute = _absolute_path(path)
    current = Path(absolute.anchor)
    components = [current]
    for part in absolute.parts[1:]:
        current = current / part
        components.append(current)
    return tuple(components)


def _validate_ledger_location(path: Path, *, create_parent: bool) -> tuple[tuple[int, int], ...]:
    """Validate every lexical ancestor and the database entry without following symlinks."""

    components = _path_components(path)
    parent_components = components[:-1]
    if create_parent:
        for component in parent_components:
            try:
                metadata = os.lstat(component)
            except FileNotFoundError:
                component.mkdir()
                metadata = os.lstat(component)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise SchemaError(f"ledger ancestor is not a real directory: {component}")
    fingerprints: list[tuple[int, int]] = []
    for component in parent_components:
        try:
            metadata = os.lstat(component)
        except FileNotFoundError as exc:
            raise SchemaError(f"ledger ancestor disappeared: {component}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SchemaError(f"ledger ancestor is not a real directory: {component}")
        fingerprints.append((metadata.st_dev, metadata.st_ino))
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return tuple(fingerprints)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SchemaError(f"ledger database is not a real file: {path}")
    return tuple(fingerprints)


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
        self.path = _absolute_path(path)
        self._timeout = timeout
        self._fault_injector = fault_injector
        self._connection: sqlite3.Connection | None = None
        self._open()

    def _open(self) -> None:
        if self._connection is not None:
            return
        try:
            before = _validate_ledger_location(self.path, create_parent=True)
            _validate_ledger_location(self.path, create_parent=False)
            connection = sqlite3.connect(
                str(self.path),
                timeout=self._timeout,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1000)}")
            after = _validate_ledger_location(self.path, create_parent=False)
            if before != after:
                connection.close()
                raise SchemaError("ledger ancestors changed while opening")
            self._connection = connection
            self._ensure_schema()
        except LedgerError:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise
        except sqlite3.DatabaseError as exc:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise SchemaError(f"unable to open workflow ledger {self.path}") from exc
        except BaseException:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise

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
        metadata_keys = {
            str(row[0]) for row in connection.execute("SELECT key FROM schema_meta ORDER BY key").fetchall()
        }
        if metadata_keys != {"schema_version", "migration_marker", "schema_identity"}:
            raise CorruptSchemaError("schema metadata keys are not the owned v2 contract")
        identity = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_identity'").fetchone()
        if identity is None or identity[0] != _SCHEMA_IDENTITIES[version]:
            raise CorruptSchemaError("schema identity does not match its version")
        marker = connection.execute("SELECT value FROM schema_meta WHERE key = 'migration_marker'").fetchone()
        if marker is None and version == CURRENT_SCHEMA_VERSION:
            raise CorruptSchemaError("migration marker is missing")
        expected_markers = {"complete"} if version == CURRENT_SCHEMA_VERSION else {"complete", f"v{int(version)}"}
        if marker is not None and marker[0] not in expected_markers:
            raise CorruptSchemaError("invalid migration marker")
        if version < CURRENT_SCHEMA_VERSION:
            self._migrate(version)
        self._validate_shape(CURRENT_SCHEMA_VERSION)
        self._validate_rows()

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
                    CHECK(metadata_json = '{{}}')
            );

            CREATE TABLE milestones (
                run_id TEXT NOT NULL,
                milestone_id TEXT NOT NULL,
                current_state TEXT NOT NULL CHECK(current_state IN ({_STATES_SQL})),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{{}}'
                    CHECK(metadata_json = '{{}}'),
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
                CHECK(dispatch_id = run_id || '/' || milestone_id || '/' || role || '/' || generation),
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
                event_type TEXT NOT NULL CHECK(event_type IN ({_EVENT_TYPES_SQL})),
                reason_code TEXT CHECK(reason_code IS NULL OR reason_code IN ({_REASON_CODES_SQL})),
                reason_detail TEXT CHECK(reason_detail IS NULL),
                dispatch_id TEXT,
                data_json TEXT CHECK(data_json IS NULL),
                occurred_at TEXT NOT NULL,
                CHECK(event_id = run_id || '/' || milestone_id || '/' || sequence),
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
                self.connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_identity', ?)", (_SCHEMA_IDENTITY,)
                )
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

    def _validate_shape(self, version: SchemaVersion) -> None:
        required_tables = {"schema_meta", "runs", "milestones", "dispatches", "events"}
        tables = {
            str(row[0])
            for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if tables - required_tables:
            raise CorruptSchemaError(f"unexpected tables in workflow ledger: {sorted(tables - required_tables)!r}")
        expected: dict[str, dict[str, tuple[str, int, int]]] = {
            "schema_meta": {
                "key": ("TEXT", 1, 1),
                "value": ("TEXT", 1, 0),
            },
            "runs": {
                "run_id": ("TEXT", 1, 1),
                "created_at": ("TEXT", 1, 0),
                "closed_at": ("TEXT", 0, 0),
                "metadata_json": ("TEXT", 1, 0),
            },
            "milestones": {
                "run_id": ("TEXT", 1, 1),
                "milestone_id": ("TEXT", 1, 2),
                "current_state": ("TEXT", 1, 0),
                "created_at": ("TEXT", 1, 0),
                "updated_at": ("TEXT", 1, 0),
                "metadata_json": ("TEXT", 1, 0),
            },
            "dispatches": {
                "dispatch_id": ("TEXT", 1, 1),
                "run_id": ("TEXT", 1, 0),
                "milestone_id": ("TEXT", 1, 0),
                "role": ("TEXT", 1, 0),
                "generation": ("INTEGER", 1, 0),
                "claimed_at": ("TEXT", 1, 0),
            },
            "events": {
                "event_id": ("TEXT", 1, 1),
                "run_id": ("TEXT", 1, 0),
                "milestone_id": ("TEXT", 1, 0),
                "sequence": ("INTEGER", 1, 0),
                "from_state": ("TEXT", 0, 0),
                "to_state": ("TEXT", 1, 0),
                "event_type": ("TEXT", 1, 0),
                "reason_code": ("TEXT", 0, 0),
                "reason_detail": ("TEXT", 0, 0),
                "dispatch_id": ("TEXT", 0, 0),
                "data_json": ("TEXT", 0, 0),
                "occurred_at": ("TEXT", 1, 0),
            },
        }
        if version == SchemaVersion(1):
            expected["runs"].pop("closed_at")
        for table, expected_columns in expected.items():
            rows = self.connection.execute(f"PRAGMA table_info({table})").fetchall()
            actual = {str(row[1]): (str(row[2]).upper(), int(row[3]), int(row[5])) for row in rows}
            if actual != expected_columns:
                raise CorruptSchemaError(f"ledger table {table!r} has an unexpected column contract")

        signatures = {
            "schema_meta": ("CHECK(LENGTH(KEY)>0)", "CHECK(LENGTH(VALUE)>0)"),
            "runs": ("CHECK(LENGTH(RUN_ID)BETWEEN1AND128)", "CHECK(METADATA_JSON='{}')"),
            "milestones": ("CHECK(CURRENT_STATEIN(", "CHECK(METADATA_JSON='{}')"),
            "dispatches": (
                "CHECK(LENGTH(ROLE)BETWEEN1AND128)",
                "CHECK(GENERATION>0)",
                "CHECK(DISPATCH_ID=RUN_ID||'/'||MILESTONE_ID||'/'||ROLE||'/'||GENERATION)",
            ),
            "events": (
                "CHECK(SEQUENCE>0)",
                "CHECK(TO_STATEIN(",
                "CHECK(EVENT_TYPEIN(",
                "CHECK(REASON_CODEISNULLORREASON_CODEIN(",
                "CHECK(REASON_DETAILISNULL)",
                "CHECK(DATA_JSONISNULL)",
                "CHECK(EVENT_ID=RUN_ID||'/'||MILESTONE_ID||'/'||SEQUENCE)",
            ),
        }
        for table, fragments in signatures.items():
            row = self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            if row is None or row[0] is None:
                raise CorruptSchemaError(f"ledger table {table!r} has no owned SQL definition")
            compact = "".join(str(row[0]).upper().split())
            if any(fragment not in compact for fragment in fragments):
                raise CorruptSchemaError(f"ledger table {table!r} is missing an integrity constraint")
            if table in {"milestones", "events"} and any(state.value not in str(row[0]) for state in WorkflowState):
                raise CorruptSchemaError(f"ledger table {table!r} does not constrain all workflow states")

        self._require_unique_index("dispatches", ("run_id", "milestone_id", "role"))
        self._require_unique_index("events", ("run_id", "milestone_id", "sequence"))
        self._require_foreign_keys(
            "milestones",
            (("runs", "run_id", "run_id", "CASCADE"),),
        )
        self._require_foreign_keys(
            "dispatches",
            (("milestones", "run_id", "run_id", "CASCADE"), ("milestones", "milestone_id", "milestone_id", "CASCADE")),
        )
        self._require_foreign_keys(
            "events",
            (
                ("milestones", "run_id", "run_id", "CASCADE"),
                ("milestones", "milestone_id", "milestone_id", "CASCADE"),
                ("dispatches", "dispatch_id", "dispatch_id", "RESTRICT"),
            ),
        )
        foreign_keys = self.connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or int(foreign_keys[0]) != 1:
            raise CorruptSchemaError("foreign-key enforcement is disabled")

    def _require_unique_index(self, table: str, columns: tuple[str, ...]) -> None:
        for row in self.connection.execute(f"PRAGMA index_list({table})").fetchall():
            if int(row[2]) != 1:
                continue
            index_columns = tuple(
                str(info[2]) for info in self.connection.execute(f"PRAGMA index_info({row[1]})").fetchall()
            )
            if index_columns == columns:
                return
        raise CorruptSchemaError(f"ledger table {table!r} is missing unique index {columns!r}")

    def _require_foreign_keys(self, table: str, expected: tuple[tuple[str, str, str, str], ...]) -> None:
        actual = tuple(
            (str(row[2]), str(row[3]), str(row[4]), str(row[6]).upper())
            for row in sorted(
                self.connection.execute(f"PRAGMA foreign_key_list({table})").fetchall(),
                key=lambda row: (int(row[0]), int(row[1])),
            )
        )
        if sorted(actual) != sorted(expected):
            raise CorruptSchemaError(f"ledger table {table!r} has unexpected foreign keys")

    def _validate_rows(self) -> None:
        orphan_queries = (
            "SELECT COUNT(*) FROM milestones m LEFT JOIN runs r ON r.run_id = m.run_id WHERE r.run_id IS NULL",
            "SELECT COUNT(*) FROM dispatches d LEFT JOIN milestones m ON m.run_id = d.run_id AND m.milestone_id = d.milestone_id WHERE m.run_id IS NULL",
            "SELECT COUNT(*) FROM events e LEFT JOIN milestones m ON m.run_id = e.run_id AND m.milestone_id = e.milestone_id WHERE m.run_id IS NULL",
            "SELECT COUNT(*) FROM events e LEFT JOIN dispatches d ON d.dispatch_id = e.dispatch_id WHERE e.dispatch_id IS NOT NULL AND d.dispatch_id IS NULL",
        )
        if any(int(self.connection.execute(query).fetchone()[0]) for query in orphan_queries):
            raise CorruptSchemaError("workflow ledger contains orphan rows")
        duplicate_queries = (
            "SELECT COUNT(*) FROM (SELECT run_id, milestone_id FROM milestones GROUP BY run_id, milestone_id HAVING COUNT(*) > 1)",
            "SELECT COUNT(*) FROM (SELECT run_id, milestone_id, role FROM dispatches GROUP BY run_id, milestone_id, role HAVING COUNT(*) > 1)",
            "SELECT COUNT(*) FROM (SELECT run_id, milestone_id, sequence FROM events GROUP BY run_id, milestone_id, sequence HAVING COUNT(*) > 1)",
        )
        if any(int(self.connection.execute(query).fetchone()[0]) for query in duplicate_queries):
            raise CorruptSchemaError("workflow ledger contains duplicate logical rows")
        run_rows = self.connection.execute("SELECT * FROM runs ORDER BY run_id").fetchall()
        for row in run_rows:
            RunId(row["run_id"])
            _decode_object(row["metadata_json"], field_name="run metadata")
        milestone_rows = self.connection.execute("SELECT * FROM milestones ORDER BY run_id, milestone_id").fetchall()
        milestone_keys = {(str(row["run_id"]), str(row["milestone_id"])) for row in milestone_rows}
        dispatch_rows = self.connection.execute(
            "SELECT * FROM dispatches ORDER BY run_id, milestone_id, role"
        ).fetchall()
        dispatch_keys = set()
        for row in dispatch_rows:
            key = (str(row["run_id"]), str(row["milestone_id"]), str(row["role"]))
            if key in dispatch_keys:
                raise CorruptSchemaError("duplicate dispatch ownership")
            dispatch_keys.add(key)
            dispatch = self._dispatch_from_row(row)
            if dispatch.dispatch_id != DispatchId.from_parts(
                dispatch.run_id, dispatch.milestone_id, dispatch.role, dispatch.generation
            ):
                raise CorruptSchemaError("dispatch identity does not match its columns")
            if (str(dispatch.run_id), str(dispatch.milestone_id)) not in milestone_keys:
                raise CorruptSchemaError("dispatch points outside its milestone")
        event_rows = self.connection.execute("SELECT * FROM events ORDER BY run_id, milestone_id, sequence").fetchall()
        events_by_milestone: dict[tuple[str, str], list[EventRecord]] = {}
        for row in event_rows:
            event = self._event_from_row(row)
            if event.event_id != EventId(f"{event.run_id}/{event.milestone_id}/{int(event.sequence)}"):
                raise CorruptSchemaError("event identity does not match its columns")
            events_by_milestone.setdefault((str(event.run_id), str(event.milestone_id)), []).append(event)
            if event.from_state is None or event.to_state not in ALLOWED_TRANSITIONS[event.from_state]:
                raise CorruptSchemaError("event contains a forbidden state transition")
            if event.dispatch_id is not None and event.event_type != "dispatch_claimed":
                raise CorruptSchemaError("only dispatch claims may reference a dispatch")
        for row in milestone_rows:
            key = (str(row["run_id"]), str(row["milestone_id"]))
            milestone = self._milestone_from_row(row)
            history = events_by_milestone.get(key, [])
            sequences = tuple(int(event.sequence) for event in history)
            if sequences != tuple(range(1, len(history) + 1)):
                raise CorruptSchemaError("milestone event sequence is not contiguous")
            if history and history[-1].to_state is not milestone.state:
                raise CorruptSchemaError("milestone state does not match its last event")
            if not history and milestone.state is not WorkflowState.PLANNED:
                raise CorruptSchemaError("non-planned milestone has no causal history")

    def _migrate(self, version: SchemaVersion) -> None:
        if version != SchemaVersion(1):
            raise UnsupportedSchemaVersion(f"ledger schema {version} has no owned migration")
        self._validate_shape(version)
        self._validate_rows()
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
            self.connection.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[CURRENT_SCHEMA_VERSION],),
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
        checked, encoded = _empty_object(metadata, field_name="run metadata")
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
        checked, encoded = _empty_object(metadata, field_name="milestone metadata")
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

    def get_milestone(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> MilestoneRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        row = self.connection.execute(
            "SELECT * FROM milestones WHERE run_id = ? AND milestone_id = ?", (str(run), str(milestone))
        ).fetchone()
        if row is None:
            raise RecordNotFound(f"milestone {milestone} does not exist")
        return self._milestone_from_row(row)

    def current_state(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> WorkflowState:
        return self.get_milestone(run_id, milestone_id).state

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
            current = self.get_milestone(run, milestone)
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

    def transition(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        to_state: WorkflowState | str,
        *,
        expected_state: WorkflowState | str | None = None,
        reason: WorkflowReason | ReasonCode | str | BaseException | None = None,
        event_type: str | None = None,
        data: Mapping[str, object] | None = None,
        dispatch_id: DispatchId | str | None = None,
    ) -> EventRecord:
        """Advance a milestone and append its causal event atomically.

        The run and milestone are always explicit; milestone ids may be reused
        safely across independent runs.
        """

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        target = coerce_state(to_state)
        expected_value = coerce_state(expected_state) if expected_state is not None else None
        normalized_reason = _reason(reason)
        if data is not None:
            _empty_object(data, field_name="event data")
        dispatch = None
        if dispatch_id is not None:
            dispatch = DispatchId(dispatch_id if isinstance(dispatch_id, str) else str(dispatch_id))
        normalized_event_type = _event_type(event_type or "state_transition")
        with self._transaction():
            current = self.get_milestone(run, milestone)
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
                data=None,
            )
            self._fault("after_event_insert")
            return event

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
            _empty_object(data, field_name="event data")
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

    def events(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> tuple[EventRecord, ...]:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        rows = self.connection.execute(
            "SELECT * FROM events WHERE run_id = ? AND milestone_id = ? ORDER BY sequence",
            (str(run), str(milestone)),
        ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def recovery_facts(self, run_id: RunId | str) -> tuple[RecoveryFact, ...]:
        run = _run_id(run_id)
        rows = self.connection.execute(
            "SELECT d.*, m.current_state FROM dispatches d JOIN milestones m "
            "ON m.run_id = d.run_id AND m.milestone_id = d.milestone_id "
            "WHERE d.run_id = ? ORDER BY d.milestone_id, d.role, d.generation",
            (str(run),),
        ).fetchall()
        facts: list[RecoveryFact] = []
        for row in rows:
            state = _row_state(row["current_state"])
            if state in TERMINAL_STATES:
                continue
            dispatch = self._dispatch_from_row(row)
            last_sequence = self.connection.execute(
                "SELECT MAX(sequence) FROM events WHERE run_id = ? AND milestone_id = ?",
                (str(dispatch.run_id), str(dispatch.milestone_id)),
            ).fetchone()[0]
            last = self.connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND milestone_id = ? AND sequence = ?",
                (str(dispatch.run_id), str(dispatch.milestone_id), int(last_sequence)),
            ).fetchone()
            if last is None:
                raise SchemaError(f"dispatch {dispatch.dispatch_id} has no causal event")
            facts.append(RecoveryFact(dispatch, state, self._event_from_row(last)))
        return tuple(facts)

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
                if row["reason_detail"] is not None:
                    raise SchemaError("persisted reason detail is not allowed")
                code = ReasonCode(reason_code)
                reason_type: type[WorkflowReason] = {
                    ReasonCode.TRANSPORT_FAILURE: PreIdentityTransportFailure,
                    ReasonCode.EXECUTION_FAILURE: PostIdentityExecutionFailure,
                    ReasonCode.REVIEW_REJECTED: ReviewRejected,
                    ReasonCode.TERMINAL_OUTCOME: TerminalOutcome,
                }.get(code, WorkflowReason)
                if reason_type is WorkflowReason:
                    reason = WorkflowReason(code)
                else:
                    reason = reason_type()
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
