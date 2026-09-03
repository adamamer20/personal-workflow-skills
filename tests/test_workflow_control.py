from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_flow.cli import _resume_argv, app
from codex_flow.contracts import (
    ModelAuthority,
    ModelFacingCapsule,
    ModelFacingResult,
    ModelResultStatus,
    ModelValidation,
    model_facing_capsule_schema,
    model_facing_result_schema,
)
from codex_flow.domain import (
    AcceptanceMode,
    ConversationContent,
    ConversationContentKind,
    ConversationHistoryPage,
    ConversationHistoryStatus,
    ConversationMessageFragment,
    ConversationSpeaker,
    ConversationSubjectKind,
    ConversationTurnSlice,
    RoleId,
    ThreadIdentity,
    validate_output_schema,
)
from codex_flow.tui import CodexFlowTerminalApp
from codex_flow.tui_models import ActivityView, DecisionView, TerminalUiSnapshot, WorkerView

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "prompt-input"


def test_model_facing_capsule_and_result_are_typed_round_trips() -> None:
    capsule = ModelFacingCapsule(
        1,
        "package the controller path",
        ("define contract", "wire entrypoint"),
        (AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
        ("CLI reaches controller", "protected surfaces stay unchanged"),
        ("src/codex_flow",),
        ("plugins/personal-workflow-skills/skills/codex-thread-handoff",),
        (
            ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),
            ModelAuthority(AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")),
        ),
        "Use the controller and return one durable result.",
    )
    assert ModelFacingCapsule.from_json(capsule.to_json()) == capsule
    result = ModelFacingResult(
        1,
        ModelResultStatus.COMPLETED,
        "controller path reached",
        ("src/codex_flow/contracts.py",),
        (ModelValidation("focused tests", True, "digest:abc"),),
        "completed",
    )
    assert ModelFacingResult.from_json(result.to_json()) == result


def test_model_facing_capsule_rejects_non_completion_recovery() -> None:
    try:
        ModelFacingCapsule(
            1,
            "bounded objective",
            ("implement",),
            (AcceptanceMode.OBJECTIVE,),
            ("tests pass",),
            ("src",),
            ("protected",),
            (ModelAuthority(AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")),),
            "return one result",
            recovery_policy="retry_forever",
        )
    except ValueError as exc:
        assert "completion_biased" in str(exc)
    else:  # pragma: no cover - assertion captures contract drift
        raise AssertionError("non-completion recovery policy must be rejected")


def test_model_facing_schemas_are_closed_and_validated() -> None:
    validate_output_schema(model_facing_capsule_schema())
    validate_output_schema(model_facing_result_schema())


def test_cli_exposes_controller_control_and_model_schemas() -> None:
    runner = CliRunner()
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "control" in help_result.stdout
    assert "schema" in help_result.stdout
    assert "tui" in help_result.stdout
    schema_result = runner.invoke(app, ["schema", "--kind", "all"])
    assert schema_result.exit_code == 0
    payload = json.loads(schema_result.stdout)
    assert set(payload) == {"capsule", "result"}


def test_cli_exposes_native_conversation_under_live_control() -> None:
    result = CliRunner().invoke(app, ["live", "conversation", "--help"])
    assert result.exit_code == 0
    assert "native SDK conversation page" in result.stdout
    assert "--subject-kind" in result.stdout
    assert "--thread-id" in result.stdout


def test_cli_exposes_closed_compatibility_rebind_separately_from_retry() -> None:
    result = CliRunner().invoke(app, ["live", "compatibility-rebind", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "without retrying" in result.stdout
    assert "--expected-compatibility-sha256" in result.stdout
    assert "--proposed-compatibility-sha256" in result.stdout
    assert "--expected-profile-sha256" in result.stdout
    assert "--proposed-profile-sha256" in result.stdout
    assert "--expected-generation" in result.stdout
    assert "--expected-attempt" in result.stdout


def test_prompt_fixtures_keep_one_intended_skill_and_budget() -> None:
    for skill in ("plan-work", "execute-milestone", "workflow-control"):
        fixture = json.loads((FIXTURES / f"{skill}.json").read_text(encoding="utf-8"))
        assert fixture["input_text"].count(f"${skill}") == 1
        skill_text = (ROOT / "plugins/personal-workflow-skills/skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        assert len(skill_text.encode()) <= json.loads((FIXTURES / "budget.json").read_text())["budget_bytes"][skill]


def test_explicit_legacy_route_remains_reachable_without_mixing() -> None:
    handoff = (ROOT / "plugins/personal-workflow-skills/skills/codex-thread-handoff/SKILL.md").read_text(
        encoding="utf-8"
    )
    control = (ROOT / "plugins/personal-workflow-skills/skills/workflow-control/SKILL.md").read_text(encoding="utf-8")
    assert "Explicit legacy route" in handoff
    assert "$codex-thread-handoff" in handoff
    assert "never invoke both routes" in handoff
    assert "codex-flow control" in control
    assert "--hosting app-native" in control
    assert "codex-flow app-bind" in control
    assert "codex-flow app-result" in control
    assert "attach to a Desktop socket" in control


class _StaticTerminalClient:
    def __init__(self, snapshot: TerminalUiSnapshot) -> None:
        self.snapshot = snapshot

    async def refresh(self) -> TerminalUiSnapshot:
        return self.snapshot

    def add_live_listener(self, _listener: object) -> None:
        return None

    def live_keyframe(self, _dispatch_id: str) -> None:
        return None

    def live_stream_active(self, _dispatch_id: str) -> bool:
        return False

    async def subscribe_live(self, _worker_id: str) -> None:
        return None

    async def close_live_stream(self) -> None:
        return None


def _terminal_snapshot(*, connected: bool = True) -> TerminalUiSnapshot:
    worker = WorkerView(
        "run/milestone/executor/1",
        "run",
        "milestone",
        "executor",
        "running",
        1,
        2,
        "thread-01",
        "turn-01",
        "not exposed by control API",
        "not exposed by control API",
        "control: authenticated local supervisor; execution: not exposed",
        "not exposed by control API",
        "2m 18s",
        "not exposed by control API",
        "none; rev 3; pre 0/5; schema 0/2",
        (
            ActivityView(41, "turn_started", "2026-08-30T10:12:00Z", "SDK turn is active"),
            ActivityView(42, "agent_message", "2026-08-30T10:12:08Z", "Running deterministic checks"),
        ),
    )
    decision = DecisionView(
        "decision/run/milestone/executor/1/checkpoint/1",
        worker.dispatch_id,
        "checkpoint",
        "awaiting_claim",
        7,
        1,
        "1/2",
        "unclaimed",
        "unclaimed",
        "2026-08-30T11:00:00Z",
        "Inspect the worker and preserve the exact successor snapshot.",
        "thread-source",
        (),
    )
    return TerminalUiSnapshot(
        connected,
        "2026-08-30T10:14:18+00:00",
        (worker,),
        (decision,),
        None if connected else "supervisor control endpoint is unavailable",
    )


def test_tui_headless_driver_is_keyboard_bounded_at_wide_and_narrow_sizes() -> None:
    async def scenario() -> None:
        for size in ((120, 40), (80, 24)):
            client = _StaticTerminalClient(_terminal_snapshot())
            terminal = CodexFlowTerminalApp(client, refresh_on_mount=False)  # type: ignore[arg-type]
            async with terminal.run_test(size=size) as pilot:
                await pilot.pause()
                assert "LIVE · authenticated supervisor" in str(terminal.query_one("#mode").render())
                assert "CONTROLLER" in str(terminal.query_one("#controller-summary").render())
                assert "Implementer · Working" in str(terminal.query_one("#conversation-header").render())
                conversation = terminal.query_one("#conversation")
                assert "CONVERSATION EXCERPT" in str(conversation.render())
                assert "Running deterministic checks" in str(conversation.render())
                assert conversation.size.height > 0
                assert terminal.query_one("#actions").size.height > 0
                actions = str(terminal.query_one("#actions").render())
                assert "View top" in actions
                assert "[/] Scroll" in actions
                assert "O Open" in actions
                assert "T Details" in actions
                assert "run/milestone/executor/1" not in str(terminal.query_one("#conversation-header").render())
                workers = terminal.query_one("#workers")
                decisions = terminal.query_one("#decisions")
                assert workers.index == decisions.index == 0  # type: ignore[attr-defined]
                terminal.set_focus(workers)
                await pilot.press("tab")
                await pilot.pause()
                assert terminal.focused is decisions
                assert "Progress check" in str(terminal.query_one("#conversation-header").render())
                assert "WHY THIS NEEDS ATTENTION" in str(terminal.query_one("#conversation").render())
                assert workers.index == decisions.index == 0  # type: ignore[attr-defined]
                await pilot.press("shift+tab")
                await pilot.pause()
                assert terminal.focused is workers
                assert "Implementer" in str(terminal.query_one("#conversation-header").render())
                assert workers.index == decisions.index == 0  # type: ignore[attr-defined]
                await pilot.press("t")
                await pilot.pause()
                technical = terminal.query_one("#technical")
                assert technical.display and technical.size.height > 0
                assert "run/milestone/executor/1" in str(technical.render())
                await pilot.press("d")
                await pilot.press("r")
                await pilot.pause()
                assert terminal.client.snapshot.connected

    asyncio.run(scenario())


def test_tui_preserves_empty_user_message_without_inventing_text() -> None:
    fragment = ConversationMessageFragment(
        "turn-1",
        "user-empty",
        ConversationSpeaker.USER,
        ConversationContent(ConversationContentKind.TEXT, ""),
        0,
    )
    page = ConversationHistoryPage(
        ConversationSubjectKind.WORKER,
        "run/milestone/executor/1",
        ThreadIdentity("thread-1"),
        ConversationHistoryStatus.AVAILABLE,
        "stable",
        (ConversationTurnSlice("turn-1", 0, (fragment,)),),
        None,
    )

    assert CodexFlowTerminalApp._conversation_messages((page,)) == ("USER · text\n",)


def test_resume_handoff_is_one_validated_argv_without_shell_interpolation() -> None:
    assert _resume_argv("thread-01") == ("codex", "resume", "thread-01")
    with pytest.raises(ValueError):
        _resume_argv("thread-01; touch /tmp/not-allowed")


def test_terminal_render_evidence_is_fixed_full_size_svg() -> None:
    expected = {
        "light-wide.svg": "0 0 1482 1026.0",
        "dark-wide.svg": "0 0 1482 1026.0",
        "light-narrow.svg": "0 0 994 635.5999999999999",
        "no-color-narrow.svg": "0 0 994 635.5999999999999",
    }
    root = ROOT / "docs" / "reviews" / "evidence" / "human-terminal-ui"
    for name, view_box in expected.items():
        source = (root / name).read_text(encoding="utf-8")
        assert source.startswith('<svg class="rich-terminal"')
        assert f'viewBox="{view_box}"' in source[:200]
        assert "Generated with Rich" in source

    light = (root / "light-wide.svg").read_text(encoding="utf-8")
    section = re.search(r'class="([^"]+)"[^>]*>WORKERS</text>', light)
    assert section is not None
    section_style = re.search(rf"\.{re.escape(section.group(1))} \{{ fill: (#[0-9a-f]{{6}})", light)
    assert section_style is not None

    def relative_luminance(color: str) -> float:
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        adjusted = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
        return 0.2126 * adjusted[0] + 0.7152 * adjusted[1] + 0.0722 * adjusted[2]

    foreground = relative_luminance(section_style.group(1))
    background = relative_luminance("#e0e0e0")
    assert (max(foreground, background) + 0.05) / (min(foreground, background) + 0.05) >= 4.5

    for label in ("Refresh", "Steer", "Interrupt", "Claim", "Ack", "Re-arm", "Copy", "Open", "Theme"):
        assert label in light
    narrow = (root / "light-narrow.svg").read_text(encoding="utf-8")
    assert "command&#160;palette" in narrow
    monochrome = (root / "no-color-narrow.svg").read_text(encoding="utf-8")
    assert "command&#160;palette" in monochrome


def test_conversation_first_render_sentinels_cover_wide_80x24_and_no_color() -> None:
    expected = {
        "conversation-light-wide.svg": "0 0 1482 1026.0",
        "conversation-dark-wide.svg": "0 0 1482 1026.0",
        "conversation-light-80x24.svg": "0 0 994 635.5999999999999",
        "conversation-no-color-80x24.svg": "0 0 994 635.5999999999999",
    }
    root = ROOT / "docs" / "reviews" / "evidence" / "human-terminal-ui"
    for name, view_box in expected.items():
        source = (root / name).read_text(encoding="utf-8")
        assert source.startswith('<svg class="rich-terminal"')
        assert f'viewBox="{view_box}"' in source[:200]
        assert "CONTROLLER&#160;·&#160;1&#160;workflow" in source
        assert "WORKER&#160;CONVERSATIONS" in source
        assert "Implementer&#160;·&#160;Working" in source
        assert (
            "COMPLETE&#160;CONVERSATION" in source
            or "CONVERSATION&#160;·&#160;older&#160;messages&#160;available&#160;(L)" in source
        )
        assert "View&#160;top&#160;·&#160;[/]&#160;Scroll" in source
        assert "L&#160;Load&#160;older" in source
        assert "O&#160;Open" in source
        assert "T&#160;Details" in source

    light_wide = (root / "conversation-light-wide.svg").read_text(encoding="utf-8")
    dark_wide = (root / "conversation-dark-wide.svg").read_text(encoding="utf-8")
    assert "fill: #1f1f1f" in light_wide
    assert "fill: #1f1f1f" not in dark_wide
