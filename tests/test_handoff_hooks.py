from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "plugins" / "personal-workflow-skills" / "hooks" / "lifecycle.py"


class HandoffHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="handoff-hooks-")
        self.data = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_hook(self, event: dict[str, Any], *, use_data: bool = True) -> dict[str, Any]:
        environment = os.environ.copy()
        if use_data:
            environment["PLUGIN_DATA"] = str(self.data)
        else:
            environment.pop("PLUGIN_DATA", None)
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(event),
            text=True,
            capture_output=True,
            env=environment,
            check=True,
        )
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def state(self) -> dict[str, Any]:
        return json.loads((self.data / "native-thread-handoff.json").read_text())

    @staticmethod
    def create_input(**overrides: Any) -> dict[str, Any]:
        value: dict[str, Any] = {
            "prompt": "Implement exactly one bounded milestone.",
            "title": "M5 hooks",
            "target": {"type": "project", "projectId": "project-1", "environment": {"type": "worktree"}},
            "model": "gpt-5.6-luna",
            "thinking": "xhigh",
        }
        value.update(overrides)
        return value

    def pre_create(
        self,
        *,
        turn: str = "turn-1",
        session: str = "session-1",
        use_data: bool = True,
        **overrides: Any,
    ) -> dict[str, Any]:
        return self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "create_thread",
                "turn_id": turn,
                "session_id": session,
                "tool_input": self.create_input(**overrides),
            },
            use_data=use_data,
        )

    def post_create(
        self,
        response: Any,
        *,
        turn: str = "turn-1",
        session: str = "session-1",
        tool_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.run_hook(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "create_thread",
                "turn_id": turn,
                "session_id": session,
                "tool_input": tool_input or self.create_input(),
                "tool_response": response,
            }
        )

    def test_safe_top_level_project_repair(self) -> None:
        original_shape = {
            "prompt": "Implement exactly one bounded milestone.",
            "title": "M5 hooks",
            "projectId": "project-1",
            "target": {"type": "project", "environment": {"type": "worktree"}},
            "model": "gpt-5.6-luna",
            "thinking": "xhigh",
        }
        output = self.pre_create(
            target=original_shape["target"],
            projectId=original_shape["projectId"],
        )
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["permissionDecision"], "allow")
        self.assertNotIn("projectId", specific["updatedInput"])
        self.assertEqual(specific["updatedInput"]["target"]["projectId"], "project-1")
        self.post_create('{"error":"timeout"}', tool_input=original_shape)
        self.assertEqual(self.state()["attempts"][-1]["status"], "uncertain")

    def test_conflicting_or_malformed_targets_are_denied(self) -> None:
        conflict = self.pre_create(target={"type": "project", "projectId": "project-2"}, projectId="project-1")
        malformed = self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "create_thread",
                "turn_id": "turn-1",
                "tool_input": self.create_input(target=["not", "an", "object"]),
            }
        )
        self.assertEqual(conflict["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(malformed["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_same_turn_and_unresolved_duplicates_are_denied(self) -> None:
        self.assertEqual(self.pre_create()["hookSpecificOutput"]["permissionDecision"], "allow")
        same_turn = self.pre_create()
        next_turn = self.pre_create(turn="turn-2")
        self.assertEqual(same_turn["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(next_turn["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_confirmed_and_queued_results_clear_recovery(self) -> None:
        self.pre_create()
        self.post_create('{"threadId":"thread-1","hostId":"local"}')
        self.assertEqual(self.state()["attempts"][-1]["status"], "confirmed")
        self.pre_create(turn="turn-2")
        self.post_create('{"clientThreadId":"client-1"}', turn="turn-2")
        self.assertEqual(self.state()["attempts"][-1]["status"], "queued")
        self.assertEqual(self.state()["attempts"][-1]["result_classification"], "queued")

    def test_error_is_uncertain_and_allows_one_exact_reconciliation(self) -> None:
        self.pre_create()
        output = self.post_create({"error": "server failure", "secret": "must not persist"})
        self.assertIn("exactly one", output["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.state()["attempts"][-1]["status"], "uncertain")
        self.assertEqual(self.state()["attempts"][-1]["result_classification"], "error")
        listed = self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "list_threads",
                "turn_id": "turn-2",
                "session_id": "session-1",
                "tool_input": {"limit": 10},
            }
        )
        self.assertEqual(listed["hookSpecificOutput"]["permissionDecision"], "allow")
        second = self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "list_threads",
                "turn_id": "turn-2",
                "session_id": "session-1",
                "tool_input": {"limit": 10},
            }
        )
        self.assertEqual(second["hookSpecificOutput"]["permissionDecision"], "deny")
        response = {
            "threads": [
                {
                    "threadId": "thread-found",
                    "title": "M5 hooks",
                    "projectId": "project-1",
                    "environment": {"type": "worktree"},
                },
                {"threadId": "wrong", "title": "other", "projectId": "project-1"},
            ]
        }
        reconciliation = self.run_hook(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "list_threads",
                "turn_id": "turn-2",
                "session_id": "session-1",
                "tool_response": json.dumps(response),
            }
        )
        self.assertIn("exactly one", reconciliation["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.state()["attempts"][-1]["status"], "confirmed")
        self.assertEqual(self.state()["attempts"][-1]["reconciliation_classification"], "found")
        self.assertNotIn("must not persist", (self.data / "native-thread-handoff.json").read_text())

    def test_reconciliation_not_found_and_ambiguous_are_terminal(self) -> None:
        self.pre_create()
        self.post_create({"error": "unknown project"})
        self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "list_threads",
                "session_id": "session-1",
                "tool_input": {},
            }
        )
        self.run_hook(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "list_threads",
                "session_id": "session-1",
                "tool_response": json.dumps({"threads": []}),
            }
        )
        self.assertEqual(self.state()["attempts"][-1]["status"], "not_found")
        self.assertEqual(self.state()["attempts"][-1]["reconciliation_classification"], "not_found")

        self.pre_create(turn="turn-2")
        self.post_create({"error": "unknown project"}, turn="turn-2")
        self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "list_threads",
                "session_id": "session-1",
                "tool_input": {},
            }
        )
        self.run_hook(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "list_threads",
                "session_id": "session-1",
                "tool_response": {
                    "threads": [
                        {
                            "threadId": "a",
                            "title": "M5 hooks",
                            "projectId": "project-1",
                            "environment": {"type": "worktree"},
                        },
                        {
                            "threadId": "b",
                            "title": "M5 hooks",
                            "projectId": "project-1",
                            "environment": {"type": "worktree"},
                        },
                    ]
                },
            }
        )
        self.assertEqual(self.state()["attempts"][-1]["status"], "ambiguous")
        self.assertEqual(self.state()["attempts"][-1]["reconciliation_classification"], "ambiguous")

    def test_stop_is_bounded_and_honors_stop_hook_active(self) -> None:
        self.pre_create()
        first = self.run_hook({"hook_event_name": "Stop", "session_id": "session-1", "stop_hook_active": False})
        second = self.run_hook({"hook_event_name": "Stop", "session_id": "session-1", "stop_hook_active": False})
        active = self.run_hook({"hook_event_name": "Stop", "session_id": "session-1", "stop_hook_active": True})
        self.assertEqual(first["decision"], "block")
        self.assertNotIn("decision", second)
        self.assertNotIn("decision", active)

    def test_resume_restores_unresolved_context_once(self) -> None:
        self.pre_create()
        self.post_create({"error": "timeout"})
        first = self.run_hook({"hook_event_name": "SessionStart", "source": "resume", "session_id": "session-1"})
        second = self.run_hook({"hook_event_name": "SessionStart", "source": "resume", "session_id": "session-1"})
        self.assertIn("M5 hooks", first["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(second, {})

    def test_internal_state_error_fails_open_with_warning(self) -> None:
        output = self.pre_create(use_data=False)
        self.assertIn("warning", output["systemMessage"])

    def test_sessions_isolate_create_duplicates_and_reconciliation_reservations(self) -> None:
        self.pre_create(session="session-a")
        self.post_create('{"error":"timeout"}', session="session-a")
        allowed = self.pre_create(session="session-b", turn="turn-b")
        self.assertEqual(allowed["hookSpecificOutput"]["permissionDecision"], "allow")
        self.post_create('{"error":"timeout"}', session="session-b", turn="turn-b")

        first_a = self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "list_threads",
                "session_id": "session-a",
                "tool_input": {},
            }
        )
        first_b = self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "list_threads",
                "session_id": "session-b",
                "tool_input": {},
            }
        )
        self.assertEqual(first_a["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(first_b["hookSpecificOutput"]["permissionDecision"], "allow")
        second_a = self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "list_threads",
                "session_id": "session-a",
                "tool_input": {},
            }
        )
        second_b = self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "list_threads",
                "session_id": "session-b",
                "tool_input": {},
            }
        )
        self.assertEqual(second_a["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(second_b["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_sessions_isolate_stop_and_resume_context(self) -> None:
        self.pre_create(session="session-a")
        self.post_create('{"error":"timeout"}', session="session-a")
        stop_b = self.run_hook(
            {"hook_event_name": "Stop", "session_id": "session-b", "stop_hook_active": False}
        )
        stop_a = self.run_hook(
            {"hook_event_name": "Stop", "session_id": "session-a", "stop_hook_active": False}
        )
        self.assertNotIn("decision", stop_b)
        self.assertEqual(stop_a["decision"], "block")

        resume_b = self.run_hook(
            {"hook_event_name": "SessionStart", "source": "resume", "session_id": "session-b"}
        )
        resume_a = self.run_hook(
            {"hook_event_name": "SessionStart", "source": "resume", "session_id": "session-a"}
        )
        self.assertEqual(resume_b, {})
        self.assertIn("M5 hooks", resume_a["hookSpecificOutput"]["additionalContext"])

    def test_malformed_session_fails_open_without_state_access(self) -> None:
        self.pre_create(session="session-a")
        output = self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "create_thread",
                "turn_id": "turn-b",
                "tool_input": self.create_input(),
            }
        )
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertIn("session_id", output["systemMessage"])
        self.assertEqual(len(self.state()["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()
