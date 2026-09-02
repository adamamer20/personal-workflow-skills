from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jsonschema import Draft202012Validator

import codex_flow.backends.codex_sdk as codex_sdk_module
from codex_flow.backends.codex_sdk import (
    LEAF_WORKER_CONFIG_OVERRIDES,
    CodexSdkAdapter,
    CodexSdkConfig,
    ControllerThreadInspectionKind,
    ResponseChainInvalid,
    SchemaOutputInvalid,
    ThreadInspectionKind,
    _decode_schema_output,
    _provider_output_schema,
)
from codex_flow.contracts import model_facing_controller_action_schema, model_facing_result_schema
from codex_flow.domain import (
    Capability,
    CapabilityStatus,
    ControllerDecisionId,
    ConversationHistoryRequest,
    ConversationHistoryStatus,
    ConversationSubjectKind,
    Generation,
    ReasoningEffort,
    Sandbox,
    SkillInput,
    StrictJSONError,
    TemporaryRateLimitAfterIdentity,
    TerminalFailureAfterIdentity,
    ThreadIdentity,
    TransientFailureAfterIdentity,
    TransportFailureBeforeIdentity,
    UnsupportedCapability,
    strict_json_loads,
    validate_structured_output,
)
from codex_flow.worker import SCHEMA_CORRECTION_BUDGET, _run_bounded_schema_turn, run_nested_delegation_sentinel

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


class _ReadClient(FakeClient):
    def __init__(self, snapshot: object = None, failure: BaseException | None = None) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.failure = failure
        self.read_calls: list[tuple[str, bool]] = []

    def thread_read(self, thread_id: str, include_turns: bool = False) -> object:
        self.read_calls.append((thread_id, include_turns))
        if self.failure is not None:
            raise self.failure
        return self.snapshot


class InvalidRequestError(RuntimeError):
    # The adapter intentionally recognizes the installed SDK's exact class
    # name and structured attributes, not arbitrary message text.
    code = -32600
    message = "the thread already has an active writer"


def _terminal_snapshot(text: str) -> dict[str, object]:
    return {
        "thread": {
            "id": "thread-1",
            "turns": [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [{"root": {"type": "agentMessage", "text": text}}],
                }
            ],
        }
    }


def _ordered_snapshot(turns: list[dict[str, object]]) -> dict[str, object]:
    return {"thread": {"id": "thread-1", "turns": turns}}


def _history_turn(turn_id: str, status: str, *texts: str) -> dict[str, object]:
    return {
        "id": turn_id,
        "status": status,
        "items": [{"root": {"type": "agentMessage", "text": text}} for text in texts],
    }


