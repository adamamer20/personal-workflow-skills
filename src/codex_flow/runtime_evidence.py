"""Bounded, immutable runtime-evidence capture.

This module deliberately knows nothing about the ledger or harness.  It only
proves which regular files were copied from an approved checkout into a
content-addressed snapshot and can rehash that snapshot before review or an
effect.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
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


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)


def _absolute_lexical(path: Path) -> Path:
    value = Path(path)
    if not value.is_absolute():
        value = Path.cwd() / value
    return Path(os.path.abspath(os.fspath(value)))


def _same_identity(first: os.stat_result, second: os.stat_result, *, directory: bool = False) -> bool:
    fields = ("st_dev", "st_ino", "st_mode", "st_nlink")
    if any(getattr(first, field) != getattr(second, field) for field in fields):
        return False
    if directory:
        return stat.S_ISDIR(first.st_mode) and stat.S_ISDIR(second.st_mode)
    return stat.S_ISREG(first.st_mode) and stat.S_ISREG(second.st_mode)


def _stat_at(directory: int, name: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence path cannot be inspected safely") from exc


def _open_directory_at(directory: int, name: str, *, create: bool = False) -> int:
    try:
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            raise RuntimeEvidenceError("runtime evidence path cannot be inspected safely") from None
        try:
            os.mkdir(name, 0o700, dir_fd=directory)
        except FileExistsError:
            pass
        before = _stat_at(directory, name)
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence path cannot be inspected safely") from exc
    if not stat.S_ISDIR(before.st_mode):
        raise RuntimeEvidenceError("runtime evidence path is not a directory")
    try:
        child = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory)
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence directory cannot be opened safely") from exc
    try:
        opened = os.fstat(child)
        after = _stat_at(directory, name)
        if not _same_identity(before, opened, directory=True) or not _same_identity(opened, after, directory=True):
            raise RuntimeEvidenceError("runtime evidence ancestor changed during capture")
        return child
    except BaseException:
        os.close(child)
        raise


def _open_directory_chain(path: Path, *, create: bool = False) -> int:
    """Open a directory through no-follow descriptors for every ancestor."""

    lexical = _absolute_lexical(path)
    descriptors: list[int] = []
    try:
        current = os.open(lexical.anchor, _DIRECTORY_FLAGS)
        descriptors.append(current)
        for part in lexical.parts[1:]:
            current = _open_directory_at(current, part, create=create)
            descriptors.append(current)
        result = descriptors[-1]
        descriptors.pop()
        return result
    except RuntimeEvidenceError:
        raise
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence root is unavailable") from exc
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _safe_root(path: Path) -> Path:
    """Validate and return an existing directory's lexical physical path."""

    absolute = _absolute_lexical(path)
    descriptor = _open_directory_chain(absolute)
    try:
        return absolute
    finally:
        os.close(descriptor)


def _safe_destination_root(path: Path) -> Path:
    """Create and validate the content-addressed snapshot parent."""

    absolute = _absolute_lexical(path)
    descriptor = _open_directory_chain(absolute, create=True)
    try:
        return absolute
    finally:
        os.close(descriptor)


def _validated_relative_parts(relative: str) -> tuple[str, ...]:
    path = Path(relative)
    if (
        not isinstance(relative, str)
        or not relative
        or "\x00" in relative
        or "\\" in relative
        or relative == "."
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != relative
    ):
        raise RuntimeEvidenceError("runtime evidence paths must be repository-relative")
    parts = tuple(relative.split("/"))
    if any(not part or part == "." for part in parts):
        raise RuntimeEvidenceError("runtime evidence paths must be repository-relative")
    return parts


