from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from codex_flow.contracts import model_facing_result_schema_sha256
from codex_flow.domain import AcceptanceMode, DispatchId
from codex_flow.ledger import Ledger, StaleWriter
from codex_flow.plan_capsule import PlanCapsuleError, compile_canonical_plan, compile_program_graph


def test_canonical_plan_compiler_accepts_only_the_requested_typed_block() -> None:
    compiled = compile_canonical_plan(Path("docs/reviews/peer-thread-workflow.md"), "H6-E-W-R1")

    assert compiled.capsule.schema_version == 1
    assert compiled.capsule.objective.startswith("Close H6-E-W")
    assert len(compiled.plan_revision_sha256) == 64
    assert len(compiled.source_block_sha256) == 64


def test_human_terminal_control_capsule_has_frozen_identity_and_surfaces() -> None:
    plan = Path("docs/reviews/peer-thread-workflow.md")
    compiled = compile_canonical_plan(plan, "human-terminal-control-correctness-closure")
    assert compiled.source_block_sha256 == "65fb555193f21df60bbace155f945605e7767b2e8ba7e0c4a499773f8a519578"
    assert len(compiled.capsule.mutable_surfaces) == 8
    assert compiled.capsule.plugin_requirements == ()


def test_canonical_plan_compiler_accepts_typed_plugin_requirement(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(
        """## Next execution — plugin-capability-parity

```python
ModelFacingCapsule(
    schema_version=2,
    objective="plugin",
    decomposition=("discover",),
    acceptance_modes=(AcceptanceMode.OBJECTIVE,),
    acceptance_criteria=("ready",),
    mutable_surfaces=("src/plugin.py",),
    protected_surfaces=("README.md",),
    authorities=(ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),),
    prompt="run",
    plugin_requirements=(PluginRequirement("demo-plugin", "1", "bundled", "0000000000000000000000000000000000000000000000000000000000000000"),),
)
```
""",
        encoding="utf-8",
    )
    compiled = compile_canonical_plan(plan, "plugin-capability-parity")
    assert compiled.capsule.schema_version == 2
    assert compiled.capsule.plugin_requirements[0].canonical_id == "demo-plugin"


def test_active_controller_turn_recovery_correctness_closure_capsule_compiles_from_canonical_plan() -> None:
    plan = Path("docs/reviews/peer-thread-workflow.md")
    with pytest.raises(PlanCapsuleError, match="exactly one active milestone heading"):
        compile_canonical_plan(plan, "controller-turn-recovery-correctness-closure")


def test_active_native_profile_compatibility_capsule_compiles_from_canonical_plan() -> None:
    plan = Path("docs/reviews/peer-thread-workflow.md")
    with pytest.raises(PlanCapsuleError, match="exactly one active milestone heading"):
        compile_canonical_plan(plan, "native-profile-runtime-compatibility-closure")


def test_integrated_control_capsule_has_frozen_identity_and_ordered_ownership() -> None:
    compiled = compile_canonical_plan(Path("docs/reviews/peer-thread-workflow.md"), "integrated-control")

    assert compiled.source_block_sha256 == "3c552a43d63297721ba72d8099372ca591345c17a18b9bdf147153044a9b1905"
    assert compiled.capsule.mutable_surfaces == (
        "tests/test_production_pilots.py",
        "tests/test_plan_compilation.py",
        "docs/reviews/codex-controller-compatibility.md",
        "docs/reviews/codex-sdk-compatibility.md",
        "docs/reviews/evidence/integrated-control.json",
    )
    assert compiled.capsule.acceptance_modes == (AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE)
    assert tuple(authority.role for authority in compiled.capsule.authorities) == (
        "code-reviewer",
        "architecture-reviewer",
    )
    assert compiled.capsule.plugin_requirements == ()


def test_plugin_and_controller_installation_schema_compatible_capsule_is_frozen() -> None:
    compiled = compile_canonical_plan(
        Path("docs/reviews/peer-thread-workflow.md"), "plugin-and-controller-installation-schema-compatible"
    )

    assert compiled.source_block_sha256 == "6da5536bf12b1ce906e5ffa0d8e0a6b1c238b8832f0473343f99f557d7d8f369"
    assert compiled.capsule.acceptance_modes == (AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE)
    assert "scripts/install_personal_workflow_skills.py" in compiled.capsule.mutable_surfaces
    assert compiled.capsule.plugin_requirements == ()


def test_complete_conversation_history_capsule_is_the_frozen_mixed_mode_owner() -> None:
    compiled = compile_canonical_plan(Path("docs/reviews/peer-thread-workflow.md"), "complete-conversation-history")

    assert compiled.source_block_sha256 == "1c0f204fc94d3bd48d38b9b791f14c3a067447fffa8fd2530071fbd9d83e4d18"
    assert compiled.capsule.acceptance_modes == (
        AcceptanceMode.OBJECTIVE,
        AcceptanceMode.VISUAL,
        AcceptanceMode.ARCHITECTURE,
    )
    assert (
        "src/codex_flow/backends/codex_sdk.py only for official thread/read conversation projection"
        in compiled.capsule.mutable_surfaces
    )
    assert any("ledger persistence/schema" in surface for surface in compiled.capsule.protected_surfaces)
    assert compiled.capsule.plugin_requirements == ()


def test_event_driven_program_controller_capsule_is_the_frozen_pre_tui_owner() -> None:
    compiled = compile_canonical_plan(Path("docs/reviews/peer-thread-workflow.md"), "event-driven-program-controller")

    assert compiled.source_block_sha256 == "dea2dcc10ca25d7d9ffd99fa84901b7cf70d66c35c7f346cadb57052360b6842"
    assert compiled.capsule.acceptance_modes == (
        AcceptanceMode.OBJECTIVE,
        AcceptanceMode.ARCHITECTURE,
    )
    assert tuple(str(authority.role) for authority in compiled.capsule.authorities) == (
        "code-reviewer",
        "architecture-reviewer",
    )
    assert "src/codex_flow/program_controller.py" in compiled.capsule.mutable_surfaces
    assert any("controller-model turns" in criterion for criterion in compiled.capsule.acceptance_criteria)
    assert "event-driven-program-controller" in compiled.capsule.prompt
    assert compiled.capsule.plugin_requirements == ()


def test_remaining_program_graph_is_serial_and_visual_review_stays_cancelled() -> None:
    plan = Path("docs/reviews/peer-thread-workflow.md")
    milestones = (
        "live-coding-agent-terminal-ui",
        "live-plan-dag-revision",
        "module-responsibility-decomposition",
    )
    graph = compile_program_graph(
        plan,
        program_id="codex-flow-remaining-plan",
        milestone_ids=milestones,
        dependencies={
            "live-plan-dag-revision": ("live-coding-agent-terminal-ui",),
            "module-responsibility-decomposition": ("live-plan-dag-revision",),
        },
    )

    assert tuple(node.milestone_id for node in graph.nodes) == milestones
    assert graph.node("live-coding-agent-terminal-ui").dependencies == ()
    assert graph.node("live-plan-dag-revision").dependencies == ("live-coding-agent-terminal-ui",)
    assert graph.node("module-responsibility-decomposition").dependencies == ("live-plan-dag-revision",)
    assert graph.node("live-coding-agent-terminal-ui").capsule.source_block_sha256 == (
        "9f3d55ee8c1bcaacb8203103cf30617a2d80abd6feb3c45ca0cc79396211b3b2"
    )
    assert graph.node("live-plan-dag-revision").capsule.source_block_sha256 == (
        "d4837f238b7744ad6b094a8b57ff4a6a3a18f07fe02d7b63f7f480435a952a8b"
    )
    assert graph.node("module-responsibility-decomposition").capsule.source_block_sha256 == (
        "0a4b9b7aec055239e10f804ec429b77549d5658471d5c9cdb2c3b906a1c10bbb"
    )
    for node in graph.nodes:
        assert node.capsule.capsule.acceptance_modes == (
            AcceptanceMode.OBJECTIVE,
            AcceptanceMode.ARCHITECTURE,
        )
        assert all(authority.mode is not AcceptanceMode.VISUAL for authority in node.capsule.capsule.authorities)


def test_active_capsule_rejects_an_injected_non_python_fence(tmp_path: Path) -> None:
    plan_text = Path("docs/reviews/peer-thread-workflow.md").read_text(encoding="utf-8")
    marker = "## Next execution — structured-output-runtime-and-naming-closure"
    poisoned = plan_text.replace(marker, marker + "\n\n```text\nnot a capsule\n```", 1)
    plan = tmp_path / "plan.md"
    plan.write_text(poisoned, encoding="utf-8")

    with pytest.raises(PlanCapsuleError):
        compile_canonical_plan(plan, "structured-output-runtime-and-naming-closure")


@pytest.mark.parametrize(
    "body",
    (
        "__import__('os').system('touch escaped')",
        "ModelFacingCapsule(schema_version=1, objective='x', decomposition=('x',), acceptance_modes=(), acceptance_criteria=('x',), mutable_surfaces=('x',), protected_surfaces=('y',), authorities=(), prompt='x')\nModelFacingCapsule(schema_version=1, objective='x', decomposition=('x',), acceptance_modes=(), acceptance_criteria=('x',), mutable_surfaces=('x',), protected_surfaces=('y',), authorities=(), prompt='x')",
    ),
)
def test_canonical_plan_compiler_rejects_execution_and_ambiguous_blocks(tmp_path: Path, body: str) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(f"## Next execution — H6-E-W\n\n```python\n{body}\n```\n", encoding="utf-8")

    with pytest.raises(PlanCapsuleError):
        compile_canonical_plan(plan, "H6-E-W")
    assert not (tmp_path / "escaped").exists()


def _route() -> str:
    return json.dumps(
        {
            "effective_permission": {
                "approval_policy": "never",
                "mode": "inherit_native",
                "monotonic": True,
                "sandbox_mode": "workspace-write",
            },
            "leaf_worker_policy": {
                "agents.enabled": False,
                "config_overrides": ["agents.enabled=false", "features.multi_agent=false"],
                "features.multi_agent": False,
            },
            "native_compatibility_sha256": None,
            "native_profile_sha256": None,
        },
        sort_keys=True,
    )


def _queue(ledger: Ledger, root: Path, run: str, milestone: str, role: str = "executor") -> str:
    ledger.create_run(run)
    ledger.create_milestone(run, milestone)
    ledger.claim_dispatch(run, milestone, role, 1)
    dispatch_id = str(DispatchId.from_parts(run, milestone, role, 1))
    ledger.enqueue_dispatch(
        dispatch_id,
        backend="sdk_headless",
        capsule_json='{"model":"gpt-test","prompt":"bounded"}\n',
        route_json=_route(),
        workspace_path=root,
        result_contract_sha256=model_facing_result_schema_sha256(),
    )
    return dispatch_id


def _result() -> str:
    return json.dumps(
        {
            "changed_surfaces": [],
            "durable_status": "completed",
            "next_action": None,
            "schema_version": 1,
            "status": "completed",
            "summary": "done",
            "validations": [],
        },
        sort_keys=True,
    )


def test_terminal_commit_releases_only_pre_authorized_successors_and_wakes_once(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    source = _queue(ledger, tmp_path, "run", "source")
    successor = _queue(ledger, tmp_path, "run", "successor", role="code-reviewer")
    ledger.authorize_successor(source, successor)
    authority = ledger.acquire_supervisor(
        repository_root=tmp_path,
        state_root=tmp_path,
        pid=os.getpid(),
        process_birth_identity="test",
        executable_digest="a" * 64,
        version="test",
        owner_nonce_sha256="b" * 64,
    )
    ledger.claim_queue_dispatch(epoch=int(authority["epoch"]), claim_nonce_sha256="c" * 64)
    ledger.set_queue_state(source, "starting", epoch=int(authority["epoch"]))
    token = "d" * 64
    ledger.issue_attempt_capability(
        source,
        generation=1,
        attempt=1,
        operation="submit_result",
        schema_sha256=model_facing_result_schema_sha256(),
        workspace_path=tmp_path,
        backend="sdk_headless",
        token_sha256=hashlib.sha256(token.encode()).hexdigest(),
        expires_at="2999-01-01T00:00:00Z",
    )
    terminal = ledger.commit_queue_result(
        source,
        generation=1,
        attempt=1,
        token=token,
        raw_result=_result(),
    )

    assert terminal["state"] == "completed"
    assert ledger.authorized_successors(source)[0]["released_at"] is not None
    assert (
        ledger.claim_queue_dispatch(epoch=int(authority["epoch"]), claim_nonce_sha256="e" * 64)["dispatch_id"]
        == successor
    )
    wakes = ledger.wake_outbox()
    assert len([wake for wake in wakes if wake["kind"] == "terminal"]) == 1
    ledger.close()


def test_checkpoint_is_one_shot_until_explicit_rearm_and_carries_liveness(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    dispatch = _queue(ledger, tmp_path, "checkpoint-run", "source")
    authority = ledger.acquire_supervisor(
        repository_root=tmp_path,
        state_root=tmp_path,
        pid=os.getpid(),
        process_birth_identity="test",
        executable_digest="a" * 64,
        version="test",
        owner_nonce_sha256="b" * 64,
    )
    epoch = int(authority["epoch"])
    ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="c" * 64)
    token = "d" * 64
    ledger.issue_attempt_capability(
        dispatch,
        generation=1,
        attempt=1,
        operation="submit_result",
        schema_sha256=model_facing_result_schema_sha256(),
        workspace_path=tmp_path,
        backend="sdk_headless",
        token_sha256=hashlib.sha256(token.encode()).hexdigest(),
        expires_at="2999-01-01T00:00:00Z",
    )
    ledger.bind_worker_liveness(
        dispatch,
        epoch=epoch,
        generation=1,
        attempt=1,
        pid=os.getpid(),
        process_birth_identity="test-worker",
        lease_token_sha256=hashlib.sha256(token.encode()).hexdigest(),
    )
    wake = ledger.claim_due_checkpoint(epoch=epoch, now="2999-01-01T00:00:00Z")
    assert wake is not None and wake["kind"] == "checkpoint"
    assert wake["state"] == "not_applicable"
    binding = ledger.queue_binding(dispatch)
    assert binding["checkpoint_armed"] == 0
    assert ledger.claim_due_checkpoint(epoch=epoch, now="2999-01-02T00:00:00Z") is None
    ledger.rearm_checkpoint(dispatch, seconds=1800)
    assert ledger.queue_binding(dispatch)["checkpoint_armed"] == 1
    ledger.close()


def test_checkpoint_rearm_rejects_an_active_wake(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    dispatch = _queue(ledger, tmp_path, "rearm-run", "source")
    ledger._db().execute(
        "UPDATE queue_bindings SET source_thread_id = 'source-thread' WHERE dispatch_id = ?", (dispatch,)
    )
    ledger._db().execute(
        "UPDATE queue_bindings SET checkpoint_deadline = '2000-01-01T00:00:00Z' WHERE dispatch_id = ?", (dispatch,)
    )
    ledger._db().commit()
    authority = ledger.acquire_supervisor(
        repository_root=tmp_path,
        state_root=tmp_path,
        pid=os.getpid(),
        process_birth_identity="test",
        executable_digest="a" * 64,
        version="test",
        owner_nonce_sha256="b" * 64,
    )
    ledger.claim_queue_dispatch(epoch=int(authority["epoch"]), claim_nonce_sha256="c" * 64)
    ledger.claim_due_checkpoint(epoch=int(authority["epoch"]), now="2999-01-01T00:00:00Z")
    with pytest.raises(StaleWriter, match="already active"):
        ledger.rearm_checkpoint(dispatch)
    ledger.close()


def test_wake_ambiguity_is_restart_reconciled_once(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    dispatch = _queue(ledger, tmp_path, "wake-run", "source")
    ledger._db().execute(
        "UPDATE queue_bindings SET source_thread_id = 'source-thread' WHERE dispatch_id = ?", (dispatch,)
    )
    ledger._db().commit()
    authority = ledger.acquire_supervisor(
        repository_root=tmp_path,
        state_root=tmp_path,
        pid=os.getpid(),
        process_birth_identity="test",
        executable_digest="a" * 64,
        version="test",
        owner_nonce_sha256="b" * 64,
    )
    ledger.claim_queue_dispatch(epoch=int(authority["epoch"]), claim_nonce_sha256="c" * 64)
    ledger.set_queue_state(dispatch, "starting", epoch=int(authority["epoch"]))
    token = "d" * 64
    ledger.issue_attempt_capability(
        dispatch,
        generation=1,
        attempt=1,
        operation="submit_result",
        schema_sha256=model_facing_result_schema_sha256(),
        workspace_path=tmp_path,
        backend="sdk_headless",
        token_sha256=hashlib.sha256(token.encode()).hexdigest(),
        expires_at="2999-01-01T00:00:00Z",
    )
    ledger.commit_queue_result(dispatch, generation=1, attempt=1, token=token, raw_result=_result())
    claimed = ledger.claim_wake(f"wake/{dispatch}/terminal")
    assert claimed is not None and claimed["state"] == "starting"
    assert ledger.reconcile_inflight_wakes() == 1
    reconciled = ledger.reconcile_wake_delivery(f"wake/{dispatch}/terminal", source_turn_id="source-turn-1")
    assert reconciled["state"] == "delivered"
    with pytest.raises(StaleWriter, match="immutable"):
        ledger.reconcile_wake_delivery(f"wake/{dispatch}/terminal", source_turn_id="source-turn-2")
    ledger.close()


def test_wake_delivery_failure_keeps_one_bounded_retry_pending(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    dispatch = _queue(ledger, tmp_path, "wake-retry-run", "source")
    ledger._db().execute(
        "UPDATE queue_bindings SET source_thread_id = 'source-thread' WHERE dispatch_id = ?", (dispatch,)
    )
    ledger._db().commit()
    authority = ledger.acquire_supervisor(
        repository_root=tmp_path,
        state_root=tmp_path,
        pid=os.getpid(),
        process_birth_identity="test",
        executable_digest="a" * 64,
        version="test",
        owner_nonce_sha256="b" * 64,
    )
    epoch = int(authority["epoch"])
    ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="c" * 64)
    ledger.set_queue_state(dispatch, "starting", epoch=epoch)
    token = "d" * 64
    ledger.issue_attempt_capability(
        dispatch,
        generation=1,
        attempt=1,
        operation="submit_result",
        schema_sha256=model_facing_result_schema_sha256(),
        workspace_path=tmp_path,
        backend="sdk_headless",
        token_sha256=hashlib.sha256(token.encode()).hexdigest(),
        expires_at="2999-01-01T00:00:00Z",
    )
    ledger.commit_queue_result(dispatch, generation=1, attempt=1, token=token, raw_result=_result())
    delivery_id = f"wake/{dispatch}/terminal"
    assert ledger.claim_wake(delivery_id)["state"] == "starting"
    first_failure = ledger.record_wake_delivery(delivery_id, outcome="failed")
    assert first_failure["state"] == "pending"
    assert first_failure["attempt_count"] == 1
    assert ledger.claim_wake(delivery_id)["attempt_count"] == 2
    final_failure = ledger.record_wake_delivery(delivery_id, outcome="failed")
    assert final_failure["state"] == "failed"
    assert final_failure["attempt_count"] == 2
    ledger.close()