def _valid_result() -> str:
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
        separators=(",", ":"),
    )


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
    def test_reduced_schema_decoder_is_removed(self) -> None:
        self.assertFalse(hasattr(codex_sdk_module, "_type_matches"))
        self.assertFalse(hasattr(codex_sdk_module, "_validate_decoded_schema"))

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

    def test_tool_notifications_emit_only_bounded_redacted_summaries(self) -> None:
        mcp = codex_sdk_module._event_from_notification(
            _event(
                "item/completed",
                SimpleNamespace(
                    item=SimpleNamespace(
                        root=SimpleNamespace(
                            type="mcpToolCall",
                            server="browser",
                            tool="screenshot",
                            status=SimpleNamespace(value="completed"),
                            result="token=provider-secret and raw tool output",
                        )
                    )
                ),
            ),
            1,
        )
        command = codex_sdk_module._event_from_notification(
            _event(
                "item/completed",
                SimpleNamespace(
                    item=SimpleNamespace(
                        root=SimpleNamespace(
                            type="commandExecution",
                            command="git status --ignored provider-secret",
                            aggregated_output="raw provider output",
                        )
                    )
                ),
            ),
            2,
        )

        self.assertEqual(mcp.text, "MCP tool browser/screenshot completed")
        self.assertEqual(command.text, "command execution completed")
        self.assertNotIn("provider-secret", str(mcp))
        self.assertNotIn("provider-secret", str(command))

    def test_provider_schema_omits_unique_items_but_local_authority_enforces_it(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "uniqueItems": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                }
            },
            "required": ["uniqueItems"],
            "additionalProperties": False,
        }
        original = deepcopy(schema)
        client = FakeClient()
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())

        identity = adapter.start_thread()
        adapter.start_turn(identity, "hello", output_schema=schema)

        projected = client.thread.turn_calls[0]["output_schema"]
        self.assertIn("uniqueItems", projected["properties"])
        self.assertNotIn("uniqueItems", projected["properties"]["uniqueItems"])
        self.assertEqual(schema, original)
        self.assertEqual(_provider_output_schema(schema), projected)
        with self.assertRaises(TerminalFailureAfterIdentity):
            _decode_schema_output('{"uniqueItems":["duplicate","duplicate"]}', schema)

    def test_provider_schema_is_a_positive_supported_subset_projection(self) -> None:
        schema = model_facing_result_schema()
        original = deepcopy(schema)

        projected = _provider_output_schema(schema)

        self.assertIsNotNone(projected)
        assert projected is not None
        self.assertNotIn("$schema", projected)
        self.assertNotIn("$id", projected)
        self.assertNotIn("title", projected)
        self.assertEqual(projected["additionalProperties"], False)
        properties = projected["properties"]
        self.assertIsInstance(properties, dict)
        assert isinstance(properties, dict)
        changed_items = properties["changed_surfaces"]["items"]
        self.assertEqual(changed_items, {"type": "string"})
        self.assertNotIn("uniqueItems", properties["changed_surfaces"])
        self.assertEqual(properties["schema_version"], {"type": "integer", "enum": [1]})
        self.assertEqual(schema, original)

    def test_controller_action_schema_retains_supported_numeric_bounds(self) -> None:
        schema = model_facing_controller_action_schema()

        projected = _provider_output_schema(schema)

        assert projected is not None
        properties = projected["properties"]
        assert isinstance(properties, dict)
        self.assertEqual(properties["generation"]["minimum"], 1)
        self.assertEqual(properties["generation"]["maximum"], 2)
        self.assertEqual(properties["expected_revision"]["minimum"], 0)
        validate_structured_output(
            {
                "schema_version": 1,
                "decision_id": "decision/wake/run/milestone/executor/1/checkpoint",
                "generation": 1,
                "action_id": "action-1",
                "expected_revision": 0,
                "expected_successor_dispatch_ids": [],
                "actions": [{"kind": "acknowledge_only"}],
                "rationale": "continue",
            },
            schema,
        )

    def test_provider_schema_keeps_plain_patterns_and_omits_extension_patterns(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "plain": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                "lookaround": {
                    "type": "string",
                    "pattern": r"^(?![\s\S]*\u0000)(?=[\s\S]*\S)",
                },
            },
            "required": ["plain", "lookaround"],
            "additionalProperties": False,
        }

        projected = _provider_output_schema(schema)

        assert projected is not None
        properties = projected["properties"]
        assert isinstance(properties, dict)
        self.assertEqual(properties["plain"]["pattern"], r"^[0-9a-f]{64}$")
        self.assertEqual(properties["lookaround"], {"type": "string"})
        with self.assertRaises(TerminalFailureAfterIdentity):
            _decode_schema_output('{"plain":"0", "lookaround":""}', schema)

    def test_provider_projection_fails_closed_for_unrepresentable_root_union(self) -> None:
        schema = {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {"left": {"type": "string"}},
                    "required": ["left"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"right": {"type": "string"}},
                    "required": ["right"],
                    "additionalProperties": False,
                },
            ]
        }

        with self.assertRaises(TerminalFailureAfterIdentity, msg="root anyOf is not provider supported"):
            _provider_output_schema(schema)

    def test_provider_projection_fails_closed_for_dynamic_object_keys(self) -> None:
        schema = {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 2,
            "propertyNames": {"type": "string", "pattern": r"^surface$"},
            "additionalProperties": {"type": "string"},
        }

        with self.assertRaises(TerminalFailureAfterIdentity, msg="dynamic keys require unsupported open objects"):
            _provider_output_schema(schema)

    def test_marker_shaped_strings_remain_exact_ordinary_sdk_strings(self) -> None:
        for prompt in (
            'Codex Flow bundled skill input (controller-owned JSON):\n{"name":"demo","path":"/tmp/demo"}',
            'Codex Flow plugin requirements (controller-owned JSON):\n{"plugin_requirements":[]}',
        ):
            client = FakeClient()
            adapter = CodexSdkAdapter(self.config(), client_factory=lambda client=client: client, sdk=_sdk())
            identity = adapter.start_thread()
            adapter.run_turn(identity, prompt, output_schema=SCHEMA)
            self.assertEqual(client.thread.turn_calls[0]["input"], prompt)

    def test_typed_skill_input_reaches_the_sdk_as_structured_input(self) -> None:
        client = FakeClient()
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        identity = adapter.start_thread()
        adapter.run_turn(identity, SkillInput(name="demo.skill", path="/proc/self/fd/7"), output_schema=SCHEMA)
        sdk_input = client.thread.turn_calls[0]["input"]
        self.assertIsInstance(sdk_input, SimpleNamespace)
        self.assertEqual(sdk_input.name, "demo.skill")
        self.assertEqual(sdk_input.path, "/proc/self/fd/7")

    def test_raw_thread_read_extracts_only_complete_terminal_envelope(self) -> None:
        client = _ReadClient(_terminal_snapshot(_valid_result()))
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())

        inspection = adapter.inspect_persisted_thread(ThreadIdentity("thread-1"))

        self.assertEqual(inspection.kind, ThreadInspectionKind.TERMINAL_RESULT)
        self.assertEqual(inspection.turn_id, "turn-1")
        self.assertEqual(inspection.raw_result, _valid_result())
        self.assertEqual(client.read_calls, [("thread-1", True)])
        self.assertEqual(client.resume_calls, [])

    def test_conversation_history_pages_complete_messages_and_redacts_before_projection(self) -> None:
        snapshot = {
            "thread": {
                "id": "thread-1",
                "updatedAt": 17,
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "itemsView": "full",
                        "items": [
                            {"root": {"type": "reasoning", "summary": ["lossy provider activity"]}},
                            {
                                "root": {
                                    "id": "user-1",
                                    "type": "userMessage",
                                    "content": [
                                        {"root": {"type": "text", "text": "hello token=private-value"}},
                                        {"root": {"type": "image", "url": "file:///secret/path.png"}},
                                    ],
                                }
                            },
                            {"root": {"id": "agent-1", "type": "agentMessage", "text": "answer one"}},
                        ],
                    },
                    {
                        "id": "turn-2",
                        "status": "completed",
                        "itemsView": "full",
                        "items": [
                            {
                                "root": {
                                    "id": "user-2",
                                    "type": "userMessage",
                                    "content": [{"root": {"type": "text", "text": "next"}}],
                                }
                            },
                            {"root": {"id": "agent-2", "type": "agentMessage", "text": "answer two"}},
                        ],
                    },
                ],
            }
        }
        client = _ReadClient(snapshot)
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        first = adapter.read_conversation_history(
            ConversationHistoryRequest(
                ConversationSubjectKind.WORKER,
                "run/milestone/executor/1",
                ThreadIdentity("thread-1"),
                generation=1,
                attempt=1,
                page_fragments=3,
            )
        )
        self.assertEqual(first.status, ConversationHistoryStatus.AVAILABLE)
        self.assertIsNotNone(first.older_token)
        older = adapter.read_conversation_history(
            ConversationHistoryRequest(
                ConversationSubjectKind.WORKER,
                "run/milestone/executor/1",
                ThreadIdentity("thread-1"),
                generation=1,
                attempt=1,
                page_token=first.older_token,
                page_fragments=3,
            )
        )
        self.assertEqual(first.snapshot_token, older.snapshot_token)
        projected = json.dumps([older.to_json(), first.to_json()])
        self.assertIn("token=[REDACTED]", projected)
        self.assertIn("[image]", projected)
        self.assertNotIn("private-value", projected)
        self.assertNotIn("secret/path", projected)
        self.assertEqual(sorted({turn.ordinal for page in (older, first) for turn in page.turns}), [0, 1])
        self.assertEqual(client.read_calls, [("thread-1", True), ("thread-1", True)])

    def test_conversation_history_retains_empty_user_message_identity_and_order(self) -> None:
        snapshot = {
            "thread": {
                "id": "thread-1",
                "turns": [
                    {
                        "id": "turn-1",
                        "itemsView": "full",
                        "items": [
                            {"root": {"id": "user-empty", "type": "userMessage", "content": []}},
                            {"root": {"id": "agent-after", "type": "agentMessage", "text": "after"}},
                        ],
                    }
                ],
            }
        }
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: _ReadClient(snapshot), sdk=_sdk())
        page = adapter.read_conversation_history(
            ConversationHistoryRequest(
                ConversationSubjectKind.WORKER,
                "run/milestone/executor/1",
                ThreadIdentity("thread-1"),
                generation=1,
                attempt=1,
                page_fragments=1,
            )
        )
        self.assertEqual(
            [(item.item_id, item.content.text) for item in page.turns[0].messages], [("agent-after", "after")]
        )
        self.assertIsNotNone(page.older_token)
        older = adapter.read_conversation_history(
            ConversationHistoryRequest(
                ConversationSubjectKind.WORKER,
                "run/milestone/executor/1",
                ThreadIdentity("thread-1"),
                generation=1,
                attempt=1,
                page_token=page.older_token,
                page_fragments=1,
            )
        )
        self.assertEqual([(item.item_id, item.content.text) for item in older.turns[0].messages], [("user-empty", "")])
        self.assertEqual(older.turns[0].messages[0].ordinal, 0)
        self.assertEqual(page.turns[0].messages[0].ordinal, 1)

    def test_active_adapter_history_read_reuses_current_sdk_client_and_reconstructs_split_text(self) -> None:
        long_text = "complete-message-" * 700
        snapshot = {
            "thread": {
                "id": "thread-1",
                "turns": [
                    {
                        "id": "turn-1",
                        "itemsView": "full",
                        "items": [
                            {
                                "root": {
                                    "id": "user-1",
                                    "type": "userMessage",
                                    "content": [{"root": {"type": "text", "text": long_text}}],
                                }
                            }
                        ],
                    }
                ],
            }
        }
        client = _ReadClient(snapshot)
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        adapter.start_thread()
        request = ConversationHistoryRequest(
            ConversationSubjectKind.WORKER,
            "run/milestone/executor/1",
            ThreadIdentity("thread-1"),
            generation=1,
            attempt=1,
            page_fragments=1,
        )

        pages = []
        while True:
            page = adapter.read_conversation_history(request)
            pages.insert(0, page)
            if page.older_token is None:
                break
            request = ConversationHistoryRequest(
                request.subject_kind,
                request.subject_id,
                request.thread_id,
                request.generation,
                request.attempt,
                page_token=page.older_token,
                page_fragments=1,
            )

        reconstructed = "".join(
            message.content.text or "" for page in pages for turn in page.turns for message in turn.messages
        )
        self.assertEqual(reconstructed, long_text)
        self.assertEqual(
            {message.item_id for page in pages for turn in page.turns for message in turn.messages}, {"user-1"}
        )
        self.assertFalse(client.closed)
        self.assertEqual(len(client.start_calls), 1)
        self.assertEqual(len(client.read_calls), len(pages))

    def test_conversation_history_fails_closed_for_incomplete_and_changed_sources(self) -> None:
        partial = _ReadClient(
            {"thread": {"id": "thread-1", "turns": [{"id": "turn-1", "itemsView": "summary", "items": []}]}}
        )
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: partial, sdk=_sdk())
        request = ConversationHistoryRequest(
            ConversationSubjectKind.WORKER,
            "run/milestone/executor/1",
            ThreadIdentity("thread-1"),
            generation=1,
            attempt=1,
            page_fragments=1,
        )
        self.assertEqual(adapter.read_conversation_history(request).status, ConversationHistoryStatus.SOURCE_INCOMPLETE)

        partial.snapshot = {
            "thread": {
                "id": "thread-1",
                "updatedAt": 1,
                "turns": [
                    {
                        "id": "turn-1",
                        "itemsView": "full",
                        "items": [
                            {
                                "root": {
                                    "id": "user-1",
                                    "type": "userMessage",
                                    "content": [{"root": {"type": "text", "text": "one"}}],
                                }
                            },
                            {"root": {"id": "agent-1", "type": "agentMessage", "text": "two"}},
                        ],
                    }
                ],
            }
        }
        first = adapter.read_conversation_history(request)
        self.assertIsNotNone(first.older_token)
        partial.snapshot["thread"]["updatedAt"] = 2  # type: ignore[index]
        stale = adapter.read_conversation_history(
            ConversationHistoryRequest(
                request.subject_kind,
                request.subject_id,
                request.thread_id,
                request.generation,
                request.attempt,
                page_token=first.older_token,
                page_fragments=1,
            )
        )
        self.assertEqual(stale.status, ConversationHistoryStatus.STALE)
        self.assertEqual(stale.turns, ())

        partial.snapshot["thread"]["updatedAt"] = 1  # type: ignore[index]
        replaced_attempt = adapter.read_conversation_history(
            ConversationHistoryRequest(
                request.subject_kind,
                request.subject_id,
                request.thread_id,
                generation=2,
                attempt=request.attempt,
                page_token=first.older_token,
                page_fragments=1,
            )
        )
        self.assertEqual(replaced_attempt.status, ConversationHistoryStatus.STALE)

    def test_raw_thread_read_classifies_exact_active_writer_without_resume(self) -> None:
        client = _ReadClient(failure=InvalidRequestError())
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())

        inspection = adapter.inspect_persisted_thread(ThreadIdentity("thread-1"))

        self.assertEqual(inspection.kind, ThreadInspectionKind.ACTIVE_WRITER)
        self.assertEqual(client.read_calls, [("thread-1", True)])
        self.assertEqual(client.resume_calls, [])

    def test_raw_thread_read_rejects_unrelated_error_and_malformed_history(self) -> None:
        unrelated = InvalidRequestError()
        unrelated.message = "the thread is busy"  # type: ignore[attr-defined]
        client = _ReadClient(failure=unrelated)
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        self.assertEqual(
            adapter.inspect_persisted_thread(ThreadIdentity("thread-1")).kind,
            ThreadInspectionKind.AMBIGUOUS,
        )

        malformed = _ReadClient({"thread": {"id": "thread-1", "turns": [{"id": "turn-1"}]}})
        malformed_adapter = CodexSdkAdapter(self.config(), client_factory=lambda: malformed, sdk=_sdk())
        self.assertEqual(
            malformed_adapter.inspect_persisted_thread(ThreadIdentity("thread-1")).kind,
            ThreadInspectionKind.MALFORMED,
        )

    def test_raw_thread_read_latest_turn_is_authoritative_over_older_result(self) -> None:
        older = _history_turn("turn-1", "completed", _valid_result())
        cases = (
            ("later-active", _history_turn("turn-2", "inProgress"), ThreadInspectionKind.AMBIGUOUS),
            ("later-malformed", {"id": "turn-2", "status": "completed"}, ThreadInspectionKind.MALFORMED),
            (
                "later-invalid-agent-payload",
                {
                    "id": "turn-2",
                    "status": "completed",
                    "items": [{"root": {"type": "agentMessage", "text": {"summary": "no"}}}],
                },
                ThreadInspectionKind.MALFORMED,
            ),
            ("later-summary", _history_turn("turn-2", "completed", "summary only"), ThreadInspectionKind.MALFORMED),
            ("later-non-result", _history_turn("turn-2", "completed"), ThreadInspectionKind.IDLE_NO_RESULT),
            (
                "later-conflicting-terminal",
                _history_turn("turn-2", "completed", _valid_result(), _valid_result().replace("done", "other")),
                ThreadInspectionKind.AMBIGUOUS,
            ),
        )
        for label, later, expected in cases:
            with self.subTest(label=label):
                client = _ReadClient(_ordered_snapshot([older, later]))
                adapter = CodexSdkAdapter(self.config(), client_factory=lambda client=client: client, sdk=_sdk())
                inspection = adapter.inspect_persisted_thread(ThreadIdentity("thread-1"))
                self.assertEqual(inspection.kind, expected)
                self.assertIsNone(inspection.raw_result)
                self.assertEqual(client.read_calls, [("thread-1", True)])
                self.assertEqual(client.resume_calls, [])

    def test_raw_thread_read_preserves_empty_and_latest_failed_history_as_typed_facts(self) -> None:
        cases = (
            ("empty", _ordered_snapshot([]), ThreadInspectionKind.EMPTY_HISTORY, None),
            (
                "latest-failed",
                _ordered_snapshot([_history_turn("turn-failed", "failed")]),
                ThreadInspectionKind.FAILED_TURN,
                "turn-failed",
            ),
        )
        for label, snapshot, expected_kind, expected_turn in cases:
            with self.subTest(label=label):
                client = _ReadClient(snapshot)
                adapter = CodexSdkAdapter(self.config(), client_factory=lambda client=client: client, sdk=_sdk())
                inspection = adapter.inspect_persisted_thread(ThreadIdentity("thread-1"))
                self.assertEqual(inspection.kind, expected_kind)
                self.assertEqual(inspection.turn_id, expected_turn)
                self.assertIsNone(inspection.raw_result)

    def test_controller_thread_read_accepts_one_exact_latest_bundle(self) -> None:
        decision_id = ControllerDecisionId("decision/wake/recovery/controller/checkpoint")
        bundle = {
            "schema_version": 1,
            "decision_id": str(decision_id),
            "generation": 1,
            "action_id": "controller-action",
            "expected_revision": 0,
            "expected_successor_dispatch_ids": [],
            "actions": [{"kind": "acknowledge_only"}],
            "rationale": "close",
        }
        snapshot = {
            "thread": {
                "id": "thread-1",
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [{"root": {"type": "agentMessage", "text": json.dumps(bundle)}}],
                    }
                ],
            }
        }
        client = _ReadClient(snapshot)
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())

        inspection = adapter.inspect_controller_thread(
            ThreadIdentity("thread-1"), decision_id=decision_id, generation=Generation(1)
        )

        self.assertEqual(inspection.kind, ControllerThreadInspectionKind.COMPLETED)
        self.assertIsNotNone(inspection.bundle)
        self.assertEqual(inspection.bundle.action_id, "controller-action")
        self.assertEqual(client.read_calls, [("thread-1", True)])

    def test_controller_thread_read_rejects_open_or_ambiguous_agent_message_envelopes(self) -> None:
        decision_id = ControllerDecisionId("decision/wake/recovery/controller/checkpoint")
        bundle = {
            "schema_version": 1,
            "decision_id": str(decision_id),
            "generation": 1,
            "action_id": "controller-action",
            "expected_revision": 0,
            "expected_successor_dispatch_ids": [],
            "actions": [{"kind": "acknowledge_only"}],
            "rationale": "close",
        }
        text = json.dumps(bundle)
        valid_root = {"type": "agentMessage", "text": text}
        cases = (
            ("item-extra", {"root": valid_root, "extra": True}),
            ("root-extra", {"type": "agentMessage", "text": text, "extra": True}),
            ("item-missing-root", {}),
            ("root-missing-text", {"type": "agentMessage"}),
            ("unknown-type", {"type": "toolResult", "text": text}),
            (
                "extra-item",
                [
                    {"root": valid_root},
                    {"root": valid_root},
                ],
            ),
            ("malformed-text", {"type": "agentMessage", "text": {"bundle": text}}),
        )
        for label, value in cases:
            with self.subTest(label=label):
                items = value if isinstance(value, list) else [{"root": value}]
                snapshot = {
                    "thread": {
                        "id": "thread-1",
                        "turns": [{"id": "turn-1", "status": "completed", "items": items}],
                    }
                }
                client = _ReadClient(snapshot)
                adapter = CodexSdkAdapter(self.config(), client_factory=lambda client=client: client, sdk=_sdk())
                inspection = adapter.inspect_controller_thread(
                    ThreadIdentity("thread-1"), decision_id=decision_id, generation=Generation(1)
                )
                self.assertEqual(inspection.kind, ControllerThreadInspectionKind.MALFORMED)
                self.assertEqual(client.read_calls, [("thread-1", True)])

    def test_controller_snapshot_active_status_is_ambiguous_without_writer_proof(self) -> None:
        client = _ReadClient(_ordered_snapshot([_history_turn("turn-1", "inProgress")]))
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())

        inspection = adapter.inspect_controller_thread(
            ThreadIdentity("thread-1"),
            decision_id=ControllerDecisionId("decision/wake/recovery/controller/checkpoint"),
            generation=1,
        )

        self.assertEqual(inspection.kind, ControllerThreadInspectionKind.AMBIGUOUS)
        self.assertEqual(client.read_calls, [("thread-1", True)])

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

    def test_leaf_worker_binds_official_runtime_and_thread_overrides(self) -> None:
        client = FakeClient()

        class LeafSdk(SimpleNamespace):
            def CodexConfig(
                self,
                *,
                config_overrides: tuple[str, ...] = (),
                env: dict[str, str] | None = None,
                cwd: str | None = None,
            ) -> object:
                self.config = {"config_overrides": config_overrides, "env": env, "cwd": cwd}
                return self.config

            def Codex(self, *, config: object) -> FakeClient:
                return client

        sdk = LeafSdk(
            Sandbox=SimpleNamespace(read_only="sdk-read-only", workspace_write="sdk-write"),
            ApprovalMode=SimpleNamespace(deny_all="sdk-deny-all"),
            ReasoningEffort=SimpleNamespace(medium="sdk-medium"),
            SkillInput=None,
            version="0.147.0-test",
        )
        # The injected client remains typed like the stable SDK and records
        # the exact per-thread config payload.
        adapter = CodexSdkAdapter(
            CodexSdkConfig(
                model="gpt-test",
                reasoning_effort=ReasoningEffort.MEDIUM,
                sandbox=Sandbox.READ_ONLY,
                cwd=self.cwd,
                leaf_worker=True,
            ),
            sdk=sdk,
        )
        identity = adapter.start_thread()
        self.assertEqual(identity.id, "thread-1")
        self.assertEqual(
            client.start_calls[0]["config"], {"agents": {"enabled": False}, "features": {"multi_agent": False}}
        )
        self.assertEqual(sdk.config["config_overrides"], LEAF_WORKER_CONFIG_OVERRIDES)
        adapter.close()

    def test_nested_delegation_sentinel_blocks_every_lifecycle_operation(self) -> None:
        evidence = run_nested_delegation_sentinel()
        self.assertEqual(evidence["attempted"], evidence["blocked"])
        self.assertEqual(evidence["child_created"], False)
        self.assertEqual(evidence["lifecycle_mutations"], 0)
        self.assertEqual(evidence["hidden_successors"], 0)

    def test_invalid_previous_response_continuation_is_typed_post_identity_failure(self) -> None:
        client = FakeClient()

        def fail_resume(thread_id: str, **kwargs: Any) -> FakeThread:
            raise RuntimeError(
                json.dumps(
                    {
                        "type": "error",
                        "status": 400,
                        "error": {
                            "type": "invalid_request_error",
                            "message": "Invalid `previous_response_id`.",
                        },
                    }
                )
            )

        client.thread_resume = fail_resume  # type: ignore[method-assign]
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        with self.assertRaisesRegex(ResponseChainInvalid, "thread-1"):
            adapter.resume_thread(ThreadIdentity("thread-1"))

    def test_invalid_previous_response_prose_does_not_authorize_chain_replacement(self) -> None:
        client = FakeClient()

        def fail_resume(thread_id: str, **kwargs: Any) -> FakeThread:
            raise RuntimeError("400 Invalid previous_response_id")

        client.thread_resume = fail_resume  # type: ignore[method-assign]
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        with self.assertRaises(TerminalFailureAfterIdentity) as raised:
            adapter.resume_thread(ThreadIdentity("thread-1"))
        self.assertNotIsInstance(raised.exception, ResponseChainInvalid)

    def test_invalid_response_chain_during_turn_is_typed_at_sdk_boundary(self) -> None:
        client = FakeClient()
        provider_error = json.dumps(
            {
                "type": "error",
                "status": 400,
                "error": {
                    "type": "invalid_request_error",
                    "message": "Invalid `previous_response_id`.",
                },
            }
        )

        def fail_turn(input: object, **kwargs: Any) -> FakeTurn:
            raise RuntimeError(provider_error)

        client.thread.turn = fail_turn  # type: ignore[method-assign]
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        identity = adapter.start_thread()

        with self.assertRaises(ResponseChainInvalid):
            adapter.run_turn(identity, "continue")

    def test_canonical_app_server_502_is_typed_as_transient_after_identity(self) -> None:
        client = FakeClient()
        error = SimpleNamespace(
            message="unexpected status 502 Bad Gateway: Previous response owner account is unavailable; retry later.",
            codex_error_info=SimpleNamespace(root=SimpleNamespace(value="other")),
        )
        events = [
            _event("turn/started", SimpleNamespace(turn=SimpleNamespace(id="turn-502"))),
            _event(
                "turn/completed",
                SimpleNamespace(
                    turn=SimpleNamespace(
                        id="turn-502",
                        status=SimpleNamespace(value="failed"),
                        error=error,
                    )
                ),
            ),
        ]
        client.thread.turn = lambda input, **kwargs: FakeTurn("turn-502", events)
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        identity = adapter.start_thread()

        with self.assertRaises(TransientFailureAfterIdentity):
            adapter.run_turn(identity, "continue")

    def test_canonical_retryable_provider_statuses_are_typed_after_identity(self) -> None:
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                client = FakeClient()
                error = SimpleNamespace(
                    message=f"unexpected status {status} Provider failure: retry later.",
                    codex_error_info=SimpleNamespace(root=SimpleNamespace(value="other")),
                )
                events = [
                    _event("turn/started", SimpleNamespace(turn=SimpleNamespace(id=f"turn-{status}"))),
                    _event(
                        "turn/completed",
                        SimpleNamespace(
                            turn=SimpleNamespace(
                                id=f"turn-{status}",
                                status=SimpleNamespace(value="failed"),
                                error=error,
                            )
                        ),
                    ),
                ]
                client.thread.turn = lambda input, events=events, status=status, **kwargs: FakeTurn(
                    f"turn-{status}", events
                )
                adapter = CodexSdkAdapter(self.config(), client_factory=lambda client=client: client, sdk=_sdk())
                identity = adapter.start_thread()

                with self.assertRaises(TransientFailureAfterIdentity):
                    adapter.run_turn(identity, "continue")

    def test_typed_usage_limit_surfaces_once_without_prompt_replay(self) -> None:
        client = FakeClient()
        calls: list[str] = []
        turn_ids: list[str] = []

        def limited_turn(input: object, **kwargs: Any) -> FakeTurn:
            turn_id = f"turn-limit-{len(calls) + 1}"
            calls.append(turn_id)
            error = SimpleNamespace(
                message=(
                    "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
                    "visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 11:59 PM."
                ),
                codex_error_info=SimpleNamespace(root=SimpleNamespace(value="usageLimitExceeded")),
            )
            return FakeTurn(
                turn_id,
                [
                    _event("turn/started", SimpleNamespace(turn=SimpleNamespace(id=turn_id))),
                    _event(
                        "turn/completed",
                        SimpleNamespace(
                            turn=SimpleNamespace(
                                id=turn_id,
                                status=SimpleNamespace(value="failed"),
                                error=error,
                            )
                        ),
                    ),
                ],
            )

        client.thread.turn = limited_turn  # type: ignore[method-assign]
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        identity = adapter.start_thread()

        with self.assertRaises(TemporaryRateLimitAfterIdentity) as raised:
            adapter.run_turn(identity, "continue", turn_callback=turn_ids.append)

        self.assertEqual(len(calls), 1)
        self.assertEqual(turn_ids, calls)
        self.assertRegex(raised.exception.retry_at, r"\A[0-9T:+-]+Z\Z")

    def test_typed_usage_limit_without_reset_is_not_automatically_retried(self) -> None:
        client = FakeClient()
        calls = 0

        def quota_turn(input: object, **kwargs: Any) -> FakeTurn:
            nonlocal calls
            calls += 1
            error = SimpleNamespace(
                message="You've hit your usage limit. Purchase credits to continue.",
                codex_error_info=SimpleNamespace(root=SimpleNamespace(value="usageLimitExceeded")),
            )
            return FakeTurn(
                "turn-quota",
                [
                    _event("turn/started", SimpleNamespace(turn=SimpleNamespace(id="turn-quota"))),
                    _event(
                        "turn/completed",
                        SimpleNamespace(
                            turn=SimpleNamespace(
                                id="turn-quota",
                                status=SimpleNamespace(value="failed"),
                                error=error,
                            )
                        ),
                    ),
                ],
            )

        client.thread.turn = quota_turn  # type: ignore[method-assign]
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        identity = adapter.start_thread()

        with self.assertRaises(TerminalFailureAfterIdentity) as raised:
            adapter.run_turn(identity, "continue")

        self.assertNotIsInstance(raised.exception, TransientFailureAfterIdentity)
        self.assertEqual(calls, 1)

    def test_unstructured_or_nonretryable_status_prose_remains_terminal(self) -> None:
        messages = (
            "the provider mentioned 502 while explaining another error",
            "unexpected status 404 Not Found: Previous response owner account is unavailable; retry later.",
        )
        for index, message in enumerate(messages):
            with self.subTest(message=message):
                client = FakeClient()
                error = SimpleNamespace(
                    message=message,
                    codex_error_info=SimpleNamespace(root=SimpleNamespace(value="other")),
                )
                events = [
                    _event("turn/started", SimpleNamespace(turn=SimpleNamespace(id=f"turn-terminal-{index}"))),
                    _event(
                        "turn/completed",
                        SimpleNamespace(
                            turn=SimpleNamespace(
                                id=f"turn-terminal-{index}",
                                status=SimpleNamespace(value="failed"),
                                error=error,
                            )
                        ),
                    ),
                ]
                client.thread.turn = lambda input, events=events, index=index, **kwargs: FakeTurn(
                    f"turn-terminal-{index}", events
                )
                adapter = CodexSdkAdapter(self.config(), client_factory=lambda client=client: client, sdk=_sdk())
                identity = adapter.start_thread()

                with self.assertRaises(TerminalFailureAfterIdentity) as raised:
                    adapter.run_turn(identity, "continue")
                self.assertNotIsInstance(raised.exception, TransientFailureAfterIdentity)

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

    def test_worker_repairs_schema_envelope_twice_without_repeating_work_prompt(self) -> None:
        client = FakeClient()
        prompts: list[object] = []
        responses = iter((json.dumps({"ok": "wrong"}), json.dumps({"ok": "still wrong"}), json.dumps({"ok": True})))

        def sequenced_turn(input: object, **kwargs: Any) -> FakeTurn:
            prompts.append(input)
            return FakeTurn(f"turn-{len(prompts)}", _turn_events(f"turn-{len(prompts)}", text=next(responses)))

        client.thread.turn = sequenced_turn  # type: ignore[method-assign]
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        identity = adapter.start_thread()

        observation = _run_bounded_schema_turn(adapter, identity, "implement milestone", SCHEMA)

        self.assertEqual(observation.structured_output, {"ok": True})
        self.assertEqual(len(prompts), SCHEMA_CORRECTION_BUDGET + 1)
        self.assertEqual(prompts[0], "implement milestone")
        self.assertTrue(all("Do not repeat repository work" in str(item) for item in prompts[1:]))

    def test_worker_stops_after_schema_correction_budget(self) -> None:
        client = FakeClient()
        prompts: list[object] = []

        def invalid_turn(input: object, **kwargs: Any) -> FakeTurn:
            prompts.append(input)
            return FakeTurn(
                f"turn-{len(prompts)}",
                _turn_events(f"turn-{len(prompts)}", text=json.dumps({"ok": "wrong"})),
            )

        client.thread.turn = invalid_turn  # type: ignore[method-assign]
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        identity = adapter.start_thread()

        with self.assertRaises(SchemaOutputInvalid):
            _run_bounded_schema_turn(adapter, identity, "implement milestone", SCHEMA)
        self.assertEqual(len(prompts), SCHEMA_CORRECTION_BUDGET + 1)

    def test_recursive_schema_rejects_nested_unexpected_fields(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "payload": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                }
            },
            "required": ["payload"],
            "additionalProperties": False,
        }
        with self.assertRaises(TerminalFailureAfterIdentity):
            _decode_schema_output(json.dumps({"payload": {"ok": True, "evil": 1}}), schema)

    def test_malformed_recursive_schema_is_rejected(self) -> None:
        malformed = {
            "type": "object",
            "properties": {"payload": {"type": "object", "required": "payload"}},
            "required": ["payload"],
        }
        with self.assertRaises(TerminalFailureAfterIdentity):
            _decode_schema_output(json.dumps({"payload": {}}), malformed)

    def test_strict_nested_object_and_array_output_is_validated_recursively(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}, "ok": {"type": "boolean"}},
                        "required": ["name", "ok"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        }
        self.assertEqual(
            _decode_schema_output(json.dumps({"items": [{"name": "one", "ok": True}]}), schema),
            {"items": [{"name": "one", "ok": True}]},
        )
        with self.assertRaises(TerminalFailureAfterIdentity):
            _decode_schema_output(json.dumps({"items": [{"name": "one"}]}), schema)
        with self.assertRaises(TerminalFailureAfterIdentity):
            _decode_schema_output(json.dumps({"items": [{"name": "one", "ok": True, "extra": 1}]}), schema)

    def test_model_facing_result_accepts_nullable_next_action_union(self) -> None:
        schema = model_facing_result_schema()
        base = {
            "schema_version": 1,
            "status": "completed",
            "summary": "done",
            "changed_surfaces": [],
            "validations": [],
            "durable_status": "completed",
        }
        for next_action in (None, "inspect status"):
            with self.subTest(next_action=next_action):
                payload = {**base, "next_action": next_action}
                self.assertEqual(_decode_schema_output(json.dumps(payload), schema), payload)

    def test_canonical_constraints_and_semantic_maps_are_enforced_by_sdk_boundary(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "const": "owned"},
                "status": {"type": "string", "enum": ["ready", "done"]},
                "label": {"type": "string", "minLength": 3, "maxLength": 8, "pattern": r"^item-$"},
                "values": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 1,
                    "maxItems": 2,
                    "uniqueItems": True,
                },
                "surfaces": {
                    "type": "object",
                    "minProperties": 1,
                    "maxProperties": 2,
                    "propertyNames": {"type": "string", "pattern": r"^surface$"},
                    "additionalProperties": {"type": "string", "enum": ["mutable", "protected"]},
                },
            },
            "required": ["kind", "status", "label", "values", "surfaces"],
            "additionalProperties": False,
        }
        valid = {
            "kind": "owned",
            "status": "ready",
            "label": "item-",
            "values": [1, 2],
            "surfaces": {"surface": "mutable"},
        }
        self.assertEqual(_decode_schema_output(json.dumps(valid), schema), valid)
        invalid_values = (
            {**valid, "kind": "other"},
            {**valid, "status": "unknown"},
            {**valid, "label": "item-x"},
            {**valid, "values": [1, 1]},
            {**valid, "values": []},
            {**valid, "surfaces": {"wrong": "mutable"}},
            {**valid, "surfaces": {"surface": "unknown"}},
        )
        for payload in invalid_values:
            with self.subTest(payload=payload), self.assertRaises(TerminalFailureAfterIdentity):
                _decode_schema_output(json.dumps(payload), schema)

    def test_structured_output_matches_draft_2020_12_oracle_table(self) -> None:
        nullable_string = deepcopy(model_facing_result_schema()["properties"]["next_action"])
        cases = (
            ("nullable-null", nullable_string, None, True),
            ("nullable-valid-string", nullable_string, "inspect status", True),
            ("nullable-empty-string", nullable_string, "", False),
            ("nullable-whitespace-string", nullable_string, "   ", False),
            ("nullable-nul-string", nullable_string, "bad\x00value", False),
            ("numeric-const-equivalence", {"type": "number", "const": 1}, 1.0, True),
            ("numeric-enum-membership", {"type": "number", "enum": [1, 2]}, 1.0, True),
            ("numeric-enum-miss", {"type": "number", "enum": [1, 2]}, 3, False),
            (
                "numeric-unique-items",
                {"type": "array", "items": {"type": "number"}, "uniqueItems": True},
                [1, 1.0],
                False,
            ),
            (
                "boolean-number-distinction",
                {"type": "array", "items": {"type": ["boolean", "number"]}, "uniqueItems": True},
                [True, 1],
                True,
            ),
            (
                "nested-array-object-equality",
                {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "values": {"type": "array", "items": {"type": "number"}},
                        },
                        "required": ["label", "values"],
                        "additionalProperties": False,
                    },
                },
                [{"label": "same", "values": [1, 2]}, {"values": [1.0, 2.0], "label": "same"}],
                False,
            ),
        )
        for label, leaf_schema, value, expected in cases:
            schema = {
                "type": "object",
                "properties": {"value": leaf_schema},
                "required": ["value"],
                "additionalProperties": False,
            }
            payload = {"value": value}
            with self.subTest(label=label):
                oracle_valid = Draft202012Validator(schema).is_valid(payload)
                self.assertEqual(oracle_valid, expected)
                try:
                    decoded = _decode_schema_output(json.dumps(payload), schema)
                except TerminalFailureAfterIdentity:
                    domain_valid = False
                else:
                    domain_valid = True
                    self.assertEqual(decoded, payload)
                self.assertEqual(domain_valid, oracle_valid)

        numeric_enum = {
            "type": "object",
            "properties": {"value": {"type": "number", "enum": [1, 1.0]}},
            "required": ["value"],
            "additionalProperties": False,
        }
        oracle_values_are_duplicates = not Draft202012Validator({"type": "array", "uniqueItems": True}).is_valid(
            [1, 1.0]
        )
        self.assertTrue(oracle_values_are_duplicates)
        with self.assertRaises(TerminalFailureAfterIdentity):
            _decode_schema_output('{"value":1}', numeric_enum)

    def test_four_hundred_digit_number_is_preserved_without_unknown_sdk_failure(self) -> None:
        large_integer = int("9" * 400)
        schema = {
            "type": "object",
            "properties": {
                "value": {"type": "number"},
                "constant": {"type": "number", "const": large_integer},
                "choice": {"type": "number", "enum": [large_integer]},
                "separated": {
                    "type": "array",
                    "items": {"type": ["boolean", "number"]},
                    "uniqueItems": True,
                },
            },
            "required": ["value", "constant", "choice", "separated"],
            "additionalProperties": False,
            "minProperties": 4,
            "maxProperties": 4,
        }
        payload = {
            "value": large_integer,
            "constant": large_integer,
            "choice": large_integer,
            "separated": [True, large_integer],
        }
        encoded = json.dumps(payload)

        self.assertTrue(Draft202012Validator(schema).is_valid(payload))
        self.assertIsNone(validate_structured_output(payload, schema))
        self.assertEqual(_decode_schema_output(encoded, schema), payload)

        client = FakeClient()
        client.thread.turn = lambda input, **kwargs: FakeTurn(
            "turn-large-integer",
            _turn_events("turn-large-integer", text=encoded),
        )
        adapter = CodexSdkAdapter(self.config(), client_factory=lambda: client, sdk=_sdk())
        identity = adapter.start_thread()
        observation = adapter.run_turn(identity, "provider-free large integer", output_schema=schema)

        self.assertEqual(observation.structured_output, payload)
        self.assertIs(type(observation.structured_output["value"]), int)
        self.assertEqual(observation.structured_output["value"], large_integer)

    def test_production_schema_authority_does_not_import_development_oracle(self) -> None:
        domain_source = (Path(codex_sdk_module.__file__).parents[1] / "domain.py").read_text(encoding="utf-8")
        self.assertNotIn("jsonschema", domain_source)

    def test_missing_schema_bounded_output_is_rejected(self) -> None:
        with self.assertRaises(TerminalFailureAfterIdentity):
            _decode_schema_output(None, SCHEMA)

    def test_sdk_post_validation_rejects_open_or_ambiguous_schemas(self) -> None:
        invalid = (
            {"type": "object", "properties": {}, "required": []},
            {"type": "object", "properties": {}, "required": [], "additionalProperties": True},
            {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": [],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"items": {"type": "array"}},
                "required": ["items"],
                "additionalProperties": False,
            },
        )
        for schema in invalid:
            with self.subTest(schema=schema), self.assertRaises(TerminalFailureAfterIdentity):
                _decode_schema_output("{}", schema)

    def test_strict_decoder_rejects_constants_and_large_nonfinite_exponents(self) -> None:
        for payload in ('{"ok":NaN}', '{"ok":Infinity}', '{"ok":-Infinity}', '{"ok":1e999}'):
            with self.subTest(payload=payload), self.assertRaises(StrictJSONError):
                strict_json_loads(payload)

    def test_strict_decoder_rejects_duplicate_keys_at_every_nesting_depth(self) -> None:
        for payload in (
            '{"ok":true,"ok":false}',
            '{"outer":{"ok":true,"ok":false}}',
            '{"items":[{"ok":true,"ok":false}]}',
        ):
            with self.subTest(payload=payload), self.assertRaises(TerminalFailureAfterIdentity):
                _decode_schema_output(payload, SCHEMA)

    def test_strict_decoder_accepts_finite_unicode_and_bounded_large_exponent(self) -> None:
        payload = '{"ok":true,"\u03bb":1e308}'
        decoded = strict_json_loads(payload)
        self.assertEqual(decoded, {"ok": True, "λ": 1e308})
        with self.assertRaises(StrictJSONError):
            strict_json_loads('{"ok":"\\ud800"}')
        with self.assertRaises(TerminalFailureAfterIdentity):
            _decode_schema_output('{"ok":"\ud800"}', SCHEMA)

    def test_strict_decoder_requires_utf8_without_bom_or_ambiguous_encoding(self) -> None:
        valid = '{"ok":"café"}'.encode()
        self.assertEqual(strict_json_loads(valid), {"ok": "café"})
        for payload in (
            b"\xef\xbb\xbf{}",
            b"\xff\xfe{}",
            b"\xfe\xff{}",
            b"\xff\xfe\x00\x00{}",
            b"\x00\x00\xfe\xff{}",
            b"\xc0\xaf{}",
            b"\xff{}",
        ):
            with self.subTest(payload=payload), self.assertRaises(StrictJSONError):
                strict_json_loads(payload)
        with self.assertRaises(StrictJSONError):
            strict_json_loads("\ufeff{}")
        with self.assertRaises(TerminalFailureAfterIdentity):
            _decode_schema_output('\ufeff{"ok":true}', SCHEMA)


if __name__ == "__main__":
    unittest.main()