def _read_stable_at(directory: int, name: str, *, max_bytes: int) -> tuple[bytes, os.stat_result]:
    try:
        before = _stat_at(directory, name)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeEvidenceError("runtime evidence file must be a single-link regular file")
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory)
    except RuntimeEvidenceError:
        raise
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence file cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if not _same_identity(before, opened) or opened.st_size != before.st_size:
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
        linked = _stat_at(directory, name)
        if (
            not _same_identity(opened, after)
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise RuntimeEvidenceError("runtime evidence file changed during capture")
        if not _same_identity(after, linked):
            raise RuntimeEvidenceError("runtime evidence file path changed during capture")
        return b"".join(chunks), after
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence file cannot be read safely") from exc
    finally:
        os.close(descriptor)


def _walk_source_directory(
    directory: int,
    prefix: str,
    *,
    max_bytes: int,
    contents: dict[str, bytes],
    seen: set[str],
) -> int:
    """Collect one directory while retaining its parent-entry identity."""

    before = os.fstat(directory)
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence directory cannot be listed safely") from exc
    total = 0
    for name in names:
        metadata = _stat_at(directory, name)
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeEvidenceError("runtime evidence does not accept symlinks")
        if stat.S_ISDIR(metadata.st_mode):
            child = _open_directory_at(directory, name)
            try:
                total += _walk_source_directory(
                    child,
                    relative,
                    max_bytes=max_bytes - total,
                    contents=contents,
                    seen=seen,
                )
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            if relative == "manifest.json":
                raise RuntimeEvidenceError("runtime evidence path manifest.json is reserved")
            if relative in seen:
                continue
            data, _ = _read_stable_at(directory, name, max_bytes=max_bytes - total)
            seen.add(relative)
            contents[relative] = data
            total += len(data)
        else:
            raise RuntimeEvidenceError("runtime evidence accepts regular files and directories only")
        if total > max_bytes:
            raise RuntimeEvidenceError("runtime evidence bytes exceed their registered limit")
    after = os.fstat(directory)
    if not _same_identity(before, after, directory=True):
        raise RuntimeEvidenceError("runtime evidence directory changed during capture")
    return total


def _collect_source_files(
    source_descriptor: int,
    relative_roots: tuple[str, ...],
    *,
    max_files: int,
    max_bytes: int,
) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    seen: set[str] = set()
    for relative in relative_roots:
        parts = _validated_relative_parts(relative)
        current = source_descriptor
        opened: list[int] = []
        try:
            for part in parts[:-1]:
                current = _open_directory_at(current, part)
                opened.append(current)
            name = parts[-1]
            metadata = _stat_at(current, name)
            if stat.S_ISLNK(metadata.st_mode):
                raise RuntimeEvidenceError("runtime evidence does not accept symlinks")
            if stat.S_ISDIR(metadata.st_mode):
                child = _open_directory_at(current, name)
                try:
                    _walk_source_directory(
                        child,
                        relative,
                        max_bytes=max_bytes - sum(len(item) for item in contents.values()),
                        contents=contents,
                        seen=seen,
                    )
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode):
                if relative == "manifest.json":
                    raise RuntimeEvidenceError("runtime evidence path manifest.json is reserved")
                if relative not in seen:
                    data, _ = _read_stable_at(
                        current, name, max_bytes=max_bytes - sum(len(item) for item in contents.values())
                    )
                    seen.add(relative)
                    contents[relative] = data
            else:
                raise RuntimeEvidenceError("runtime evidence accepts regular files and directories only")
            if len(contents) > max_files:
                raise RuntimeEvidenceError("runtime evidence file count exceeds its registered limit")
            if sum(len(item) for item in contents.values()) > max_bytes:
                raise RuntimeEvidenceError("runtime evidence bytes exceed their registered limit")
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)
    return {key: contents[key] for key in sorted(contents)}


def _ensure_stage_directory(root: int, parts: tuple[str, ...]) -> int:
    current = root
    opened: list[int] = []
    try:
        for part in parts:
            current = _open_directory_at(current, part, create=True)
            opened.append(current)
        result = current
        if not parts:
            return root
        opened.pop()
        return result
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise RuntimeEvidenceError("runtime evidence snapshot write was short")
        offset += written


