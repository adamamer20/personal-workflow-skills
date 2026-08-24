"""The durable, local source of truth for Codex workflow state.

H2 deliberately keeps this module small and boring: the standard-library
``sqlite3`` connection is the only persistence boundary, writes are explicit
transactions, and every state mutation appends its event before commit.  No
SDK, subprocess, network, or worktree code belongs here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .domain import (
    TERMINAL_STATES,
    ControllerCheckpoint,
    DispatchClaim,
    DispatchId,
    EventId,
    EventRecord,
    EventSequence,
    ExecutionCapsule,
    ExecutionIntegrityRecord,
    ExecutionRecord,
    ExecutionStatus,
    Generation,
    JsonObject,
    LedgerSnapshot,
    LifecycleEvent,
    MilestoneId,
    MilestoneRecord,
    PostIdentityExecutionFailure,
    PreIdentityTransportFailure,
    ReasonCode,
    ReasoningEffort,
    RecoveryFact,
    ReviewRejected,
    RoleId,
    RunId,
    RunRecord,
    SchemaVersion,
    TerminalFailureAfterIdentity,
    TerminalOutcome,
    ThreadIdentity,
    TransportFailureBeforeIdentity,
    TurnObservation,
    ValidationFailureCode,
    ValidationObservation,
    WorkflowReason,
    WorkflowState,
    WorkspaceLeaseRecord,
    WorkspaceMode,
    coerce_state,
    is_transition_allowed,
)

CURRENT_SCHEMA_VERSION = SchemaVersion(4)
SUPPORTED_SCHEMA_VERSIONS = frozenset({SchemaVersion(1), SchemaVersion(2), SchemaVersion(3), CURRENT_SCHEMA_VERSION})
_STATES_SQL = ", ".join(f"'{state.value}'" for state in WorkflowState)
_REASON_CODES_SQL = ", ".join(f"'{reason.value}'" for reason in ReasonCode)
_EVENT_TYPES = frozenset({"dispatch_claimed", "state_transition"})
_EVENT_TYPES_SQL = ", ".join(f"'{event_type}'" for event_type in sorted(_EVENT_TYPES))
_TURN_STARTING_MARKER: JsonObject = {"__controller_checkpoint": "turn_starting"}

_SCHEMA_META_DDL = """CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY NOT NULL CHECK(length(key) > 0),
    value TEXT NOT NULL CHECK(length(value) > 0)
)"""
_RUNS_V1_DDL = """CREATE TABLE runs (
    run_id TEXT PRIMARY KEY NOT NULL CHECK(length(run_id) BETWEEN 1 AND 128),
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(metadata_json = '{}')
)"""
_RUNS_V2_DDL = """CREATE TABLE runs (
    run_id TEXT PRIMARY KEY NOT NULL CHECK(length(run_id) BETWEEN 1 AND 128),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(metadata_json = '{}')
)"""
_MILESTONES_DDL = f"""CREATE TABLE milestones (
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    current_state TEXT NOT NULL CHECK(current_state IN ({_STATES_SQL})),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{{}}' CHECK(metadata_json = '{{}}'),
    PRIMARY KEY(run_id, milestone_id),
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
)"""
_DISPATCHES_DDL = """CREATE TABLE dispatches (
    dispatch_id TEXT PRIMARY KEY NOT NULL CHECK(length(dispatch_id) BETWEEN 1 AND 512),
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(length(role) BETWEEN 1 AND 128),
    generation INTEGER NOT NULL CHECK(generation > 0),
    claimed_at TEXT NOT NULL,
    CHECK(dispatch_id = run_id || '/' || milestone_id || '/' || role || '/' || generation),
    UNIQUE(run_id, milestone_id, role),
    FOREIGN KEY(run_id, milestone_id) REFERENCES milestones(run_id, milestone_id) ON DELETE CASCADE
)"""
_EVENTS_DDL = f"""CREATE TABLE events (
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
)"""
_V1_TABLE_DDL = {
    "schema_meta": _SCHEMA_META_DDL,
    "runs": _RUNS_V1_DDL,
    "milestones": _MILESTONES_DDL,
    "dispatches": _DISPATCHES_DDL,
    "events": _EVENTS_DDL,
}
_V2_TABLE_DDL = {**_V1_TABLE_DDL, "runs": _RUNS_V2_DDL}

_EXECUTION_STATUSES_SQL = ", ".join(f"'{status.value}'" for status in ExecutionStatus)
_CHECKPOINTS_SQL = ", ".join(f"'{checkpoint.value}'" for checkpoint in ControllerCheckpoint)
_WORKSPACE_MODES_SQL = ", ".join(f"'{mode.value}'" for mode in WorkspaceMode)
_WORKSPACE_LEASES_DDL = f"""CREATE TABLE workspace_leases (
    workspace_path TEXT PRIMARY KEY NOT NULL CHECK(length(workspace_path) > 1),
    repository_root TEXT NOT NULL CHECK(length(repository_root) > 1),
    mode TEXT NOT NULL CHECK(mode IN ({_WORKSPACE_MODES_SQL})),
    branch TEXT NOT NULL CHECK(length(branch) > 0),
    base_sha TEXT NOT NULL CHECK(length(base_sha) = 40),
    lane TEXT NOT NULL CHECK(length(lane) BETWEEN 1 AND 128),
    owner_run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(owner_run_id, lane),
    FOREIGN KEY(owner_run_id) REFERENCES runs(run_id) ON DELETE CASCADE
)"""
_EXECUTIONS_DDL = f"""CREATE TABLE executions (
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    capsule_path TEXT NOT NULL CHECK(length(capsule_path) > 1),
    capsule_sha256 TEXT NOT NULL CHECK(length(capsule_sha256) = 64),
    workspace_path TEXT NOT NULL CHECK(length(workspace_path) > 1),
    model TEXT NOT NULL CHECK(length(model) > 0),
    reasoning_effort TEXT NOT NULL CHECK(length(reasoning_effort) > 0),
    status TEXT NOT NULL CHECK(status IN ({_EXECUTION_STATUSES_SQL})),
    checkpoint TEXT NOT NULL CHECK(checkpoint IN ({_CHECKPOINTS_SQL})),
    thread_id TEXT,
    turn_id TEXT,
    turn_output_json TEXT,
    result_json TEXT,
    validation_argv_json TEXT,
    validation_exit_code INTEGER,
    validation_stdout_sha256 TEXT,
    validation_stderr_sha256 TEXT,
    validation_timed_out INTEGER CHECK(validation_timed_out IN (0, 1)),
    validation_duration_seconds REAL CHECK(validation_duration_seconds >= 0),
    protected_before_sha256 TEXT NOT NULL CHECK(length(protected_before_sha256) = 64),
    protected_after_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, milestone_id),
    FOREIGN KEY(run_id, milestone_id) REFERENCES milestones(run_id, milestone_id) ON DELETE CASCADE
)"""
_SDK_EVENTS_DDL = """CREATE TABLE sdk_lifecycle_events (
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    turn_id TEXT NOT NULL CHECK(length(turn_id) > 0),
    sequence INTEGER NOT NULL CHECK(sequence >= 0),
    method TEXT NOT NULL CHECK(length(method) > 0),
    event_turn_id TEXT,
    PRIMARY KEY(run_id, milestone_id, turn_id, sequence),
    FOREIGN KEY(run_id, milestone_id) REFERENCES executions(run_id, milestone_id) ON DELETE CASCADE
)"""
_V3_TABLE_DDL = {
    **_V2_TABLE_DDL,
    "workspace_leases": _WORKSPACE_LEASES_DDL,
    "executions": _EXECUTIONS_DDL,
    "sdk_lifecycle_events": _SDK_EVENTS_DDL,
}
_EXECUTION_INTEGRITY_DDL = """CREATE TABLE execution_integrity (
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN ('legacy_v3', 'controller_v1')),
    sandbox_policy_sha256 TEXT CHECK(sandbox_policy_sha256 IS NULL OR length(sandbox_policy_sha256) = 64),
    git_authority_before_sha256 TEXT CHECK(git_authority_before_sha256 IS NULL OR length(git_authority_before_sha256) = 64),
    git_authority_after_sha256 TEXT CHECK(git_authority_after_sha256 IS NULL OR length(git_authority_after_sha256) = 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, milestone_id),
    CHECK((provenance = 'legacy_v3' AND sandbox_policy_sha256 IS NULL AND git_authority_before_sha256 IS NULL AND git_authority_after_sha256 IS NULL)
       OR (provenance = 'controller_v1' AND sandbox_policy_sha256 IS NOT NULL)),
    FOREIGN KEY(run_id, milestone_id) REFERENCES executions(run_id, milestone_id) ON DELETE CASCADE
)"""
_V4_TABLE_DDL = {**_V3_TABLE_DDL, "execution_integrity": _EXECUTION_INTEGRITY_DDL}


def _canonical_ddl(sql: str) -> str:
    """Return exact SQLite DDL modulo whitespace outside quoted values."""

    canonical: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote is None:
            if character.isspace():
                index += 1
                continue
            canonical.append(character)
            if character in {"'", '"'}:
                quote = character
        else:
            canonical.append(character)
            if character == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    canonical.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        index += 1
    return "".join(canonical)


SchemaObject = tuple[str, str, str, str | None]
FileIdentity = tuple[int, int]


def _owned_inventory(definitions: Mapping[str, str]) -> tuple[SchemaObject, ...]:
    return tuple(sorted(("table", name, name, sql) for name, sql in definitions.items()))


def _canonical_inventory(inventory: tuple[SchemaObject, ...]) -> tuple[SchemaObject, ...]:
    return tuple(
        (object_type, name, table, _canonical_ddl(sql) if sql is not None else None)
        for object_type, name, table, sql in inventory
    )


def _inventory_fingerprint(inventory: tuple[SchemaObject, ...]) -> str:
    payload = "\n".join(repr(item) for item in inventory)
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"


def _schema_inventory(connection: sqlite3.Connection, *, temporary: bool = False) -> tuple[SchemaObject, ...]:
    statement = (
        "SELECT type, name, tbl_name, sql FROM sqlite_temp_master "
        "WHERE substr(name, 1, 7) != 'sqlite_' ORDER BY type, name"
        if temporary
        else "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE substr(name, 1, 7) != 'sqlite_' ORDER BY type, name"
    )
    rows = connection.execute(statement).fetchall()
    return tuple(
        (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]) if row[3] is not None else None,
        )
        for row in rows
    )


_SCHEMA_IDENTITIES = {
    SchemaVersion(1): "codex_flow_h2_v1",
    SchemaVersion(2): "codex_flow_h2_v2",
    SchemaVersion(3): "codex_flow_h3_v3",
    CURRENT_SCHEMA_VERSION: "codex_flow_h3_integrity_v4",
}
_SCHEMA_IDENTITY = _SCHEMA_IDENTITIES[CURRENT_SCHEMA_VERSION]


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


class WorkspaceLeaseConflict(LedgerError):
    """A workspace or execution owner conflicts with durable lease facts."""


FaultInjector = Callable[[str], None]


def utc_now() -> str:
    """Return a sortable, explicit UTC timestamp."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _identifier(value: str, constructor: Callable[[str], Any], label: str) -> Any:
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}: expected a string, got {type(value).__name__}")
    try:
        return constructor(value)
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


