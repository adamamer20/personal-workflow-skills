from __future__ import annotations

import inspect
import os
import sqlite3
import unittest
from multiprocessing import Process, Queue
from pathlib import Path
from tempfile import TemporaryDirectory

import codex_flow.artifacts as artifacts_module
import codex_flow.ledger as ledger_module
from codex_flow.artifacts import ArtifactError, ArtifactProjector, UnsafeArtifactPath, rebuild_projections
from codex_flow.domain import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    DispatchId,
    PostIdentityExecutionFailure,
    PreIdentityTransportFailure,
    ReasonCode,
    ReviewRejected,
    TerminalFailureAfterIdentity,
    WorkflowState,
)
from codex_flow.ledger import (
    _EVENT_TYPES_SQL,
    _REASON_CODES_SQL,
    _SCHEMA_IDENTITY,
    _STATES_SQL,
    _V2_TABLE_DDL,
    CorruptSchemaError,
    DispatchConflict,
    InvalidTransition,
    Ledger,
    SchemaError,
    StaleWriter,
    UnsupportedSchemaVersion,
)


def _claim_worker(path: str, queue: Queue[str]) -> None:
    try:
        ledger = Ledger(path)
        ledger.create_run("race")
        ledger.create_milestone("race", "m")
        claim = ledger.claim_dispatch("race", "m", "executor", 1)
        queue.put(f"ok:{claim.dispatch_id}")
    except Exception as exc:  # pragma: no cover - assertion captures worker result
        queue.put(f"error:{type(exc).__name__}:{exc}")


def _claim_generation_worker(path: str, generation: int, queue: Queue[str]) -> None:
    try:
        ledger = Ledger(path)
        claim = ledger.claim_dispatch("race", "m", "executor", generation)
        queue.put(f"ok:{claim.dispatch_id}")
    except Exception as exc:  # pragma: no cover - assertion captures worker result
        queue.put(f"error:{type(exc).__name__}")