def _fsync_directory_tree(directory: int) -> None:
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence snapshot directory cannot be listed") from exc
    for name in names:
        metadata = _stat_at(directory, name)
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeEvidenceError("runtime evidence snapshot contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            child = _open_directory_at(directory, name)
            try:
                _fsync_directory_tree(child)
            finally:
                os.close(child)
    try:
        os.fsync(directory)
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence snapshot directory cannot be synced") from exc


def _write_snapshot_staging(root: int, contents: dict[str, bytes], manifest: RuntimeEvidenceManifest) -> None:
    try:
        for relative, data in contents.items():
            parts = tuple(relative.split("/"))
            parent = _ensure_stage_directory(root, parts[:-1])
            try:
                descriptor = os.open(parts[-1], _WRITE_FLAGS, 0o600, dir_fd=parent)
                try:
                    _write_all(descriptor, data)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.fsync(parent)
            finally:
                if parts[:-1]:
                    os.close(parent)
        descriptor = os.open("manifest.json", _WRITE_FLAGS, 0o600, dir_fd=root)
        try:
            _write_all(descriptor, manifest.canonical_bytes())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory_tree(root)
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence snapshot publication failed") from exc


def _read_staged_manifest_at(root: int) -> RuntimeEvidenceManifest | None:
    try:
        data, _ = _read_stable_at(root, "manifest.json", max_bytes=2 * 1024 * 1024)
        from .domain import strict_json_loads

        value = strict_json_loads(data, max_bytes=2 * 1024 * 1024)
        if not isinstance(value, dict):
            return None
        return RuntimeEvidenceManifest.from_json(value)
    except (RuntimeEvidenceError, TypeError, ValueError):
        return None


def _create_staging_directory(destination: Path, destination_descriptor: int) -> tuple[str, Path, int]:
    """Create and open one staging directory relative to the held root."""

    for _ in range(16):
        name = f".runtime-evidence-{os.urandom(12).hex()}"
        try:
            os.mkdir(name, 0o700, dir_fd=destination_descriptor)
        except FileExistsError:
            continue
        try:
            descriptor = _open_directory_at(destination_descriptor, name)
        except BaseException:
            # The directory remains retained as forensic state if opening it
            # loses its identity; capture still fails closed.
            raise
        return name, destination / name, descriptor
    raise RuntimeEvidenceError("runtime evidence staging directory identity collided")


def _publish_staging(
    staging_name: str,
    final_name: str,
    destination_descriptor: int,
    manifest: RuntimeEvidenceManifest,
) -> None:
    """Atomically publish a complete directory, never replacing a valid one."""

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        if (
            renameat2(
                destination_descriptor,
                os.fsencode(staging_name),
                destination_descriptor,
                os.fsencode(final_name),
                1,
            )
            != 0
        ):
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
    except AttributeError as exc:
        raise RuntimeEvidenceError("atomic no-overwrite snapshot publication is unavailable") from exc
    except OSError as exc:
        if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY, errno.EISDIR}:
            raise RuntimeEvidenceError("runtime evidence snapshot publication failed") from exc
        final_descriptor: int | None = None
        try:
            final_descriptor = _open_directory_at(destination_descriptor, final_name)
            if _verify_runtime_evidence_at(RuntimeEvidenceSnapshot(manifest, Path(final_name)), final_descriptor):
                return
        except (RuntimeEvidenceError, TypeError, ValueError):
            pass
        finally:
            if final_descriptor is not None:
                os.close(final_descriptor)
        raise RuntimeEvidenceError("runtime evidence snapshot publication raced") from exc
    try:
        os.fsync(destination_descriptor)
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence snapshot parent cannot be synced") from exc


