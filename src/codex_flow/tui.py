"""Deterministic Textual interface for typed worker and controller control."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from functools import partial
from typing import Any, ClassVar

from textual import events, on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Static

from .control_client import ControlClientError
from .domain import ConversationHistoryPage, LiveToolState, LiveTurnKeyframe
from .ipc import IpcError
from .tui_client import TerminalUiClient, TerminalUiCommandResult, TerminalUiOfflineError
from .tui_models import DecisionView, TerminalUiSnapshot, WorkerView


class ConfirmActionScreen(ModalScreen[bool]):
    """Keyboard-accessible confirmation for one exact visible identity."""

    DEFAULT_CSS = """
    ConfirmActionScreen { align: center middle; background: $background 70%; }
    #confirm-card { width: 68; height: auto; padding: 1 2; border: heavy $warning; background: $surface; }
    #confirm-buttons { height: 3; align-horizontal: right; }
    #confirm-buttons Button { margin-left: 1; }
    """

    def __init__(self, title: str, detail: str) -> None:
        super().__init__()
        self._title = title
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-card"):
            yield Label(self._title, id="confirm-title")
            yield Static(self._detail, markup=False)
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Confirm", variant="warning", id="confirm")

    @on(Button.Pressed)
    def close_confirmation(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class SteerActionScreen(ModalScreen[str | None]):
    """Bounded steer editor with an explicit exact-turn confirmation."""

    DEFAULT_CSS = """
    SteerActionScreen { align: center middle; background: $background 70%; }
    #steer-card { width: 76; height: auto; padding: 1 2; border: heavy $accent; background: $surface; }
    #steer-text { margin: 1 0; }
    #steer-buttons { height: 3; align-horizontal: right; }
    #steer-buttons Button { margin-left: 1; }
    """

    def __init__(self, identity: str) -> None:
        super().__init__()
        self._identity = identity

    def compose(self) -> ComposeResult:
        with Vertical(id="steer-card"):
            yield Label("Confirm bounded steer")
            yield Static(self._identity, markup=False)
            yield Input(placeholder="Steer text (max 8 KiB)", id="steer-text", max_length=8192)
            with Horizontal(id="steer-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Queue steer", variant="primary", id="confirm")

    @on(Button.Pressed)
    def close_editor(self, event: Button.Pressed) -> None:
        if event.button.id != "confirm":
            self.dismiss(None)
            return
        value = self.query_one("#steer-text", Input).value
        if not value.strip():
            self.notify("Steer text is required", severity="warning")
            return
        self.dismiss(value)


class CodexFlowTerminalApp(App[None]):
    """A thin event-driven UI that has no worker lifecycle authority."""

    TITLE = "Codex Flow"
    SUB_TITLE = "Conversations and control"
    ENABLE_COMMAND_PALETTE = True
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("r", "refresh", "Refresh"),
        ("s", "steer", "Steer"),
        ("i", "interrupt", "Interrupt"),
        ("k", "claim", "Claim"),
        ("a", "acknowledge", "Ack"),
        ("p", "rearm", "Re-arm"),
        ("l", "load_older", "Load older"),
        ("[", "scroll_up", "Scroll up"),
        ("]", "scroll_down", "Scroll down"),
        ("c", "copy_resume", "Copy"),
        ("o", "open_resume", "Open"),
        ("t", "toggle_details", "Details"),
        ("d", "toggle_dark", "Theme"),
        ("q", "quit", "Quit"),
    ]
    CSS = """
    Screen { layout: vertical; background: $background; color: $text; }
    Header { background: $primary-background; color: $text; }
    #mode { height: 2; padding: 0 2; content-align: left middle; border-bottom: solid $accent; }
    #main { height: 1fr; }
    #navigation { width: 36%; min-width: 30; border-right: solid $primary; padding: 0 1; }
    #controller-summary { height: 3; padding: 0 1; border-bottom: solid $secondary; content-align: left middle; }
    .section-title { height: 1; padding: 0 1; color: $text-muted; text-style: bold; }
    ListView { height: 1fr; }
    ListItem { padding: 0 1; }
    .worker-row { height: 3; }
    .decision-row { height: 2; }
    ListItem.--highlight { background: $accent 25%; }
    #decisions { max-height: 7; }
    #detail { width: 64%; padding: 0 1; }
    #conversation-header { height: 4; border-bottom: solid $primary; padding: 0 1; content-align: left middle; }
    #conversation { height: 1fr; min-height: 5; padding: 1; overflow-y: auto; }
    #technical { display: none; height: 11; border-top: solid $secondary; padding: 0 1; overflow-y: auto; }
    .diagnostics #technical { display: block; }
    #actions { height: 3; padding: 0 1; border-top: solid $primary; content-align: left middle; text-style: bold; }
    #feedback { height: 2; padding: 0 1; border-top: solid $accent; content-align: left middle; }
    .offline #mode { border-bottom: solid $warning; }
    .no-color { background: black; color: white; }
    .no-color Header, .no-color #mode, .no-color ListItem.--highlight { background: black; color: white; }
    .no-color ListItem.--highlight Label { background: black; color: white; text-style: bold; }
    .no-color Label, .no-color ListItem { color: white; background: black; }
    .no-color #feedback { color: white; background: black; }
    .no-color #navigation, .no-color #conversation, .no-color #technical {
        border: solid white;
    }
    .no-color #mode, .no-color #conversation-header, .no-color #actions, .no-color #feedback {
        border: none; border-bottom: solid white;
    }
    .narrow #main { layout: vertical; }
    .narrow #navigation { width: 100%; height: 38%; border-right: none; border-bottom: solid $primary; }
    .narrow #detail { width: 100%; height: 62%; }
    .narrow #controller-summary { height: 2; }
    .narrow .worker-row { height: 2; }
    .narrow #decisions { max-height: 2; }
    .narrow #conversation-header { height: 2; }
    .narrow #conversation { padding: 0 1; min-height: 4; }
    .narrow.diagnostics #technical { height: 4; }
    .narrow Footer { display: none; }
    .narrow #feedback { height: 2; }
    .no-color.narrow #navigation { border: none; border-bottom: solid white; }
    .no-color.narrow #feedback { border: none; border-top: solid white; }
    """

    def __init__(
        self,
        client: TerminalUiClient,
        *,
        resume_handler: Callable[[str], str] | None = None,
        no_color: bool = False,
        refresh_on_mount: bool = True,
    ) -> None:
        super().__init__()
        self.client = client
        self.resume_handler = resume_handler
        self.no_color = no_color
        self.refresh_on_mount = refresh_on_mount
        self._worker_ids: list[str] = []
        self._decision_ids: list[str] = []
        self._context_actions = "R Refresh · O Open · T Details"
        self._live_target: str | None = None
        self.client.add_live_listener(self._live_frame_received)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("Connecting to local supervisor…", id="mode", markup=False)
        with Horizontal(id="main"):
            with Vertical(id="navigation"):
                yield Static("CONTROLLER\n└ Workers and their conversations", id="controller-summary", markup=False)
                yield Static("WORKER CONVERSATIONS", classes="section-title")
                yield ListView(id="workers")
                yield Static("NEEDS YOUR ATTENTION", classes="section-title")
                yield ListView(id="decisions")
            with Vertical(id="detail"):
                yield Static("Select a worker conversation", id="conversation-header", markup=False)
                yield Static("No conversation activity selected", id="conversation", markup=False)
                yield Static("Technical details are hidden · T to show", id="technical", markup=False)
                yield Static("R Refresh · T Technical details", id="actions", markup=False)
        yield Static("Ready · Tab moves focus · ^P all commands", id="feedback", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.set_class(self.size.width < 100, "narrow")
        if self.no_color:
            self.add_class("no-color")
        if self.refresh_on_mount:
            await self._refresh()
        else:
            self._render_snapshot(self.client.snapshot)

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 100, "narrow")

    async def _refresh(self) -> None:
        snapshot = await self.client.refresh()
        loader = getattr(self.client, "load_conversation", None)
        if callable(loader) and snapshot.workers:
            try:
                await loader(worker_id=snapshot.workers[0].dispatch_id)
            except (ControlClientError, TerminalUiOfflineError, ValueError, OSError):
                pass
        self._render_snapshot(snapshot)
        self._feedback("Snapshot refreshed" if snapshot.connected else "Supervisor unavailable · offline read-only")

    def _render_snapshot(self, snapshot: TerminalUiSnapshot) -> None:
        self.set_class(not snapshot.connected, "offline")
        self.query_one("#mode", Static).update(
            f"{snapshot.mode_label}  ·  {len(snapshot.workers)} worker conversation(s)  ·  "
            f"{len(snapshot.decisions)} item(s) need attention"
        )
        worker_list = self.query_one("#workers", ListView)
        decision_list = self.query_one("#decisions", ListView)
        selected_worker_id = self._selected_worker_id()
        selected_decision_id = self._selected_decision_id()
        worker_list.clear()
        decision_list.clear()
        self._worker_ids = [item.dispatch_id for item in snapshot.workers]
        self._decision_ids = [item.decision_id for item in snapshot.decisions]
        controllers = len({item.run_id for item in snapshot.workers})
        self.query_one("#controller-summary", Static).update(
            f"CONTROLLER · {controllers or 1} workflow{'s' if controllers != 1 else ''}\n"
            f"└ {len(snapshot.workers)} worker conversation{'s' if len(snapshot.workers) != 1 else ''}"
        )
        for item in snapshot.workers:
            worker_list.append(
                ListItem(
                    Label(
                        f"└ {item.role_label} · {item.state_label}\n"
                        f"  {item.task_label} · updated {item.activity_age} ago"
                    ),
                    classes="worker-row",
                )
            )
        for item in snapshot.decisions:
            decision_list.append(ListItem(Label(f"! {item.kind_label}\n  {item.state_label}"), classes="decision-row"))
        worker_list.index = self._restored_index(selected_worker_id, self._worker_ids)
        decision_list.index = self._restored_index(selected_decision_id, self._decision_ids)
        if self.focused is decision_list and decision_list.index is not None:
            self._show_decision(snapshot.decisions[decision_list.index])
        elif worker_list.index is not None:
            self._show_worker(snapshot.workers[worker_list.index])
        elif decision_list.index is not None:
            self._show_decision(snapshot.decisions[decision_list.index])
        else:
            self.query_one("#conversation-header", Static).update("No worker conversations available")
            self.query_one("#conversation", Static).update(
                "Refresh to check for work. This screen never starts or polls workers on its own."
            )
            self.query_one("#technical", Static).update("No typed diagnostic snapshot is available.")
            self.query_one("#actions", Static).update("R Refresh")

    @staticmethod
    def _restored_index(previous_id: str | None, current_ids: list[str]) -> int | None:
        if not current_ids:
            return None
        if previous_id is not None:
            try:
                return current_ids.index(previous_id)
            except ValueError:
                pass
        return 0

    @staticmethod
    def _conversation_messages(pages: tuple[ConversationHistoryPage, ...]) -> tuple[str, ...]:
        """Reassemble split fragments into one visible message/content entry."""

        fragments = sorted(
            (fragment for page in pages for turn in page.turns for fragment in turn.messages),
            key=lambda fragment: fragment.ordinal,
        )
        rendered: list[str] = []
        current_key: tuple[str, str, str] | None = None
        current_text: list[str] = []
        for fragment in fragments:
            key = (fragment.item_id, fragment.speaker.value, fragment.content.kind.value)
            if current_key is not None and key != current_key:
                rendered.append(f"{current_key[1].upper()} · {current_key[2]}\n{''.join(current_text)}")
                current_text = []
            current_key = key
            current_text.append(fragment.content.text)
        if current_key is not None:
            rendered.append(f"{current_key[1].upper()} · {current_key[2]}\n{''.join(current_text)}")
        return tuple(rendered)

    @staticmethod
    def _live_conversation(frame: LiveTurnKeyframe) -> str:
        """Render only the bounded cumulative assistant/tool projection."""

        blocks: list[str] = []
        if frame.assistant_text:
            blocks.append(f"ASSISTANT · working\n{frame.assistant_text}")
        else:
            blocks.append("ASSISTANT · working\nThinking…")
        state_glyph = {
            LiveToolState.RUNNING: "…",
            LiveToolState.COMPLETED: "✓",
            LiveToolState.FAILED: "!",
            LiveToolState.INTERRUPTED: "x",
        }
        for tool in frame.tools:
            presence = "".join(
                marker
                for marker, present in (
                    (" · path", tool.path_present),
                    (" · URL", tool.url_present),
                    (" · image", tool.image_present),
                )
                if present
            )
            blocks.append(f"TOOL {state_glyph[tool.state]} · {tool.label} · {tool.state.value}{presence}")
        return "LIVE RESPONSE · intermediate\n\n" + "\n\n".join(blocks)

    def _live_frame_received(self, _frame: LiveTurnKeyframe | None) -> None:
        """Update the selected transcript on an already-running event-loop turn."""

        if not self._worker_ids:
            return
        worker_id = self._selected_worker_id()
        if worker_id is None:
            return
        worker = next((item for item in self.client.snapshot.workers if item.dispatch_id == worker_id), None)
        if worker is not None:
            self._show_worker(worker)

    def _ensure_live_subscription(self, worker: WorkerView) -> None:
        active = worker.turn_id is not None and worker.state in {"running", "active"}
        target = worker.dispatch_id if active else None
        if target == self._live_target and self.client.live_stream_active(target):
            return
        self._live_target = target
        if target is None:
            self.run_worker(self._close_live_subscription, exclusive=True, group="live")
        else:
            self.run_worker(partial(self._start_live_subscription, target), exclusive=True, group="live")

    async def _close_live_subscription(self) -> None:
        await self.client.close_live_stream()

    async def _start_live_subscription(self, worker_id: str) -> None:
        try:
            await self.client.subscribe_live(worker_id)
        except (ControlClientError, IpcError, OSError, ValueError):
            # A live stream is presentation-only; its loss cannot make the
            # selected stable control snapshot or TUI lifecycle fail.
            return

    def _show_worker(self, worker: WorkerView) -> None:
        header = (
            f"{worker.role_label} · {worker.state_label}\n"
            f"Controller: {worker.controller_label}  >  Task: {worker.task_label}"
        )
        pages_for = getattr(self.client, "conversation_pages", None)
        pages = pages_for(worker.dispatch_id) if callable(pages_for) else ()
        live = self.client.live_keyframe(worker.dispatch_id)
        if live is not None:
            conversation = self._live_conversation(live)
        elif pages:
            conversation = (
                "CONVERSATION · older messages available (L)"
                if pages[0].older_token is not None
                else "COMPLETE CONVERSATION"
            )
            conversation += "\n\n" + "\n\n".join(self._conversation_messages(pages))
            if pages[0].status.value != "available":
                conversation = f"HISTORY · {pages[0].status.value.replace('_', ' ')}\n\n{pages[0].reason}"
        elif worker.activity:
            visible = worker.activity[-3:] if self.has_class("narrow") else worker.activity
            conversation = "CONVERSATION EXCERPT · recent activity available to the supervisor\n\n" + "\n\n".join(
                f"{item.speaker_label.upper()} · {item.event_label}\n{item.text or 'No message text was exposed.'}"
                for item in visible
            )
        else:
            conversation = (
                "CONVERSATION EXCERPT\n\nNo message text is available in the supervisor's bounded activity window."
            )
        technical = (
            f"TECHNICAL DETAILS\n"
            f"dispatch {worker.dispatch_id}\nthread {worker.thread_id or 'not assigned'} · "
            f"turn {worker.turn_id or 'not active'} · generation {worker.generation} · attempt {worker.attempt}\n"
            f"raw state {worker.state} · role {worker.role} · retry {worker.retry}\nroute {worker.route}"
            "\nhistory scope persisted user/agent messages · non-message tool activity may be omitted"
        )
        self.query_one("#conversation-header", Static).update(header)
        self.query_one("#conversation", Static).update(conversation)
        self.query_one("#technical", Static).update(technical)
        active = worker.turn_id is not None and worker.state in {"running", "active"}
        actions = "S Send direction · I Stop · " if active else ""
        older_available = bool(pages and pages[0].older_token is not None)
        actions += "L Load older · " if older_available else ""
        self._set_actions(actions + "C Copy · O Open · T Details")
        self._ensure_live_subscription(worker)

    def _show_decision(self, decision: DecisionView) -> None:
        if self._live_target is not None:
            self._live_target = None
            self.run_worker(self._close_live_subscription, exclusive=True, group="live")
        header = f"{decision.kind_label} · {decision.state_label}\nController needs a human decision"
        successors = ", ".join(decision.expected_successors) or "none"
        self.query_one("#conversation-header", Static).update(header)
        pages_for = getattr(self.client, "conversation_pages", None)
        pages = pages_for(decision.decision_id) if callable(pages_for) else ()
        if pages:
            heading = (
                "CONTROLLER CONVERSATION · older messages available (L)"
                if pages[0].older_token is not None
                else "COMPLETE CONTROLLER CONVERSATION"
            )
            conversation = heading + "\n\n" + "\n\n".join(self._conversation_messages(pages))
        else:
            conversation = (
                f"WHY THIS NEEDS ATTENTION\n\n{decision.summary}\n\n"
                "Choose an action only after reviewing the linked worker conversation."
            )
        self.query_one("#conversation", Static).update(conversation)
        self.query_one("#technical", Static).update(
            f"TECHNICAL DETAILS\ndecision {decision.decision_id}\ndispatch {decision.dispatch_id}\n"
            f"raw kind {decision.kind} · state {decision.state} · revision {decision.revision} · "
            f"generation {decision.generation}\nclaim {decision.claimant} · lease {decision.lease} · "
            f"deadline {decision.deadline}\nsource thread {decision.source_thread_id or 'not available'}\n"
            f"authorized successors {successors}"
            "\nhistory scope persisted user/agent messages · non-message tool activity may be omitted"
        )
        history_action = "L Load older · " if pages and pages[0].older_token is not None else "L Load conversation · "
        self._set_actions(history_action + "K Claim · A Ack · P Remind · O Open · T Details")

    def _set_actions(self, actions: str) -> None:
        self._context_actions = actions
        self._update_action_row()

    def _update_action_row(self) -> None:
        conversation = self.query_one("#conversation", Static)
        maximum = conversation.max_scroll_y
        current = conversation.scroll_y
        if maximum <= 0 or current <= 0:
            position = "top"
        elif current >= maximum:
            position = "bottom"
        else:
            position = f"{current + 1}/{maximum + 1}"
        self.query_one("#actions", Static).update(f"View {position} · [/] Scroll\n{self._context_actions}")

    @on(ListView.Highlighted, "#workers")
    def worker_highlighted(self, event: ListView.Highlighted) -> None:
        if (
            self.focused is event.list_view
            and event.list_view.index is not None
            and event.list_view.index < len(self.client.snapshot.workers)
        ):
            self._show_worker(self.client.snapshot.workers[event.list_view.index])

    @on(ListView.Highlighted, "#decisions")
    def decision_highlighted(self, event: ListView.Highlighted) -> None:
        if (
            self.focused is event.list_view
            and event.list_view.index is not None
            and event.list_view.index < len(self.client.snapshot.decisions)
        ):
            self._show_decision(self.client.snapshot.decisions[event.list_view.index])

    @on(events.DescendantFocus)
    def resume_selection_focused(self, event: events.DescendantFocus) -> None:
        worker_list = self.query_one("#workers", ListView)
        decision_list = self.query_one("#decisions", ListView)
        if event.widget is worker_list:
            index = worker_list.index
            if index is not None and index < len(self.client.snapshot.workers):
                self._show_worker(self.client.snapshot.workers[index])
        elif event.widget is decision_list:
            index = decision_list.index
            if index is not None and index < len(self.client.snapshot.decisions):
                self._show_decision(self.client.snapshot.decisions[index])

    def _selected_worker_id(self) -> str | None:
        index = self.query_one("#workers", ListView).index
        return self._worker_ids[index] if index is not None and index < len(self._worker_ids) else None

    def _selected_decision_id(self) -> str | None:
        index = self.query_one("#decisions", ListView).index
        return self._decision_ids[index] if index is not None and index < len(self._decision_ids) else None

    def _feedback(self, value: str) -> None:
        suffix = " · ^P commands" if self.has_class("narrow") else ""
        self.query_one("#feedback", Static).update(value + suffix)

    def action_toggle_details(self) -> None:
        showing = self.has_class("diagnostics")
        self.set_class(not showing, "diagnostics")
        self._feedback("Technical details hidden" if showing else "Technical details shown below the conversation")

    def _mutations_available(self) -> bool:
        if self.client.snapshot.connected:
            return True
        self._feedback("Rejected · offline snapshot is read-only; reconnect before mutation")
        return False

    def action_refresh(self) -> None:
        self.run_worker(self._refresh(), exclusive=True, group="refresh")

    def action_load_older(self) -> None:
        loader = getattr(self.client, "load_conversation", None)
        if not callable(loader):
            self._feedback("Older conversation pages are unavailable")
            return
        worker_list = self.query_one("#workers", ListView)
        decision_list = self.query_one("#decisions", ListView)
        worker_id = self._selected_worker_id() if self.focused is worker_list else None
        decision_id = self._selected_decision_id() if self.focused is decision_list else None
        if worker_id is None and decision_id is None:
            worker_id = self._selected_worker_id()
        if worker_id is None and decision_id is None:
            self._feedback("Select a conversation before loading older messages")
            return

        async def load() -> None:
            try:
                await loader(worker_id=worker_id, decision_id=decision_id, older=True)
            except (ControlClientError, TerminalUiOfflineError, ValueError, OSError) as exc:
                self._feedback(f"History load rejected · {exc}")
                return
            if worker_id is not None:
                self._show_worker(self.client.snapshot.workers[self._worker_ids.index(worker_id)])
            elif decision_id is not None:
                self._show_decision(self.client.snapshot.decisions[self._decision_ids.index(decision_id)])
            self._feedback("Older conversation messages loaded")

        self.run_worker(load(), exclusive=True, group="history")

    def action_scroll_up(self) -> None:
        self.query_one("#conversation", Static).scroll_relative(y=-6)
        self.call_after_refresh(self._update_action_row)

    def action_scroll_down(self) -> None:
        self.query_one("#conversation", Static).scroll_relative(y=6)
        self.call_after_refresh(self._update_action_row)

    def _run_mutation(self, operation: Coroutine[Any, Any, TerminalUiCommandResult]) -> None:
        async def execute() -> None:
            try:
                result = await operation
            except (TerminalUiOfflineError, ValueError, RuntimeError) as exc:
                self._feedback(f"Rejected · {exc}")
                self.notify(str(exc), severity="error")
                return
            if result.outcome != "post_send_uncertain":
                await self._refresh()
            label = result.outcome.replace("_", " ").title()
            self._feedback(f"{label} · {result.operation} · {result.identity} · {result.detail}")

        self.run_worker(execute(), exclusive=True, group="mutation")

    def action_steer(self) -> None:
        if not self._mutations_available():
            return
        dispatch_id = self._selected_worker_id()
        if dispatch_id is None:
            self._feedback("Select a worker before steering")
            return
        status = self.client.worker_status(dispatch_id)
        identity = (
            f"dispatch {status.dispatch_id} · generation {status.generation} · attempt {status.attempt}\n"
            f"thread {status.thread_id or 'none'} · turn {status.active_turn_id or 'none'}"
        )

        def submit(text: str | None) -> None:
            if text is not None:
                self._run_mutation(self.client.steer(status, text))

        self.push_screen(SteerActionScreen(identity), submit)

    def action_interrupt(self) -> None:
        if not self._mutations_available():
            return
        dispatch_id = self._selected_worker_id()
        if dispatch_id is None:
            self._feedback("Select a worker before interrupting")
            return
        status = self.client.worker_status(dispatch_id)
        detail = (
            f"dispatch {status.dispatch_id}\ngeneration {status.generation} · attempt {status.attempt}\n"
            f"thread {status.thread_id or 'none'} · turn {status.active_turn_id or 'none'}"
        )

        def confirmed(value: bool) -> None:
            if value:
                self._run_mutation(self.client.interrupt(status))

        self.push_screen(ConfirmActionScreen("Interrupt this exact active turn?", detail), confirmed)

    def action_claim(self) -> None:
        if not self._mutations_available():
            return
        decision_id = self._selected_decision_id()
        if decision_id is None:
            self._feedback("Select a decision before claiming")
            return
        status = self.client.decision_status(decision_id)
        detail = f"decision {status.decision_id}\nrevision {status.revision} · generation {status.current_generation}"

        def confirmed(value: bool) -> None:
            if value:
                self._run_mutation(self.client.human_claim(status))

        self.push_screen(ConfirmActionScreen("Claim this exact decision as the terminal operator?", detail), confirmed)

    def _confirm_decision_action(self, title: str, operation: str) -> None:
        if not self._mutations_available():
            return
        decision_id = self._selected_decision_id()
        if decision_id is None:
            self._feedback("Select a decision first")
            return
        status = self.client.decision_status(decision_id)
        detail = f"decision {status.decision_id}\nrevision {status.revision} · generation {status.current_generation}"

        def confirmed(value: bool) -> None:
            if value and operation == "acknowledge":
                self._run_mutation(self.client.acknowledge(status))
            elif value:
                self._run_mutation(self.client.rearm_checkpoint(status))

        self.push_screen(ConfirmActionScreen(title, detail), confirmed)

    def action_acknowledge(self) -> None:
        self._confirm_decision_action("Acknowledge and close this exact decision?", "acknowledge")

    def action_rearm(self) -> None:
        self._confirm_decision_action("Re-arm a 30 minute checkpoint for this decision?", "rearm")

    def _focused_resume_selection(self) -> tuple[str | None, str | None]:
        worker_list = self.query_one("#workers", ListView)
        decision_list = self.query_one("#decisions", ListView)
        if self.focused is worker_list:
            worker_id = self._selected_worker_id()
            if worker_id is None:
                raise ValueError("focused worker list has no selected worker")
            return worker_id, None
        if self.focused is decision_list:
            decision_id = self._selected_decision_id()
            if decision_id is None:
                raise ValueError("focused decision list has no selected decision")
            return None, decision_id
        raise ValueError("focus a worker or decision before selecting a transcript")

    def _resume_thread_id(self) -> str | None:
        worker_id, decision_id = self._focused_resume_selection()
        if worker_id is not None:
            status = self.client.worker_status(worker_id)
            return status.thread_id.id if status.thread_id is not None else None
        assert decision_id is not None
        return self.client.decision_status(decision_id).summary.source_thread_id

    def action_copy_resume(self) -> None:
        try:
            thread_id = self._resume_thread_id()
        except ValueError as exc:
            self._feedback(f"Resume selection rejected · {exc}")
            return
        if thread_id is None:
            self._feedback("Selected item has no SDK thread identity")
            return
        command = f"codex resume {thread_id}"
        self.copy_to_clipboard(command)
        self._feedback(f"Copied exact argv · {command}")

    def action_open_resume(self) -> None:
        if self.resume_handler is None:
            self._feedback("Transcript handoff is unavailable while an exact live turn owns the thread")
            return
        try:
            worker_id, decision_id = self._focused_resume_selection()
            thread_id = self.client.open_resume_thread_id(worker_id=worker_id, decision_id=decision_id)
        except (TerminalUiOfflineError, ValueError) as exc:
            self._feedback(f"Resume handoff rejected · {exc}")
            return
        try:
            detail = self.resume_handler(thread_id)
        except (OSError, ValueError) as exc:
            self._feedback(f"Resume handoff rejected · {exc}")
            return
        self._feedback(detail)


__all__ = ["CodexFlowTerminalApp", "ConfirmActionScreen", "SteerActionScreen"]