def _encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_json_object(raw: str | None, *, field_name: str) -> JsonObject | None:
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"{field_name} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise SchemaError(f"{field_name} must be a JSON object")
    return decoded


def _sha256(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


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


def _file_identity(metadata: os.stat_result) -> FileIdentity:
    return metadata.st_dev, metadata.st_ino


def _open_parent_chain(path: Path) -> tuple[int, ...]:
    """Create and pin every parent using no-follow directory descriptors."""

    descriptors = [os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    try:
        for component in path.parent.parts[1:]:
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptors[-1],
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptors[-1])
                except FileExistsError:
                    pass
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptors[-1],
                )
            descriptors.append(next_fd)
        return tuple(descriptors)
    except (NotADirectoryError, OSError) as exc:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise SchemaError(f"ledger ancestor is not a stable real directory: {path.parent}") from exc


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
        self._database_fd: int | None = None
        self._directory_fds: tuple[int, ...] = ()
        self._open_identity: FileIdentity | None = None
        self._expected_identity: FileIdentity | None = None
        self._open()

    def _open(self) -> None:
        if self._connection is not None:
            return
        directory_fds: tuple[int, ...] = ()
        database_fd: int | None = None
        connection: sqlite3.Connection | None = None
        created = False
        try:
            directory_fds = _open_parent_chain(self.path)
            try:
                database_fd = os.open(
                    self.path.name,
                    os.O_RDWR | os.O_NOFOLLOW,
                    dir_fd=directory_fds[-1],
                )
            except FileNotFoundError as exc:
                if self._expected_identity is not None:
                    raise SchemaError("bound ledger path is missing") from exc
                try:
                    database_fd = os.open(
                        self.path.name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fds[-1],
                    )
                    created = True
                except FileExistsError:
                    database_fd = os.open(
                        self.path.name,
                        os.O_RDWR | os.O_NOFOLLOW,
                        dir_fd=directory_fds[-1],
                    )
            pinned = os.fstat(database_fd)
            if not stat.S_ISREG(pinned.st_mode) or pinned.st_nlink != 1:
                raise SchemaError(f"ledger database is not a single-link regular file: {self.path}")
            linked = os.stat(self.path.name, dir_fd=directory_fds[-1], follow_symlinks=False)
            identity = _file_identity(pinned)
            if identity != _file_identity(linked) or linked.st_nlink != 1:
                raise SchemaError("ledger database changed while being pinned")
            if self._expected_identity is not None and identity != self._expected_identity:
                raise SchemaError("ledger path no longer names this Ledger instance's bound database")
            self._database_fd = database_fd
            self._directory_fds = directory_fds
            self._open_identity = identity
            database_fd = None
            directory_fds = ()
            self._fault("before_connect")
            self._validate_live_identity()
            descriptor_path = f"/proc/self/fd/{self._database_fd}"
            if not os.path.exists(descriptor_path):
                raise SchemaError("supported Linux /proc descriptor path is unavailable")
            connection = sqlite3.connect(
                f"file:{descriptor_path}?mode=rw",
                timeout=self._timeout,
                isolation_level=None,
                check_same_thread=False,
                uri=True,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1000)}")
            self._connection = connection
            connection = None
            self._ensure_schema()
            self._validate_live_identity()
            if self._expected_identity is None:
                self._expected_identity = identity
        except sqlite3.DatabaseError as exc:
            self._close_connection()
            if created and self._expected_identity is None:
                self._cleanup_failed_first_open()
            self.close()
            raise SchemaError(f"unable to open workflow ledger {self.path}") from exc
        except OSError as exc:
            self._close_connection()
            if created and self._expected_identity is None:
                self._cleanup_failed_first_open()
            self.close()
            raise SchemaError(f"unable to pin workflow ledger {self.path}") from exc
        except BaseException:
            self._close_connection()
            if created and self._expected_identity is None:
                self._cleanup_failed_first_open()
            self.close()
            raise
        finally:
            if connection is not None:
                connection.close()
            if database_fd is not None:
                os.close(database_fd)
            for descriptor in reversed(directory_fds):
                os.close(descriptor)

    def _close_connection(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _cleanup_failed_first_open(self) -> None:
        if self._database_fd is None or not self._directory_fds or self._open_identity is None:
            return
        pinned = os.fstat(self._database_fd)
        if _file_identity(pinned) != self._open_identity or pinned.st_nlink != 1 or pinned.st_size != 0:
            return
        try:
            linked = os.stat(self.path.name, dir_fd=self._directory_fds[-1], follow_symlinks=False)
        except FileNotFoundError:
            return
        if _file_identity(linked) == self._open_identity and linked.st_nlink == 1 and linked.st_size == 0:
            os.unlink(self.path.name, dir_fd=self._directory_fds[-1])

    def _validate_live_identity(self) -> None:
        if self._database_fd is None or not self._directory_fds or self._open_identity is None:
            raise LedgerClosedError("workflow ledger identity is not pinned")
        pinned = os.fstat(self._database_fd)
        if not stat.S_ISREG(pinned.st_mode) or pinned.st_nlink != 1 or _file_identity(pinned) != self._open_identity:
            raise SchemaError("pinned ledger inode or link count changed")
        if self._expected_identity is not None and self._open_identity != self._expected_identity:
            raise SchemaError("open ledger inode differs from its bound identity")
        try:
            linked = os.stat(self.path.name, dir_fd=self._directory_fds[-1], follow_symlinks=False)
        except OSError as exc:
            raise SchemaError("ledger path entry is unavailable") from exc
        if not stat.S_ISREG(linked.st_mode) or linked.st_nlink != 1 or _file_identity(linked) != self._open_identity:
            raise SchemaError("ledger path no longer names the pinned single-link inode")

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise LedgerClosedError("workflow ledger is closed")
        return self._connection

    def schema_columns(
        self,
        table: Literal[
            "schema_meta",
            "runs",
            "milestones",
            "dispatches",
            "events",
            "workspace_leases",
            "executions",
            "sdk_lifecycle_events",
            "execution_integrity",
        ],
    ) -> tuple[str, ...]:
        """Expose a narrow, immutable schema diagnostic without the raw connection."""

        if table not in _V4_TABLE_DDL:
            raise ValueError(f"unknown owned table: {table!r}")
        return tuple(str(row[1]) for row in self._db().execute(f"PRAGMA table_info({table})").fetchall())

    @property
    def schema_version(self) -> SchemaVersion:
        row = self._db().execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            raise CorruptSchemaError("schema_version marker is missing")
        try:
            return SchemaVersion(row[0])
        except ValueError as exc:
            raise CorruptSchemaError("schema_version marker is not an integer") from exc

    @property
    def schema_identity(self) -> str:
        """Return the verified immutable identity of the owned schema."""

        row = self._db().execute("SELECT value FROM schema_meta WHERE key = 'schema_identity'").fetchone()
        if row is None:
            raise CorruptSchemaError("schema_identity marker is missing")
        return str(row[0])

    @property
    def journal_mode(self) -> str:
        """Return SQLite's read-only journal-mode diagnostic."""

        row = self._db().execute("PRAGMA journal_mode").fetchone()
        if row is None:
            raise SchemaError("SQLite did not report a journal mode")
        return str(row[0])

    def _ensure_schema(self) -> None:
        connection = self._db()
        inventory = _schema_inventory(connection)
        if not inventory:
            self._create_schema()
            return
        if not any(item[0] == "table" and item[1] == "schema_meta" for item in inventory):
            raise CorruptSchemaError("workflow ledger is missing schema metadata")
        try:
            version = self.schema_version
        except sqlite3.DatabaseError as exc:
            raise CorruptSchemaError("unable to read schema metadata") from exc
        if version > CURRENT_SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(f"ledger schema {version} is newer than supported {CURRENT_SCHEMA_VERSION}")
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersion(f"ledger schema {version} is unsupported")
        self._validate_schema_metadata(version)
        if version < CURRENT_SCHEMA_VERSION:
            self._migrate(version)
        with self._read_transaction():
            self._validate_schema_metadata(CURRENT_SCHEMA_VERSION)
            self._validate_shape(CURRENT_SCHEMA_VERSION)
            try:
                self._validate_rows()
            except ValueError as exc:
                raise CorruptSchemaError("workflow ledger contains invalid typed values") from exc

    def _validate_schema_metadata(self, expected_version: SchemaVersion) -> None:
        version = self.schema_version
        if version != expected_version:
            raise CorruptSchemaError(
                f"schema version changed during validation: expected {expected_version}, found {version}"
            )
        metadata_keys = {
            str(row[0]) for row in self._db().execute("SELECT key FROM schema_meta ORDER BY key").fetchall()
        }
        if metadata_keys != {"schema_version", "migration_marker", "schema_identity"}:
            raise CorruptSchemaError("schema metadata keys are not the owned contract")
        identity = self._db().execute("SELECT value FROM schema_meta WHERE key = 'schema_identity'").fetchone()
        if identity is None or identity[0] != _SCHEMA_IDENTITIES[version]:
            raise CorruptSchemaError("schema identity does not match its version")
        marker = self._db().execute("SELECT value FROM schema_meta WHERE key = 'migration_marker'").fetchone()
        if marker is None and version == CURRENT_SCHEMA_VERSION:
            raise CorruptSchemaError("migration marker is missing")
        expected_markers = {"complete"} if version == CURRENT_SCHEMA_VERSION else {"complete", f"v{int(version)}"}
        if marker is not None and marker[0] not in expected_markers:
            raise CorruptSchemaError("invalid migration marker")

    def _create_schema(self) -> None:
        try:
            with self._transaction(validate_authority=False):
                for statement in _V4_TABLE_DDL.values():
                    self._db().execute(statement)
                self._db().execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                    (str(int(CURRENT_SCHEMA_VERSION)),),
                )
                self._db().execute("INSERT INTO schema_meta(key, value) VALUES ('migration_marker', 'complete')")
                self._db().execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_identity', ?)", (_SCHEMA_IDENTITY,)
                )
        except sqlite3.OperationalError as exc:
            # Two first openers may observe an empty file concurrently.  The
            # loser waits for the winner's transaction, then observes the
            # already committed schema and validates it read-only.
            if "already exists" not in str(exc).lower():
                raise
            self._ensure_schema()

    def _validate_shape(self, version: SchemaVersion) -> None:
        if _schema_inventory(self._db(), temporary=True):
            raise CorruptSchemaError("ledger connection has unexpected temporary schema objects")
        definitions = {
            SchemaVersion(1): _V1_TABLE_DDL,
            SchemaVersion(2): _V2_TABLE_DDL,
            SchemaVersion(3): _V3_TABLE_DDL,
            SchemaVersion(4): _V4_TABLE_DDL,
        }[version]
        actual_inventory = _schema_inventory(self._db())
        expected_inventory = _owned_inventory(definitions)
        if version == SchemaVersion(1):
            actual_inventory = _canonical_inventory(actual_inventory)
            expected_inventory = _canonical_inventory(expected_inventory)
        if _inventory_fingerprint(actual_inventory) != _inventory_fingerprint(expected_inventory):
            raise CorruptSchemaError("ledger sqlite_master inventory does not match the canonical owned schema")

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
            "workspace_leases": {
                "workspace_path": ("TEXT", 1, 1),
                "repository_root": ("TEXT", 1, 0),
                "mode": ("TEXT", 1, 0),
                "branch": ("TEXT", 1, 0),
                "base_sha": ("TEXT", 1, 0),
                "lane": ("TEXT", 1, 0),
                "owner_run_id": ("TEXT", 1, 0),
                "created_at": ("TEXT", 1, 0),
            },
            "executions": {
                "run_id": ("TEXT", 1, 1),
                "milestone_id": ("TEXT", 1, 2),
                "capsule_path": ("TEXT", 1, 0),
                "capsule_sha256": ("TEXT", 1, 0),
                "workspace_path": ("TEXT", 1, 0),
                "model": ("TEXT", 1, 0),
                "reasoning_effort": ("TEXT", 1, 0),
                "status": ("TEXT", 1, 0),
                "checkpoint": ("TEXT", 1, 0),
                "thread_id": ("TEXT", 0, 0),
                "turn_id": ("TEXT", 0, 0),
                "turn_output_json": ("TEXT", 0, 0),
                "result_json": ("TEXT", 0, 0),
                "validation_argv_json": ("TEXT", 0, 0),
                "validation_exit_code": ("INTEGER", 0, 0),
                "validation_stdout_sha256": ("TEXT", 0, 0),
                "validation_stderr_sha256": ("TEXT", 0, 0),
                "validation_timed_out": ("INTEGER", 0, 0),
                "validation_duration_seconds": ("REAL", 0, 0),
                "protected_before_sha256": ("TEXT", 1, 0),
                "protected_after_sha256": ("TEXT", 0, 0),
                "created_at": ("TEXT", 1, 0),
                "updated_at": ("TEXT", 1, 0),
            },
            "sdk_lifecycle_events": {
                "run_id": ("TEXT", 1, 1),
                "milestone_id": ("TEXT", 1, 2),
                "turn_id": ("TEXT", 1, 3),
                "sequence": ("INTEGER", 1, 4),
                "method": ("TEXT", 1, 0),
                "event_turn_id": ("TEXT", 0, 0),
            },
            "execution_integrity": {
                "run_id": ("TEXT", 1, 1),
                "milestone_id": ("TEXT", 1, 2),
                "provenance": ("TEXT", 1, 0),
                "sandbox_policy_sha256": ("TEXT", 0, 0),
                "git_authority_before_sha256": ("TEXT", 0, 0),
                "git_authority_after_sha256": ("TEXT", 0, 0),
                "created_at": ("TEXT", 1, 0),
                "updated_at": ("TEXT", 1, 0),
            },
        }
        if version == SchemaVersion(1):
            expected["runs"].pop("closed_at")
        if version < SchemaVersion(3):
            expected.pop("workspace_leases")
            expected.pop("executions")
            expected.pop("sdk_lifecycle_events")
        if version < SchemaVersion(4):
            expected.pop("execution_integrity")
        for table, expected_columns in expected.items():
            rows = self._db().execute(f"PRAGMA table_info({table})").fetchall()
            actual = {str(row[1]): (str(row[2]).upper(), int(row[3]), int(row[5])) for row in rows}
            if actual != expected_columns:
                raise CorruptSchemaError(f"ledger table {table!r} has an unexpected column contract")

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
        if version >= SchemaVersion(3):
            self._require_unique_index("workspace_leases", ("owner_run_id", "lane"))
            self._require_foreign_keys(
                "workspace_leases",
                (("runs", "owner_run_id", "run_id", "CASCADE"),),
            )
            self._require_foreign_keys(
                "executions",
                (
                    ("milestones", "run_id", "run_id", "CASCADE"),
                    ("milestones", "milestone_id", "milestone_id", "CASCADE"),
                ),
            )
            self._require_foreign_keys(
                "sdk_lifecycle_events",
                (
                    ("executions", "run_id", "run_id", "CASCADE"),
                    ("executions", "milestone_id", "milestone_id", "CASCADE"),
                ),
            )
        if version >= SchemaVersion(4):
            self._require_foreign_keys(
                "execution_integrity",
                (
                    ("executions", "run_id", "run_id", "CASCADE"),
                    ("executions", "milestone_id", "milestone_id", "CASCADE"),
                ),
            )
        foreign_keys = self._db().execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or int(foreign_keys[0]) != 1:
            raise CorruptSchemaError("foreign-key enforcement is disabled")

    def _require_unique_index(self, table: str, columns: tuple[str, ...]) -> None:
        for row in self._db().execute(f"PRAGMA index_list({table})").fetchall():
            if int(row[2]) != 1:
                continue
            index_columns = tuple(
                str(info[2]) for info in self._db().execute(f"PRAGMA index_info({row[1]})").fetchall()
            )
            if index_columns == columns:
                return
        raise CorruptSchemaError(f"ledger table {table!r} is missing unique index {columns!r}")

    def _require_foreign_keys(self, table: str, expected: tuple[tuple[str, str, str, str], ...]) -> None:
        actual = tuple(
            (str(row[2]), str(row[3]), str(row[4]), str(row[6]).upper())
            for row in sorted(
                self._db().execute(f"PRAGMA foreign_key_list({table})").fetchall(),
                key=lambda row: (int(row[0]), int(row[1])),
            )
        )
        if sorted(actual) != sorted(expected):
            raise CorruptSchemaError(f"ledger table {table!r} has unexpected foreign keys")

    def _validate_rows(self) -> None:
        has_h3 = any(item[1] == "executions" for item in _schema_inventory(self._db()))
        has_integrity = any(item[1] == "execution_integrity" for item in _schema_inventory(self._db()))
        orphan_queries = [
            "SELECT COUNT(*) FROM milestones m LEFT JOIN runs r ON r.run_id = m.run_id WHERE r.run_id IS NULL",
            "SELECT COUNT(*) FROM dispatches d LEFT JOIN milestones m ON m.run_id = d.run_id AND m.milestone_id = d.milestone_id WHERE m.run_id IS NULL",
            "SELECT COUNT(*) FROM events e LEFT JOIN milestones m ON m.run_id = e.run_id AND m.milestone_id = e.milestone_id WHERE m.run_id IS NULL",
            "SELECT COUNT(*) FROM events e LEFT JOIN dispatches d ON d.dispatch_id = e.dispatch_id WHERE e.dispatch_id IS NOT NULL AND d.dispatch_id IS NULL",
        ]
        if has_h3:
            orphan_queries.extend(
                (
                    "SELECT COUNT(*) FROM workspace_leases w LEFT JOIN runs r ON r.run_id = w.owner_run_id WHERE r.run_id IS NULL",
                    "SELECT COUNT(*) FROM executions x LEFT JOIN milestones m ON m.run_id = x.run_id AND m.milestone_id = x.milestone_id WHERE m.run_id IS NULL",
                    "SELECT COUNT(*) FROM sdk_lifecycle_events s LEFT JOIN executions x ON x.run_id = s.run_id AND x.milestone_id = s.milestone_id WHERE x.run_id IS NULL",
                )
            )
        if has_integrity:
            orphan_queries.append(
                "SELECT COUNT(*) FROM execution_integrity i LEFT JOIN executions x "
                "ON x.run_id = i.run_id AND x.milestone_id = i.milestone_id WHERE x.run_id IS NULL"
            )
        if any(int(self._db().execute(query).fetchone()[0]) for query in orphan_queries):
            raise CorruptSchemaError("workflow ledger contains orphan rows")
        duplicate_queries = (
            "SELECT COUNT(*) FROM (SELECT run_id, milestone_id FROM milestones GROUP BY run_id, milestone_id HAVING COUNT(*) > 1)",
            "SELECT COUNT(*) FROM (SELECT run_id, milestone_id, role FROM dispatches GROUP BY run_id, milestone_id, role HAVING COUNT(*) > 1)",
            "SELECT COUNT(*) FROM (SELECT run_id, milestone_id, sequence FROM events GROUP BY run_id, milestone_id, sequence HAVING COUNT(*) > 1)",
        )
        if any(int(self._db().execute(query).fetchone()[0]) for query in duplicate_queries):
            raise CorruptSchemaError("workflow ledger contains duplicate logical rows")
        run_rows = self._db().execute("SELECT * FROM runs ORDER BY run_id").fetchall()
        for row in run_rows:
            RunId(row["run_id"])
            _decode_object(row["metadata_json"], field_name="run metadata")
        milestone_rows = self._db().execute("SELECT * FROM milestones ORDER BY run_id, milestone_id").fetchall()
        milestone_keys = {(str(row["run_id"]), str(row["milestone_id"])) for row in milestone_rows}
        dispatch_rows = self._db().execute("SELECT * FROM dispatches ORDER BY run_id, milestone_id, role").fetchall()
        self._fault("after_dispatch_rows_read")
        dispatch_keys = set()
        dispatches_by_id: dict[DispatchId, DispatchClaim] = {}
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
            dispatches_by_id[dispatch.dispatch_id] = dispatch
        event_rows = self._db().execute("SELECT * FROM events ORDER BY run_id, milestone_id, sequence").fetchall()
        events_by_milestone: dict[tuple[str, str], list[EventRecord]] = {}
        claim_counts = dict.fromkeys(dispatches_by_id, 0)
        for row in event_rows:
            event = self._event_from_row(row)
            try:
                _event_type(event.event_type)
            except ValueError as exc:
                raise CorruptSchemaError("event has an unsupported kind") from exc
            if event.event_id != EventId(f"{event.run_id}/{event.milestone_id}/{int(event.sequence)}"):
                raise CorruptSchemaError("event identity does not match its columns")
            events_by_milestone.setdefault((str(event.run_id), str(event.milestone_id)), []).append(event)
            if event.from_state is None or not is_transition_allowed(event.from_state, event.to_state):
                raise CorruptSchemaError("event contains a forbidden state transition")
            if event.event_type == "dispatch_claimed":
                if (
                    event.dispatch_id is None
                    or event.reason is None
                    or event.reason.code is not ReasonCode.DISPATCH_CLAIMED
                    or event.from_state is not WorkflowState.PLANNED
                    or event.to_state is not WorkflowState.STARTING
                ):
                    raise CorruptSchemaError("dispatch claim event has an invalid causal contract")
                dispatch = dispatches_by_id.get(event.dispatch_id)
                if dispatch is None or (dispatch.run_id, dispatch.milestone_id) != (
                    event.run_id,
                    event.milestone_id,
                ):
                    raise CorruptSchemaError("dispatch claim event does not match its dispatch row")
                claim_counts[event.dispatch_id] += 1
            elif event.dispatch_id is not None or (
                event.reason is not None and event.reason.code is ReasonCode.DISPATCH_CLAIMED
            ):
                raise CorruptSchemaError("normal state transition carries dispatch authority")
            elif event.from_state is WorkflowState.PLANNED and event.to_state is WorkflowState.STARTING:
                raise CorruptSchemaError("PLANNED -> STARTING requires a dispatch claim event")
        if any(count != 1 for count in claim_counts.values()):
            raise CorruptSchemaError("every dispatch row must have exactly one matching claim event")
        for row in milestone_rows:
            key = (str(row["run_id"]), str(row["milestone_id"]))
            milestone = self._milestone_from_row(row)
            history = events_by_milestone.get(key, [])
            sequences = tuple(int(event.sequence) for event in history)
            if sequences != tuple(range(1, len(history) + 1)):
                raise CorruptSchemaError("milestone event sequence is not contiguous")
            replay_state = WorkflowState.PLANNED
            for event in history:
                if event.from_state is not replay_state:
                    raise CorruptSchemaError("milestone event history is non-causal")
                replay_state = event.to_state
            if replay_state is not milestone.state:
                raise CorruptSchemaError("milestone state does not match replayed history")
        if has_h3:
            lease_rows = self._db().execute("SELECT * FROM workspace_leases ORDER BY workspace_path").fetchall()
            leases = {str(row["workspace_path"]): self._workspace_lease_from_row(row) for row in lease_rows}
            integrity_keys = (
                {
                    (str(row["run_id"]), str(row["milestone_id"]))
                    for row in self._db().execute("SELECT run_id, milestone_id FROM execution_integrity").fetchall()
                }
                if has_integrity
                else set()
            )
            execution_rows = self._db().execute("SELECT * FROM executions ORDER BY run_id, milestone_id").fetchall()
            for row in execution_rows:
                execution = self._execution_from_row(row)
                if (
                    has_integrity
                    and execution.status not in {ExecutionStatus.PLANNED, ExecutionStatus.CANCELLED}
                    and (str(execution.run_id), str(execution.milestone_id)) not in integrity_keys
                ):
                    raise CorruptSchemaError("non-planned execution lacks its integrity authority row")
                milestone_state = _row_state(
                    self._db()
                    .execute(
                        "SELECT current_state FROM milestones WHERE run_id = ? AND milestone_id = ?",
                        (str(execution.run_id), str(execution.milestone_id)),
                    )
                    .fetchone()[0]
                )
                if execution.status not in {ExecutionStatus.PLANNED, ExecutionStatus.CANCELLED}:
                    lease = leases.get(str(execution.workspace_path))
                    if lease is None or lease.owner_run_id != execution.run_id:
                        raise CorruptSchemaError("active execution has no matching workspace lease")
                if execution.status is ExecutionStatus.PLANNED:
                    if (
                        execution.checkpoint
                        not in {
                            ControllerCheckpoint.CAPSULE_PLANNED,
                            ControllerCheckpoint.WORKSPACE_LEASED,
                            ControllerCheckpoint.THREAD_STARTING,
                        }
                        or execution.thread_id
                    ):
                        raise CorruptSchemaError("planned execution has post-plan authority")
                elif execution.status is ExecutionStatus.THREAD_STARTED:
                    if execution.thread_id is None or execution.checkpoint not in {
                        ControllerCheckpoint.THREAD_IDENTITY_DURABLE,
                        ControllerCheckpoint.TURN_DURABLE,
                    }:
                        raise CorruptSchemaError("started execution has incomplete thread authority")
                    if milestone_state is not WorkflowState.RUNNING:
                        raise CorruptSchemaError("started execution is not paired with RUNNING workflow state")
                elif execution.status is ExecutionStatus.COMPLETED:
                    if (
                        execution.checkpoint is not ControllerCheckpoint.RESULT_DURABLE
                        or execution.thread_id is None
                        or execution.turn_id is None
                        or execution.result is None
                        or execution.validation is None
                        or execution.protected_after_sha256 is None
                    ):
                        raise CorruptSchemaError("completed execution is missing terminal evidence")
                    if milestone_state is not WorkflowState.COMPLETED:
                        raise CorruptSchemaError("completed execution is not paired with COMPLETED workflow state")
                elif execution.status is ExecutionStatus.UNCERTAIN_PRE_IDENTITY and execution.thread_id is not None:
                    raise CorruptSchemaError("pre-identity uncertainty cannot carry a thread identity")
                elif execution.status is ExecutionStatus.FAILED and (
                    execution.checkpoint is not ControllerCheckpoint.RESULT_DURABLE
                    or execution.result is None
                    or execution.protected_after_sha256 is None
                ):
                    raise CorruptSchemaError("failed execution is missing terminal evidence")
                if execution.status is ExecutionStatus.FAILED and milestone_state is not WorkflowState.FAILED:
                    raise CorruptSchemaError("failed execution is not paired with FAILED workflow state")
                if execution.status is ExecutionStatus.CANCELLED and milestone_state is not WorkflowState.CANCELLED:
                    raise CorruptSchemaError("cancelled execution is not paired with CANCELLED workflow state")
                if (
                    execution.status is ExecutionStatus.UNCERTAIN_PRE_IDENTITY
                    and milestone_state is not WorkflowState.STARTING
                ):
                    raise CorruptSchemaError("pre-identity uncertainty is not paired with STARTING workflow state")
                event_rows = (
                    self._db()
                    .execute(
                        "SELECT sequence FROM sdk_lifecycle_events WHERE run_id = ? AND milestone_id = ? "
                        "ORDER BY turn_id, sequence",
                        (str(execution.run_id), str(execution.milestone_id)),
                    )
                    .fetchall()
                )
                if execution.turn_id is not None and event_rows:
                    sequences = tuple(int(item[0]) for item in event_rows)
                    if sequences != tuple(range(len(sequences))):
                        raise CorruptSchemaError("SDK lifecycle sequence is not contiguous")
            if has_integrity:
                for row in (
                    self._db().execute("SELECT * FROM execution_integrity ORDER BY run_id, milestone_id").fetchall()
                ):
                    integrity = self._execution_integrity_from_row(row)
                    if (
                        integrity.git_authority_after_sha256 is not None
                        and integrity.git_authority_before_sha256 is None
                    ):
                        raise CorruptSchemaError("Git authority after-state lacks its pre-turn authority")

    def _migrate(self, version: SchemaVersion) -> None:
        if version == SchemaVersion(3):
            self._migrate_v3_to_v4()
            return
        if version == SchemaVersion(2):
            self._migrate_v2_to_v3()
            return
        if version != SchemaVersion(1):
            raise UnsupportedSchemaVersion(f"ledger schema {version} has no owned migration")
        self._validate_shape(version)
        self._validate_rows()
        rows = {
            "runs": [tuple(row) for row in self._db().execute("SELECT run_id, created_at, metadata_json FROM runs")],
            "milestones": [tuple(row) for row in self._db().execute("SELECT * FROM milestones")],
            "dispatches": [tuple(row) for row in self._db().execute("SELECT * FROM dispatches")],
            "events": [tuple(row) for row in self._db().execute("SELECT * FROM events")],
        }
        self._db().execute("PRAGMA foreign_keys = OFF")
        try:
            with self._transaction(validate_authority=False):
                for table in ("events", "dispatches", "milestones", "runs", "schema_meta"):
                    self._db().execute(f"DROP TABLE {table}")
                for statement in _V2_TABLE_DDL.values():
                    self._db().execute(statement)
                self._db().executemany(
                    "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                    (
                        ("schema_version", "2"),
                        ("migration_marker", "complete"),
                        ("schema_identity", _SCHEMA_IDENTITIES[SchemaVersion(2)]),
                    ),
                )
                self._db().executemany(
                    "INSERT INTO runs(run_id, created_at, closed_at, metadata_json) VALUES (?, ?, NULL, ?)",
                    rows["runs"],
                )
                for table in ("milestones", "dispatches", "events"):
                    placeholders = ", ".join("?" for _ in rows[table][0]) if rows[table] else ""
                    if placeholders:
                        self._db().executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows[table])
                self._fault("after_migration")
        finally:
            self._db().execute("PRAGMA foreign_keys = ON")
        if self._db().execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise CorruptSchemaError("migrated ledger violates the canonical foreign-key contract")
        self._migrate_v2_to_v3()

    def _migrate_v2_to_v3(self) -> None:
        self._validate_schema_metadata(SchemaVersion(2))
        self._validate_shape(SchemaVersion(2))
        self._validate_rows()
        with self._transaction(validate_authority=False):
            for table in ("workspace_leases", "executions", "sdk_lifecycle_events"):
                self._db().execute(_V3_TABLE_DDL[table])
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(int(SchemaVersion(3))),),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(3)],),
            )
            self._fault("after_migration")
        self._migrate_v3_to_v4()

    def _migrate_v3_to_v4(self) -> None:
        self._validate_schema_metadata(SchemaVersion(3))
        self._validate_shape(SchemaVersion(3))
        self._validate_rows()
        with self._transaction(validate_authority=False):
            self._db().execute(_EXECUTION_INTEGRITY_DDL)
            self._db().execute(
                "INSERT INTO execution_integrity(run_id, milestone_id, provenance, sandbox_policy_sha256, "
                "git_authority_before_sha256, git_authority_after_sha256, created_at, updated_at) "
                "SELECT run_id, milestone_id, 'legacy_v3', NULL, NULL, NULL, created_at, updated_at "
                "FROM executions WHERE status != ?",
                (ExecutionStatus.PLANNED.value,),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(int(CURRENT_SCHEMA_VERSION)),),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[CURRENT_SCHEMA_VERSION],),
            )
            self._fault("after_migration")

    @contextmanager
    def _transaction(self, *, validate_authority: bool = True) -> Iterator[None]:
        connection = self._db()
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._validate_live_identity()
            if validate_authority:
                self._validate_schema_metadata(CURRENT_SCHEMA_VERSION)
                self._validate_shape(CURRENT_SCHEMA_VERSION)
                self._validate_rows()
                self._fault("after_authority_validation")
            yield
            self._fault("before_commit")
            self._validate_live_identity()
            if validate_authority:
                self._validate_schema_metadata(CURRENT_SCHEMA_VERSION)
                self._validate_shape(CURRENT_SCHEMA_VERSION)
                self._validate_rows()
            self._commit_transaction(connection)
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise

    def _commit_transaction(self, connection: sqlite3.Connection) -> None:
        """Authorize identity at SQLite's transaction-commit boundary."""

        boundary_error: BaseException | None = None

        def authorize(
            action_code: int,
            operation: str | None,
            _argument: str | None,
            _database: str | None,
            _source: str | None,
        ) -> int:
            nonlocal boundary_error
            if action_code == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT":
                try:
                    self._fault("at_commit_boundary")
                    self._validate_live_identity()
                except BaseException as exc:
                    boundary_error = exc
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        try:
            connection.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            if boundary_error is not None:
                raise boundary_error from exc
            raise
        finally:
            connection.set_authorizer(None)

    def _fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def close(self) -> None:
        self._close_connection()
        if self._database_fd is not None:
            os.close(self._database_fd)
            self._database_fd = None
        for descriptor in reversed(self._directory_fds):
            os.close(descriptor)
        self._directory_fds = ()
        self._open_identity = None

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

    def _verify_run(self, expected: RunRecord) -> RunRecord:
        row = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(expected.run_id),)).fetchone()
        if row is None:
            raise CorruptSchemaError("successful run write did not leave its durable row")
        actual = self._run_from_row(row)
        if actual != expected:
            raise CorruptSchemaError("successful run write was rewritten before commit")
        return actual

    def _verify_milestone(self, expected: MilestoneRecord) -> MilestoneRecord:
        row = (
            self._db()
            .execute(
                "SELECT * FROM milestones WHERE run_id = ? AND milestone_id = ?",
                (str(expected.run_id), str(expected.milestone_id)),
            )
            .fetchone()
        )
        if row is None:
            raise CorruptSchemaError("successful milestone write did not leave its durable row")
        actual = self._milestone_from_row(row)
        if actual != expected:
            raise CorruptSchemaError("successful milestone write was rewritten before commit")
        return actual

    def _verify_dispatch(self, expected: DispatchClaim) -> DispatchClaim:
        row = (
            self._db()
            .execute("SELECT * FROM dispatches WHERE dispatch_id = ?", (str(expected.dispatch_id),))
            .fetchone()
        )
        if row is None:
            raise CorruptSchemaError("successful dispatch claim did not leave its durable row")
        actual = self._dispatch_from_row(row)
        if actual != expected:
            raise CorruptSchemaError("successful dispatch claim was rewritten before commit")
        return actual

    def _verify_event(self, expected: EventRecord) -> EventRecord:
        row = self._db().execute("SELECT * FROM events WHERE event_id = ?", (str(expected.event_id),)).fetchone()
        if row is None:
            raise CorruptSchemaError("successful event write did not leave its durable row")
        actual = self._event_from_row(row)
        actual_reason = (actual.reason.code, actual.reason.detail) if actual.reason is not None else None
        expected_reason = (expected.reason.code, expected.reason.detail) if expected.reason is not None else None
        if (
            actual.event_id,
            actual.run_id,
            actual.milestone_id,
            actual.sequence,
            actual.from_state,
            actual.to_state,
            actual.event_type,
            actual_reason,
            actual.occurred_at,
            actual.dispatch_id,
            actual.data,
        ) != (
            expected.event_id,
            expected.run_id,
            expected.milestone_id,
            expected.sequence,
            expected.from_state,
            expected.to_state,
            expected.event_type,
            expected_reason,
            expected.occurred_at,
            expected.dispatch_id,
            expected.data,
        ):
            raise CorruptSchemaError("successful event write was rewritten before commit")
        return actual

    # ------------------------------------------------------------------
    # Record creation and lookup
    # ------------------------------------------------------------------

    def create_run(self, run_id: RunId | str, *, metadata: Mapping[str, object] | None = None) -> RunRecord:
        run = _run_id(run_id)
        checked, encoded = _empty_object(metadata, field_name="run metadata")
        created = utc_now()
        with self._transaction():
            existing = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(run),)).fetchone()
            if existing is not None:
                if _decode_object(existing["metadata_json"], field_name="run metadata") != checked:
                    raise LedgerError(f"run {run} already exists with different metadata")
                return self._verify_run(self._run_from_row(existing))
            expected = RunRecord(run, created, None, checked)
            self._db().execute(
                "INSERT INTO runs(run_id, created_at, closed_at, metadata_json) VALUES (?, ?, NULL, ?)",
                (str(run), created, encoded),
            )
            return self._verify_run(expected)

    def get_run(self, run_id: RunId | str) -> RunRecord:
        run = _run_id(run_id)
        row = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(run),)).fetchone()
        if row is None:
            raise RecordNotFound(f"run {run} does not exist")
        return self._run_from_row(row)

    def close_run(self, run_id: RunId | str) -> RunRecord:
        run = _run_id(run_id)
        closed = utc_now()
        with self._transaction():
            existing = self.get_run(run)
            expected = RunRecord(existing.run_id, existing.created_at, existing.closed_at or closed, existing.metadata)
            self._db().execute(
                "UPDATE runs SET closed_at = COALESCE(closed_at, ?) WHERE run_id = ?", (closed, str(run))
            )
            return self._verify_run(expected)

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
            existing = (
                self._db()
                .execute("SELECT * FROM milestones WHERE run_id = ? AND milestone_id = ?", (str(run), str(milestone)))
                .fetchone()
            )
            if existing is not None:
                if _decode_object(existing["metadata_json"], field_name="milestone metadata") != checked:
                    raise LedgerError(f"milestone {milestone} already exists with different metadata")
                return self._verify_milestone(self._milestone_from_row(existing))
            expected = MilestoneRecord(run, milestone, WorkflowState.PLANNED, now, now, checked)
            self._db().execute(
                "INSERT INTO milestones(run_id, milestone_id, current_state, created_at, updated_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(run), str(milestone), WorkflowState.PLANNED.value, now, now, encoded),
            )
            return self._verify_milestone(expected)

    def get_milestone(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> MilestoneRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        row = (
            self._db()
            .execute("SELECT * FROM milestones WHERE run_id = ? AND milestone_id = ?", (str(run), str(milestone)))
            .fetchone()
        )
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
            existing = (
                self._db().execute("SELECT * FROM dispatches WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if existing is not None:
                return self._verify_dispatch(self._dispatch_from_row(existing))
            owner = (
                self._db()
                .execute(
                    "SELECT dispatch_id FROM dispatches WHERE run_id = ? AND milestone_id = ? AND role = ?",
                    (str(run), str(milestone), str(role_value)),
                )
                .fetchone()
            )
            if owner is not None:
                raise DispatchConflict(f"milestone {milestone} role {role_value} is owned by {owner['dispatch_id']}")
            current = self.get_milestone(run, milestone)
            if current.state is not WorkflowState.PLANNED:
                raise InvalidTransition(f"dispatch claim requires PLANNED, found {current.state.value}")
            self._db().execute(
                "INSERT INTO dispatches(dispatch_id, run_id, milestone_id, role, generation, claimed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(dispatch_id), str(run), str(milestone), str(role_value), int(generation_value), now),
            )
            self._db().execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (WorkflowState.STARTING.value, now, str(run), str(milestone)),
            )
            self._fault("after_state_update")
            event = self._append_event_in_transaction(
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
            expected_claim = DispatchClaim(dispatch_id, run, milestone, role_value, generation_value, now)
            expected_milestone = MilestoneRecord(
                current.run_id,
                current.milestone_id,
                WorkflowState.STARTING,
                current.created_at,
                now,
                current.metadata,
            )
            self._verify_dispatch(expected_claim)
            self._verify_milestone(expected_milestone)
            self._verify_event(event)
            return expected_claim

    def get_dispatch(self, dispatch_id: DispatchId | str) -> DispatchClaim:
        dispatch = DispatchId(dispatch_id)
        row = self._db().execute("SELECT * FROM dispatches WHERE dispatch_id = ?", (str(dispatch),)).fetchone()
        if row is None:
            raise RecordNotFound(f"dispatch {dispatch} does not exist")
        return self._dispatch_from_row(row)

    def transition(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        to_state: WorkflowState | str,
        *,
        expected_state: WorkflowState | str,
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
        expected_value = coerce_state(expected_state)
        normalized_reason = _reason(reason)
        if data is not None:
            _empty_object(data, field_name="event data")
        dispatch = DispatchId(dispatch_id) if dispatch_id is not None else None
        normalized_event_type = _event_type(event_type or "state_transition")
        if normalized_event_type != "state_transition" or dispatch is not None:
            raise ValueError("normal state transitions cannot carry dispatch authority")
        if normalized_reason is not None and normalized_reason.code is ReasonCode.DISPATCH_CLAIMED:
            raise ValueError("dispatch_claimed reason is reserved for claim_dispatch")
        with self._transaction():
            current = self.get_milestone(run, milestone)
            if current.state is not expected_value:
                raise StaleWriter(
                    f"stale milestone writer: expected {expected_value.value}, current is {current.state.value}"
                )
            if current.state is WorkflowState.PLANNED and target is WorkflowState.STARTING:
                raise InvalidTransition("PLANNED -> STARTING is reserved for claim_dispatch")
            if not is_transition_allowed(current.state, target):
                raise InvalidTransition(f"{current.state.value} -> {target.value} is not allowed")
            now = utc_now()
            self._db().execute(
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
            expected_milestone = MilestoneRecord(
                current.run_id,
                current.milestone_id,
                target,
                current.created_at,
                now,
                current.metadata,
            )
            self._verify_milestone(expected_milestone)
            return self._verify_event(event)

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
        last = (
            self._db()
            .execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE run_id = ? AND milestone_id = ?",
                (str(run_id), str(milestone_id)),
            )
            .fetchone()
        )
        sequence = EventSequence(int(last[0]) + 1)
        event_id = EventId(f"{run_id}/{milestone_id}/{int(sequence)}")
        encoded_data = None
        occurred_at = utc_now()
        if data is not None:
            _empty_object(data, field_name="event data")
        self._db().execute(
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
                occurred_at,
            ),
        )
        expected = EventRecord(
            event_id,
            run_id,
            milestone_id,
            sequence,
            from_state,
            to_state,
            event_type,
            reason,
            occurred_at,
            dispatch_id,
            data,
        )
        return self._verify_event(expected)

    # ------------------------------------------------------------------
    # H3 controller facts
    # ------------------------------------------------------------------

    def record_sandbox_policy(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        sandbox_policy_sha256: str,
    ) -> ExecutionIntegrityRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        policy = _sha256(sandbox_policy_sha256, field_name="sandbox policy digest")
        now = utc_now()
        with self._transaction():
            self.get_execution(run, milestone)
            existing = (
                self._db()
                .execute(
                    "SELECT * FROM execution_integrity WHERE run_id = ? AND milestone_id = ?",
                    (str(run), str(milestone)),
                )
                .fetchone()
            )
            if existing is not None:
                record = self._execution_integrity_from_row(existing)
                if record.sandbox_policy_sha256 != policy:
                    raise StaleWriter("execution is already bound to a different sandbox policy")
                return record
            self._db().execute(
                "INSERT INTO execution_integrity(run_id, milestone_id, provenance, sandbox_policy_sha256, "
                "git_authority_before_sha256, git_authority_after_sha256, created_at, updated_at) "
                "VALUES (?, ?, 'controller_v1', ?, NULL, NULL, ?, ?)",
                (str(run), str(milestone), policy, now, now),
            )
            return self.get_execution_integrity(run, milestone)

    def record_git_authority_before(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        git_authority_before_sha256: str,
    ) -> ExecutionIntegrityRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        authority = _sha256(git_authority_before_sha256, field_name="Git authority digest")
        now = utc_now()
        with self._transaction():
            current = self.get_execution_integrity(run, milestone)
            if current.git_authority_before_sha256 is not None:
                if current.git_authority_before_sha256 != authority:
                    raise StaleWriter("execution Git authority changed before its SDK turn")
                return current
            self._db().execute(
                "UPDATE execution_integrity SET git_authority_before_sha256 = ?, updated_at = ? "
                "WHERE run_id = ? AND milestone_id = ?",
                (authority, now, str(run), str(milestone)),
            )
            return self.get_execution_integrity(run, milestone)

    def get_execution_integrity(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionIntegrityRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        row = (
            self._db()
            .execute(
                "SELECT * FROM execution_integrity WHERE run_id = ? AND milestone_id = ?",
                (str(run), str(milestone)),
            )
            .fetchone()
        )
        if row is None:
            raise KeyError(f"unknown execution integrity: {run}/{milestone}")
        return self._execution_integrity_from_row(row)

    def plan_execution(
        self,
        capsule: ExecutionCapsule,
        *,
        capsule_path: Path,
        capsule_sha256: str,
        protected_before_sha256: str,
    ) -> ExecutionRecord:
        capsule_digest = _sha256(capsule_sha256, field_name="capsule digest")
        protected_digest = _sha256(protected_before_sha256, field_name="protected-path digest")
        if not capsule_path.is_absolute():
            raise ValueError("capsule path must be absolute")
        now = utc_now()
        with self._transaction():
            self.get_run(capsule.run_id)
            self.get_milestone(capsule.run_id, capsule.milestone_id)
            existing = (
                self._db()
                .execute(
                    "SELECT * FROM executions WHERE run_id = ? AND milestone_id = ?",
                    (str(capsule.run_id), str(capsule.milestone_id)),
                )
                .fetchone()
            )
            if existing is not None:
                record = self._execution_from_row(existing)
                expected = (
                    capsule_path,
                    capsule_digest,
                    capsule.workspace_path,
                    capsule.model,
                    capsule.reasoning_effort,
                    protected_digest,
                )
                actual = (
                    record.capsule_path,
                    record.capsule_sha256,
                    record.workspace_path,
                    record.model,
                    record.reasoning_effort,
                    record.protected_before_sha256,
                )
                if actual != expected:
                    raise WorkspaceLeaseConflict("execution capsule conflicts with its durable plan")
                return record
            self._db().execute(
                "INSERT INTO executions(run_id, milestone_id, capsule_path, capsule_sha256, workspace_path, "
                "model, reasoning_effort, status, checkpoint, thread_id, turn_id, turn_output_json, result_json, "
                "validation_argv_json, validation_exit_code, validation_stdout_sha256, "
                "validation_stderr_sha256, validation_timed_out, validation_duration_seconds, "
                "protected_before_sha256, protected_after_sha256, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, NULL, ?, ?)",
                (
                    str(capsule.run_id),
                    str(capsule.milestone_id),
                    str(capsule_path),
                    capsule_digest,
                    str(capsule.workspace_path),
                    capsule.model,
                    capsule.reasoning_effort.value,
                    ExecutionStatus.PLANNED.value,
                    ControllerCheckpoint.CAPSULE_PLANNED.value,
                    protected_digest,
                    now,
                    now,
                ),
            )
            return self.get_execution(capsule.run_id, capsule.milestone_id)

    def acquire_workspace_lease(
        self,
        capsule: ExecutionCapsule,
    ) -> WorkspaceLeaseRecord:
        now = utc_now()
        with self._transaction():
            execution = self.get_execution(capsule.run_id, capsule.milestone_id)
            if execution.workspace_path != capsule.workspace_path:
                raise WorkspaceLeaseConflict("durable execution selected a different workspace")
            active_statuses = tuple(
                status.value
                for status in ExecutionStatus
                if status
                not in {
                    ExecutionStatus.COMPLETED,
                    ExecutionStatus.FAILED,
                    ExecutionStatus.CANCELLED,
                }
            )
            placeholders = ", ".join("?" for _ in active_statuses)
            active_owner = (
                self._db()
                .execute(
                    "SELECT x.run_id, x.milestone_id, x.status FROM executions x "
                    "JOIN workspace_leases l ON l.workspace_path = x.workspace_path "
                    f"WHERE x.workspace_path = ? AND x.status IN ({placeholders}) "
                    "AND NOT (x.run_id = ? AND x.milestone_id = ?)",
                    (str(capsule.workspace_path), *active_statuses, str(capsule.run_id), str(capsule.milestone_id)),
                )
                .fetchone()
            )
            if active_owner is not None:
                raise WorkspaceLeaseConflict(
                    "workspace already has a nonterminal execution owner "
                    f"{active_owner['run_id']}/{active_owner['milestone_id']}"
                )
            owner = (
                self._db()
                .execute(
                    "SELECT * FROM workspace_leases WHERE owner_run_id = ? AND lane = ?",
                    (str(capsule.run_id), capsule.lane),
                )
                .fetchone()
            )
            path_owner = (
                self._db()
                .execute(
                    "SELECT * FROM workspace_leases WHERE workspace_path = ?",
                    (str(capsule.workspace_path),),
                )
                .fetchone()
            )
            if owner is not None or path_owner is not None:
                candidate = owner or path_owner
                lease = self._workspace_lease_from_row(candidate)
                expected = (
                    capsule.workspace_path,
                    capsule.repository_root,
                    capsule.workspace_mode,
                    capsule.branch,
                    capsule.base_sha,
                    capsule.lane,
                    capsule.run_id,
                )
                actual = (
                    lease.workspace_path,
                    lease.repository_root,
                    lease.mode,
                    lease.branch,
                    lease.base_sha,
                    lease.lane,
                    lease.owner_run_id,
                )
                if actual != expected:
                    raise WorkspaceLeaseConflict("workspace is already leased under different facts")
            else:
                self._db().execute(
                    "INSERT INTO workspace_leases(workspace_path, repository_root, mode, branch, base_sha, lane, "
                    "owner_run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(capsule.workspace_path),
                        str(capsule.repository_root),
                        capsule.workspace_mode.value,
                        capsule.branch,
                        capsule.base_sha,
                        capsule.lane,
                        str(capsule.run_id),
                        now,
                    ),
                )
                lease = self._workspace_lease_from_row(
                    self._db()
                    .execute("SELECT * FROM workspace_leases WHERE workspace_path = ?", (str(capsule.workspace_path),))
                    .fetchone()
                )
            if execution.status is ExecutionStatus.PLANNED:
                self._db().execute(
                    "UPDATE executions SET checkpoint = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                    (
                        ControllerCheckpoint.WORKSPACE_LEASED.value,
                        now,
                        str(capsule.run_id),
                        str(capsule.milestone_id),
                    ),
                )
            return lease

    def record_thread_identity(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        thread_id: ThreadIdentity,
    ) -> ExecutionRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        now = utc_now()
        with self._transaction():
            current = self.get_execution(run, milestone)
            if current.thread_id is not None:
                if current.thread_id != thread_id:
                    raise StaleWriter("execution already owns a different SDK thread identity")
                return current
            if (
                current.status is not ExecutionStatus.PLANNED
                or current.checkpoint is not ControllerCheckpoint.THREAD_STARTING
            ):
                raise StaleWriter("thread identity requires a planned leased execution")
            workflow = self.get_milestone(run, milestone)
            if workflow.state is not WorkflowState.STARTING:
                raise StaleWriter("thread identity requires a STARTING workflow state")
            self._db().execute(
                "UPDATE executions SET status = ?, checkpoint = ?, thread_id = ?, updated_at = ? "
                "WHERE run_id = ? AND milestone_id = ?",
                (
                    ExecutionStatus.THREAD_STARTED.value,
                    ControllerCheckpoint.THREAD_IDENTITY_DURABLE.value,
                    thread_id.id,
                    now,
                    str(run),
                    str(milestone),
                ),
            )
            self._db().execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (WorkflowState.RUNNING.value, now, str(run), str(milestone)),
            )
            event = self._append_event_in_transaction(
                run,
                milestone,
                from_state=WorkflowState.STARTING,
                to_state=WorkflowState.RUNNING,
                event_type="state_transition",
                reason=None,
                dispatch_id=None,
                data=None,
            )
            expected = self.get_execution(run, milestone)
            if self.current_state(run, milestone) is not WorkflowState.RUNNING:
                raise CorruptSchemaError("thread identity write did not advance workflow state")
            self._verify_event(event)
            return expected

    def record_thread_starting(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        now = utc_now()
        with self._transaction():
            current = self.get_execution(run, milestone)
            if current.status is not ExecutionStatus.PLANNED:
                raise StaleWriter("SDK thread start requires a planned execution")
            if current.checkpoint is ControllerCheckpoint.THREAD_STARTING:
                return current
            if current.checkpoint is not ControllerCheckpoint.WORKSPACE_LEASED:
                raise StaleWriter("SDK thread start requires a durable workspace lease")
            self._db().execute(
                "UPDATE executions SET checkpoint = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (
                    ControllerCheckpoint.THREAD_STARTING.value,
                    now,
                    str(run),
                    str(milestone),
                ),
            )
            return self.get_execution(run, milestone)

    def record_pre_identity_uncertainty(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        now = utc_now()
        with self._transaction():
            current = self.get_execution(run, milestone)
            if current.thread_id is not None:
                raise StaleWriter("pre-identity uncertainty cannot replace a durable identity")
            self._db().execute(
                "UPDATE executions SET status = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (ExecutionStatus.UNCERTAIN_PRE_IDENTITY.value, now, str(run), str(milestone)),
            )
            return self.get_execution(run, milestone)

    def record_turn_starting(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        """Durably mark the SDK turn boundary before making an external call."""

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        now = utc_now()
        with self._transaction():
            current = self.get_execution(run, milestone)
            if current.status is not ExecutionStatus.THREAD_STARTED or current.thread_id is None:
                raise StaleWriter("turn start requires a durable SDK thread")
            if current.turn_id is not None:
                return current
            if current.turn_output == _TURN_STARTING_MARKER:
                return current
            self._db().execute(
                "UPDATE executions SET turn_output_json = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (_encode_json(_TURN_STARTING_MARKER), now, str(run), str(milestone)),
            )
            return self.get_execution(run, milestone)

    def record_turn(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        observation: TurnObservation,
    ) -> ExecutionRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        now = utc_now()
        with self._transaction():
            current = self.get_execution(run, milestone)
            if current.thread_id != observation.thread_id:
                raise StaleWriter("turn observation belongs to a different SDK thread")
            if current.turn_id is not None:
                if current.turn_id != observation.turn_id:
                    raise StaleWriter("execution already owns a different SDK turn")
                return current
            for event in observation.events:
                self._db().execute(
                    "INSERT INTO sdk_lifecycle_events(run_id, milestone_id, turn_id, sequence, method, event_turn_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(run),
                        str(milestone),
                        observation.turn_id,
                        event.sequence,
                        event.method,
                        event.turn_id,
                    ),
                )
            self._db().execute(
                "UPDATE executions SET checkpoint = ?, turn_id = ?, turn_output_json = ?, updated_at = ? "
                "WHERE run_id = ? AND milestone_id = ?",
                (
                    ControllerCheckpoint.TURN_DURABLE.value,
                    observation.turn_id,
                    _encode_json(observation.structured_output or {}),
                    now,
                    str(run),
                    str(milestone),
                ),
            )
            return self.get_execution(run, milestone)

    def record_terminal_execution(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        *,
        result: JsonObject,
        validation: ValidationObservation,
        protected_after_sha256: str,
        git_authority_after_sha256: str,
    ) -> ExecutionRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        after_digest = _sha256(protected_after_sha256, field_name="protected-path digest")
        git_after = _sha256(git_authority_after_sha256, field_name="Git authority digest")
        now = utc_now()
        outcome_status = result.get("status") if isinstance(result, Mapping) else None
        outcome_failed = isinstance(outcome_status, str) and outcome_status.strip().lower() in {
            "failed",
            "failure",
            "blocked",
            "cancelled",
            "error",
        }
        terminal = (
            ExecutionStatus.COMPLETED
            if validation.exit_code == 0 and not validation.timed_out and not outcome_failed
            else ExecutionStatus.FAILED
        )
        with self._transaction():
            current = self.get_execution(run, milestone)
            if current.status in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED}:
                return current
            if current.status is not ExecutionStatus.THREAD_STARTED or current.turn_id is None:
                raise StaleWriter("terminal result requires a durable SDK turn")
            integrity = self.get_execution_integrity(run, milestone)
            if integrity.git_authority_before_sha256 is None:
                raise StaleWriter("terminal result requires a durable pre-turn Git authority snapshot")
            workflow = self.get_milestone(run, milestone)
            if workflow.state is not WorkflowState.RUNNING:
                raise StaleWriter("terminal result requires a RUNNING workflow state")
            self._db().execute(
                "UPDATE executions SET status = ?, checkpoint = ?, result_json = ?, validation_argv_json = ?, "
                "validation_exit_code = ?, validation_stdout_sha256 = ?, validation_stderr_sha256 = ?, "
                "validation_timed_out = ?, validation_duration_seconds = ?, protected_after_sha256 = ?, "
                "updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (
                    terminal.value,
                    ControllerCheckpoint.RESULT_DURABLE.value,
                    _encode_json(result),
                    _encode_json(
                        list(validation.argv)
                        if validation.error_code is None
                        else {
                            "argv": list(validation.argv),
                            "error_code": validation.error_code.value,
                        }
                    ),
                    validation.exit_code,
                    validation.stdout_sha256,
                    validation.stderr_sha256,
                    int(validation.timed_out),
                    validation.duration_seconds,
                    after_digest,
                    now,
                    str(run),
                    str(milestone),
                ),
            )
            self._db().execute(
                "UPDATE execution_integrity SET git_authority_after_sha256 = ?, updated_at = ? "
                "WHERE run_id = ? AND milestone_id = ?",
                (git_after, now, str(run), str(milestone)),
            )
            target = WorkflowState.COMPLETED if terminal is ExecutionStatus.COMPLETED else WorkflowState.FAILED
            self._db().execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (target.value, now, str(run), str(milestone)),
            )
            event = self._append_event_in_transaction(
                run,
                milestone,
                from_state=WorkflowState.RUNNING,
                to_state=target,
                event_type="state_transition",
                reason=WorkflowReason(
                    ReasonCode.TERMINAL_OUTCOME
                    if terminal is ExecutionStatus.COMPLETED
                    else ReasonCode.EXECUTION_FAILURE
                ),
                dispatch_id=None,
                data=None,
            )
            result_record = self.get_execution(run, milestone)
            if self.current_state(run, milestone) is not target:
                raise CorruptSchemaError("terminal execution write did not advance workflow state")
            self._verify_event(event)
            return result_record

    def cancel_execution(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        now = utc_now()
        with self._transaction():
            current = self.get_execution(run, milestone)
            if current.status in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}:
                return current
            workflow = self.get_milestone(run, milestone)
            if workflow.state in TERMINAL_STATES:
                return current
            self._db().execute(
                "UPDATE executions SET status = ?, checkpoint = ?, updated_at = ? "
                "WHERE run_id = ? AND milestone_id = ?",
                (
                    ExecutionStatus.CANCELLED.value,
                    ControllerCheckpoint.RESULT_DURABLE.value,
                    now,
                    str(run),
                    str(milestone),
                ),
            )
            self._db().execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (WorkflowState.CANCELLED.value, now, str(run), str(milestone)),
            )
            event = self._append_event_in_transaction(
                run,
                milestone,
                from_state=workflow.state,
                to_state=WorkflowState.CANCELLED,
                event_type="state_transition",
                reason=None,
                dispatch_id=None,
                data=None,
            )
            result_record = self.get_execution(run, milestone)
            self._verify_event(event)
            return result_record

    def get_execution(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        row = (
            self._db()
            .execute("SELECT * FROM executions WHERE run_id = ? AND milestone_id = ?", (str(run), str(milestone)))
            .fetchone()
        )
        if row is None:
            raise RecordNotFound(f"execution {run}/{milestone} does not exist")
        return self._execution_from_row(row)

    def get_workspace_lease(self, workspace_path: Path) -> WorkspaceLeaseRecord:
        row = (
            self._db()
            .execute("SELECT * FROM workspace_leases WHERE workspace_path = ?", (str(workspace_path),))
            .fetchone()
        )
        if row is None:
            raise RecordNotFound(f"workspace lease {workspace_path} does not exist")
        return self._workspace_lease_from_row(row)

    def sdk_lifecycle_events(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> tuple[LifecycleEvent, ...]:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        rows = (
            self._db()
            .execute(
                "SELECT * FROM sdk_lifecycle_events WHERE run_id = ? AND milestone_id = ? ORDER BY turn_id, sequence",
                (str(run), str(milestone)),
            )
            .fetchall()
        )
        return tuple(LifecycleEvent(int(row["sequence"]), str(row["method"]), row["event_turn_id"]) for row in rows)

    # ------------------------------------------------------------------
    # Read-only snapshots and recovery facts
    # ------------------------------------------------------------------

    def events(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> tuple[EventRecord, ...]:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        rows = (
            self._db()
            .execute(
                "SELECT * FROM events WHERE run_id = ? AND milestone_id = ? ORDER BY sequence",
                (str(run), str(milestone)),
            )
            .fetchall()
        )
        return tuple(self._event_from_row(row) for row in rows)

    def recovery_facts(self, run_id: RunId | str) -> tuple[RecoveryFact, ...]:
        run = _run_id(run_id)
        with self._read_transaction():
            rows = (
                self._db()
                .execute(
                    "SELECT d.*, m.current_state FROM dispatches d JOIN milestones m "
                    "ON m.run_id = d.run_id AND m.milestone_id = d.milestone_id "
                    "WHERE d.run_id = ? ORDER BY d.milestone_id, d.role, d.generation",
                    (str(run),),
                )
                .fetchall()
            )
            self._fault("after_recovery_dispatch_rows_read")
            facts: list[RecoveryFact] = []
            for row in rows:
                state = _row_state(row["current_state"])
                if state in TERMINAL_STATES:
                    continue
                dispatch = self._dispatch_from_row(row)
                last_sequence = (
                    self._db()
                    .execute(
                        "SELECT MAX(sequence) FROM events WHERE run_id = ? AND milestone_id = ?",
                        (str(dispatch.run_id), str(dispatch.milestone_id)),
                    )
                    .fetchone()[0]
                )
                last = (
                    self._db()
                    .execute(
                        "SELECT * FROM events WHERE run_id = ? AND milestone_id = ? AND sequence = ?",
                        (str(dispatch.run_id), str(dispatch.milestone_id), int(last_sequence)),
                    )
                    .fetchone()
                )
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
            milestone_rows = (
                self._db()
                .execute("SELECT * FROM milestones WHERE run_id = ? ORDER BY milestone_id", (str(run),))
                .fetchall()
            )
            dispatch_rows = (
                self._db()
                .execute(
                    "SELECT * FROM dispatches WHERE run_id = ? ORDER BY milestone_id, role, generation", (str(run),)
                )
                .fetchall()
            )
            event_rows = (
                self._db()
                .execute("SELECT * FROM events WHERE run_id = ? ORDER BY milestone_id, sequence", (str(run),))
                .fetchall()
            )
            return LedgerSnapshot(
                record,
                tuple(self._milestone_from_row(row) for row in milestone_rows),
                tuple(self._dispatch_from_row(row) for row in dispatch_rows),
                tuple(self._event_from_row(row) for row in event_rows),
            )

    @contextmanager
    def _read_transaction(self) -> Iterator[None]:
        self._db().execute("BEGIN")
        try:
            yield
        finally:
            try:
                self._db().execute("ROLLBACK")
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

    @staticmethod
    def _workspace_lease_from_row(row: sqlite3.Row) -> WorkspaceLeaseRecord:
        return WorkspaceLeaseRecord(
            Path(str(row["workspace_path"])),
            Path(str(row["repository_root"])),
            WorkspaceMode(str(row["mode"])),
            str(row["branch"]),
            str(row["base_sha"]),
            str(row["lane"]),
            RunId(row["owner_run_id"]),
            str(row["created_at"]),
        )

    @staticmethod
    def _execution_from_row(row: sqlite3.Row) -> ExecutionRecord:
        validation: ValidationObservation | None = None
        argv_raw = row["validation_argv_json"]
        if argv_raw is not None:
            try:
                argv_decoded = json.loads(str(argv_raw))
            except json.JSONDecodeError as exc:
                raise SchemaError("validation argv is not valid JSON") from exc
            error_code: ValidationFailureCode | None = None
            if isinstance(argv_decoded, dict):
                encoded_argv = argv_decoded.get("argv")
                encoded_error = argv_decoded.get("error_code")
                if set(argv_decoded) != {"argv", "error_code"} or not isinstance(encoded_argv, list):
                    raise SchemaError("validation observation envelope is invalid")
                if encoded_error is not None:
                    try:
                        error_code = ValidationFailureCode(str(encoded_error))
                    except ValueError as exc:
                        raise SchemaError("validation error code is unsupported") from exc
                argv_decoded = encoded_argv
            if not isinstance(argv_decoded, list) or any(not isinstance(item, str) for item in argv_decoded):
                raise SchemaError("validation argv is not a string list")
            required = (
                row["validation_exit_code"],
                row["validation_stdout_sha256"],
                row["validation_stderr_sha256"],
                row["validation_timed_out"],
                row["validation_duration_seconds"],
            )
            if any(value is None for value in required):
                raise SchemaError("validation observation is incomplete")
            validation = ValidationObservation(
                tuple(argv_decoded),
                int(row["validation_exit_code"]),
                _sha256(str(row["validation_stdout_sha256"]), field_name="validation stdout digest"),
                _sha256(str(row["validation_stderr_sha256"]), field_name="validation stderr digest"),
                bool(row["validation_timed_out"]),
                float(row["validation_duration_seconds"]),
                error_code,
            )
        return ExecutionRecord(
            RunId(row["run_id"]),
            MilestoneId(row["milestone_id"]),
            Path(str(row["workspace_path"])),
            Path(str(row["capsule_path"])),
            _sha256(str(row["capsule_sha256"]), field_name="capsule digest"),
            str(row["model"]),
            ReasoningEffort(str(row["reasoning_effort"])),
            ExecutionStatus(str(row["status"])),
            ControllerCheckpoint(str(row["checkpoint"])),
            ThreadIdentity(str(row["thread_id"])) if row["thread_id"] is not None else None,
            str(row["turn_id"]) if row["turn_id"] is not None else None,
            _decode_json_object(row["turn_output_json"], field_name="turn output"),
            _decode_json_object(row["result_json"], field_name="execution result"),
            validation,
            _sha256(str(row["protected_before_sha256"]), field_name="protected before digest"),
            (
                _sha256(str(row["protected_after_sha256"]), field_name="protected after digest")
                if row["protected_after_sha256"] is not None
                else None
            ),
            str(row["created_at"]),
            str(row["updated_at"]),
        )

    @staticmethod
    def _execution_integrity_from_row(row: sqlite3.Row) -> ExecutionIntegrityRecord:
        return ExecutionIntegrityRecord(
            RunId(row["run_id"]),
            MilestoneId(row["milestone_id"]),
            str(row["provenance"]),
            (
                _sha256(str(row["sandbox_policy_sha256"]), field_name="sandbox policy digest")
                if row["sandbox_policy_sha256"] is not None
                else None
            ),
            (
                _sha256(str(row["git_authority_before_sha256"]), field_name="Git authority before digest")
                if row["git_authority_before_sha256"] is not None
                else None
            ),
            (
                _sha256(str(row["git_authority_after_sha256"]), field_name="Git authority after digest")
                if row["git_authority_after_sha256"] is not None
                else None
            ),
            str(row["created_at"]),
            str(row["updated_at"]),
        )