def _reconcile_final_or_staging(
    destination: Path,
    manifest: RuntimeEvidenceManifest,
    *,
    destination_descriptor: int | None = None,
) -> RuntimeEvidenceSnapshot | None:
    owns_descriptor = destination_descriptor is None
    destination_descriptor = (
        destination_descriptor if destination_descriptor is not None else _open_directory_chain(destination)
    )
    try:
        final_root = destination / manifest.sha256
        try:
            metadata = _stat_at(destination_descriptor, final_root.name)
        except RuntimeEvidenceError:
            metadata = None
        if metadata is not None:
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeEvidenceError("runtime evidence snapshot identity is not a directory")
            final_descriptor: int | None = None
            try:
                final_descriptor = _open_directory_at(destination_descriptor, final_root.name)
                if _verify_runtime_evidence_at(RuntimeEvidenceSnapshot(manifest, final_root), final_descriptor):
                    return RuntimeEvidenceSnapshot(manifest, final_root)
            except RuntimeEvidenceError:
                pass
            finally:
                if final_descriptor is not None:
                    os.close(final_descriptor)
            raise RuntimeEvidenceError("runtime evidence snapshot identity already names different bytes")
        try:
            names = sorted(os.listdir(destination_descriptor))
        except OSError as exc:
            raise RuntimeEvidenceError("runtime evidence snapshot root cannot be listed") from exc
        for name in names:
            if not name.startswith(".runtime-evidence-"):
                continue
            candidate_descriptor: int | None = None
            try:
                candidate_descriptor = _open_directory_at(destination_descriptor, name)
                staged_manifest = _read_staged_manifest_at(candidate_descriptor)
                if staged_manifest is None or staged_manifest.canonical_bytes() != manifest.canonical_bytes():
                    continue
                if not _verify_runtime_evidence_at(
                    RuntimeEvidenceSnapshot(manifest, destination / name), candidate_descriptor
                ):
                    continue
                _publish_staging(name, final_root.name, destination_descriptor, manifest)
                return RuntimeEvidenceSnapshot(manifest, final_root)
            except RuntimeEvidenceError:
                continue
            finally:
                if candidate_descriptor is not None:
                    os.close(candidate_descriptor)
        return None
    finally:
        if owns_descriptor:
            os.close(destination_descriptor)


def capture_runtime_evidence(
    source_root: Path,
    artifact_paths: tuple[str, ...] | list[str],
    *,
    max_files: int,
    max_bytes: int,
    manifest: RuntimeEvidenceManifest,
    snapshot_root: Path,
) -> RuntimeEvidenceSnapshot:
    """Capture declared regular files and publish one immutable snapshot."""

    if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files <= 0:
        raise RuntimeEvidenceError("runtime evidence max_files must be positive")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise RuntimeEvidenceError("runtime evidence max_bytes must be positive")
    if not isinstance(manifest, RuntimeEvidenceManifest):
        raise RuntimeEvidenceError("runtime evidence capture requires a typed manifest")
    destination = _absolute_lexical(Path(snapshot_root))
    # Keep both roots open for the whole operation.  A lexical path check
    # followed by a second path open would let an ancestor swap redirect a
    # valid source or snapshot root between the check and the read/write.
    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    staging_descriptor: int | None = None
    try:
        source_descriptor = _open_directory_chain(Path(source_root))
        destination_descriptor = _open_directory_chain(destination, create=True)
        contents = _collect_source_files(
            source_descriptor,
            tuple(artifact_paths),
            max_files=max_files,
            max_bytes=max_bytes,
        )
        expected = tuple(
            RuntimeEvidenceFile(relative, hashlib.sha256(data).hexdigest(), len(data))
            for relative, data in contents.items()
        )
        if expected != manifest.files:
            if manifest.files:
                raise RuntimeEvidenceError("runtime evidence manifest does not match captured files")
            manifest = replace(manifest, files=expected)
        reconciled = _reconcile_final_or_staging(
            destination,
            manifest,
            destination_descriptor=destination_descriptor,
        )
        if reconciled is not None:
            return reconciled
        staging_name, _staging_path, staging_descriptor = _create_staging_directory(destination, destination_descriptor)
        _write_snapshot_staging(staging_descriptor, contents, manifest)
        os.close(staging_descriptor)
        staging_descriptor = None
        _publish_staging(staging_name, manifest.sha256, destination_descriptor, manifest)
        return RuntimeEvidenceSnapshot(manifest, destination / manifest.sha256)
    except RuntimeEvidenceError:
        raise
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence snapshot publication failed") from exc
    finally:
        if staging_descriptor is not None:
            os.close(staging_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)


