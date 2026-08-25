"""Final integrated H6 production pilots and legacy-retirement evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

from .backends.codex_sdk import CodexSdkAdapter, CodexSdkConfig
from .config import WorkflowConfig, load_workflow_config
from .contracts import model_facing_result_schema
from .controller import Controller, capsule_json
from .domain import (
    AcceptanceMode,
    ExecutionCapsule,
    ExecutionStatus,
    JsonObject,
    LifecyclePhase,
    MilestoneId,
    NativePermissionMode,
    ReasoningEffort,
    RoleId,
    RunId,
    Sandbox,
    Severity,
    ThreadIdentity,
    ValidationSpec,
    WorkflowState,
    WorkspaceMode,
)
from .projection import selected_checkout_facts

_EXECUTION_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "validation": {"type": "string"},
    },
    "required": ["status", "changed_files", "validation"],
    "additionalProperties": False,
}

_REVIEW_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "accepted": {"type": "boolean"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string"},
                    "severity": {"type": "string"},
                    "promotion_blocking": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "criterion": {"type": "string"},
                },
                "required": ["finding_id", "severity", "promotion_blocking", "reason", "criterion"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["accepted", "findings"],
    "additionalProperties": False,
}


class H6PilotError(RuntimeError):
    """The one-shot integrated pilot could not satisfy its fixed contract."""


def app_native_visible_pilot_capsule(*, state_root: Path, parent_run_id: RunId | str) -> ExecutionCapsule:
    """Build the bounded host-only H6-C visible-App follow-up capsule."""

    checkout = state_root.resolve(strict=True)
    if not checkout.is_dir():
        raise H6PilotError("App-native pilot state root must be a checkout directory")

    def git_output(*arguments: str) -> str:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=checkout,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            raise H6PilotError("unable to resolve App-native pilot Git facts")
        return completed.stdout.strip()

    if Path(git_output("rev-parse", "--show-toplevel")).resolve() != checkout:
        raise H6PilotError("App-native pilot requires the exact selected Git toplevel")
    route = load_workflow_config(checkout / "workflow.toml").route("executor")
    repository, workspace, workspace_mode, branch, base_sha, lane = selected_checkout_facts(checkout)
    return ExecutionCapsule(
        2,
        RunId(parent_run_id),
        MilestoneId("h6-c-visible-app-pilot"),
        repository,
        workspace_mode,
        workspace,
        branch,
        base_sha,
        lane,
        ("docs/reviews/codex-controller-compatibility.md",),
        ("AGENTS.md", "docs/reviews/peer-thread-workflow.md", "workflow.toml"),
        ValidationSpec(("git", "diff", "--check"), 60),
        route.model,
        route.reasoning_effort,
        (
            "Append exactly one line `App-native visible pilot: passed` to "
            "docs/reviews/codex-controller-compatibility.md if it is absent. Change no other file. "
            "Do not commit. Return exactly one schema-valid ModelFacingResult with objective validation evidence."
        ),
        model_facing_result_schema(),
        NativePermissionMode.INHERIT_NATIVE,
        (AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    )


class CompatibilityClassification(str, Enum):
    PROVEN = "proven"
    UNSUPPORTED = "unsupported"
    NOT_EXPOSED = "not_exposed"
    NOT_RUN = "not_run"


class LegacyRetirementDecision(str, Enum):
    RETAIN_LEGACY = "retain_legacy"
    DISABLE_HOOKS_KEEP_MANUAL = "disable_hooks_keep_manual"
    READY_FOR_SEPARATE_CLEANUP = "ready_for_separate_cleanup"


@dataclass(frozen=True, slots=True)
class LegacyRetirementDecisionRecord:
    """Closed H6 decision; it never authorizes cleanup by itself."""

    schema_version: int
    decision: LegacyRetirementDecision
    closed: bool
    production_reachability_proven: bool
    required_behavioral_parity_proven: bool
    evidence_ids: tuple[str, ...]
    rationale_codes: tuple[str, ...]
    cleanup_authorized: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported legacy-retirement decision schema version")
        if not isinstance(self.decision, LegacyRetirementDecision):
            object.__setattr__(self, "decision", LegacyRetirementDecision(self.decision))
        if self.closed is not True:
            raise ValueError("H6 legacy-retirement decision must be closed")
        if self.cleanup_authorized:
            raise ValueError("H6 decision evidence cannot authorize cleanup")
        evidence = tuple(self.evidence_ids)
        rationale = tuple(self.rationale_codes)
        if not evidence or any(not item.strip() for item in evidence):
            raise ValueError("legacy-retirement decision requires evidence identities")
        if not rationale or any(not item.strip() for item in rationale):
            raise ValueError("legacy-retirement decision requires rationale codes")
        if self.decision is not LegacyRetirementDecision.RETAIN_LEGACY and not (
            self.production_reachability_proven and self.required_behavioral_parity_proven
        ):
            raise ValueError("legacy disablement requires proven reachability and behavioral parity")
        object.__setattr__(self, "evidence_ids", evidence)
        object.__setattr__(self, "rationale_codes", rationale)

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "decision": self.decision.value,
            "closed": self.closed,
            "production_reachability_proven": self.production_reachability_proven,
            "required_behavioral_parity_proven": self.required_behavioral_parity_proven,
            "evidence_ids": list(self.evidence_ids),
            "rationale_codes": list(self.rationale_codes),
            "cleanup_authorized": self.cleanup_authorized,
        }


@dataclass(frozen=True, slots=True)
class _PilotSpec:
    scale: str
    run_id: RunId
    milestone_id: MilestoneId
    prompt: str
    mutable_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    validation_argv: tuple[str, ...]
    expected_changed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ReviewEvidence:
    mode: AcceptanceMode
    role: RoleId
    model: str
    effort: ReasoningEffort
    thread_digest: str
    turn_count: int
    event_count: int
    token_usage_event_count: int
    accepted: bool
    p0: int
    p1: int
    p2: int
    other: int
    finding_ids: tuple[str, ...]
    workspace_unchanged: bool

    @property
    def promotion_blockers(self) -> int:
        return self.p0 + self.p1 if not self.accepted else 0

    def to_json(self) -> JsonObject:
        return {
            "mode": self.mode.value,
            "role": str(self.role),
            "model": self.model,
            "reasoning_effort": self.effort.value,
            "thread_identity_sha256": self.thread_digest,
            "turn_count": self.turn_count,
            "event_count": self.event_count,
            "token_usage_event_count": self.token_usage_event_count,
            "accepted": self.accepted,
            "finding_counts": {"P0": self.p0, "P1": self.p1, "P2": self.p2, "other": self.other},
            "finding_ids": list(self.finding_ids),
            "workspace_unchanged": self.workspace_unchanged,
        }


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(("git", *arguments), cwd=root, text=True, capture_output=True, check=True)
    return completed.stdout.strip()


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _tree_digest(root: Path) -> str:
    """Hash the disposable repository tree while excluding Git/controller state."""

    digest = hashlib.sha256()
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in {".git", ".codex-flow", "__pycache__"})
        for name in sorted(files):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            metadata = os.lstat(path)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise H6PilotError(f"pilot workspace contains an unsupported file: {relative}")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(f"{stat.S_IMODE(metadata.st_mode):04o}".encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _initialize_repository(root: Path, spec: _PilotSpec) -> str:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "codex-flow@example.test")
    _git(root, "config", "user.name", "Codex Flow H6 Pilot")
    _write(root / ".gitignore", ".codex-flow/\n__pycache__/\n")
    if spec.scale == "medium":
        _write(root / "pyproject.toml", "[project]\nname='h6-medium-pilot'\nversion='0.0.0'\n")
        _write(
            root / "src/pilot_text/__init__.py", "from .slug import normalize_slug\n\n__all__ = ['normalize_slug']\n"
        )
        _write(
            root / "src/pilot_text/slug.py",
            '"""Text normalization boundary."""\n\n\ndef normalize_slug(value: str) -> str:\n'
            '    """Return a stable ASCII slug."""\n\n    raise NotImplementedError\n',
        )
        _write(
            root / "tests/test_slug.py",
            "import sys\nimport unittest\nfrom pathlib import Path\n\n"
            "sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))\n"
            "from pilot_text import normalize_slug\n\n\n"
            "class SlugTests(unittest.TestCase):\n"
            "    def test_normalizes_words(self):\n        self.assertEqual(normalize_slug('  Hello, WORLD!  '), 'hello-world')\n\n"
            "    def test_collapses_separators(self):\n        self.assertEqual(normalize_slug('A___B---C'), 'a-b-c')\n\n"
            "    def test_non_ascii_is_a_separator(self):\n        self.assertEqual(normalize_slug('café noir'), 'caf-noir')\n\n"
            "    def test_rejects_non_string(self):\n        with self.assertRaises(TypeError):\n            normalize_slug(3)\n\n"
            "    def test_rejects_empty_result(self):\n        with self.assertRaises(ValueError):\n            normalize_slug('---')\n\n\n"
            "if __name__ == '__main__':\n    unittest.main()\n",
        )
    else:
        _write(root / "pyproject.toml", "[project]\nname='h6-large-pilot'\nversion='0.0.0'\n")
        _write(
            root / "src/pilot_queue/__init__.py",
            "from .domain import Job, JobStatus\nfrom .service import QueueService\n"
            "from .store import RevisionConflict, StoreCorrupt, TaskStore\n\n"
            "__all__ = ['Job', 'JobStatus', 'QueueService', 'RevisionConflict', 'StoreCorrupt', 'TaskStore']\n",
        )
        _write(
            root / "src/pilot_queue/domain.py",
            '"""Typed queue domain."""\n\n# Implement this module without changing public names.\n',
        )
        _write(
            root / "src/pilot_queue/store.py",
            '"""Atomic JSON queue storage."""\n\n# Implement this module without changing public names.\n',
        )
        _write(
            root / "src/pilot_queue/service.py",
            '"""Queue use-cases."""\n\n# Implement this module without changing public names.\n',
        )
        _write(
            root / "tests/test_queue.py",
            "import json\nimport sys\nimport tempfile\nimport unittest\nfrom pathlib import Path\n\n"
            "sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))\n"
            "from pilot_queue import Job, JobStatus, QueueService, RevisionConflict, StoreCorrupt, TaskStore\n\n\n"
            "class QueueTests(unittest.TestCase):\n"
            "    def setUp(self):\n        self.temporary = tempfile.TemporaryDirectory()\n"
            "        self.path = Path(self.temporary.name) / 'queue.json'\n        self.store = TaskStore(self.path)\n\n"
            "    def tearDown(self):\n        self.temporary.cleanup()\n\n"
            "    def test_empty_store(self):\n        self.assertEqual(self.store.load(), (0, ()))\n\n"
            "    def test_job_validation(self):\n        with self.assertRaises(ValueError):\n            Job('', JobStatus.PENDING)\n\n"
            "    def test_round_trip_is_deterministic(self):\n"
            "        job = Job('job-1', JobStatus.PENDING)\n        revision = self.store.save(0, (job,))\n"
            "        self.assertEqual(revision, 1)\n        self.assertEqual(self.store.load(), (1, (job,)))\n"
            "        self.assertTrue(self.path.read_text().endswith('\\n'))\n"
            "        self.assertEqual(list(json.loads(self.path.read_text())), ['jobs', 'revision'])\n\n"
            "    def test_stale_revision_is_rejected_without_change(self):\n"
            "        self.store.save(0, (Job('job-1', JobStatus.PENDING),))\n        before = self.path.read_bytes()\n"
            "        with self.assertRaises(RevisionConflict):\n            self.store.save(0, ())\n"
            "        self.assertEqual(self.path.read_bytes(), before)\n\n"
            "    def test_corrupt_store_fails_closed(self):\n"
            "        self.path.write_text('{broken')\n        with self.assertRaises(StoreCorrupt):\n            self.store.load()\n\n"
            "    def test_service_enqueue_and_complete(self):\n"
            "        service = QueueService(self.store)\n        self.assertEqual(service.enqueue('job-1'), 1)\n"
            "        self.assertEqual(service.complete('job-1'), 2)\n"
            "        revision, jobs = self.store.load()\n        self.assertEqual(revision, 2)\n"
            "        self.assertEqual(jobs, (Job('job-1', JobStatus.DONE),))\n\n"
            "    def test_duplicate_enqueue_fails(self):\n"
            "        service = QueueService(self.store)\n        service.enqueue('job-1')\n"
            "        with self.assertRaises(ValueError):\n            service.enqueue('job-1')\n\n"
            "    def test_missing_complete_fails(self):\n"
            "        with self.assertRaises(KeyError):\n            QueueService(self.store).complete('missing')\n\n\n"
            "if __name__ == '__main__':\n    unittest.main()\n",
        )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", f"h6 {spec.scale} pilot base")
    return _git(root, "rev-parse", "HEAD")


def _pilot_specs() -> tuple[_PilotSpec, _PilotSpec]:
    validation = ("python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-v")
    medium = _PilotSpec(
        "medium",
        RunId("h6-medium-pilot"),
        MilestoneId("slug-normalization"),
        (
            "Implement src/pilot_text/slug.py only. normalize_slug must require str, lowercase ASCII alphanumeric "
            "runs, replace every non-alphanumeric run with one hyphen, trim separators, and raise ValueError when "
            "the result is empty. Do not edit tests or other files and do not commit. Run exactly "
            "`python3 -B -m unittest discover -s tests -v` so validation creates no bytecode files. "
            "Return status='completed', changed_files=['src/pilot_text/slug.py'], and validation='passed'."
        ),
        ("src/pilot_text/slug.py",),
        (".gitignore", "pyproject.toml", "src/pilot_text/__init__.py", "tests"),
        validation,
        ("src/pilot_text/slug.py",),
    )
    large = _PilotSpec(
        "large",
        RunId("h6-large-pilot"),
        MilestoneId("atomic-queue"),
        (
            "Implement only src/pilot_queue/domain.py, store.py, and service.py as a cohesive typed standard-library "
            "queue. Define string JobStatus values pending/running/done and a frozen Job validating a bounded non-empty "
            "id. TaskStore.load returns (revision, tuple[Job,...]); missing storage is (0,()). TaskStore.save performs "
            "optimistic revision checking, increments the revision, validates a closed JSON shape, and writes canonical "
            "sorted JSON plus one newline atomically with a same-directory temporary file and os.replace. Define typed "
            "RevisionConflict and StoreCorrupt failures and fail closed on malformed or unknown data. QueueService.enqueue "
            "rejects duplicates and complete rejects missing ids, persisting through TaskStore. Do not edit tests, package "
            "exports, or other files and do not commit. Run exactly `python3 -B -m unittest discover -s tests -v` so "
            "validation creates no bytecode files. Return status='completed', the three "
            "changed source paths, and validation='passed'."
        ),
        ("src/pilot_queue/domain.py", "src/pilot_queue/store.py", "src/pilot_queue/service.py"),
        (".gitignore", "pyproject.toml", "src/pilot_queue/__init__.py", "tests"),
        validation,
        ("src/pilot_queue/domain.py", "src/pilot_queue/service.py", "src/pilot_queue/store.py"),
    )
    return medium, large


def _run_control(root: Path, capsule_path: Path) -> tuple[subprocess.CompletedProcess[str], float]:
    executable = shutil.which("codex-flow")
    if executable is None:
        raise H6PilotError("the packaged codex-flow executable is unavailable")
    environment = os.environ.copy()
    environment["CODEX_FLOW_REAL_SDK"] = "1"
    started = time.monotonic()
    completed = subprocess.run(
        (
            executable,
            "control",
            "--capsule",
            str(capsule_path),
            "--state-root",
            str(root),
            "--json",
        ),
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=900,
        check=False,
    )
    return completed, time.monotonic() - started


def _review_prompt(mode: AcceptanceMode, base_sha: str, revision: str, validation: tuple[str, ...]) -> str:
    focus = (
        "functional correctness, edge cases, tests, and exact authorized outcome"
        if mode is AcceptanceMode.OBJECTIVE
        else "ownership boundaries, atomicity, failure behavior, maintainability, and public-contract coherence"
    )
    command = " ".join(validation)
    return (
        f"Act as the fresh independent {mode.value} acceptance authority for revision digest {revision}. "
        f"Read only this disposable repository. Inspect git diff {base_sha}, inspect relevant source/tests, and run "
        f"`{command}` without writing files. Review {focus}. Report every actionable finding with a stable id and "
        "severity P0, P1, P2, or P3. promotion_blocking may be true only for a concrete P0/P1 acceptance blocker. "
        "Set accepted=true exactly when there are zero promotion-blocking findings. Return only the required JSON."
    )


def _review_workspace(
    root: Path,
    *,
    mode: AcceptanceMode,
    role: RoleId,
    model: str,
    effort: ReasoningEffort,
    base_sha: str,
    revision: str,
    validation: tuple[str, ...],
) -> _ReviewEvidence:
    before = _tree_digest(root)
    adapter = CodexSdkAdapter(CodexSdkConfig(model=model, reasoning_effort=effort, sandbox=Sandbox.READ_ONLY, cwd=root))
    thread: ThreadIdentity | None = None
    try:
        thread = adapter.start_thread()
        observation = adapter.run_turn(
            thread,
            _review_prompt(mode, base_sha, revision, validation),
            output_schema=_REVIEW_SCHEMA,
        )
        payload = observation.structured_output
        if payload is None:
            raise H6PilotError(f"{mode.value} reviewer returned no structured output")
        accepted = payload.get("accepted")
        raw_findings = payload.get("findings")
        if not isinstance(accepted, bool) or not isinstance(raw_findings, list):
            raise H6PilotError(f"{mode.value} reviewer returned an invalid typed result")
        seen: set[str] = set()
        counts = {severity.value: 0 for severity in Severity}
        blockers = 0
        finding_ids: list[str] = []
        for raw in raw_findings:
            if not isinstance(raw, dict):
                raise H6PilotError(f"{mode.value} reviewer finding is not an object")
            finding_id = raw.get("finding_id")
            severity_value = raw.get("severity")
            blocking = raw.get("promotion_blocking")
            if (
                not isinstance(finding_id, str)
                or not finding_id.strip()
                or finding_id in seen
                or not isinstance(severity_value, str)
                or not isinstance(blocking, bool)
            ):
                raise H6PilotError(f"{mode.value} reviewer finding is invalid")
            try:
                severity = Severity(severity_value)
            except ValueError as exc:
                raise H6PilotError(f"{mode.value} reviewer returned an unknown severity") from exc
            if blocking and severity not in {Severity.P0, Severity.P1}:
                raise H6PilotError(f"{mode.value} reviewer misclassified a lower-severity finding as blocking")
            seen.add(finding_id)
            finding_ids.append(finding_id)
            counts[severity.value] += 1
            blockers += int(blocking)
        if accepted != (blockers == 0):
            raise H6PilotError(f"{mode.value} reviewer acceptance contradicts its findings")
        after = _tree_digest(root)
        unchanged = before == after
        if not unchanged:
            raise H6PilotError(f"{mode.value} reviewer changed the read-only workspace")
        return _ReviewEvidence(
            mode,
            role,
            model,
            effort,
            _sha256_bytes(thread.id.encode()),
            1,
            len(observation.events),
            sum(event.method == "thread/tokenUsage/updated" for event in observation.events),
            accepted,
            counts[Severity.P0.value],
            counts[Severity.P1.value],
            counts[Severity.P2.value],
            counts[Severity.P3.value],
            tuple(finding_ids),
            unchanged,
        )
    finally:
        if thread is not None:
            try:
                adapter.archive_thread(thread)
            except Exception as exc:
                raise H6PilotError(f"unable to archive {mode.value} review thread") from exc
        adapter.close()


def _review_fact(review: _ReviewEvidence, revision: str) -> JsonObject:
    return {
        "mode": review.mode.value,
        "role": str(review.role),
        "model": review.model,
        "reasoning_effort": review.effort.value,
        "reviewed_revision": revision,
        "fresh": True,
        "read_only": True,
        "accepted": review.accepted,
        "finding_ids": list(review.finding_ids),
        "finding_counts": {"P0": review.p0, "P1": review.p1, "P2": review.p2, "other": review.other},
        "thread_identity_sha256": review.thread_digest,
    }


def _notification_failure() -> None:
    raise RuntimeError("injected H6 notification delivery failure")


def _run_one_pilot(spec: _PilotSpec, config: WorkflowConfig, parent: Path) -> dict[str, Any]:
    root = parent / f"{spec.scale}-repository"
    base_sha = _initialize_repository(root, spec)
    executor = config.route("executor")
    capsule = ExecutionCapsule(
        2,
        spec.run_id,
        spec.milestone_id,
        root,
        WorkspaceMode.CURRENT_CHECKOUT,
        root,
        "main",
        base_sha,
        f"h6-{spec.scale}-pilot",
        spec.mutable_paths,
        spec.protected_paths,
        ValidationSpec(spec.validation_argv, 60),
        executor.model,
        executor.reasoning_effort,
        spec.prompt,
        _EXECUTION_SCHEMA,
        NativePermissionMode.INHERIT_NATIVE,
        (AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    )
    capsule_path = parent / f"{spec.scale}-capsule.json"
    capsule_path.write_text(
        json.dumps(capsule_json(capsule), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    completed, execution_seconds = _run_control(root, capsule_path)
    try:
        control_payload = json.loads(completed.stdout.strip())
    except (json.JSONDecodeError, TypeError) as exc:
        raise H6PilotError(f"{spec.scale} control returned invalid JSON") from exc
    if completed.returncode != 0 or not isinstance(control_payload, dict):
        raise H6PilotError(f"{spec.scale} control failed with exit {completed.returncode}")
    if control_payload.get("status") != ExecutionStatus.COMPLETED.value:
        validation_fact = control_payload.get("validation")
        error_code = validation_fact.get("error_code") if isinstance(validation_fact, dict) else None
        raise H6PilotError(
            f"{spec.scale} controller did not reach durable completion "
            f"(status={control_payload.get('status')!r}, checkpoint={control_payload.get('checkpoint')!r}, "
            f"validation_error={error_code!r})"
        )
    result = control_payload.get("result")
    if not isinstance(result, dict) or result.get("status") != "completed":
        raise H6PilotError(f"{spec.scale} model result was not completed")
    changed = tuple(sorted(_git(root, "diff", "--name-only", base_sha).splitlines()))
    if changed != spec.expected_changed_paths:
        raise H6PilotError(f"{spec.scale} observable changed paths do not match the fixed capsule")
    validation = subprocess.run(spec.validation_argv, cwd=root, text=True, capture_output=True, check=False, timeout=60)
    if validation.returncode != 0:
        raise H6PilotError(f"{spec.scale} observable validation failed")
    revision = _tree_digest(root)

    controller = Controller(root)
    try:
        record = controller.status(spec.run_id, spec.milestone_id)
        if record.status is not ExecutionStatus.COMPLETED or record.result is None:
            raise H6PilotError(f"{spec.scale} durable controller result is incomplete")
        result_artifact = root / ".codex-flow" / "runs" / str(spec.run_id) / "execution.json"
        if not result_artifact.is_file():
            raise H6PilotError(f"{spec.scale} durable execution projection is missing")
        result_before = result_artifact.read_bytes()
        controller.ledger.record_h4_transition(
            spec.run_id,
            spec.milestone_id,
            WorkflowState.REVIEWING,
            expected_state=WorkflowState.COMPLETED,
            phase=LifecyclePhase.REVIEW,
            kind="h6_integrated_review_started",
            data={"revision": revision, "acceptance_modes": ["objective", "architecture"]},
        )
        review_started = time.monotonic()
        reviews: list[_ReviewEvidence] = []
        for mode, role_name in (
            (AcceptanceMode.OBJECTIVE, "code-reviewer"),
            (AcceptanceMode.ARCHITECTURE, "architecture-reviewer"),
        ):
            route = config.route(role_name)
            review = _review_workspace(
                root,
                mode=mode,
                role=route.role,
                model=route.model,
                effort=route.reasoning_effort,
                base_sha=base_sha,
                revision=revision,
                validation=spec.validation_argv,
            )
            reviews.append(review)
            controller.ledger.record_h4_fact(
                spec.run_id,
                spec.milestone_id,
                phase=LifecyclePhase.REVIEW,
                kind="h6_independent_authority_review",
                data=_review_fact(review, revision),
            )
        review_seconds = time.monotonic() - review_started
        blockers = sum(review.p0 + review.p1 for review in reviews)
        if blockers or any(not review.accepted for review in reviews):
            controller.ledger.record_h4_transition(
                spec.run_id,
                spec.milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.REVIEWING,
                phase=LifecyclePhase.ACCEPTANCE,
                kind="h6_integrated_review_rejected",
                data={"promotion_blockers": blockers},
            )
            raise H6PilotError(f"{spec.scale} independent review returned P0/P1 promotion blockers")
        controller.ledger.record_h4_transition(
            spec.run_id,
            spec.milestone_id,
            WorkflowState.ACCEPTED,
            expected_state=WorkflowState.REVIEWING,
            phase=LifecyclePhase.ACCEPTANCE,
            kind="h6_all_authorities_accepted",
            data={"revision": revision, "review_modes": [review.mode.value for review in reviews]},
        )
        notification_failed = False
        recovery_count = 0
        terminal_authority_survived = True
        if spec.scale == "large":
            try:
                _notification_failure()
            except RuntimeError as exc:
                notification_failed = True
                controller.ledger.record_h4_fact(
                    spec.run_id,
                    spec.milestone_id,
                    phase=LifecyclePhase.ACCEPTANCE,
                    kind="notification_failure",
                    data={"exception_class": type(exc).__name__},
                )
            controller.close()
            controller = Controller(root)
            recovery_count = 1
            recovered = controller.status(spec.run_id, spec.milestone_id)
            terminal_authority_survived = (
                recovered.status is ExecutionStatus.COMPLETED
                and recovered.result is not None
                and controller.ledger.current_state(spec.run_id, spec.milestone_id) is WorkflowState.ACCEPTED
                and result_artifact.read_bytes() == result_before
            )
        snapshot = controller.ledger.snapshot(spec.run_id)
        dispatch_count = len(snapshot.dispatches)
        integrity = controller.ledger.get_execution_integrity(spec.run_id, spec.milestone_id)
        permission = integrity.effective_permission
        lifecycle = controller.ledger.h4_lifecycle(spec.run_id, spec.milestone_id)
        review_threads = {review.thread_digest for review in reviews}
        if len(review_threads) != 2:
            raise H6PilotError(f"{spec.scale} review authorities did not use distinct threads")
        if dispatch_count != 1:
            raise H6PilotError(f"{spec.scale} controller recorded duplicate execution owners")
        if not terminal_authority_survived:
            raise H6PilotError("large notification recovery changed durable terminal authority")
        return {
            "evidence_id": f"h6-{spec.scale}-pilot",
            "scale": spec.scale,
            "status": "passed",
            "route": "workflow-control/codex-flow control",
            "capsule_sha256": _sha256_bytes(capsule_path.read_bytes()),
            "prompt_bytes": len(spec.prompt.encode("utf-8")),
            "acceptance_modes": ["objective", "architecture"],
            "visual_mode": "not_run",
            "base_sha": base_sha,
            "revision_sha256": revision,
            "observable_changed_paths": list(changed),
            "validation": {"command": list(spec.validation_argv), "passed": True},
            "controller": {
                "status": record.status.value,
                "checkpoint": record.checkpoint.value,
                "dispatch_count": dispatch_count,
                "execution_thread_count": 1,
                "no_duplicate_owner": dispatch_count == 1,
                "terminal_projection_sha256": _sha256_bytes(result_before),
                "sqlite_state": controller.ledger.current_state(spec.run_id, spec.milestone_id).value,
            },
            "reviews": [review.to_json() for review in reviews],
            "review_summary": {
                "fresh_independent_authorities": True,
                "distinct_roles": len({str(review.role) for review in reviews}) == 2,
                "distinct_threads": len(review_threads) == 2,
                "P0": sum(review.p0 for review in reviews),
                "P1": sum(review.p1 for review in reviews),
                "P2": sum(review.p2 for review in reviews),
                "other": sum(review.other for review in reviews),
            },
            "usage": {
                "model_turns": 1 + sum(review.turn_count for review in reviews),
                "execution_wall_seconds": round(execution_seconds, 3),
                "review_wall_seconds": round(review_seconds, 3),
                "total_wall_seconds": round(execution_seconds + review_seconds, 3),
                "prompt_bytes": len(spec.prompt.encode("utf-8")),
                "compaction_use": "not_exposed",
                "cost": "unavailable",
                "token_count": "unavailable",
            },
            "permission_profile": {
                "status": CompatibilityClassification.PROVEN.value if permission is not None else "not_exposed",
                "mode": capsule.permission_mode.value,
                "sandbox_mode": permission.sandbox_mode if permission is not None else None,
                "approval_policy": permission.approval_policy if permission is not None else None,
            },
            "recovery_notification": {
                "bounded_recovery_count": recovery_count,
                "notification_failure_injected": notification_failed,
                "terminal_authority_survived": terminal_authority_survived,
                "duplicate_owner_created": False,
            },
            "lifecycle_kinds": [item.kind for item in lifecycle],
        }
    finally:
        controller.close()


def _compatibility_matrix(pilots: tuple[dict[str, Any], ...]) -> JsonObject:
    permission_proven = all(
        cast(dict[str, object], pilot["permission_profile"])["status"] == CompatibilityClassification.PROVEN.value
        for pilot in pilots
    )
    return {
        "local_sdk": {
            "status": CompatibilityClassification.PROVEN.value,
            "rationale": "both pilots completed through the pinned Python SDK and bundled local app-server",
        },
        "desktop_visibility": {
            "status": CompatibilityClassification.NOT_EXPOSED.value,
            "rationale": "the stable high-level SDK exposes no Desktop sidebar visibility operation",
        },
        "idle_wake": {
            "status": CompatibilityClassification.NOT_EXPOSED.value,
            "rationale": "the local SDK exposes no idle queue or wake operation",
        },
        "remote_host": {
            "status": CompatibilityClassification.NOT_EXPOSED.value,
            "rationale": "the controller transport is local and exposes no remote-host selector",
        },
        "permission_profile_behavior": {
            "status": (
                CompatibilityClassification.PROVEN.value
                if permission_proven
                else CompatibilityClassification.NOT_EXPOSED.value
            ),
            "rationale": "both controller executions durably recorded inherited native sandbox and approval authority",
        },
        "native_review": {
            "status": CompatibilityClassification.NOT_EXPOSED.value,
            "rationale": "required reviews used distinct schema-bounded read-only turns; the SDK has no native review operation",
        },
    }


def _comparison(pilots: tuple[dict[str, Any], ...]) -> JsonObject:
    model_turns = sum(cast(int, cast(dict[str, object], pilot["usage"])["model_turns"]) for pilot in pilots)
    wall_seconds = round(
        sum(cast(float, cast(dict[str, object], pilot["usage"])["total_wall_seconds"]) for pilot in pilots), 3
    )
    prompt_bytes = sum(cast(int, cast(dict[str, object], pilot["usage"])["prompt_bytes"]) for pilot in pilots)
    return {
        "controller_observed": {
            "pilot_count": 2,
            "execution_dispatch_count": 2,
            "execution_owner_count": 2,
            "review_thread_count": 4,
            "model_turns": model_turns,
            "wall_seconds": wall_seconds,
            "prompt_bytes": prompt_bytes,
            "compaction_use": "not_exposed",
            "validation_passes": 2,
            "required_mode_reviews": 4,
            "P0": 0,
            "P1": 0,
            "bounded_recoveries": 1,
            "notification_failures": 1,
            "terminal_durability_without_notification": "proven",
        },
        "retained_legacy_baseline": {
            "source_contract": "codex-thread-handoff explicit legacy route",
            "ownership_and_dispatch": "source_contract_only",
            "thread_count": "unavailable",
            "terminal_durability": "notification_dependent_closure_contract",
            "recovery": "one bounded native status snapshot described; no matched retained pilot",
            "review_outcomes": "unavailable_for_matched_medium_and_large_pilots",
            "model_turns": "unavailable",
            "wall_seconds": "unavailable",
            "prompt_and_compaction_use": "unavailable",
            "validation": "unavailable_for_matched_pilots",
            "notification_independence": "not_proven",
        },
        "limitations": [
            "no matched legacy medium or large pilot was run because H6 may not mix controller and legacy routes",
            "cost and token counts are unavailable from the retained evidence",
            "quality is not inferred from test, timing, or lifecycle counts",
            "Desktop, idle-wake, remote-host, and native-review parity remain unexposed",
            "installed-plugin and downstream-pin migration were not authorized or run",
        ],
    }


def make_legacy_decision(
    *, pilots_passed: bool, required_behavioral_parity_proven: bool
) -> LegacyRetirementDecisionRecord:
    """Apply the H6 fail-closed retirement rule to established evidence."""

    decision = (
        LegacyRetirementDecision.READY_FOR_SEPARATE_CLEANUP
        if pilots_passed and required_behavioral_parity_proven
        else LegacyRetirementDecision.RETAIN_LEGACY
    )
    rationale = (
        ("canonical_reachability_proven", "required_behavioral_parity_proven")
        if decision is LegacyRetirementDecision.READY_FOR_SEPARATE_CLEANUP
        else (
            "canonical_reachability_proven" if pilots_passed else "canonical_reachability_not_proven",
            "required_behavioral_parity_not_proven",
            "cleanup_requires_separate_authorization",
        )
    )
    return LegacyRetirementDecisionRecord(
        1,
        decision,
        True,
        pilots_passed,
        required_behavioral_parity_proven,
        ("h6-medium-pilot", "h6-large-pilot", "h6-compatibility-matrix", "h6-legacy-comparison"),
        rationale,
        False,
    )


def run_h6_pilots(*, config_path: Path) -> dict[str, Any]:
    """Run the fixed H6 medium/large suite and return sanitized retained evidence."""

    config = load_workflow_config(config_path)
    with tempfile.TemporaryDirectory(prefix="codex-flow-h6-pilots-") as directory:
        parent = Path(directory)
        pilots = tuple(_run_one_pilot(spec, config, parent) for spec in _pilot_specs())
    recovery_count = sum(
        cast(int, cast(dict[str, object], pilot["recovery_notification"])["bounded_recovery_count"]) for pilot in pilots
    )
    notification_failures = sum(
        int(cast(bool, cast(dict[str, object], pilot["recovery_notification"])["notification_failure_injected"]))
        for pilot in pilots
    )
    if recovery_count != 1 or notification_failures != 1:
        raise H6PilotError("H6 must exercise exactly one bounded recovery and one notification failure")
    compatibility = _compatibility_matrix(pilots)
    comparison = _comparison(pilots)
    decision = make_legacy_decision(pilots_passed=True, required_behavioral_parity_proven=False)
    return {
        "schema": "codex-flow/h6-production-pilots/v1",
        "status": "passed",
        "pilots": list(pilots),
        "compatibility": compatibility,
        "comparison": comparison,
        "legacy_retirement_decision": decision.to_json(),
        "protected_actions": {
            "hooks_disabled": False,
            "legacy_deleted": False,
            "installed_plugins_changed": False,
            "global_codex_state_changed": False,
            "downstream_pins_changed": False,
        },
    }


def write_h6_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
