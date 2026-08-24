from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from codex_flow.backends.codex_sdk import CodexSdkAdapter, CodexSdkConfig
from codex_flow.domain import (
    Capability,
    CapabilityStatus,
    ReasoningEffort,
    Sandbox,
    SkillInput,
    TerminalFailureAfterIdentity,
    TransportFailureBeforeIdentity,
    UnsupportedCapability,
)

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _event(method: str, payload: Any) -> SimpleNamespace:
    return SimpleNamespace(method=method, payload=payload)


def _turn_events(turn_id: str, *, status: str = "completed", text: str | None = None) -> list[Any]:
    events: list[Any] = [
        _event("turn/started", SimpleNamespace(turn=SimpleNamespace(id=turn_id))),
    ]
    if text is not None:
        events.append(
            _event(
                "item/completed",
                SimpleNamespace(
                    item=SimpleNamespace(root=SimpleNamespace(text=text)),
                    turn_id=turn_id,
                ),
            )
        )
    events.append(
        _event(
            "turn/completed",
            SimpleNamespace(
                turn=SimpleNamespace(
                    id=turn_id,
                    status=SimpleNamespace(value=status),
                    error=(SimpleNamespace(message="terminal failure") if status != "completed" else None),
                )
            ),
        )
    )
    return events


class FakeTurn:
    def __init__(self, turn_id: str, events: list[Any]) -> None:
        self.id = turn_id
        self._events = events

    def stream(self) -> list[Any]:
        return self._events


class FakeThread:
    def __init__(self, thread_id: str, *, failure: bool = False) -> None:
        self.id = thread_id
        self.failure = failure
        self.turn_calls: list[dict[str, Any]] = []
        self.next_turn = 0

    def turn(self, input: Any, **kwargs: Any) -> FakeTurn:
        self.turn_calls.append({"input": input, **kwargs})
        self.next_turn += 1
        turn_id = f"turn-{self.next_turn}"
        return FakeTurn(
            turn_id,
            _turn_events(
                turn_id,
                status="failed" if self.failure else "completed",
                text=json.dumps({"ok": True}) if not self.failure else None,
            ),
        )


class FakeClient:
    def __init__(self, *, failure: bool = False) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.resume_calls: list[tuple[str, dict[str, Any]]] = []
        self.thread = FakeThread("thread-1", failure=failure)
        self.closed = False

    def thread_start(self, **kwargs: Any) -> FakeThread:
        self.start_calls.append(kwargs)
        return self.thread

    def thread_resume(self, thread_id: str, **kwargs: Any) -> FakeThread:
        self.resume_calls.append((thread_id, kwargs))
        return self.thread

    def close(self) -> None:
        self.closed = True


def _sdk(*, with_skill: bool = True) -> SimpleNamespace:
    sandbox = SimpleNamespace(read_only="sdk-read-only")
    approval = SimpleNamespace(deny_all="sdk-deny-all")
    effort = SimpleNamespace(medium="sdk-medium")
    return SimpleNamespace(
        Sandbox=sandbox,
        ApprovalMode=approval,
        ReasoningEffort=effort,
        SkillInput=(lambda **kwargs: SimpleNamespace(**kwargs)) if with_skill else None,
        version="0.147.0-test",
    )


class CodexSdkAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="codex-flow-adapter-")
        self.cwd = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def config(self) -> CodexSdkConfig:
        return CodexSdkConfig(
            model="gpt-test",
            reasoning_effort=ReasoningEffort.MEDIUM,
            sandbox=Sandbox.READ_ONLY,
            cwd=self.cwd,
        )

    def test_success_passes_explicit_contract_and_preserves_event_order(self) -> None:
        client = FakeClient()
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())

        identity = adapter.start_thread()
        result = adapter.run_turn(identity, "hello", output_schema=SCHEMA)

        self.assertEqual(identity.id, "thread-1")
        self.assertEqual(result.structured_output, {"ok": True})
        self.assertEqual(
            [event.method for event in result.events],
            ["turn/started", "item/completed", "turn/completed"],
        )
        self.assertEqual(client.start_calls[0]["model"], "gpt-test")
        self.assertEqual(client.start_calls[0]["sandbox"], "sdk-read-only")
        self.assertEqual(client.thread.turn_calls[0]["effort"], "sdk-medium")
        self.assertEqual(client.thread.turn_calls[0]["model"], "gpt-test")
        self.assertEqual(client.thread.turn_calls[0]["sandbox"], "sdk-read-only")
        self.assertEqual(client.thread.turn_calls[0]["output_schema"], SCHEMA)

    def test_sdk_exception_before_identity_is_transport_failure(self) -> None:
        def fail() -> FakeClient:
            raise OSError("runtime unavailable")

        adapter = CodexSdkAdapter(self.config(), client_factory=fail, sdk=_sdk())

        with self.assertRaises(TransportFailureBeforeIdentity):
            adapter.start_thread()

    def test_terminal_failure_after_identity_is_not_transport_failure(self) -> None:
        client = FakeClient(failure=True)
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        identity = adapter.start_thread()

        with self.assertRaises(TerminalFailureAfterIdentity):
            adapter.run_turn(identity, "hello")

    def test_unsupported_structured_skill_input_is_explicit(self) -> None:
        adapter = CodexSdkAdapter(self.config(), client_factory=FakeClient, sdk=_sdk(with_skill=False))

        with self.assertRaises(UnsupportedCapability):
            adapter.validate_skill_input(SkillInput(name="demo", path="/tmp/demo"))
        observation = next(
            item for item in adapter.capability_matrix() if item.capability is Capability.STRUCTURED_SKILL_INPUT
        )
        self.assertEqual(observation.status, CapabilityStatus.UNSUPPORTED)

    def test_resume_keeps_the_same_thread_identity(self) -> None:
        client = FakeClient()
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        identity = adapter.start_thread()

        resumed = adapter.resume_thread(identity)

        self.assertEqual(resumed, identity)
        self.assertEqual(client.resume_calls[0][0], "thread-1")
        self.assertEqual(client.resume_calls[0][1]["model"], "gpt-test")
        self.assertEqual(client.resume_calls[0][1]["sandbox"], "sdk-read-only")

    def test_schema_violation_is_terminal_after_identity(self) -> None:
        client = FakeClient()
        client.thread = FakeThread("thread-1")
        client.thread.turn = lambda input, **kwargs: FakeTurn(
            "turn-1",
            _turn_events("turn-1", text=json.dumps({"ok": True, "extra": 1})),
        )
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        identity = adapter.start_thread()

        with self.assertRaises(TerminalFailureAfterIdentity):
            adapter.run_turn(identity, "hello", output_schema=SCHEMA)


if __name__ == "__main__":
    unittest.main()