def _make_v1_fixture(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        f"""
        CREATE TABLE schema_meta(
            key TEXT PRIMARY KEY NOT NULL CHECK(length(key) > 0),
            value TEXT NOT NULL CHECK(length(value) > 0)
        );
        INSERT INTO schema_meta VALUES ('schema_version', '1');
        INSERT INTO schema_meta VALUES ('migration_marker', 'complete');
        INSERT INTO schema_meta VALUES ('schema_identity', 'codex_flow_h2_v1');
        CREATE TABLE runs(
            run_id TEXT PRIMARY KEY NOT NULL CHECK(length(run_id) BETWEEN 1 AND 128),
            created_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{{}}' CHECK(metadata_json = '{{}}')
        );
        CREATE TABLE milestones(
            run_id TEXT NOT NULL,
            milestone_id TEXT NOT NULL,
            current_state TEXT NOT NULL CHECK(current_state IN ({_STATES_SQL})),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{{}}' CHECK(metadata_json = '{{}}'),
            PRIMARY KEY(run_id, milestone_id),
            FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );
        CREATE TABLE dispatches(
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
        CREATE TABLE events(
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
    )
    connection.commit()
    connection.close()


class LedgerTests(unittest.TestCase):
    def make_ledger(self, directory: str) -> Ledger:
        ledger = Ledger(Path(directory) / ".codex-flow" / "workflow.db")
        ledger.create_run("run-1")
        ledger.create_milestone("run-1", "m-1")
        return ledger

    def test_ids_and_dispatch_identity_are_validated(self) -> None:
        for value in ("", "../escape", "with space", "a/b", "' OR 1=1"):
            with self.assertRaises(ValueError):
                DispatchId(value)
        identity = DispatchId.from_parts("run-1", "m-1", "executor", 3)
        self.assertEqual(identity, "run-1/m-1/executor/3")
        self.assertEqual(identity.parts[3], 3)
        with self.assertRaises(ValueError):
            DispatchId.from_parts(7, "m-1", "executor", 1)  # type: ignore[arg-type]
        with TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "workflow.db")
            with self.assertRaises(ValueError):
                ledger.create_run(7)  # type: ignore[arg-type]
            ledger.create_run("r")
            with self.assertRaises(ValueError):
                ledger.create_milestone("r", 7)  # type: ignore[arg-type]
            ledger.create_milestone("r", "m")
            with self.assertRaises(ValueError):
                ledger.claim_dispatch("r", "m", 7, 1)  # type: ignore[arg-type]

    def test_transition_policy_is_immutable_to_callers(self) -> None:
        with self.assertRaises(TypeError):
            ALLOWED_TRANSITIONS[WorkflowState.PLANNED] = frozenset()  # type: ignore[index]
        self.assertIn(WorkflowState.STARTING, ALLOWED_TRANSITIONS[WorkflowState.PLANNED])

    def test_matrix_and_terminal_immutability_are_explicit(self) -> None:
        self.assertEqual(
            set(TERMINAL_STATES),
            {WorkflowState.ACCEPTED, WorkflowState.BLOCKED, WorkflowState.FAILED, WorkflowState.CANCELLED},
        )
        for state, allowed in ALLOWED_TRANSITIONS.items():
            if state in TERMINAL_STATES:
                self.assertEqual(allowed, frozenset())
        with TemporaryDirectory() as directory:
            ledger = self.make_ledger(directory)
            ledger.claim_dispatch("run-1", "m-1", "executor", 1)
            ledger.transition("run-1", "m-1", WorkflowState.RUNNING, expected_state=WorkflowState.STARTING)
            with self.assertRaises(InvalidTransition):
                ledger.transition("run-1", "m-1", WorkflowState.REVIEWING, expected_state=WorkflowState.RUNNING)
            ledger.transition("run-1", "m-1", WorkflowState.FAILED, expected_state=WorkflowState.RUNNING)
            with self.assertRaises(InvalidTransition):
                ledger.transition("run-1", "m-1", WorkflowState.RUNNING, expected_state=WorkflowState.FAILED)

    def test_all_121_state_pairs_execute_through_ledger(self) -> None:
        def prepare(ledger: Ledger, run_id: str, state: WorkflowState) -> None:
            ledger.create_run(run_id)
            ledger.create_milestone(run_id, "m")
            if state is WorkflowState.PLANNED:
                return
            if state in {WorkflowState.BLOCKED, WorkflowState.FAILED, WorkflowState.CANCELLED}:
                ledger.transition(run_id, "m", state, expected_state=WorkflowState.PLANNED)
                return
            ledger.claim_dispatch(run_id, "m", "executor", 1)
            if state is WorkflowState.STARTING:
                return
            ledger.transition(run_id, "m", WorkflowState.RUNNING, expected_state=WorkflowState.STARTING)
            if state is WorkflowState.RUNNING:
                return
            if state is WorkflowState.NEEDS_DECISION:
                ledger.transition(run_id, "m", state, expected_state=WorkflowState.RUNNING)
                return
            ledger.transition(run_id, "m", WorkflowState.COMPLETED, expected_state=WorkflowState.RUNNING)
            if state is WorkflowState.COMPLETED:
                return
            ledger.transition(run_id, "m", WorkflowState.REVIEWING, expected_state=WorkflowState.COMPLETED)
            if state is WorkflowState.REVIEWING:
                return
            if state is WorkflowState.ACCEPTED:
                ledger.transition(run_id, "m", WorkflowState.ACCEPTED, expected_state=WorkflowState.REVIEWING)
                return
            ledger.transition(run_id, "m", WorkflowState.REPAIR_REQUIRED, expected_state=WorkflowState.REVIEWING)
            if state is WorkflowState.REPAIR_REQUIRED:
                return
            raise AssertionError(state)

        with TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "workflow.db")
            pair_index = 0
            for source in WorkflowState:
                for target in WorkflowState:
                    run_id = f"pair-{pair_index}"
                    prepare(ledger, run_id, source)
                    if target in ALLOWED_TRANSITIONS[source]:
                        ledger.transition(run_id, "m", target, expected_state=source)
                    else:
                        with self.assertRaises(InvalidTransition):
                            ledger.transition(run_id, "m", target, expected_state=source)
                    pair_index += 1

    def test_stale_writer_and_idempotent_conflict(self) -> None:
        with TemporaryDirectory() as directory:
            ledger = self.make_ledger(directory)
            first = ledger.claim_dispatch("run-1", "m-1", "executor", 1)
            self.assertEqual(first, ledger.claim_dispatch("run-1", "m-1", "executor", 1))
            with self.assertRaises(DispatchConflict):
                ledger.claim_dispatch("run-1", "m-1", "executor", 2)
            with self.assertRaises(StaleWriter):
                ledger.transition("run-1", "m-1", WorkflowState.RUNNING, expected_state=WorkflowState.PLANNED)

    def test_fault_injection_rolls_back_state_and_event_together(self) -> None:
        with TemporaryDirectory() as directory:

            def fault(stage: str) -> None:
                if stage == "after_state_update":
                    raise RuntimeError(stage)

            ledger = Ledger(Path(directory) / "workflow.db", fault_injector=fault)
            ledger.create_run("r")
            ledger.create_milestone("r", "m")
            with self.assertRaisesRegex(RuntimeError, "after_state_update"):
                ledger.claim_dispatch("r", "m", "executor", 1)
            self.assertEqual(ledger.current_state("r", "m"), WorkflowState.PLANNED)
            self.assertEqual(ledger.events("r", "m"), ())

    def test_fault_after_event_insert_rolls_back_both_state_and_event(self) -> None:
        with TemporaryDirectory() as directory:
            armed = False

            def fault(stage: str) -> None:
                if stage == "after_event_insert" and armed:
                    raise RuntimeError(stage)

            ledger = Ledger(Path(directory) / "workflow.db", fault_injector=fault)
            ledger.create_run("r")
            ledger.create_milestone("r", "m")
            ledger.claim_dispatch("r", "m", "executor", 1)
            armed = True
            with self.assertRaisesRegex(RuntimeError, "after_event_insert"):
                ledger.transition("r", "m", WorkflowState.RUNNING, expected_state=WorkflowState.STARTING)
            self.assertEqual(ledger.current_state("r", "m"), WorkflowState.STARTING)
            self.assertEqual(len(ledger.events("r", "m")), 1)

    def test_reopen_and_recovery_facts(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"
            ledger = Ledger(path)
            ledger.create_run("r")
            ledger.create_milestone("r", "m")
            claim = ledger.claim_dispatch("r", "m", "executor", 1)
            before = ledger.events("r", "m")
            ledger.close()
            ledger.reopen()
            self.assertEqual(ledger.get_dispatch(claim.dispatch_id), claim)
            self.assertEqual(ledger.events("r", "m"), before)
            facts = ledger.recovery_facts("r")
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0].dispatch.dispatch_id, claim.dispatch_id)
            self.assertEqual(ledger.journal_mode, "delete")
            self.assertFalse(hasattr(ledger, "connection"))

    def test_invalid_event_dispatch_combinations_fail_before_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"
            ledger = Ledger(path)
            ledger.create_run("r")
            ledger.create_milestone("r", "m")
            claim = ledger.claim_dispatch("r", "m", "executor", 1)
            before = ledger.snapshot("r")
            invalid = (
                {"event_type": "dispatch_claimed"},
                {"dispatch_id": claim.dispatch_id},
                {"reason": ReasonCode.DISPATCH_CLAIMED},
            )
            for arguments in invalid:
                with self.assertRaises(ValueError):
                    ledger.transition(
                        "r",
                        "m",
                        WorkflowState.RUNNING,
                        expected_state=WorkflowState.STARTING,
                        **arguments,  # type: ignore[arg-type]
                    )
                self.assertEqual(ledger.snapshot("r"), before)
            ledger.reopen()
            self.assertEqual(ledger.snapshot("r"), before)

    def test_reopen_rejects_database_and_ancestor_symlink_swaps(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            db_dir = root / "db"
            db_dir.mkdir()
            path = db_dir / "workflow.db"
            ledger = Ledger(path)
            ledger.close()
            outside = Path(directory) / "outside"
            outside.mkdir()
            path.rename(outside / "workflow.db")
            path.symlink_to(outside / "workflow.db")
            with self.assertRaises(SchemaError):
                ledger.reopen()

        with TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            db_dir = root / "db"
            db_dir.mkdir()
            path = db_dir / "workflow.db"
            ledger = Ledger(path)
            ledger.close()
            outside = Path(directory) / "outside"
            outside.mkdir()
            db_dir.rename(root / "db-real")
            db_dir.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(SchemaError):
                ledger.reopen()

    def test_database_hardlink_substitution_cannot_redirect_writes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "workflow.db"
            original = root / "workflow-original.db"
            attacker = root / "attacker.db"
            Ledger(path).close()
            attacker_ledger = Ledger(attacker)
            attacker_ledger.create_run("attacker")
            attacker_ledger.close()
            swapped = False

            def swap(stage: str) -> None:
                nonlocal swapped
                if stage == "before_connect" and not swapped:
                    swapped = True
                    path.rename(original)
                    os.link(attacker, path)

            ledger = Ledger(path, fault_injector=swap)
            ledger.create_run("pinned")
            self.assertEqual(ledger.get_run("pinned").run_id, "pinned")
            ledger.close()
            original_connection = sqlite3.connect(original)
            self.assertEqual(original_connection.execute("SELECT run_id FROM runs").fetchall(), [("pinned",)])
            original_connection.close()
            attacker_connection = sqlite3.connect(attacker)
            self.assertEqual(attacker_connection.execute("SELECT run_id FROM runs").fetchall(), [("attacker",)])
            attacker_connection.close()
            self.assertFalse(any(root.glob("*-journal")))
            path.unlink()
            original.rename(path)
            reopened = Ledger(path)
            self.assertEqual(reopened.get_run("pinned").run_id, "pinned")

    def test_reason_categories_do_not_collapse(self) -> None:
        self.assertEqual(PreIdentityTransportFailure().code, ReasonCode.TRANSPORT_FAILURE)
        self.assertEqual(PostIdentityExecutionFailure().code, ReasonCode.EXECUTION_FAILURE)
        self.assertEqual(ReviewRejected().code, ReasonCode.REVIEW_REJECTED)

    def test_sensitive_fields_and_h1_exception_details_never_persist(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"
            ledger = Ledger(path)
            with self.assertRaises(ValueError):
                ledger.create_run("r", metadata={"api_key": "do-not-store"})
            ledger.create_run("r")
            with self.assertRaises(ValueError):
                ledger.create_milestone("r", "m", metadata={"sdk_response": "raw response"})
            ledger.create_milestone("r", "m")
            ledger.claim_dispatch("r", "m", "executor", 1)
            with self.assertRaises(ValueError):
                ledger.transition(
                    "r", "m", WorkflowState.RUNNING, expected_state=WorkflowState.STARTING, data={"body": "prompt body"}
                )
            event = ledger.transition(
                "r",
                "m",
                WorkflowState.RUNNING,
                expected_state=WorkflowState.STARTING,
                reason=TerminalFailureAfterIdentity("private SDK exception details"),
            )
            self.assertEqual(event.reason.code, ReasonCode.EXECUTION_FAILURE)
            self.assertIsNone(event.reason.detail)
            self.assertNotIn(b"private SDK exception details", path.read_bytes())

    def test_schema_reopen_migration_and_rejection(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"
            _make_v1_fixture(path)
            connection = sqlite3.connect(path)
            connection.execute("INSERT INTO runs VALUES ('r', 't0', '{}')")
            connection.execute("INSERT INTO milestones VALUES ('r', 'm', 'STARTING', 't0', 't1', '{}')")
            connection.execute("INSERT INTO dispatches VALUES ('r/m/executor/1', 'r', 'm', 'executor', 1, 't1')")
            connection.execute(
                "INSERT INTO events VALUES "
                "('r/m/1', 'r', 'm', 1, 'PLANNED', 'STARTING', 'dispatch_claimed', "
                "'dispatch_claimed', NULL, 'r/m/executor/1', NULL, 't1')"
            )
            connection.commit()
            connection.close()

            def fault(stage: str) -> None:
                if stage == "after_migration":
                    raise RuntimeError(stage)

            with self.assertRaisesRegex(RuntimeError, "after_migration"):
                Ledger(path, fault_injector=fault)
            connection = sqlite3.connect(path)
            self.assertEqual(
                connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0], "1"
            )
            self.assertNotIn("closed_at", {row[1] for row in connection.execute("PRAGMA table_info(runs)")})
            connection.close()
            migrated = Ledger(path)
            self.assertEqual(int(migrated.schema_version), 2)
            self.assertIn("closed_at", migrated.schema_columns("runs"))
            self.assertEqual(migrated.current_state("r", "m"), WorkflowState.STARTING)
            self.assertEqual(migrated.get_dispatch("r/m/executor/1").role, "executor")
            self.assertEqual(len(migrated.events("r", "m")), 1)
            migrated_identity = migrated.schema_identity
            migrated.close()
            fresh = Ledger(Path(directory) / "fresh.db")
            self.assertEqual(migrated_identity, fresh.schema_identity)
            fresh.close()
            connection = sqlite3.connect(path)
            connection.execute("UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'")
            connection.commit()
            connection.close()
            with self.assertRaises(UnsupportedSchemaVersion):
                Ledger(path)

    def test_counterfeit_constraintless_v2_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "counterfeit.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE schema_meta(key TEXT, value TEXT);
                INSERT INTO schema_meta VALUES ('schema_version', '2');
                INSERT INTO schema_meta VALUES ('migration_marker', 'complete');
                INSERT INTO schema_meta VALUES ('schema_identity', 'codex_flow_h2_v2');
                CREATE TABLE runs(run_id TEXT, created_at TEXT, closed_at TEXT, metadata_json TEXT);
                CREATE TABLE milestones(run_id TEXT, milestone_id TEXT, current_state TEXT, created_at TEXT, updated_at TEXT, metadata_json TEXT);
                CREATE TABLE dispatches(dispatch_id TEXT, run_id TEXT, milestone_id TEXT, role TEXT, generation INTEGER, claimed_at TEXT);
                CREATE TABLE events(event_id TEXT, run_id TEXT, milestone_id TEXT, sequence INTEGER, from_state TEXT, to_state TEXT, event_type TEXT, reason_code TEXT, reason_detail TEXT, dispatch_id TEXT, data_json TEXT, occurred_at TEXT);
                """
            )
            connection.commit()
            connection.close()
            with self.assertRaises(CorruptSchemaError):
                Ledger(path)

    def test_comment_only_counterfeit_constraint_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "comment-counterfeit.db"
            connection = sqlite3.connect(path)
            for table, ddl in _V2_TABLE_DDL.items():
                if table == "runs":
                    ddl = ddl.replace(
                        "CHECK(length(run_id) BETWEEN 1 AND 128)",
                        "/* CHECK(length(run_id) BETWEEN 1 AND 128) */",
                    )
                connection.execute(ddl)
            connection.executemany(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                (("schema_version", "2"), ("migration_marker", "complete"), ("schema_identity", _SCHEMA_IDENTITY)),
            )
            connection.commit()
            connection.close()
            with self.assertRaises(CorruptSchemaError):
                Ledger(path)

    def test_reopen_rejects_discontinuous_and_noncausal_histories(self) -> None:
        for corruption in ("discontinuous", "noncausal"):
            with self.subTest(corruption=corruption), TemporaryDirectory() as directory:
                path = Path(directory) / "workflow.db"
                ledger = Ledger(path)
                ledger.create_run("r")
                ledger.create_milestone("r", "m")
                ledger.claim_dispatch("r", "m", "executor", 1)
                ledger.transition("r", "m", WorkflowState.RUNNING, expected_state=WorkflowState.STARTING)
                ledger.close()
                connection = sqlite3.connect(path)
                if corruption == "discontinuous":
                    connection.execute("UPDATE events SET sequence = 3, event_id = 'r/m/3' WHERE sequence = 2")
                else:
                    connection.execute(
                        "UPDATE events SET from_state = 'PLANNED', to_state = 'FAILED' WHERE sequence = 2"
                    )
                    connection.execute(
                        "UPDATE milestones SET current_state = 'FAILED' WHERE run_id = 'r' AND milestone_id = 'm'"
                    )
                connection.commit()
                connection.close()
                with self.assertRaises(CorruptSchemaError):
                    Ledger(path)

    def test_reopen_rejects_orphaned_or_duplicate_dispatch_authority(self) -> None:
        for corruption in ("orphaned", "duplicate"):
            with self.subTest(corruption=corruption), TemporaryDirectory() as directory:
                path = Path(directory) / "workflow.db"
                ledger = Ledger(path)
                ledger.create_run("r")
                ledger.create_milestone("r", "m")
                claim = ledger.claim_dispatch("r", "m", "executor", 1)
                ledger.close()
                connection = sqlite3.connect(path)
                if corruption == "orphaned":
                    connection.execute("DELETE FROM events WHERE dispatch_id = ?", (str(claim.dispatch_id),))
                    connection.execute(
                        "UPDATE milestones SET current_state = 'PLANNED' WHERE run_id = 'r' AND milestone_id = 'm'"
                    )
                else:
                    connection.execute(
                        "INSERT INTO events SELECT 'r/m/2', run_id, milestone_id, 2, from_state, to_state, "
                        "event_type, reason_code, reason_detail, dispatch_id, data_json, occurred_at "
                        "FROM events WHERE sequence = 1"
                    )
                connection.commit()
                connection.close()
                with self.assertRaises(CorruptSchemaError):
                    Ledger(path)

    def test_linux_directory_flags_are_direct_attributes(self) -> None:
        for module in (ledger_module, artifacts_module):
            source = inspect.getsource(module)
            self.assertIn("os.O_DIRECTORY", source)
            self.assertNotIn('getattr(os, "O_DIRECTORY"', source)

    def test_concurrent_processes_establish_one_owner(self) -> None:
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "workflow.db")
            queue: Queue[str] = Queue()
            processes = [Process(target=_claim_worker, args=(path, queue)) for _ in range(4)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
            results = [queue.get(timeout=2) for _ in processes]
            self.assertTrue(all(result == "ok:race/m/executor/1" for result in results), results)
            ledger = Ledger(path)
            self.assertEqual(len(ledger.events("race", "m")), 1)

    def test_concurrent_conflicting_generations_have_one_owner(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"
            ledger = Ledger(path)
            ledger.create_run("race")
            ledger.create_milestone("race", "m")
            queue: Queue[str] = Queue()
            processes = [
                Process(target=_claim_generation_worker, args=(str(path), generation, queue)) for generation in (1, 2)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
            results = [queue.get(timeout=2) for _ in processes]
            self.assertEqual(sum(result.startswith("ok:") for result in results), 1, results)
            self.assertEqual(sum(result == "error:DispatchConflict" for result in results), 1, results)
            self.assertEqual(len(ledger.events("race", "m")), 1)


class ArtifactTests(unittest.TestCase):
    def test_rebuild_is_byte_stable_and_path_safe(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = Ledger(root / ".codex-flow" / "workflow.db")
            ledger.create_run("r")
            ledger.create_milestone("r", "m")
            ledger.claim_dispatch("r", "m", "executor", 1)
            paths = rebuild_projections(ledger, root, "r")
            first = paths.events.read_bytes()
            rebuild_projections(ledger, root, "r")
            self.assertEqual(first, paths.events.read_bytes())
            outside = root / "outside"
            outside.mkdir()
            events_path = root / ".codex-flow" / "runs" / "r" / "events.jsonl"
            events_path.unlink()
            events_path.symlink_to(outside / "bad.json")
            with self.assertRaises(UnsafeArtifactPath):
                ArtifactProjector(root).rebuild(ledger, "r")

    def test_projection_failure_does_not_change_ledger(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = Ledger(root / ".codex-flow" / "workflow.db")
            ledger.create_run("r")
            ledger.create_milestone("r", "m")
            before = ledger.snapshot("r")

            def fail(stage: str, _target: Path) -> None:
                if stage == "projection_0":
                    raise OSError("injected")

            with self.assertRaises(ArtifactError):
                ArtifactProjector(root, fault_injector=fail).rebuild(ledger, "r")
            self.assertEqual(ledger.snapshot("r"), before)

    def test_projection_ancestor_swap_stays_on_pinned_directory(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            ledger = Ledger(root / ".codex-flow" / "workflow.db")
            ledger.create_run("r")
            ledger.create_milestone("r", "m")
            outside = Path(directory) / "outside"
            outside.mkdir()
            swapped = False

            def swap(stage: str, _target: Path) -> None:
                nonlocal swapped
                if stage == "projection_0" and not swapped:
                    swapped = True
                    root.rename(Path(directory) / "repo-real")
                    root.symlink_to(outside, target_is_directory=True)

            ArtifactProjector(root, fault_injector=swap).rebuild(ledger, "r")
            self.assertTrue((Path(directory) / "repo-real" / ".codex-flow" / "runs" / "r" / "run.json").is_file())
            self.assertFalse((outside / ".codex-flow" / "runs" / "r" / "run.json").exists())
