"""Bounded model generations for the event-driven program controller.

The program controller is intentionally ephemeral.  It receives one durable
program event, emits one closed action bundle, and leaves all scheduling,
Git effects, callbacks, and recovery authority to the SQLite-backed harness.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .backends.codex_sdk import CodexSdkAdapter, ControllerThreadInspectionKind
from .contracts import ModelFacingProgramControllerActionBundle, model_facing_program_controller_action_schema
from .domain import (
    ControllerActionReceipt,
    ControllerDecisionClaim,
    ControllerDecisionId,
    ControllerDecisionState,
    ControllerGenerationState,
    ControllerGenerationStatus,
    ControllerRecoveryInspectionClaim,
    Generation,
    ProgramControllerContext,
    ProgramControllerDecisionStatus,
    ReasoningEffort,
    thaw_json,
)


class ProgramControllerClient(Protocol):
    """The typed IPC operations required by one program generation."""

    def program_status(self, decision_id: str) -> ProgramControllerDecisionStatus: ...

    def program_context(
        self,
        program_id: str,
        *,
        expected_revision: int,
        expected_trunk_head: str,
    ) -> ProgramControllerContext: ...

    def program_claim(
        self, decision_id: str, *, claimant_id: str, expected_revision: int, generation: int | None = None
    ) -> ControllerDecisionClaim: ...

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

    def submit_program_actions(
        self, bundle: ModelFacingProgramControllerActionBundle, *, claimant_id: str, token: str
    ) -> ControllerActionReceipt: ...

    def acknowledge_program(
        self,
        decision_id: str,
        *,
        action_id: str,
        bundle_sha256: str,
        committed_revision: int,
        claimant_id: str,
        token: str,
    ) -> ControllerActionReceipt: ...

    def acknowledge_program_recovered(
        self, decision_id: str, *, action_id: str, bundle_sha256: str, committed_revision: int
    ) -> ControllerActionReceipt: ...

    def submit_program_recovered_actions(self, decision_id: str) -> ControllerActionReceipt: ...

    def complete_generation(self, decision_id: str, *, generation: int, state: str) -> ControllerGenerationStatus: ...

    def reset_generation_delivery(
        self,
        decision_id: str,
        *,
        generation: int,
        expected_revision: int,
        claimant_id: str,
        token: str,
    ) -> ControllerGenerationStatus: ...

    def generation(self, decision_id: str, generation: int | None = None) -> ControllerGenerationStatus: ...

    def reserve_recovery_inspection(self, decision_id: str) -> ControllerRecoveryInspectionClaim: ...

    def complete_recovery_inspection(
        self,
        decision_id: str,
        *,
        inspection_outcome: str,
        claim: ControllerRecoveryInspectionClaim,
        bundle: ModelFacingProgramControllerActionBundle | None = None,
    ) -> ControllerGenerationStatus: ...


@dataclass(frozen=True, slots=True)
class ProgramControllerGenerationResult:
    decision_id: ControllerDecisionId
    generation: Generation
    outcome: str
    action_id: str | None = None
    detail: str | None = None


class ProgramControllerGenerationRunner:
    """Run exactly one claimed program decision generation."""

    def __init__(
        self,
        client: ProgramControllerClient,
        adapter: CodexSdkAdapter,
        *,
        decision_id: ControllerDecisionId | str,
        claimant_id: str,
        cwd: Path,
        model: str,
        reasoning_effort: ReasoningEffort = ReasoningEffort.MEDIUM,
    ) -> None:
        self.client = client
        self.adapter = adapter
        self.decision_id = ControllerDecisionId(str(decision_id))
        self.claimant_id = claimant_id
        self.cwd = Path(cwd)
        self.model = model
        self.reasoning_effort = reasoning_effort

    def _prompt(
        self,
        status: ProgramControllerDecisionStatus,
        context: ProgramControllerContext,
        *,
        revision: int,
        generation: int,
    ) -> str:
        payload = json.dumps(thaw_json(status.payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        program_context = context.to_json_bytes().decode("utf-8")
        return (
            "Event-driven program controller. Return exactly one JSON object matching the closed program action "
            "bundle schema. The SQLite ledger is the sole dynamic authority; the canonical plan is static intent. "
            "Use only the current event and its exact program revision. Start all currently ready milestones whose "
            "mutable surfaces are disjoint, start every declared read-only review authority for one exact candidate, "
            "promote only fresh exact accepted reviews, and integrate only the authorized candidate and trunk HEAD. "
            "Concrete review blockers may receive one same-owner repair. Underdetermined intent or external "
            "prerequisites must use the corresponding attention action. Do not edit files, call callbacks, invent "
            "successors, or create another scheduler.\n"
            f"decision_id={status.decision_id}; program_id={status.program_id}; generation={generation}; "
            f"revision={revision}; event_kind={status.event_kind.value}; event_key={status.event_key}; "
            f"program_payload={payload}\n"
            f"program_context={program_context}\n"
            f"Schema: {model_facing_program_controller_action_schema()}"
        )

    def run(self) -> ProgramControllerGenerationResult:
        status = self.client.program_status(str(self.decision_id))
        claim = self.client.program_claim(
            str(self.decision_id),
            claimant_id=self.claimant_id,
            expected_revision=status.revision,
            generation=int(status.current_generation),
        )
        generation = int(claim.generation)
        identity_bound = False
        thread_started = False
        bound_turn_id: str | None = None
        try:
            summary = thaw_json(status.payload)
            if not isinstance(summary, dict):
                raise RuntimeError("program controller decision summary is malformed")
            context = self.client.program_context(
                str(status.program_id),
                expected_revision=summary.get("program_revision"),  # type: ignore[arg-type]
                expected_trunk_head=summary.get("trunk_head"),  # type: ignore[arg-type]
            )
            if context.plan_digest != summary.get("plan_digest"):
                raise RuntimeError("program controller context plan identity is stale")
            prompt = self._prompt(status, context, revision=claim.revision, generation=generation)
            prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            self.client.prepare_generation(str(self.decision_id), generation=generation, prompt_sha256=prompt_sha256)
            thread = self.adapter.start_thread()
            thread_started = True
            self.client.bind_generation_thread(
                str(self.decision_id), generation=generation, controller_thread_id=thread.id
            )
            identity_bound = True

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

            observation = self.adapter.run_turn(
                thread,
                prompt,
                output_schema=model_facing_program_controller_action_schema(),
                turn_callback=bind_turn,
            )
            if observation.status != "completed" or not isinstance(observation.structured_output, dict):
                raise RuntimeError("program controller generation returned no completed typed action bundle")
            bundle = ModelFacingProgramControllerActionBundle.from_json(observation.structured_output)
            if (
                bundle.decision_id != self.decision_id
                or int(bundle.generation) != generation
                or bundle.program_id != status.program_id
                or bundle.event_kind is not status.event_kind
                or bundle.event_key != status.event_key
            ):
                raise RuntimeError("program controller generation returned a bundle for another event")
            receipt = self.client.submit_program_actions(bundle, claimant_id=self.claimant_id, token=str(claim.token))
            acknowledged = self.client.acknowledge_program(
                str(self.decision_id),
                action_id=receipt.action_id,
                bundle_sha256=bundle.sha256,
                committed_revision=receipt.expected_revision + 1,
                claimant_id=self.claimant_id,
                token=str(claim.token),
            )
            self.client.complete_generation(
                str(self.decision_id), generation=generation, state=ControllerGenerationState.COMPLETED.value
            )
            return ProgramControllerGenerationResult(
                self.decision_id, claim.generation, "acknowledged", acknowledged.action_id
            )
        except Exception:
            # Generation completion is owned by the shared controller ledger.
            # A pre-identity failure can be safely reclaimed; after a thread
            # identity or turn binding, the generation is preserved for the
            # one-shot recovery inspection path.
            if identity_bound:
                try:
                    self.client.complete_generation(
                        str(self.decision_id), generation=generation, state=ControllerGenerationState.FAILED.value
                    )
                except Exception:
                    pass
            elif thread_started:
                try:
                    self.client.complete_generation(
                        str(self.decision_id), generation=generation, state=ControllerGenerationState.AMBIGUOUS.value
                    )
                except Exception:
                    pass
            else:
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


class ProgramControllerGenerationRecovery:
    """Replay a committed result or consume one persisted-thread inspection."""

    def __init__(self, client: ProgramControllerClient, adapter: CodexSdkAdapter) -> None:
        self.client = client
        self.adapter = adapter

    def recover(self, decision_id: ControllerDecisionId | str) -> ProgramControllerGenerationResult:
        identity = ControllerDecisionId(str(decision_id))
        status = self.client.program_status(str(identity))
        if (
            status.state in {ControllerDecisionState.ACTION_COMMITTED, ControllerDecisionState.HUMAN_ATTENTION_REQUIRED}
            and status.action_id is not None
            and status.action_sha256 is not None
        ):
            receipt = self.client.acknowledge_program_recovered(
                str(identity),
                action_id=status.action_id,
                bundle_sha256=status.action_sha256,
                committed_revision=status.revision,
            )
            return ProgramControllerGenerationResult(
                identity, status.current_generation, "acknowledged", receipt.action_id
            )
        generation = self.client.generation(str(identity), int(status.current_generation))
        if generation.inspection_outcome is not None:
            if generation.inspection_outcome == ControllerGenerationState.COMPLETED.value:
                receipt = self.client.submit_program_recovered_actions(str(identity))
                acknowledged = self.client.acknowledge_program_recovered(
                    str(identity),
                    action_id=receipt.action_id,
                    bundle_sha256=receipt.bundle_sha256,
                    committed_revision=receipt.expected_revision + 1,
                )
                return ProgramControllerGenerationResult(
                    identity, generation.generation, "acknowledged", acknowledged.action_id
                )
            return ProgramControllerGenerationResult(
                identity,
                generation.generation,
                "inspection_recorded",
                detail=generation.inspection_outcome,
            )
        thread_id = generation.controller_thread_id
        if thread_id is None:
            claim = self.client.reserve_recovery_inspection(str(identity))
            completed = self.client.complete_recovery_inspection(
                str(identity), inspection_outcome=ControllerGenerationState.AMBIGUOUS.value, claim=claim
            )
            return ProgramControllerGenerationResult(
                identity, completed.generation, "ambiguous", detail="program controller thread identity was absent"
            )
        inspection = self.adapter.inspect_persisted_thread(thread_id)
        claim = self.client.reserve_recovery_inspection(str(identity))
        if inspection.kind is ControllerThreadInspectionKind.COMPLETED and inspection.bundle is not None:
            bundle = inspection.bundle
            completed = self.client.complete_recovery_inspection(
                str(identity),
                inspection_outcome=ControllerGenerationState.COMPLETED.value,
                claim=claim,
                bundle=bundle,
            )
            return ProgramControllerGenerationResult(identity, completed.generation, "recovered")
        outcome = (
            ControllerGenerationState.ACTIVE.value
            if inspection.kind is ControllerThreadInspectionKind.ACTIVE_WRITER
            else ControllerGenerationState.AMBIGUOUS.value
        )
        completed = self.client.complete_recovery_inspection(str(identity), inspection_outcome=outcome, claim=claim)
        return ProgramControllerGenerationResult(identity, completed.generation, outcome)


__all__ = [
    "ProgramControllerClient",
    "ProgramControllerGenerationRecovery",
    "ProgramControllerGenerationResult",
    "ProgramControllerGenerationRunner",
]
