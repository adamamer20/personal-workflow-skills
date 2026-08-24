"""Controller-owned Git workspace selection and creation.

Workspace identity belongs to a semantic program lane.  This module never
derives paths from a model, thread, dispatch, or attempt identifier and never
deletes worktrees automatically.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .domain import ExecutionCapsule, WorkspaceMode


class WorktreeError(RuntimeError):
    """A selected workspace does not satisfy the controller contract."""


class WorkspaceConflict(WorktreeError):
    """An existing path or branch conflicts with the requested semantic lane."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class WorkspaceSelection:
    repository_root: Path
    workspace_path: Path
    mode: WorkspaceMode
    branch: str
    base_sha: str
    lane: str
    created: bool


CommandRunner = Callable[[Sequence[str], Path], CommandResult]


def _run(argv: Sequence[str], cwd: Path) -> CommandResult:
    completed = subprocess.run(
        list(argv),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _absolute_lexical(path: Path) -> Path:
    if not path.is_absolute():
        raise WorktreeError(f"workspace path must be absolute: {path}")
    return Path(os.path.normpath(os.fspath(path)))


def _validate_existing_chain(path: Path, *, allow_missing_leaf: bool = False) -> None:
    path = _absolute_lexical(path)
    current = Path(path.anchor)
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_leaf and index == len(parts) - 1:
                return
            raise WorktreeError(f"workspace ancestor does not exist: {current}") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise WorktreeError(f"workspace path must not traverse symlinks: {current}")
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise WorktreeError(f"workspace ancestor is not a directory: {current}")


class WorktreeManager:
    """Validate or create exactly the workspace selected by a capsule."""

    def __init__(self, *, runner: CommandRunner = _run) -> None:
        self._runner = runner

    def _git(self, cwd: Path, *arguments: str) -> str:
        result = self._runner(("git", *arguments), cwd)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise WorktreeError(f"git {' '.join(arguments)} failed: {detail}")
        return result.stdout.strip()

    def _common_directory(self, path: Path) -> Path:
        common = self._git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        return Path(common)

    def _validate_repository_relationship(self, repository: Path, workspace: Path) -> None:
        repository_common = self._common_directory(repository)
        workspace_common = self._common_directory(workspace)
        if repository_common != workspace_common:
            raise WorkspaceConflict(f"{workspace} is not a worktree of {repository}")

    def _validate_checkout(self, capsule: ExecutionCapsule, workspace: Path) -> None:
        _validate_existing_chain(workspace)
        if not workspace.is_dir():
            raise WorktreeError(f"workspace is not a directory: {workspace}")
        self._validate_repository_relationship(capsule.repository_root, workspace)
        branch = self._git(workspace, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch != capsule.branch:
            raise WorkspaceConflict(f"workspace branch is {branch!r}, expected {capsule.branch!r}")
        ancestor = self._runner(("git", "merge-base", "--is-ancestor", capsule.base_sha, "HEAD"), workspace)
        if ancestor.returncode != 0:
            raise WorkspaceConflict("workspace HEAD does not descend from the selected base SHA")

    def validate_plan(self, capsule: ExecutionCapsule) -> None:
        """Validate repository/base/topology without creating a worktree."""

        repository = _absolute_lexical(capsule.repository_root)
        workspace = _absolute_lexical(capsule.workspace_path)
        _validate_existing_chain(repository)
        if not repository.is_dir():
            raise WorktreeError(f"repository root is not a directory: {repository}")
        resolved_base = self._git(repository, "rev-parse", f"{capsule.base_sha}^{{commit}}")
        if resolved_base != capsule.base_sha:
            raise WorkspaceConflict("capsule base SHA is not the repository's exact resolved commit")
        if capsule.workspace_mode is WorkspaceMode.CURRENT_CHECKOUT:
            if workspace != repository:
                raise WorkspaceConflict("current_checkout requires workspace_path == repository_root")
            self._validate_checkout(capsule, workspace)
        elif capsule.workspace_mode is WorkspaceMode.EXISTING_WORKTREE:
            if workspace == repository:
                raise WorkspaceConflict("existing_worktree must name a distinct linked checkout")
            self._validate_checkout(capsule, workspace)
        else:
            expected = repository.parent / f"{repository.name}.worktrees" / capsule.lane
            if workspace != expected:
                raise WorkspaceConflict(f"managed worktree must use semantic sibling path {expected}")
            _validate_existing_chain(expected.parent if expected.parent.exists() else expected.parent.parent)
            if workspace.exists():
                self._validate_checkout(capsule, workspace)

    def select(self, capsule: ExecutionCapsule) -> WorkspaceSelection:
        repository = _absolute_lexical(capsule.repository_root)
        workspace = _absolute_lexical(capsule.workspace_path)
        self.validate_plan(capsule)

        if capsule.workspace_mode is WorkspaceMode.CURRENT_CHECKOUT:
            if workspace != repository:
                raise WorkspaceConflict("current_checkout requires workspace_path == repository_root")
            self._validate_checkout(capsule, workspace)
            created = False
        elif capsule.workspace_mode is WorkspaceMode.EXISTING_WORKTREE:
            if workspace == repository:
                raise WorkspaceConflict("existing_worktree must name a distinct linked checkout")
            self._validate_checkout(capsule, workspace)
            created = False
        else:
            expected_root = repository.parent / f"{repository.name}.worktrees"
            expected_path = expected_root / capsule.lane
            if workspace != expected_path:
                raise WorkspaceConflict(f"managed worktree must use semantic sibling path {expected_path}")
            if not expected_root.exists():
                _validate_existing_chain(expected_root.parent)
                try:
                    os.mkdir(expected_root, mode=0o700)
                except FileExistsError:
                    pass
            _validate_existing_chain(expected_root)
            if workspace.exists():
                self._validate_checkout(capsule, workspace)
                created = False
            else:
                _validate_existing_chain(workspace, allow_missing_leaf=True)
                branch_exists = (
                    self._runner(
                        ("git", "show-ref", "--verify", "--quiet", f"refs/heads/{capsule.branch}"), repository
                    ).returncode
                    == 0
                )
                if branch_exists:
                    self._git(repository, "worktree", "add", os.fspath(workspace), capsule.branch)
                else:
                    self._git(
                        repository,
                        "worktree",
                        "add",
                        "-b",
                        capsule.branch,
                        os.fspath(workspace),
                        capsule.base_sha,
                    )
                created = True
                self._validate_checkout(capsule, workspace)

        return WorkspaceSelection(
            repository,
            workspace,
            capsule.workspace_mode,
            capsule.branch,
            capsule.base_sha,
            capsule.lane,
            created,
        )
