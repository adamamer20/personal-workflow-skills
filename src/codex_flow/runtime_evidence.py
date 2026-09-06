"""Bounded, immutable runtime-evidence capture.

This module deliberately knows nothing about the ledger or harness.  It only
proves which regular files were copied from an approved checkout into a
content-addressed snapshot and can rehash that snapshot before review or an
effect.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .domain import RuntimeEvidenceFile, RuntimeEvidenceManifest


class RuntimeEvidenceError(ValueError):
    """Evidence capture or verification cannot be established safely."""


@dataclass(frozen=True, slots=True)
class RuntimeEvidenceSnapshot:
    """Published immutable evidence manifest and its snapshot directory."""

    manifest: RuntimeEvidenceManifest
    root: Path

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def digest(self) -> str:
        return self.manifest.sha256


def _safe_root(path: Path) -> Path:
    """Resolve an existing directory without traversing a symlink."""

    lexical = Path(path)
    if not lexical.is_absolute():
        lexical = Path.cwd() / lexical
    lexical = Path(os.path.abspath(os.fspath(lexical)))
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise RuntimeEvidenceError("runtime evidence root is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeEvidenceError("runtime evidence root traverses a symlink")
    try:
        root = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeEvidenceError("runtime evidence root is unavailable") from exc
    if not root.is_dir():
        raise RuntimeEvidenceError("runtime evidence root is not a directory")
    return root


def _safe_destination_root(path: Path) -> Path:
    """Create and validate the content-addressed snapshot parent."""

    lexical = Path(path)
    if not lexical.is_absolute():
        lexical = Path.cwd() / lexical
    lexical = Path(os.path.abspath(os.fspath(lexical)))
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current, 0o700)
            except FileExistsError:
                pass
            metadata = os.lstat(current)
        except OSError as exc:
            raise RuntimeEvidenceError("runtime evidence snapshot root is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeEvidenceError("runtime evidence snapshot root is not a safe directory chain")
    return _safe_root(lexical)


def _relative_member(root: Path, relative: str) -> Path:
    candidate = root / relative
    try:
        lexical = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise RuntimeEvidenceError("runtime evidence path cannot be resolved") from exc
    if lexical != candidate or not lexical.is_relative_to(root):
        raise RuntimeEvidenceError("runtime evidence path escapes its approved root")
    return candidate


def _iter_members(root: Path, relative_roots: tuple[str, ...]) -> tuple[Path, ...]:
    members: list[Path] = []
    for relative in relative_roots:
        if (
            not isinstance(relative, str)
            or not relative
            or "\x00" in relative
            or "\\" in relative
            or relative == "."
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or Path(relative).as_posix() != relative
        ):
            raise RuntimeEvidenceError("runtime evidence paths must be repository-relative")
        candidate = _relative_member(root, relative)
        try:
            metadata = os.lstat(candidate)
        except OSError as exc:
            raise RuntimeEvidenceError("declared runtime evidence path is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeEvidenceError("runtime evidence does not accept symlinks")
        if stat.S_ISREG(metadata.st_mode):
            members.append(candidate)
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeEvidenceError("runtime evidence accepts regular files and directories only")
        for current, directories, filenames in os.walk(candidate, topdown=True, followlinks=False):
            current_path = Path(current)
            kept_directories: list[str] = []
            for name in directories:
                child = current_path / name
                child_metadata = os.lstat(child)
                if stat.S_ISLNK(child_metadata.st_mode):
                    raise RuntimeEvidenceError("runtime evidence directory contains a symlink")
                if not stat.S_ISDIR(child_metadata.st_mode):
                    raise RuntimeEvidenceError("runtime evidence directory contains a non-directory")
                kept_directories.append(name)
            directories[:] = kept_directories
            for name in filenames:
                child = current_path / name
                child_metadata = os.lstat(child)
                if stat.S_ISLNK(child_metadata.st_mode):
                    raise RuntimeEvidenceError("runtime evidence contains a symlink")
                if not stat.S_ISREG(child_metadata.st_mode):
                    raise RuntimeEvidenceError("runtime evidence contains a non-regular file")
                members.append(child)
    unique = {path.relative_to(root).as_posix(): path for path in members}
    return tuple(unique[key] for key in sorted(unique))


def _read_stable(path: Path, *, max_bytes: int) -> tuple[bytes, os.stat_result]:
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeEvidenceError("runtime evidence file must be a single-link regular file")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except RuntimeEvidenceError:
        raise
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence file cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
        ):
            raise RuntimeEvidenceError("runtime evidence file changed during capture")
        if opened.st_size > max_bytes:
            raise RuntimeEvidenceError("runtime evidence file exceeds its byte limit")
        chunks: list[bytes] = []
        remaining = max_bytes
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, remaining + 1))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
            if remaining < 0:
                raise RuntimeEvidenceError("runtime evidence file exceeds its byte limit")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise RuntimeEvidenceError("runtime evidence file changed during capture")
        return b"".join(chunks), after
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence file cannot be read safely") from exc
    finally:
        os.close(descriptor)


def capture_runtime_evidence(
    source_root: Path,
    artifact_paths: tuple[str, ...] | list[str],
    *,
    max_files: int,
    max_bytes: int,
    manifest: RuntimeEvidenceManifest,
    snapshot_root: Path,
) -> RuntimeEvidenceSnapshot:
    """Capture declared regular files and publish one immutable snapshot.

    ``manifest.files`` is checked against the captured bytes; callers cannot
    supply a worker-authored digest that differs from what the harness reads.
    """

    if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files <= 0:
        raise RuntimeEvidenceError("runtime evidence max_files must be positive")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise RuntimeEvidenceError("runtime evidence max_bytes must be positive")
    if not isinstance(manifest, RuntimeEvidenceManifest):
        raise RuntimeEvidenceError("runtime evidence capture requires a typed manifest")
    source = _safe_root(Path(source_root))
    destination = _safe_destination_root(Path(snapshot_root))
    members = _iter_members(source, tuple(artifact_paths))
    if len(members) > max_files:
        raise RuntimeEvidenceError("runtime evidence file count exceeds its registered limit")
    contents: dict[str, bytes] = {}
    total = 0
    for member in members:
        relative = member.relative_to(source).as_posix()
        remaining = max_bytes - total
        data, _metadata = _read_stable(member, max_bytes=remaining)
        total += len(data)
        if total > max_bytes:
            raise RuntimeEvidenceError("runtime evidence bytes exceed their registered limit")
        contents[relative] = data
    expected = tuple(
        RuntimeEvidenceFile(relative, hashlib.sha256(data).hexdigest(), len(data))
        for relative, data in sorted(contents.items())
    )
    if expected != manifest.files:
        # The harness supplies an intentionally empty file tuple while it is
        # asking this module to discover and authenticate the declared files.
        # A non-empty worker/controller assertion is never replaced: only the
        # empty placeholder is completed from the no-follow capture.
        if manifest.files:
            raise RuntimeEvidenceError("runtime evidence manifest does not match captured files")
        manifest = replace(manifest, files=expected)
    final_root = destination / manifest.sha256
    if not final_root.is_relative_to(destination):
        raise RuntimeEvidenceError("runtime evidence snapshot path escapes its root")
    if final_root.exists():
        try:
            if verify_runtime_evidence(RuntimeEvidenceSnapshot(manifest, final_root)):
                return RuntimeEvidenceSnapshot(manifest, final_root)
        except RuntimeEvidenceError:
            pass
        raise RuntimeEvidenceError("runtime evidence snapshot identity already names different bytes")
    staging = Path(tempfile.mkdtemp(prefix=".runtime-evidence-", dir=destination))

    def write_all(descriptor: int, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise RuntimeEvidenceError("runtime evidence snapshot write was short")
            offset += written

    try:
        for relative, data in contents.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            descriptor = os.open(target, flags, 0o600)
            try:
                write_all(descriptor, data)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        manifest_path = staging / "manifest.json"
        descriptor = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            write_all(descriptor, manifest.canonical_bytes())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_descriptor = os.open(staging, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        try:
            os.mkdir(final_root, 0o700)
        except FileExistsError:
            raise RuntimeEvidenceError("runtime evidence snapshot publication raced") from None
        for relative in (*contents.keys(), "manifest.json"):
            source_path = staging / relative
            target_path = final_root / relative
            target_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source_path, target_path)
        directory_descriptor = os.open(final_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        parent_descriptor = os.open(destination, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
        # Keep incomplete staging directories as forensic orphan snapshots.
        # A fully published snapshot has no remaining staging members.
        shutil.rmtree(staging)
        return RuntimeEvidenceSnapshot(manifest, final_root)
    except RuntimeEvidenceError:
        raise
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence snapshot publication failed") from exc


def verify_runtime_evidence(snapshot: RuntimeEvidenceSnapshot) -> bool:
    """Rehash every manifest member and the manifest file itself."""

    if not isinstance(snapshot, RuntimeEvidenceSnapshot) or not isinstance(snapshot.manifest, RuntimeEvidenceManifest):
        raise RuntimeEvidenceError("runtime evidence snapshot is not typed")
    root = _safe_root(snapshot.root)
    manifest_path = root / "manifest.json"
    manifest_data, _ = _read_stable(manifest_path, max_bytes=2 * 1024 * 1024)
    if manifest_data != snapshot.manifest.canonical_bytes():
        raise RuntimeEvidenceError("runtime evidence manifest bytes changed")
    expected_members = {item.relative_path for item in snapshot.manifest.files} | {"manifest.json"}
    actual_members: set[str] = set()
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directories:
            child = current_path / name
            metadata = os.lstat(child)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeEvidenceError("runtime evidence snapshot contains an unsafe directory")
        for name in filenames:
            child = current_path / name
            metadata = os.lstat(child)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RuntimeEvidenceError("runtime evidence snapshot contains an unsafe file")
            actual_members.add(child.relative_to(root).as_posix())
    if actual_members != expected_members:
        raise RuntimeEvidenceError("runtime evidence snapshot contains unexpected files")
    for item in snapshot.manifest.files:
        path = _relative_member(root, item.relative_path)
        data, _ = _read_stable(path, max_bytes=item.size_bytes)
        if len(data) != item.size_bytes or hashlib.sha256(data).hexdigest() != item.sha256:
            raise RuntimeEvidenceError("runtime evidence snapshot member bytes changed")
    return True


__all__ = ["RuntimeEvidenceError", "RuntimeEvidenceSnapshot", "capture_runtime_evidence", "verify_runtime_evidence"]
