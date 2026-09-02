from __future__ import annotations

import inspect
import os
import sqlite3
import unittest
from multiprocessing import Process, Queue
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread

import codex_flow
import codex_flow.artifacts as artifacts_module
import codex_flow.domain as domain_module
import codex_flow.ledger as ledger_module
from codex_flow.artifacts import ArtifactError, ArtifactProjector, UnsafeArtifactPath, rebuild_projections
from codex_flow.domain import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    DispatchId,
    PostIdentityExecutionFailure,
    PreIdentityTransportFailure,
    ReasonCode,
    RecoveryActionKind,
    RetryBudgetChange,
    ReviewRejected,
    SchemaVersion,
    TerminalFailureAfterIdentity,
    WorkflowState,
)
from codex_flow.ledger import (
    _EVENT_TYPES_SQL,
    _REASON_CODES_SQL,
    _SCHEMA_IDENTITIES,
    _SCHEMA_IDENTITY,
    _STATES_SQL,
    _V2_TABLE_DDL,
    _V16_TABLE_DDL,
    CURRENT_SCHEMA_VERSION,
    CorruptSchemaError,
    DispatchConflict,
    InvalidTransition,
    Ledger,
    RecordNotFound,
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


def _late_link_worker(path: str, exported: str, commands: Queue[str], results: Queue[str]) -> None:
    try:
        if commands.get(timeout=5) != "link":
            raise RuntimeError("unexpected late-link command")
        os.link(path, exported)
        results.put("linked")
    except Exception as exc:  # pragma: no cover - assertion captures worker result
        results.put(f"error:{type(exc).__name__}:{exc}")


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


def _noninternal_inventory(path: Path) -> list[tuple[str, str, str, str | None]]:
    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE substr(name, 1, 7) != 'sqlite_' ORDER BY type, name"
    ).fetchall()
    connection.close()
    return [
        (object_type, name, table, ledger_module._canonical_ddl(sql) if sql is not None else None)
        for object_type, name, table, sql in rows
    ]


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
        original = domain_module.ALLOWED_TRANSITIONS
        try:
            domain_module.ALLOWED_TRANSITIONS = {  # type: ignore[assignment]
                state: frozenset(WorkflowState) for state in WorkflowState
            }
            with TemporaryDirectory() as directory:
                ledger = self.make_ledger(directory)
                ledger.claim_dispatch("run-1", "m-1", "executor", 1)
                ledger.transition("run-1", "m-1", WorkflowState.RUNNING, expected_state=WorkflowState.STARTING)
                ledger.transition("run-1", "m-1", WorkflowState.COMPLETED, expected_state=WorkflowState.RUNNING)
                ledger.transition("run-1", "m-1", WorkflowState.REVIEWING, expected_state=WorkflowState.COMPLETED)
                ledger.transition("run-1", "m-1", WorkflowState.ACCEPTED, expected_state=WorkflowState.REVIEWING)
                with self.assertRaises(InvalidTransition):
                    ledger.transition("run-1", "m-1", WorkflowState.RUNNING, expected_state=WorkflowState.ACCEPTED)
        finally:
            domain_module.ALLOWED_TRANSITIONS = original
        self.assertFalse(hasattr(domain_module, "StateMachine"))

    def test_root_exports_include_semantic_public_api(self) -> None:
        self.assertEqual(
            codex_flow.__all__,
            [
                "Capability",
                "CapabilityObservation",
                "CapabilityStatus",
                "CodexFlowError",
                "ControllerActionKind",
                "ControllerClaimantKind",
                "ControllerDecisionClient",
                "ControllerDecisionId",
                "ControllerDecisionState",
                "ControllerGenerationState",
                "PilotError",
                "ReasoningEffort",
                "ReviewLifecycleResult",
                "ReviewWorkflow",
                "Sandbox",
                "Schema",
                "SkillInput",
                "TerminalFailureAfterIdentity",
                "ThreadIdentity",
                "TransportFailureBeforeIdentity",
                "TurnObservation",
                "build_visible_worker_capsule",
                "run_controller_recovery_sentinel",
                "run_multi_authority_review_pilot",
                "run_production_pilots",
                "run_review_pilot",
                "run_sdk_compatibility_sentinel",
                "run_workflow_control_pilot",
                "write_controller_recovery_evidence",
                "write_production_evidence",
                "write_review_artifact",
                "write_sdk_compatibility_evidence",
                "write_workflow_control_evidence",
            ],
        )
        self.assertFalse(hasattr(codex_flow, "Ledger"))

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
                    if source is WorkflowState.PLANNED and target is WorkflowState.STARTING:
                        claim = ledger.claim_dispatch(run_id, "m", "executor", 1)
                        self.assertEqual(claim.run_id, run_id)
                        self.assertEqual(ledger.current_state(run_id, "m"), target)
                    elif target in ALLOWED_TRANSITIONS[source]:
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

    def test_recovery_facts_hold_one_snapshot_during_transition(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"
            setup = Ledger(path)
            setup.create_run("r")
            setup.create_milestone("r", "m")
            setup.claim_dispatch("r", "m", "executor", 1)
            setup.close()

            writer_at_commit = Event()
            allow_commit = Event()
            writer_done = Event()
            writer_errors: list[BaseException] = []

            def pause_writer(stage: str) -> None:
                if stage == "before_commit":
                    writer_at_commit.set()
                    if not allow_commit.wait(5):
                        raise RuntimeError("reader did not release the concurrent transition")

            writer = Ledger(path, fault_injector=pause_writer)

            def transition() -> None:
                try:
                    writer.transition("r", "m", WorkflowState.RUNNING, expected_state=WorkflowState.STARTING)
                except BaseException as exc:  # pragma: no cover - surfaced below
                    writer_errors.append(exc)
                finally:
                    writer_done.set()

            transition_thread = Thread(target=transition)
            started = False

            def interleave_transition(stage: str) -> None:
                nonlocal started
                if stage != "after_recovery_dispatch_rows_read" or started:
                    return
                started = True
                transition_thread.start()
                self.assertTrue(writer_at_commit.wait(2), "writer did not reach commit")
                allow_commit.set()
                self.assertFalse(
                    writer_done.wait(0.2),
                    "transition committed between recovery authority queries",
                )

            reader = Ledger(path, fault_injector=interleave_transition)
            facts = reader.recovery_facts("r")
            transition_thread.join(5)
            self.assertFalse(transition_thread.is_alive())
            self.assertEqual(writer_errors, [])
            self.assertTrue(started)
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0].state, WorkflowState.STARTING)
            self.assertEqual(facts[0].last_event.from_state, WorkflowState.PLANNED)
            self.assertEqual(facts[0].last_event.to_state, WorkflowState.STARTING)
            self.assertEqual(reader.current_state("r", "m"), WorkflowState.RUNNING)
            writer.close()
            reader.close()

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

    def test_transition_requires_explicit_stale_writer_token(self) -> None:
        with TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "workflow.db")
            ledger.create_run("r")
            ledger.create_milestone("r", "m")
            before = ledger.snapshot("r")
            with self.assertRaises(TypeError):
                ledger.transition("r", "m", WorkflowState.BLOCKED)  # type: ignore[call-arg]
            with self.assertRaises(ValueError):
                ledger.transition(
                    "r",
                    "m",
                    WorkflowState.BLOCKED,
                    expected_state=None,  # type: ignore[arg-type]
                )
            self.assertEqual(ledger.snapshot("r"), before)

    def test_starting_and_execution_history_require_dispatch_authority(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"
            ledger = Ledger(path)
            ledger.create_run("r")
            ledger.create_milestone("r", "m")
            before = ledger.snapshot("r")
            with self.assertRaisesRegex(InvalidTransition, "claim_dispatch"):
                ledger.transition("r", "m", WorkflowState.STARTING, expected_state=WorkflowState.PLANNED)
            self.assertEqual(ledger.snapshot("r"), before)
            ledger.close()

            connection = sqlite3.connect(path)
            transitions = (
                (1, "PLANNED", "STARTING"),
                (2, "STARTING", "RUNNING"),
                (3, "RUNNING", "COMPLETED"),
                (4, "COMPLETED", "REVIEWING"),
                (5, "REVIEWING", "ACCEPTED"),
            )
            connection.executemany(
                "INSERT INTO events(event_id, run_id, milestone_id, sequence, from_state, to_state, "
                "event_type, occurred_at) VALUES ('r/m/' || ?, 'r', 'm', ?, ?, ?, 'state_transition', 't')",
                ((sequence, sequence, source, target) for sequence, source, target in transitions),
            )
            connection.execute(
                "UPDATE milestones SET current_state = 'ACCEPTED' WHERE run_id = 'r' AND milestone_id = 'm'"
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(CorruptSchemaError, "requires a dispatch claim"):
                Ledger(path)

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

    def test_database_hardlinks_and_reopen_substitution_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "workflow.db"
            exported = root / "exported.db"
            ledger = Ledger(path)
            ledger.create_run("original")
            ledger.close()
            os.link(path, exported)
            with self.assertRaises(SchemaError):
                ledger.reopen()
            with self.assertRaises(SchemaError):
                Ledger(path)
            exported.unlink()
            ledger.reopen()
            self.assertEqual(ledger.get_run("original").run_id, "original")

            ledger.close()
            missing_original = root / "workflow-missing.db"
            path.rename(missing_original)
            with self.assertRaises(SchemaError):
                ledger.reopen()
            self.assertFalse(path.exists())
            missing_original.rename(path)
            ledger.reopen()

            replacement = root / "replacement.db"
            replacement_ledger = Ledger(replacement)
            replacement_ledger.create_run("replacement")
            replacement_ledger.close()
            ledger.close()
            original = root / "workflow-original.db"
            path.rename(original)
            replacement.rename(path)
            with self.assertRaises(SchemaError):
                ledger.reopen()
            self.assertEqual(Ledger(path).get_run("replacement").run_id, "replacement")

    def test_mid_transaction_hardlink_rolls_back(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "workflow.db"
            exported = root / "exported.db"
            armed = False

            def export_before_commit(stage: str) -> None:
                nonlocal armed
                if stage == "before_commit" and armed:
                    armed = False
                    os.link(path, exported)

            ledger = Ledger(path, fault_injector=export_before_commit)
            armed = True
            with self.assertRaises(SchemaError):
                ledger.create_run("escaped")
            exported.unlink()
            with self.assertRaises(RecordNotFound):
                ledger.get_run("escaped")
            ledger.reopen()
            ledger.create_run("safe")
            self.assertEqual(ledger.get_run("safe").run_id, "safe")

    def test_external_late_hardlink_aborts_at_commit_boundary(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "workflow.db"
            exported = root / "exported.db"
            armed = False
            commands: Queue[str] = Queue()
            results: Queue[str] = Queue()

            def link_at_commit(stage: str) -> None:
                if stage == "at_commit_boundary" and armed:
                    commands.put("link")
                    result = results.get(timeout=5)
                    if result != "linked":
                        raise RuntimeError(result)

            ledger = Ledger(path, fault_injector=link_at_commit)
            process = Process(
                target=_late_link_worker,
                args=(str(path), str(exported), commands, results),
            )
            process.start()
            armed = True
            with self.assertRaisesRegex(SchemaError, "link count changed"):
                ledger.create_run("escaped")
            armed = False
            process.join(5)
            self.assertFalse(process.is_alive())
            exported_connection = sqlite3.connect(exported)
            self.assertEqual(
                exported_connection.execute("SELECT run_id FROM runs WHERE run_id = 'escaped'").fetchall(),
                [],
            )
            exported_connection.close()
            exported.unlink()
            ledger.reopen()
            with self.assertRaises(RecordNotFound):
                ledger.get_run("escaped")
            self.assertEqual(ledger.create_run("safe").run_id, "safe")

    def test_failed_first_open_cleans_only_its_empty_inode(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"

            def fail_before_connect(stage: str) -> None:
                if stage == "before_connect":
                    raise RuntimeError(stage)

            with self.assertRaisesRegex(RuntimeError, "before_connect"):
                Ledger(path, fault_injector=fail_before_connect)
            self.assertFalse(path.exists())

            path.touch()
            with self.assertRaisesRegex(RuntimeError, "before_connect"):
                Ledger(path, fault_injector=fail_before_connect)
            self.assertTrue(path.exists())

    def test_failed_first_open_never_deletes_a_substitution(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "workflow.db"
            created = root / "created.db"

            def substitute(stage: str) -> None:
                if stage == "before_connect":
                    path.rename(created)
                    path.write_bytes(b"replacement")

            with self.assertRaises(SchemaError):
                Ledger(path, fault_injector=substitute)
            self.assertTrue(created.exists())
            self.assertEqual(path.read_bytes(), b"replacement")

    def test_failed_initial_schema_transaction_removes_uncommitted_inode(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"

            def fail_commit(stage: str) -> None:
                if stage == "before_commit":
                    raise RuntimeError(stage)

            with self.assertRaisesRegex(RuntimeError, "before_commit"):
                Ledger(path, fault_injector=fail_commit)
            self.assertFalse(path.exists())

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
            self.assertEqual(migrated.schema_version, CURRENT_SCHEMA_VERSION)
            self.assertIn("closed_at", migrated.schema_columns("runs"))
            self.assertEqual(migrated.current_state("r", "m"), WorkflowState.STARTING)
            self.assertEqual(migrated.get_dispatch("r/m/executor/1").role, "executor")
            self.assertEqual(len(migrated.events("r", "m")), 1)
            migrated_identity = migrated.schema_identity
            migrated.close()
            fresh = Ledger(Path(directory) / "fresh.db")
            self.assertEqual(migrated_identity, fresh.schema_identity)
            fresh.close()
            self.assertEqual(_noninternal_inventory(path), _noninternal_inventory(Path(directory) / "fresh.db"))
            connection = sqlite3.connect(path)
            connection.execute("UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'")
            connection.commit()
            connection.close()
            with self.assertRaises(UnsupportedSchemaVersion):
                Ledger(path)

    def test_schema_v16_to_v17_migration_is_atomic_and_preserves_recovery_facts(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"
            ledger = Ledger(path)
            ledger.create_run("run")
            ledger.create_milestone("run", "milestone")
            ledger.claim_dispatch("run", "milestone", "executor", 1)
            ledger.enqueue_dispatch(
                "run/milestone/executor/1",
                backend="sdk_headless",
                capsule_json='{"model":"test","prompt":"bounded"}',
                route_json="{}",
                workspace_path=Path(directory),
                result_contract_sha256="a" * 64,
            )
            ledger.mark_human_attention_required("run/milestone/executor/1", reason="operator review")
            ledger.apply_recovery_action(
                "run/milestone/executor/1",
                action_id="preserved-budget-action",
                expected_revision=int(ledger.retry_policy("run/milestone/executor/1")["revision"]),
                action_kind=RecoveryActionKind.BUDGET_CHANGE,
                reason="preserve recovery receipt",
                requested_budget=RetryBudgetChange(5, 1, 2, 1),
            )
            ledger.close()

            connection = sqlite3.connect(path)
            connection.execute("ALTER TABLE retry_policies RENAME TO retry_policies_v17")
            connection.execute(_V16_TABLE_DDL["retry_policies"])
            connection.execute(
                "INSERT INTO retry_policies(dispatch_id, revision, policy_version, pre_identity_budget, "
                "invalid_chain_budget, schema_envelope_budget, post_identity_loss_budget, pre_identity_used, "
                "invalid_chain_used, schema_envelope_used, post_identity_loss_used, last_failure, strategy, "
                "next_eligible_at, prior_thread_id, prior_turn_id, human_attention_reason, created_at, updated_at) "
                "SELECT dispatch_id, revision, policy_version, pre_identity_budget, invalid_chain_budget, "
                "schema_envelope_budget, post_identity_loss_budget, pre_identity_used, invalid_chain_used, "
                "schema_envelope_used, post_identity_loss_used, last_failure, strategy, next_eligible_at, "
                "prior_thread_id, prior_turn_id, human_attention_reason, created_at, updated_at "
                "FROM retry_policies_v17"
            )
            connection.execute("DROP TABLE retry_policies_v17")
            connection.execute("UPDATE schema_meta SET value = '16' WHERE key = 'schema_version'")
            connection.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(16)],),
            )
            connection.commit()
            connection.close()

            def fault(stage: str) -> None:
                if stage == "after_copy_retry_policies":
                    raise RuntimeError(stage)

            with self.assertRaisesRegex(RuntimeError, "after_copy_retry_policies"):
                Ledger(path, fault_injector=fault)
            connection = sqlite3.connect(path)
            self.assertEqual(
                connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0], "16"
            )
            self.assertNotIn(
                "provider_transient_budget",
                {row[1] for row in connection.execute("PRAGMA table_info(retry_policies)")},
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM recovery_controls").fetchone()[0], 1)
            connection.close()

            migrated = Ledger(path)
            self.assertEqual(migrated.schema_version, CURRENT_SCHEMA_VERSION)
            self.assertEqual(migrated.retry_policy("run/milestone/executor/1")["provider_transient_budget"], 3)
            self.assertEqual(migrated.retry_policy("run/milestone/executor/1")["provider_transient_used"], 0)
            self.assertEqual(migrated._db().execute("SELECT COUNT(*) FROM recovery_controls").fetchone()[0], 1)
            migrated.close()

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

    def test_extra_sqlite_master_objects_are_rejected(self) -> None:
        extras = {
            "trigger": (
                "CREATE TRIGGER extra AFTER INSERT ON runs BEGIN DELETE FROM runs WHERE run_id = NEW.run_id; END"
            ),
            "view": "CREATE VIEW extra AS SELECT run_id FROM runs",
            "index": "CREATE INDEX extra ON runs(created_at)",
            "sqlite-lookalike": "CREATE INDEX sqliteXextra ON runs(created_at)",
        }
        for object_type, statement in extras.items():
            with self.subTest(object_type=object_type), TemporaryDirectory() as directory:
                path = Path(directory) / "workflow.db"
                ledger = Ledger(path)
                ledger.close()
                connection = sqlite3.connect(path)
                connection.execute(statement)
                connection.commit()
                connection.close()
                with self.assertRaises(CorruptSchemaError):
                    ledger.reopen()

    def test_trigger_deleted_write_is_detected_before_success(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"
            armed = False
            ledger: Ledger

            def add_trigger(stage: str) -> None:
                if stage == "after_authority_validation" and armed:
                    ledger._db().execute(
                        "CREATE TEMP TRIGGER erase_run AFTER INSERT ON runs "
                        "BEGIN DELETE FROM runs WHERE run_id = NEW.run_id; END"
                    )

            ledger = Ledger(path, fault_injector=add_trigger)
            armed = True
            with self.assertRaisesRegex(CorruptSchemaError, "did not leave its durable row"):
                ledger.create_run("ghost")
            armed = False
            with self.assertRaises(RecordNotFound):
                ledger.get_run("ghost")
            self.assertEqual(ledger.create_run("good").run_id, "good")

    def test_temporary_metadata_trigger_rolls_back_and_reopens(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"
            armed = False
            ledger: Ledger

            def add_trigger(stage: str) -> None:
                if stage == "after_authority_validation" and armed:
                    ledger._db().execute(
                        "CREATE TEMP TRIGGER corrupt_version AFTER INSERT ON runs "
                        "BEGIN UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'; END"
                    )

            ledger = Ledger(path, fault_injector=add_trigger)
            armed = True
            with self.assertRaises(CorruptSchemaError):
                ledger.create_run("ghost")
            armed = False
            self.assertEqual(ledger.schema_version, CURRENT_SCHEMA_VERSION)
            self.assertEqual(
                ledger._db()
                .execute("SELECT COUNT(*) FROM sqlite_temp_master WHERE substr(name, 1, 7) != 'sqlite_'")
                .fetchone()[0],
                0,
            )
            ledger.reopen()
            self.assertEqual(ledger.schema_version, CURRENT_SCHEMA_VERSION)
            with self.assertRaises(RecordNotFound):
                ledger.get_run("ghost")
            self.assertEqual(ledger.create_run("safe").run_id, "safe")

    def test_temporary_schema_object_alone_rolls_back_write(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"
            armed = False
            ledger: Ledger

            def add_view(stage: str) -> None:
                if stage == "after_authority_validation" and armed:
                    ledger._db().execute("CREATE TEMP VIEW extra AS SELECT run_id FROM runs")

            ledger = Ledger(path, fault_injector=add_view)
            armed = True
            with self.assertRaisesRegex(CorruptSchemaError, "temporary schema objects"):
                ledger.create_run("ghost")
            armed = False
            with self.assertRaises(RecordNotFound):
                ledger.get_run("ghost")
            self.assertEqual(
                ledger._db()
                .execute("SELECT COUNT(*) FROM sqlite_temp_master WHERE substr(name, 1, 7) != 'sqlite_'")
                .fetchone()[0],
                0,
            )
            ledger.reopen()
            self.assertEqual(ledger.create_run("safe").run_id, "safe")

    def test_trigger_added_after_open_blocks_the_next_mutator(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"
            ledger = Ledger(path)
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TRIGGER erase_run AFTER INSERT ON runs BEGIN DELETE FROM runs WHERE run_id = NEW.run_id; END"
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(CorruptSchemaError, "sqlite_master inventory"):
                ledger.create_run("ghost")
            connection = sqlite3.connect(path)
            self.assertEqual(connection.execute("SELECT run_id FROM runs").fetchall(), [])
            connection.close()

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

    def test_open_validation_keeps_one_snapshot_during_concurrent_claim(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"
            setup = Ledger(path)
            setup.create_run("race")
            setup.create_milestone("race", "m")
            setup.close()

            writer_at_commit = Event()
            allow_commit = Event()
            writer_done = Event()
            writer_errors: list[BaseException] = []

            def pause_writer(stage: str) -> None:
                if stage == "before_commit":
                    writer_at_commit.set()
                    if not allow_commit.wait(5):
                        raise RuntimeError("reader did not release the concurrent writer")

            writer = Ledger(path, fault_injector=pause_writer)

            def claim() -> None:
                try:
                    writer.claim_dispatch("race", "m", "executor", 1)
                except BaseException as exc:  # pragma: no cover - surfaced below
                    writer_errors.append(exc)
                finally:
                    writer_done.set()

            claim_thread = Thread(target=claim)
            started = False

            def interleave_claim(stage: str) -> None:
                nonlocal started
                if stage != "after_dispatch_rows_read" or started:
                    return
                started = True
                claim_thread.start()
                self.assertTrue(writer_at_commit.wait(2), "writer did not reach commit")
                allow_commit.set()
                self.assertFalse(
                    writer_done.wait(0.2),
                    "writer committed between authority queries instead of waiting for the read snapshot",
                )

            opener = Ledger(path, fault_injector=interleave_claim)
            claim_thread.join(5)
            self.assertFalse(claim_thread.is_alive())
            self.assertEqual(writer_errors, [])
            self.assertTrue(started)
            self.assertEqual(opener.current_state("race", "m"), WorkflowState.STARTING)
            self.assertEqual(opener.get_dispatch("race/m/executor/1").role, "executor")
            writer.close()
            opener.close()

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
