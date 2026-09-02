"""Bounded controller-generation execution and one-read recovery decisions.

The runner intentionally talks only to the typed controller client and the
shared-session SDK adapter.  SQLite, IPC framing and successor scheduling stay
behind those boundaries so a model turn cannot manufacture durable authority.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .backends.codex_sdk import (
    CodexSdkAdapter,
    ControllerThreadInspection,
    ControllerThreadInspectionKind,
)
from .contracts import ModelFacingControllerActionBundle, model_facing_controller_action_schema
from .domain import (
    ControllerActionReceipt,
    ControllerDecisionClaim,
    ControllerDecisionId,
    ControllerDecisionState,
    ControllerDecisionStatus,
    ControllerGenerationState,
    ControllerGenerationStatus,
    ControllerRecoveryInspectionClaim,
    Generation,
    ReasoningEffort,
    TemporaryRateLimitAfterIdentity,
    ThreadIdentity,
    TransientFailureAfterIdentity,
)


class ControllerDecisionClientProtocol(Protocol):
    def status(self, decision_id: str) -> ControllerDecisionStatus: ...

    def reserve_recovery_inspection(self, decision_id: str) -> ControllerRecoveryInspectionClaim: ...

    def complete_recovery_inspection(
        self,
        decision_id: str,
        *,
        inspection_outcome: str,
        claim: ControllerRecoveryInspectionClaim,
        bundle: ModelFacingControllerActionBundle | None = None,
    ) -> ControllerGenerationStatus: ...

    def claim(
        self, decision_id: str, *, claimant_id: str, expected_revision: int, generation: int | None = None
    ) -> ControllerDecisionClaim: ...

    def renew_claim(
        self, decision_id: str, *, claimant_id: str, token: str, expected_revision: int
    ) -> ControllerDecisionClaim: ...

    def defer_rate_limit(
        self,
        decision_id: str,
        *,
        generation: int,
        claimant_id: str,
        token: str,
        expected_revision: int,
        retry_at: str,
    ) -> ControllerDecisionClaim: ...

    def submit_actions(
        self, bundle: ModelFacingControllerActionBundle, *, claimant_id: str, token: str
    ) -> ControllerActionReceipt: ...

    def submit_recovered_actions(
        self, decision_or_bundle: ControllerDecisionId | str | ModelFacingControllerActionBundle
    ) -> ControllerActionReceipt: ...

    def acknowledge(
        self,
        decision_id: str,
        *,
        action_id: str,
        bundle_sha256: str,
        committed_revision: int,
        claimant_id: str,
        token: str,
    ) -> ControllerActionReceipt: ...

    def acknowledge_recovered(
        self,
        decision_id: str,
        *,
        action_id: str,
        bundle_sha256: str,
        committed_revision: int,
    ) -> ControllerActionReceipt: ...

    def request_recovery(self, decision_id: str, *, inspection_outcome: str) -> object: ...

    def generation(self, decision_id: str, generation: int | None = None) -> ControllerGenerationStatus: ...

    def prepare_generation(
        self, decision_id: str, *, generation: int, prompt_sha256: str | None = None
    ) -> ControllerGenerationStatus: ...

    def bind_generation_thread(
        self, decision_id: str, *, generation: int, controller_thread_id: str
    ) -> ControllerGenerationStatus: ...

    def bind_generation_turn(
        self,
        decision_id: str,
        *,
        generation: int,
        controller_thread_id: str,
        controller_turn_id: str,
        previous_controller_turn_id: str | None = None,
    ) -> ControllerGenerationStatus: ...

    def complete_generation(self, decision_id: str, *, generation: int, state: str) -> ControllerGenerationStatus: ...

    def reset_generation_delivery(self, decision_id: str, *, generation: int) -> ControllerGenerationStatus: ...


@dataclass(frozen=True, slots=True)
class ControllerGenerationResult:
    decision_id: ControllerDecisionId
    generation: Generation
    outcome: str
    action_id: str | None = None
    detail: str | None = None


class ControllerGenerationRunner:
    """Execute exactly one claimed schema-bound controller generation."""

    def __init__(
        self,
        client: ControllerDecisionClientProtocol,
        adapter: CodexSdkAdapter,
        *,
        decision_id: ControllerDecisionId | str,
        claimant_id: str,
        cwd: Path,
        model: str,
        reasoning_effort: ReasoningEffort = ReasoningEffort.MEDIUM,
        rate_limit_waiter: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.adapter = adapter
        self.decision_id = ControllerDecisionId(str(decision_id))
        self.claimant_id = claimant_id
        self.cwd = Path(cwd)
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.rate_limit_waiter = rate_limit_waiter or self._wait_for_rate_limit

    @staticmethod
    def _wait_for_rate_limit(retry_at: str) -> None:
        deadline = datetime.fromisoformat(retry_at.removesuffix("Z") + ("+00:00" if retry_at.endswith("Z") else ""))
        if deadline.tzinfo is None:
            raise ValueError("controller rate-limit deadline must be timezone-aware")
        delay = (deadline.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        if delay <= 0:
            return
        if delay > 86_405:
            raise ValueError("controller rate-limit wait exceeds its bound")
        time.sleep(delay)

    def _prompt(self, status: ControllerDecisionStatus, *, revision: int) -> str:
        context = json.dumps(status.summary.context_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if status.summary.dispatch_state in {"completed", "failed", "cancelled", "result_submitted", "finalizing"}:
            decision_guidance = (
                "The dispatch is already terminal or terminalizing. Return acknowledge_only; retry, cancellation, "
                "budget changes, checkpoint re-arming and human-attention effects are stale and forbidden."
            )
        else:
            decision_guidance = (
                "human_attention_required means this controller must make the explicit recovery decision. "
                "An automatic-retry prohibition does not prohibit a reasoned retry_dispatch action when the "
                "durable worker-exit and activity facts support it."
            )
        return (
            "Controller decision recovery. Return exactly one JSON object matching the closed action bundle schema.\n"
            f"decision_id={status.decision_id}; generation={int(status.current_generation)}; revision={revision}; "
            f"dispatch_id={status.dispatch_id}; kind={status.kind}; state={status.state.value}; "
            f"summary={status.summary.summary}; "
            f"expected_successor_dispatch_ids={list(status.summary.expected_successor_dispatch_ids)}\n"
            f"recovery_context={context}\n"
            f"{decision_guidance}\n"
            "No prose, transcript replay, successor invention or filesystem/database access.\n"
            f"Schema: {model_facing_controller_action_schema()}"
        )

    def run(self) -> ControllerGenerationResult:
        status = self.client.status(str(self.decision_id))
        claim = self.client.claim(
            str(self.decision_id),
            claimant_id=self.claimant_id,
            expected_revision=status.revision,
            generation=int(status.current_generation),
        )
        generation = int(claim.generation)
        prompt = self._prompt(status, revision=claim.revision)
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        identity_bound = False
        thread_started = False
        try:
            self.client.prepare_generation(str(self.decision_id), generation=generation, prompt_sha256=prompt_sha256)
            thread = self.adapter.start_thread()
            thread_started = True
            self.client.bind_generation_thread(
                str(self.decision_id), generation=generation, controller_thread_id=thread.id
            )
            identity_bound = True

            bound_turn_id: str | None = None

            def bind_turn(turn_id: str) -> None:
                nonlocal bound_turn_id
                self.client.bind_generation_turn(
                    str(self.decision_id),
                    generation=generation,
                    controller_thread_id=thread.id,
                    controller_turn_id=turn_id,
                    previous_controller_turn_id=bound_turn_id,
                )
                bound_turn_id = turn_id

            waited_for_rate_limit = False
            while True:
                try:
                    observation = self.adapter.run_turn(
                        thread,
                        prompt,
                        output_schema=model_facing_controller_action_schema(),
                        turn_callback=bind_turn,
                    )
                    break
                except TemporaryRateLimitAfterIdentity as error:
                    if waited_for_rate_limit:
                        raise
                    claim = self.client.defer_rate_limit(
                        str(self.decision_id),
                        generation=generation,
                        claimant_id=self.claimant_id,
                        token=str(claim.token),
                        expected_revision=claim.revision,
                        retry_at=error.retry_at,
                    )
                    self.rate_limit_waiter(error.retry_at)
                    claim = self.client.renew_claim(
                        str(self.decision_id),
                        claimant_id=self.claimant_id,
                        token=str(claim.token),
                        expected_revision=claim.revision,
                    )
                    waited_for_rate_limit = True
            if observation.status != "completed" or not isinstance(observation.structured_output, dict):
                raise RuntimeError("controller generation returned no completed typed action bundle")
            bundle = ModelFacingControllerActionBundle.from_json(observation.structured_output)
            if bundle.decision_id != self.decision_id or int(bundle.generation) != generation:
                raise RuntimeError("controller generation returned a bundle for another decision")
            receipt = self.client.submit_actions(bundle, claimant_id=self.claimant_id, token=str(claim.token))
            action_id = receipt.action_id
            committed_revision = receipt.expected_revision + 1
            self.client.acknowledge(
                str(self.decision_id),
                action_id=action_id,
                bundle_sha256=bundle.sha256,
                committed_revision=committed_revision,
                claimant_id=self.claimant_id,
                token=str(claim.token),
            )
            self.client.complete_generation(
                str(self.decision_id), generation=generation, state=ControllerGenerationState.COMPLETED.value
            )
            return ControllerGenerationResult(self.decision_id, claim.generation, "acknowledged", action_id)
        except Exception as error:
            # Preserve a durable terminal generation fact after identity.  A
            # pre-identity transport failure leaves the prepared generation
            # reusable; the supervisor may retry it within its delivery cap.
            if identity_bound:
                outcome = ControllerGenerationState.FAILED.value
                if isinstance(error, TransientFailureAfterIdentity):
                    outcome = ControllerGenerationState.INTERRUPTED.value
                try:
                    self.client.complete_generation(str(self.decision_id), generation=generation, state=outcome)
                except Exception:
                    pass
            elif thread_started:
                # The SDK returned a thread identity but the ledger bind did
                # not commit.  The identity window is ambiguous; never reset
                # the prepared claim and speculatively create another writer.
                try:
                    self.client.complete_generation(
                        str(self.decision_id), generation=generation, state=ControllerGenerationState.AMBIGUOUS.value
                    )
                    inspection_claim = self.client.reserve_recovery_inspection(str(self.decision_id))
                    self.client.complete_recovery_inspection(
                        str(self.decision_id),
                        inspection_outcome=ControllerGenerationState.AMBIGUOUS.value,
                        claim=inspection_claim,
                    )
                except Exception:
                    pass
            else:
                # No external identity crossed the boundary.  Releasing the
                # claim and returning this generation to ``prepared`` is safe
                # and permits the bounded delivery budget to retry it.
                try:
                    self.client.reset_generation_delivery(
                        str(self.decision_id),
                        generation=generation,
                        expected_revision=claim.revision,
                        claimant_id=claim.claimant_id,
                        token=str(claim.token),
                    )
                except Exception:
                    pass
            raise


class ControllerGenerationRecovery:
    """Apply one persisted-thread inspection and return one deterministic step."""

    def __init__(self, client: ControllerDecisionClientProtocol, adapter: CodexSdkAdapter) -> None:
        self.client = client
        self.adapter = adapter

    def recover(self, decision_id: ControllerDecisionId | str) -> ControllerGenerationResult:
        identity = ControllerDecisionId(str(decision_id))
        status = self.client.status(str(identity))
        # A committed effect is already durable.  If the source process died
        # before its acknowledgement RPC, close only that acknowledgement
        # fact; do not consume the generation's one authoritative SDK read a
        # second time.
        if (
            status.state
            in {
                ControllerDecisionState.ACTION_COMMITTED,
                ControllerDecisionState.HUMAN_ATTENTION_REQUIRED,
            }
            and status.action_id is not None
            and status.action_sha256 is not None
        ):
            receipt = self.client.acknowledge_recovered(
                str(identity),
                action_id=status.action_id,
                bundle_sha256=status.action_sha256,
                committed_revision=status.revision,
            )
            return ControllerGenerationResult(
                identity,
                status.current_generation,
                "acknowledged",
                receipt.action_id,
                detail="replayed durable controller acknowledgement",
            )
        generation = self.client.generation(str(identity), int(status.current_generation))
        # Recovery is one-shot per persisted generation.  A supervisor
        # restart may observe the already-recorded inspection outcome; return
        # its deterministic status instead of attempting another read.
        if generation.inspection_outcome is not None:
            if generation.inspection_outcome == ControllerGenerationState.ACTIVE.value:
                return ControllerGenerationResult(identity, generation.generation, "active_unchanged")
            if generation.inspection_outcome == ControllerGenerationState.COMPLETED.value:
                receipt = self.client.submit_recovered_actions(identity)
                acknowledged = self.client.acknowledge_recovered(
                    str(identity),
                    action_id=receipt.action_id,
                    bundle_sha256=receipt.bundle_sha256,
                    committed_revision=receipt.expected_revision + 1,
                )
                return ControllerGenerationResult(
                    identity, generation.generation, "acknowledged", acknowledged.action_id
                )
            return ControllerGenerationResult(
                identity,
                generation.generation,
                "human_attention_required",
                detail=f"controller inspection already consumed: {generation.inspection_outcome}",
            )
        thread_id = generation.controller_thread_id.id if generation.controller_thread_id is not None else None
        if thread_id is None:
            try:
                claim = self.client.reserve_recovery_inspection(str(identity))
            except Exception as error:
                # An expired reservation is durably converted by the ledger
                # into human attention.  Refresh that state before returning;
                # reporting a generic "inspection in progress" would strand
                # a restart forever even though no second SDK read is allowed.
                refreshed = self.client.status(str(identity))
                if refreshed.state is ControllerDecisionState.HUMAN_ATTENTION_REQUIRED:
                    return ControllerGenerationResult(
                        identity,
                        generation.generation,
                        "human_attention_required",
                        detail="controller inspection reservation expired",
                    )
                return ControllerGenerationResult(
                    identity,
                    generation.generation,
                    "inspection_in_progress",
                    detail=f"controller inspection reservation is already owned: {type(error).__name__}",
                )
            # A missing persisted identity is an uncommitted identity window,
            # not proof that the source controller's shared thread vanished.
            # Leave it fail-closed as ambiguous rather than speculatively
            # creating a replacement writer.
            outcome = ControllerGenerationState.AMBIGUOUS.value
            completed = self.client.complete_recovery_inspection(str(identity), inspection_outcome=outcome, claim=claim)
            next_generation = completed.generation
            result = (
                "replacement_eligible"
                if int(next_generation) > int(status.current_generation)
                else "human_attention_required"
            )
            return ControllerGenerationResult(
                identity, next_generation, result, detail="source controller thread is unavailable"
            )
        try:
            claim = self.client.reserve_recovery_inspection(str(identity))
        except Exception as error:
            return ControllerGenerationResult(
                identity,
                generation.generation,
                "inspection_in_progress",
                detail=f"controller inspection reservation is already owned: {type(error).__name__}",
            )
        try:
            inspect_method = self.adapter.inspect_controller_thread
            inspect_kwargs = {"decision_id": identity, "generation": status.current_generation}
            # Keep low-level test/double adapters that implement the frozen
            # two-key inspection seam usable while passing the decision
            # revision to the production adapter for exact bundle binding.
            try:
                supports_revision = "expected_revision" in inspect.signature(inspect_method).parameters
            except (TypeError, ValueError):
                supports_revision = False
            if supports_revision:
                inspect_kwargs["expected_revision"] = status.revision
            inspection = inspect_method(ThreadIdentity(thread_id), **inspect_kwargs)
        except Exception as error:
            # Unknown SDK/RPC failures are not source-unavailable proof.  The
            # reserved read is nevertheless completed as ambiguous so a
            # restart cannot consume a second inspection or create a writer.
            inspection = ControllerThreadInspection(
                thread_id,
                int(status.current_generation),
                ControllerThreadInspectionKind.AMBIGUOUS,
                detail=f"controller thread inspection failed: {type(error).__name__}",
            )
        completed = self.client.complete_recovery_inspection(
            str(identity), inspection_outcome=inspection.kind.value, claim=claim, bundle=inspection.bundle
        )
        if inspection.kind is ControllerThreadInspectionKind.COMPLETED and inspection.bundle is not None:
            refreshed = self.client.status(str(identity))
            # The source claim token is intentionally never persisted.  A
            # completed bundle recovered from the single authoritative read
            # crosses a dedicated typed operation whose ledger CAS requires
            # that inspection fact instead of reconstructing a capability.
            receipt = self.client.submit_recovered_actions(str(identity))
            action_id = receipt.action_id
            committed_revision = receipt.expected_revision + 1
            self.client.acknowledge_recovered(
                str(identity),
                action_id=action_id,
                bundle_sha256=inspection.bundle.sha256,
                committed_revision=committed_revision,
            )
            return ControllerGenerationResult(identity, refreshed.current_generation, "acknowledged", action_id)
        if (
            inspection.kind is ControllerThreadInspectionKind.FAILED
            or inspection.kind is ControllerThreadInspectionKind.INTERRUPTED
        ):
            outcome = (
                "replacement_eligible"
                if int(completed.generation) > int(status.current_generation)
                else "human_attention_required"
            )
        elif inspection.kind is ControllerThreadInspectionKind.ACTIVE:
            outcome = "active_unchanged"
        else:
            outcome = "human_attention_required"
        return ControllerGenerationResult(identity, status.current_generation, outcome, detail=inspection.detail)


__all__ = ["ControllerGenerationRecovery", "ControllerGenerationResult", "ControllerGenerationRunner"]
