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
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise WorktreeError(f"workspace path has no canonical physical identity: {path}") from exc


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

    def _physical_toplevel(self, path: Path) -> Path:
        return Path(self._git(path, "rev-parse", "--show-toplevel")).resolve()

    def _validate_repository_relationship(self, repository: Path, workspace: Path) -> None:
        repository_common = self._common_directory(repository)
        workspace_common = self._common_directory(workspace)
        if repository_common != workspace_common:
            raise WorkspaceConflict(f"{workspace} is not a worktree of {repository}")

    def _validate_checkout(self, capsule: ExecutionCapsule, workspace: Path) -> None:
        probe = workspace
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if probe.exists() and self._physical_toplevel(probe) != workspace.resolve():
            raise WorkspaceConflict("workspace path must equal the physical Git toplevel")
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
        if self._physical_toplevel(repository) != repository.resolve():
            raise WorkspaceConflict("repository_root must equal the physical Git toplevel")
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

    def candidate_head(self, capsule: ExecutionCapsule) -> str:
        """Read the exact commit at a validated candidate workspace."""

        workspace = _absolute_lexical(capsule.workspace_path)
        self._validate_checkout(capsule, workspace)
        return self._git(workspace, "rev-parse", "--verify", "HEAD^{commit}")

    def integrate_candidate(
        self,
        *,
        repository_root: Path,
        candidate_workspace: Path,
        candidate_branch: str,
        candidate_sha: str,
        expected_trunk_head: str,
        strategy: str,
    ) -> dict[str, object]:
        """Verify and apply one controller-authorized Git integration.

        All topology, identity, cleanliness, and conflict checks happen before
        the first mutating Git command.  The final receipt is read back from
        Git after the authorized strategy completes so the ledger can advance
        only on an exact observable result.
        """

        if strategy not in {"merge", "fast_forward", "cherry_pick"}:
            raise WorktreeError("integration strategy is unsupported")
        if len(candidate_sha) != 40 or any(character not in "0123456789abcdef" for character in candidate_sha):
            raise WorktreeError("candidate commit is not a lowercase Git SHA")
        if len(expected_trunk_head) != 40 or any(
            character not in "0123456789abcdef" for character in expected_trunk_head
        ):
            raise WorktreeError("expected trunk HEAD is not a lowercase Git SHA")
        if not candidate_branch or any(character.isspace() for character in candidate_branch):
            raise WorktreeError("candidate branch is invalid")

        repository = _absolute_lexical(Path(repository_root))
        candidate = _absolute_lexical(Path(candidate_workspace))
        _validate_existing_chain(repository)
        _validate_existing_chain(candidate)
        if not repository.is_dir() or not candidate.is_dir():
            raise WorktreeError("integration repository and candidate must be directories")
        if self._physical_toplevel(repository) != repository:
            raise WorktreeError("integration repository must equal the physical Git toplevel")
        if self._physical_toplevel(candidate) != candidate:
            raise WorktreeError("candidate workspace must equal the physical Git toplevel")
        self._validate_repository_relationship(repository, candidate)

        branch = self._git(candidate, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch != candidate_branch:
            raise WorkspaceConflict(f"candidate branch is {branch!r}, expected {candidate_branch!r}")
        before = self._git(repository, "rev-parse", "--verify", "HEAD^{commit}")
        if before != expected_trunk_head:
            raise WorkspaceConflict("trunk HEAD changed before integration")
        candidate_head = self._git(candidate, "rev-parse", "--verify", "HEAD^{commit}")
        if candidate_head != candidate_sha:
            raise WorkspaceConflict("candidate workspace HEAD does not match the authorized commit")
        self._require_clean_checkout(repository, label="trunk")
        self._require_clean_checkout(candidate, label="candidate")
        ancestry = self._runner(("git", "merge-base", "--is-ancestor", candidate_sha, candidate_branch), candidate)
        if ancestry.returncode != 0:
            raise WorkspaceConflict("candidate commit is not an ancestor of its authorized branch")

        preflight = self._runner(("git", "merge-tree", "--write-tree", expected_trunk_head, candidate_sha), repository)
        if preflight.returncode != 0:
            detail = preflight.stderr.strip() or preflight.stdout.strip() or "conflict-free preflight failed"
            raise WorktreeError(f"Git integration preflight failed: {detail}")

        # Recheck the identities and topology immediately before the first
        # mutating command.  This closes the race between preflight and apply.
        if self._git(repository, "rev-parse", "--verify", "HEAD^{commit}") != expected_trunk_head:
            raise WorkspaceConflict("trunk HEAD changed after integration preflight")
        if self._git(candidate, "rev-parse", "--verify", "HEAD^{commit}") != candidate_sha:
            raise WorkspaceConflict("candidate HEAD changed after integration preflight")
        self._require_clean_checkout(repository, label="trunk")
        self._require_clean_checkout(candidate, label="candidate")

        command = {
            "merge": ("git", "merge", "--no-ff", "--no-edit", candidate_sha),
            "fast_forward": ("git", "merge", "--ff-only", candidate_sha),
            "cherry_pick": ("git", "cherry-pick", candidate_sha),
        }[strategy]
        applied = self._runner(command, repository)
        if applied.returncode != 0:
            abort = "cherry-pick" if strategy == "cherry_pick" else "merge"
            self._runner(("git", abort, "--abort"), repository)
            detail = applied.stderr.strip() or applied.stdout.strip() or f"exit {applied.returncode}"
            raise WorktreeError(f"Git integration failed: {detail}")
        after = self._git(repository, "rev-parse", "--verify", "HEAD^{commit}")
        if strategy == "fast_forward" and after != candidate_sha:
            raise WorktreeError("fast-forward integration produced an unexpected trunk HEAD")
        if after == before:
            raise WorktreeError("Git integration did not advance the trunk")
        self._require_clean_checkout(repository, label="integrated trunk")
        return {
            "before_trunk_head": before,
            "after_trunk_head": after,
            "candidate_sha": candidate_sha,
            "candidate_branch": candidate_branch,
            "candidate_workspace": str(candidate),
            "strategy": strategy,
        }

    def _require_clean_checkout(self, path: Path, *, label: str) -> None:
        """Require no tracked or untracked bytes before an integration edge."""

        result = self._runner(("git", "status", "--porcelain", "--untracked-files=all"), path)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise WorktreeError(f"unable to inspect {label} topology: {detail}")
        if result.stdout:
            raise WorkspaceConflict(f"{label} workspace is not clean")
