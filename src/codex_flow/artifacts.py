"""Atomic, readable projections of a committed :mod:`codex_flow.ledger` view."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import (
    DispatchClaim,
    EventRecord,
    LedgerSnapshot,
    MilestoneRecord,
    RunId,
    WorkflowReason,
)
from .ledger import Ledger


class ArtifactError(RuntimeError):
    """Projection generation or path validation failed."""


class UnsafeArtifactPath(ArtifactError):
    """A projection would leave the repository or follow a symlink."""


ProjectionFaultInjector = Callable[[str, Path], None]


@dataclass(frozen=True, slots=True)
class ProjectionPaths:
    root: Path
    run: Path
    milestones: Path
    events: Path


def _reason_json(reason: WorkflowReason | None) -> dict[str, str] | None:
    if reason is None:
        return None
    result: dict[str, str] = {"code": reason.code.value}
    if reason.detail is not None:
        result["detail"] = reason.detail
    return result


def _run_json(snapshot: LedgerSnapshot) -> dict[str, Any]:
    return {
        "run_id": str(snapshot.run.run_id),
        "created_at": snapshot.run.created_at,
        "closed_at": snapshot.run.closed_at,
        "metadata": snapshot.run.metadata,
        "milestone_count": len(snapshot.milestones),
        "dispatch_count": len(snapshot.dispatches),
        "event_count": len(snapshot.events),
    }


def _milestone_json(record: MilestoneRecord) -> dict[str, Any]:
    return {
        "run_id": str(record.run_id),
        "milestone_id": str(record.milestone_id),
        "state": record.state.value,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "metadata": record.metadata,
    }


def _dispatch_json(record: DispatchClaim) -> dict[str, Any]:
    return {
        "dispatch_id": str(record.dispatch_id),
        "run_id": str(record.run_id),
        "milestone_id": str(record.milestone_id),
        "role": str(record.role),
        "generation": int(record.generation),
        "claimed_at": record.claimed_at,
    }


def _event_json(record: EventRecord) -> dict[str, Any]:
    return {
        "event_id": str(record.event_id),
        "run_id": str(record.run_id),
        "milestone_id": str(record.milestone_id),
        "sequence": int(record.sequence),
        "from_state": record.from_state.value if record.from_state is not None else None,
        "to_state": record.to_state.value,
        "event_type": record.event_type,
        "reason": _reason_json(record.reason),
        "dispatch_id": str(record.dispatch_id) if record.dispatch_id is not None else None,
        "data": record.data,
        "occurred_at": record.occurred_at,
    }


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _canonical_jsonl(values: tuple[dict[str, Any], ...]) -> str:
    return "".join(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for value in values
    )


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise UnsafeArtifactPath(f"refusing symlink in artifact path: {path}")


def _repository_root(path: str | Path) -> Path:
    root = Path(path)
    if not root.exists() or not root.is_dir():
        raise UnsafeArtifactPath(f"repository root is not an existing directory: {root}")
    _reject_symlink(root)
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise UnsafeArtifactPath(f"repository root is not a directory: {root}")
    return resolved


def _ensure_child_directory(root: Path, relative: tuple[str, ...]) -> Path:
    current = root
    for component in relative:
        current = current / component
        if current.exists() or current.is_symlink():
            _reject_symlink(current)
            if not current.is_dir():
                raise UnsafeArtifactPath(f"artifact path component is not a directory: {current}")
        else:
            current.mkdir()
        resolved = current.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise UnsafeArtifactPath(f"artifact path escapes repository: {current}") from exc
    return current


def _safe_file_path(root: Path, relative: tuple[str, ...]) -> Path:
    parent = _ensure_child_directory(root, relative[:-1])
    target = parent / relative[-1]
    if target.exists() or target.is_symlink():
        _reject_symlink(target)
        if not target.is_file():
            raise UnsafeArtifactPath(f"artifact target is not a regular file: {target}")
    resolved = target.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise UnsafeArtifactPath(f"artifact target escapes repository: {target}") from exc
    return target


class ArtifactProjector:
    """Rebuild projections from one committed ledger snapshot."""

    def __init__(
        self,
        repository_root: str | Path,
        *,
        fault_injector: ProjectionFaultInjector | None = None,
    ) -> None:
        self.repository_root = _repository_root(repository_root)
        self._fault_injector = fault_injector

    def rebuild(self, ledger: Ledger, run_id: RunId | str) -> ProjectionPaths:
        run = RunId(run_id if isinstance(run_id, str) else str(run_id))
        root = self.repository_root
        try:
            ledger_path = ledger.path.resolve(strict=True)
            ledger_path.relative_to(root)
        except (FileNotFoundError, ValueError) as exc:
            raise UnsafeArtifactPath("ledger must be stored inside the repository root") from exc
        snapshot = ledger.snapshot(run)
        run_dir = _ensure_child_directory(root, (".codex-flow", "runs", str(run)))

        run_path = _safe_file_path(root, (".codex-flow", "runs", str(run), "run.json"))
        milestones_path = _safe_file_path(root, (".codex-flow", "runs", str(run), "milestones.json"))
        events_path = _safe_file_path(root, (".codex-flow", "runs", str(run), "events.jsonl"))
        milestones = tuple(
            {
                **_milestone_json(record),
                "dispatches": [
                    _dispatch_json(dispatch)
                    for dispatch in snapshot.dispatches
                    if dispatch.milestone_id == record.milestone_id
                ],
            }
            for record in snapshot.milestones
        )
        events = tuple(_event_json(event) for event in snapshot.events)
        files = (
            (run_path, _canonical_json(_run_json(snapshot))),
            (milestones_path, _canonical_json(list(milestones))),
            (events_path, _canonical_jsonl(events)),
        )
        for index, (path, content) in enumerate(files):
            self._atomic_write(path, content, f"projection_{index}")
        return ProjectionPaths(run_dir, run_path, milestones_path, events_path)

    def _atomic_write(self, target: Path, content: str, stage: str) -> None:
        _reject_symlink(target)
        parent = target.parent
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=parent)
        temporary = Path(temporary_name)
        try:
            if self._fault_injector is not None:
                self._fault_injector(stage, target)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            _reject_symlink(temporary)
            os.replace(temporary, target)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException as exc:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ArtifactError(f"failed to atomically write projection {target}") from exc


def rebuild_projections(
    ledger: Ledger,
    repository_root: str | Path,
    run_id: RunId | str,
    *,
    fault_injector: ProjectionFaultInjector | None = None,
) -> ProjectionPaths:
    """Convenience function for the canonical projection path."""

    return ArtifactProjector(repository_root, fault_injector=fault_injector).rebuild(ledger, run_id)


write_artifacts = rebuild_projections
rebuild_artifacts = rebuild_projections
ArtifactWriter = ArtifactProjector
ProjectionWriter = ArtifactProjector


def write_run_artifacts(
    ledger: Ledger,
    run_id: RunId | str,
    repository_root: str | Path,
    *,
    fault_injector: ProjectionFaultInjector | None = None,
) -> ProjectionPaths:
    return rebuild_projections(
        ledger,
        repository_root,
        run_id,
        fault_injector=fault_injector,
    )