def _walk_snapshot_directory(directory: int, prefix: str, files: set[str], directories: set[str]) -> None:
    before = os.fstat(directory)
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        raise RuntimeEvidenceError("runtime evidence snapshot cannot be listed safely") from exc
    for name in names:
        metadata = _stat_at(directory, name)
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeEvidenceError("runtime evidence snapshot contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            directories.add(relative)
            child = _open_directory_at(directory, name)
            try:
                _walk_snapshot_directory(child, relative, files, directories)
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise RuntimeEvidenceError("runtime evidence snapshot contains a hardlink alias")
            files.add(relative)
        else:
            raise RuntimeEvidenceError("runtime evidence snapshot contains an unsafe file")
    after = os.fstat(directory)
    if not _same_identity(before, after, directory=True):
        raise RuntimeEvidenceError("runtime evidence snapshot directory changed during verification")


def _read_relative_file(root: int, parts: tuple[str, ...], *, max_bytes: int) -> bytes:
    current = root
    opened: list[int] = []
    try:
        for part in parts[:-1]:
            current = _open_directory_at(current, part)
            opened.append(current)
        data, _ = _read_stable_at(current, parts[-1], max_bytes=max_bytes)
        return data
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _verify_runtime_evidence_at(snapshot: RuntimeEvidenceSnapshot, descriptor: int) -> bool:
    """Rehash every manifest member and the manifest file itself."""

    if not isinstance(snapshot, RuntimeEvidenceSnapshot) or not isinstance(snapshot.manifest, RuntimeEvidenceManifest):
        raise RuntimeEvidenceError("runtime evidence snapshot is not typed")
    manifest_data, _ = _read_stable_at(descriptor, "manifest.json", max_bytes=2 * 1024 * 1024)
    if manifest_data != snapshot.manifest.canonical_bytes():
        raise RuntimeEvidenceError("runtime evidence manifest bytes changed")
    expected_files = {item.relative_path for item in snapshot.manifest.files} | {"manifest.json"}
    expected_directories = {
        "/".join(parts[:index])
        for item in expected_files
        for parts in [tuple(item.split("/"))]
        for index in range(1, len(parts))
    }
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    _walk_snapshot_directory(descriptor, "", actual_files, actual_directories)
    if actual_files != expected_files or actual_directories != expected_directories:
        raise RuntimeEvidenceError("runtime evidence snapshot contains unexpected files")
    for item in snapshot.manifest.files:
        data = _read_relative_file(descriptor, _validated_relative_parts(item.relative_path), max_bytes=item.size_bytes)
        if len(data) != item.size_bytes or hashlib.sha256(data).hexdigest() != item.sha256:
            raise RuntimeEvidenceError("runtime evidence snapshot member bytes changed")
    return True


def verify_runtime_evidence(snapshot: RuntimeEvidenceSnapshot) -> bool:
    """Rehash every manifest member and the manifest file itself."""

    root = Path(snapshot.root)
    descriptor = _open_directory_chain(root)
    try:
        return _verify_runtime_evidence_at(snapshot, descriptor)
    finally:
        os.close(descriptor)


__all__ = ["RuntimeEvidenceError", "RuntimeEvidenceSnapshot", "capture_runtime_evidence", "verify_runtime_evidence"]
