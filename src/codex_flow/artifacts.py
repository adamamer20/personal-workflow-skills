"""Atomic, readable projections of a committed :mod:`codex_flow.ledger` view."""

from __future__ import annotations

import json
import os
import stat
import uuid
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


def _repository_root(path: str | Path) -> Path:
    root = Path(os.path.abspath(os.fspath(path)))
    try:
        metadata = os.lstat(root)
    except FileNotFoundError as exc:
        raise UnsafeArtifactPath(f"repository root is not an existing directory: {root}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeArtifactPath(f"repository root is not a real directory: {root}")
    return root


def _open_directory_chain(path: Path) -> int:
    """Open every ancestor with O_NOFOLLOW and retain the final directory fd."""

    current_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        try:
            os.close(current_fd)
        except OSError:
            pass
        raise UnsafeArtifactPath(f"artifact ancestor is not a stable real directory: {path}") from exc


def _open_child_directory(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        try:
            return os.open(name, flags, dir_fd=parent_fd)
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            raise UnsafeArtifactPath(f"artifact directory is not stable: {name}") from exc
    except (NotADirectoryError, OSError) as exc:
        raise UnsafeArtifactPath(f"artifact directory is not a real directory: {name}") from exc


def _assert_regular_or_missing(parent_fd: int, name: str) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise UnsafeArtifactPath(f"artifact target is not a real regular file: {name}")


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
        run = RunId(run_id)
        root = self.repository_root
        try:
            ledger.path.relative_to(root)
        except ValueError as exc:
            raise UnsafeArtifactPath("ledger must be stored inside the repository root") from exc
        root_fd = _open_directory_chain(root)
        try:
            codex_fd = _open_child_directory(root_fd, ".codex-flow")
            try:
                runs_fd = _open_child_directory(codex_fd, "runs")
                try:
                    run_fd = _open_child_directory(runs_fd, str(run))
                    try:
                        snapshot = ledger.snapshot(run)
                        run_dir = root / ".codex-flow" / "runs" / str(run)
                        files = (
                            ("run.json", _canonical_json(_run_json(snapshot))),
                            (
                                "milestones.json",
                                _canonical_json(
                                    [
                                        {
                                            **_milestone_json(record),
                                            "dispatches": [
                                                _dispatch_json(dispatch)
                                                for dispatch in snapshot.dispatches
                                                if dispatch.run_id == record.run_id
                                                and dispatch.milestone_id == record.milestone_id
                                            ],
                                        }
                                        for record in snapshot.milestones
                                    ]
                                ),
                            ),
                            ("events.jsonl", _canonical_jsonl(tuple(_event_json(event) for event in snapshot.events))),
                        )
                        for index, (name, content) in enumerate(files):
                            self._atomic_write(run_fd, name, content, f"projection_{index}", run_dir / name)
                        return ProjectionPaths(
                            run_dir, run_dir / "run.json", run_dir / "milestones.json", run_dir / "events.jsonl"
                        )
                    finally:
                        os.close(run_fd)
                finally:
                    os.close(runs_fd)
            finally:
                os.close(codex_fd)
        finally:
            os.close(root_fd)

    def _atomic_write(self, run_fd: int, name: str, content: str, stage: str, target: Path) -> None:
        _assert_regular_or_missing(run_fd, name)
        temporary_name: str | None = None
        descriptor: int | None = None
        try:
            for _ in range(16):
                candidate = f".{name}.{uuid.uuid4().hex}.tmp"
                try:
                    descriptor = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=run_fd,
                    )
                    temporary_name = candidate
                    break
                except FileExistsError:
                    continue
            if descriptor is None or temporary_name is None:
                raise ArtifactError(f"unable to allocate temporary projection for {target}")
            if self._fault_injector is not None:
                self._fault_injector(stage, target)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            _assert_regular_or_missing(run_fd, name)
            os.replace(temporary_name, name, src_dir_fd=run_fd, dst_dir_fd=run_fd)
            temporary_name = None
            os.fsync(run_fd)
        except BaseException as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=run_fd)
                except OSError:
                    pass
            if isinstance(exc, ArtifactError):
                raise
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


def write_owned_artifact(
    repository_root: str | Path,
    relative_path: str | Path,
    content: bytes,
    *,
    replace: bool,
) -> Path:
    """Write one controller-owned file through pinned no-follow directories."""

    root = _repository_root(repository_root)
    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise UnsafeArtifactPath("owned artifact path must be repository-relative")
    descriptors = [_open_directory_chain(root)]
    try:
        for component in relative.parts[:-1]:
            descriptors.append(_open_child_directory(descriptors[-1], component))
        directory_fd = descriptors[-1]
        name = relative.parts[-1]
        _assert_regular_or_missing(directory_fd, name)
        if not replace:
            try:
                existing_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            except FileNotFoundError:
                existing_fd = None
            if existing_fd is not None:
                try:
                    with os.fdopen(existing_fd, "rb") as stream:
                        if stream.read() != content:
                            raise ArtifactError(f"owned artifact already exists with different content: {relative}")
                    return root / relative
                finally:
                    # fdopen owns the descriptor on the normal path.
                    pass
        temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            _assert_regular_or_missing(directory_fd, name)
            os.replace(temporary_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            raise
        return root / relative
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
