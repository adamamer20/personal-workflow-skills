from __future__ import annotations

import sqlite3
import unittest
from multiprocessing import Process, Queue
from pathlib import Path
from tempfile import TemporaryDirectory

from codex_flow.artifacts import ArtifactError, ArtifactProjector, UnsafeArtifactPath, rebuild_projections
from codex_flow.domain import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    DispatchId,
    PostIdentityExecutionFailure,
    PreIdentityTransportFailure,
    ReasonCode,
    ReviewRejected,
    WorkflowState,
)
from codex_flow.ledger import (
    DispatchConflict,
    InvalidTransition,
    Ledger,
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


class LedgerTests(unittest.TestCase):
    def make_ledger(self, directory: str) -> Ledger:
        ledger = Ledger(Path(directory) / ".codex-flow" / "workflow.db")
        ledger.create_run("run-1", metadata={"purpose": "test's value"})
        ledger.create_milestone("run-1", "m-1", metadata={"label": "H2"})
        return ledger

    def test_ids_and_dispatch_identity_are_validated(self) -> None:
        for value in ("", "../escape", "with space", "a/b", "' OR 1=1"):
            with self.assertRaises(ValueError):
                DispatchId(value)
        identity = DispatchId.from_parts("run-1", "m-1", "executor", 3)
        self.assertEqual(identity, "run-1/m-1/executor/3")
        self.assertEqual(identity.parts[3], 3)

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
            ledger.transition("run-1", "m-1", WorkflowState.RUNNING, expected=WorkflowState.STARTING)
            with self.assertRaises(InvalidTransition):
                ledger.transition("run-1", "m-1", WorkflowState.REVIEWING, expected=WorkflowState.RUNNING)
            ledger.transition("run-1", "m-1", WorkflowState.FAILED, expected=WorkflowState.RUNNING)
            with self.assertRaises(InvalidTransition):
                ledger.transition("run-1", "m-1", WorkflowState.RUNNING, expected=WorkflowState.FAILED)

    def test_stale_writer_and_idempotent_conflict(self) -> None:
        with TemporaryDirectory() as directory:
            ledger = self.make_ledger(directory)
            first = ledger.claim_dispatch("run-1", "m-1", "executor", 1)
            self.assertEqual(first, ledger.claim_dispatch("run-1", "m-1", "executor", 1))
            with self.assertRaises(DispatchConflict):
                ledger.claim_dispatch("run-1", "m-1", "executor", 2)
            with self.assertRaises(StaleWriter):
                ledger.transition("run-1", "m-1", WorkflowState.RUNNING, expected=WorkflowState.PLANNED)

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
            self.assertEqual(ledger.current_state("m", run_id="r"), WorkflowState.PLANNED)
            self.assertEqual(ledger.events("m", run_id="r"), ())

    def test_reopen_and_recovery_facts(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"
            ledger = Ledger(path)
            ledger.create_run("r")
            ledger.create_milestone("r", "m")
            claim = ledger.claim_dispatch("r", "m", "executor", 1)
            before = ledger.events("m", run_id="r")
            ledger.close()
            ledger.reopen()
            self.assertEqual(ledger.get_dispatch(claim.dispatch_id), claim)
            self.assertEqual(ledger.events("m", run_id="r"), before)
            facts = ledger.recovery_facts("r")
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0].dispatch.dispatch_id, claim.dispatch_id)

    def test_reason_categories_do_not_collapse(self) -> None:
        self.assertEqual(PreIdentityTransportFailure().code, ReasonCode.TRANSPORT_FAILURE)
        self.assertEqual(PostIdentityExecutionFailure().code, ReasonCode.EXECUTION_FAILURE)
        self.assertEqual(ReviewRejected().code, ReasonCode.REVIEW_REJECTED)

    def test_schema_reopen_migration_and_rejection(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.db"
            ledger = Ledger(path)
            ledger.close()
            connection = sqlite3.connect(path)
            connection.execute("UPDATE schema_meta SET value = '1' WHERE key = 'schema_version'")
            connection.commit()
            connection.close()
            migrated = Ledger(path)
            self.assertEqual(int(migrated.schema_version), 2)
            migrated.close()
            connection = sqlite3.connect(path)
            connection.execute("UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'")
            connection.commit()
            connection.close()
            with self.assertRaises(UnsupportedSchemaVersion):
                Ledger(path)

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
            self.assertEqual(len(ledger.events("m", run_id="race")), 1)


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
