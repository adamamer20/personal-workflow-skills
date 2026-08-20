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

    def pre_list(self, *, session: str = "session-1", turn: str = "turn-2") -> dict[str, Any]:
        return self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "list_threads",
                "turn_id": turn,
                "session_id": session,
                "tool_input": {},
            }
        )

    def post_list(
        self, response: Any, *, session: str = "session-1", turn: str = "turn-2"
    ) -> dict[str, Any]:
        return self.run_hook(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "list_threads",
                "turn_id": turn,
                "session_id": session,
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
        missing_environment = self.pre_create(target={"type": "project", "projectId": "project-1"})
        top_level_only = self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "create_thread",
                "turn_id": "turn-top-level",
                "session_id": "session-1",
                "tool_input": {
                    "prompt": "Implement exactly one bounded milestone.",
                    "title": "M5 hooks",
                    "projectId": "project-1",
                    "model": "gpt-5.6-luna",
                    "thinking": "xhigh",
                },
            }
        )
        unsupported_environment = self.pre_create(
            target={
                "type": "project",
                "projectId": "project-1",
                "environment": {"type": "remote"},
            }
        )
        malformed_starting_state = self.pre_create(
            target={
                "type": "project",
                "projectId": "project-1",
                "environment": {
                    "type": "worktree",
                    "startingState": {"type": "branch", "branchName": "main", "onMissing": []},
                },
            }
        )
        malformed = self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "create_thread",
                "turn_id": "turn-1",
                "tool_input": self.create_input(target=["not", "an", "object"]),
            }
        )
        self.assertEqual(conflict["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(missing_environment["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(top_level_only["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(unsupported_environment["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(malformed_starting_state["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(malformed["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_same_turn_and_unresolved_duplicates_are_denied(self) -> None:
        self.assertEqual(self.pre_create()["hookSpecificOutput"]["permissionDecision"], "allow")
        same_turn = self.pre_create()
        next_turn = self.pre_create(turn="turn-2")
        changed_payload = self.pre_create(
            turn="turn-3",
            title="changed title",
            prompt="changed prompt",
            model="gpt-5.6-sol",
        )
        self.assertEqual(same_turn["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(next_turn["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(changed_payload["hookSpecificOutput"]["permissionDecision"], "deny")

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
        self.assertEqual(
            self.run_hook(
                {"hook_event_name": "Stop", "session_id": "session-1", "stop_hook_active": False}
            ),
            {},
        )
        self.assertEqual(
            self.run_hook(
                {"hook_event_name": "SessionStart", "source": "resume", "session_id": "session-1"}
            ),
            {},
        )

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
        self.assertEqual(
            self.run_hook(
                {"hook_event_name": "Stop", "session_id": "session-1", "stop_hook_active": False}
            ),
            {},
        )
        self.assertEqual(
            self.run_hook(
                {"hook_event_name": "SessionStart", "source": "resume", "session_id": "session-1"}
            ),
            {},
        )

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

    def test_unresolved_ledger_saturation_never_evicts_recovery(self) -> None:
        for index in range(32):
            output = self.pre_create(
                session=f"session-{index}",
                turn=f"turn-{index}",
                title=f"M5 hooks {index}",
            )
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        saturated = self.pre_create(session="session-32", turn="turn-32", title="M5 hooks 32")
        self.assertEqual(saturated["hookSpecificOutput"]["permissionDecision"], "deny")
        state = self.state()
        self.assertEqual(len(state["attempts"]), 32)
        self.assertEqual({item["session_id"] for item in state["attempts"]}, {f"session-{i}" for i in range(32)})
        self.assertTrue(all(item["status"] == "pending" for item in state["attempts"]))

    def test_supported_target_variants_require_exact_identity(self) -> None:
        def reconcile(target: dict[str, Any], title: str, candidate: dict[str, Any]) -> str:
            self.assertEqual(
                self.pre_create(target=target, title=title)["hookSpecificOutput"]["permissionDecision"],
                "allow",
            )
            self.post_create({"error": "timeout"}, tool_input=self.create_input(target=target, title=title))
            self.assertEqual(self.pre_list()["hookSpecificOutput"]["permissionDecision"], "allow")
            self.post_list({"threads": [candidate]})
            return self.state()["attempts"][-1]["status"]

        branch_a = {
            "type": "project",
            "projectId": "project-1",
            "environment": {
                "type": "worktree",
                "startingState": {"type": "branch", "branchName": "branch-a", "onMissing": "error"},
            },
        }
        candidate_a = {
            "threadId": "thread-a",
            "title": "branch-a",
            "type": "project",
            "projectId": "project-1",
            "environment": branch_a["environment"],
        }
        self.assertEqual(reconcile(branch_a, "branch-a", candidate_a), "confirmed")

        branch_b = {
            "type": "project",
            "projectId": "project-1",
            "environment": {
                "type": "worktree",
                "startingState": {"type": "branch", "branchName": "branch-b", "onMissing": "error"},
            },
        }
        self.assertEqual(reconcile(branch_b, "branch-b", candidate_a), "not_found")

        projectless = {"type": "projectless", "directoryName": "handoff-one"}
        projectless_candidate = {
            "threadId": "thread-dir",
            "title": "directory",
            "type": "projectless",
            "directoryName": "handoff-two",
        }
        self.assertEqual(reconcile(projectless, "directory", projectless_candidate), "not_found")

        cloud = {"type": "chatgptWorkCloud", "projectId": "cloud-one"}
        cloud_candidate = {
            "threadId": "thread-cloud",
            "title": "cloud",
            "type": "chatgptWorkCloud",
            "projectId": "cloud-two",
        }
        self.assertEqual(reconcile(cloud, "cloud", cloud_candidate), "not_found")

    def test_reconciliation_rejects_unaddressable_or_lossy_candidates(self) -> None:
        target = self.create_input()["target"]
        self.pre_create(title="identity fields")
        self.post_create({"error": "timeout"}, tool_input=self.create_input(title="identity fields"))
        self.assertEqual(self.pre_list()["hookSpecificOutput"]["permissionDecision"], "allow")
        response = {
            "threads": [
                None,
                "not a thread",
                {"threadId": "missing-target", "title": "identity fields"},
                {
                    "title": "identity fields",
                    "type": "project",
                    "projectId": target["projectId"],
                    "environment": target["environment"],
                },
            ]
        }
        self.post_list(response)
        self.assertEqual(self.state()["attempts"][-1]["status"], "ambiguous")
        self.assertNotIn("peer_id", self.state()["attempts"][-1])

    def test_title_identity_avoids_prefix_collision_and_flags_normalisation(self) -> None:
        long_title_a = "L" * 180 + "A"
        long_title_b = "L" * 180 + "B"
        self.pre_create(title=long_title_a)
        self.post_create({"error": "timeout"}, tool_input=self.create_input(title=long_title_a))
        self.assertEqual(self.pre_list()["hookSpecificOutput"]["permissionDecision"], "allow")
        self.post_list(
            {
                "threads": [
                    {
                        "threadId": "wrong-long-title",
                        "title": long_title_b,
                        "type": "project",
                        "projectId": "project-1",
                        "environment": {"type": "worktree"},
                    }
                ]
            }
        )
        self.assertEqual(self.state()["attempts"][-1]["status"], "not_found")

        normalized_title = "Normalize me"
        self.pre_create(turn="turn-3", title=normalized_title)
        self.post_create(
            {"error": "timeout"},
            turn="turn-3",
            tool_input=self.create_input(title=normalized_title),
        )
        self.assertEqual(self.pre_list(turn="turn-4")["hookSpecificOutput"]["permissionDecision"], "allow")
        self.post_list(
            {
                "threads": [
                    {
                        "threadId": "normalized-title",
                        "title": f" {normalized_title} ",
                        "type": "project",
                        "projectId": "project-1",
                        "environment": {"type": "worktree"},
                    }
                ]
            },
            turn="turn-4",
        )
        self.assertEqual(self.state()["attempts"][-1]["status"], "ambiguous")

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
