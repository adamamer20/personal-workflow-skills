"""Controller-owned Git workspace selection and creation.

Workspace identity belongs to a semantic program lane.  This module never
derives paths from a model, thread, dispatch, or attempt identifier and never
deletes worktrees automatically.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .domain import CandidateDisposition, CandidateRecord, ExecutionCapsule, WorkspaceMode


class WorktreeError(RuntimeError):
    """A selected workspace does not satisfy the controller contract."""


class WorkspaceConflict(WorktreeError):
    """An existing path or branch conflicts with the requested semantic lane."""


class CandidateIntegrityError(WorktreeError):
    """A candidate commit range violates the capsule's path ownership."""


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


def _validate_raw_integration_chain(path: Path) -> None:
    """Reject aliases before integration paths are physically canonicalized."""

    try:
        lexical = Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError) as exc:
        raise WorktreeError(f"integration path has no bounded lexical identity: {path}") from exc
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            raise WorktreeError(f"integration path does not exist: {current}") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise WorktreeError(f"integration path must not traverse symlinks: {current}")


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

    def __init__(self, *, runner: CommandRunner = _run, mutation_lock_path: Path | None = None) -> None:
        self._runner = runner
        self._mutation_lock_path = Path(mutation_lock_path) if mutation_lock_path is not None else None

    @contextmanager
    def _mutation_lock(self) -> Iterator[None]:
        """Serialize Git effects through the existing controller lock."""

        path = self._mutation_lock_path
        if path is None:
            yield
            return
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise WorktreeError("integration mutation authority is unavailable") from exc
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as exc:
                raise WorktreeError("integration mutation authority is unavailable") from exc
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

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

    def inspect_terminal_workspace(
        self,
        capsule: ExecutionCapsule,
        *,
        candidate_sha: str | None = None,
        require_direct_candidate: bool = False,
        predecessor_sha: str | None = None,
    ) -> CandidateRecord:
        """Inspect an executor checkout without mutating it.

        A clean checkout yields one verified commit only when it is ahead of
        the capsule base.  An explicit candidate may be verified from a dirty
        checkout when its commit ancestry is exact; the dirty bytes remain an
        explicit fact and never become part of the candidate commit.
        """

        workspace = _absolute_lexical(capsule.workspace_path)
        self._validate_checkout(capsule, workspace)
        try:
            head = self._git(workspace, "rev-parse", "--verify", "HEAD^{commit}")
            status = self._runner(("git", "status", "--porcelain", "--untracked-files=all"), workspace)
        except WorktreeError:
            raise
        if status.returncode != 0:
            raise WorktreeError("unable to inspect terminal workspace status")
        status_digest = hashlib.sha256(status.stdout.encode("utf-8")).hexdigest() if status.stdout else None
        if status.stdout and candidate_sha is None and predecessor_sha is None:
            return CandidateRecord(
                CandidateDisposition.PRESERVED_DIRTY_WORKSPACE,
                workspace,
                workspace_head=head,
                workspace_digest=status_digest,
                dirty=True,
                reason="workspace contains tracked or untracked bytes",
            )
        selected = candidate_sha or (head if head != capsule.base_sha else None)
        if selected is None:
            return CandidateRecord(CandidateDisposition.NO_CANDIDATE, workspace, workspace_head=head)
        if len(selected) != 40 or any(character not in "0123456789abcdef" for character in selected):
            raise WorktreeError("candidate commit is not a lowercase Git SHA")
        ancestry = self._runner(("git", "merge-base", "--is-ancestor", selected, "HEAD"), workspace)
        if ancestry.returncode != 0:
            raise WorkspaceConflict("candidate commit is not present in the dispatch workspace")
        self._validate_candidate_history(capsule, workspace, selected)
        if require_direct_candidate:
            parents = self._commit_parents(workspace, selected)
            if parents != [capsule.base_sha]:
                raise WorkspaceConflict("candidate commit contains an unrelated or non-direct history")
        if predecessor_sha is not None:
            self._validate_linear_successor(workspace, predecessor_sha, selected)
        return CandidateRecord(
            CandidateDisposition.VERIFIED_COMMIT,
            workspace,
            commit_sha=selected,
            workspace_head=head,
            workspace_digest=status_digest,
            dirty=bool(status.stdout),
            reason="workspace contains tracked or untracked bytes alongside the explicit candidate"
            if status.stdout
            else None,
        )

    def adopt_candidate(self, capsule: ExecutionCapsule, candidate_sha: str) -> CandidateRecord:
        """Validate one pre-existing serial candidate for later review."""

        return self.inspect_terminal_workspace(capsule, candidate_sha=candidate_sha, require_direct_candidate=True)

    def validate_candidate(self, capsule: ExecutionCapsule, candidate_sha: str) -> None:
        """Revalidate candidate ancestry and path ownership before an effect."""

        workspace = _absolute_lexical(capsule.workspace_path)
        self._validate_checkout(capsule, workspace)
        self._validate_candidate_history(capsule, workspace, candidate_sha)
        self._require_clean_checkout(workspace, label="candidate")

    def verify_serial_candidate(
        self,
        *,
        repository_root: Path,
        candidate_sha: str,
        expected_trunk_head: str,
        strategy: str,
        capsule: ExecutionCapsule | None = None,
    ) -> dict[str, object]:
        """Return a logical promotion receipt for an already-committed checkout.

        Serial milestones commit directly in the program checkout. Promotion
        therefore records the exact commit without running a same-checkout Git
        merge, fast-forward, cherry-pick, or reset.
        """

        root = _absolute_lexical(Path(repository_root))
        _validate_raw_integration_chain(root)
        if any(
            len(value) != 40 or any(character not in "0123456789abcdef" for character in value)
            for value in (candidate_sha, expected_trunk_head)
        ):
            raise WorktreeError("serial promotion commit identity is invalid")
        head = self._git(root, "rev-parse", "--verify", "HEAD^{commit}")
        status = self._git(root, "status", "--porcelain", "--untracked-files=all")
        if status:
            raise WorktreeError("serial promotion checkout is dirty")
        if head != candidate_sha:
            raise WorkspaceConflict("serial candidate is not the current program checkout HEAD")
        if capsule is not None:
            self._validate_candidate_history(capsule, root, candidate_sha)
        lineage = self._linear_successor_lineage(root, expected_trunk_head, candidate_sha)
        parents = self._commit_parents(root, candidate_sha)
        tree = self._git(root, "show", "-s", "--format=%T", candidate_sha)
        if strategy not in {"merge", "fast_forward", "cherry_pick"}:
            raise WorktreeError("serial promotion strategy is unsupported")
        return {
            "before_trunk_head": expected_trunk_head,
            "after_trunk_head": candidate_sha,
            "parents": parents,
            "lineage": lineage,
            "linear_chain": True,
            "tree": tree,
            "logical_promotion": True,
            "strategy": strategy,
        }

    def _linear_successor_lineage(self, repository: Path, predecessor_sha: str, candidate_sha: str) -> list[str]:
        """Return one direct-parent chain from a durable predecessor to a candidate."""

        if predecessor_sha == candidate_sha:
            raise WorkspaceConflict("serial candidate is not ahead of the durable trunk")
        lineage: list[str] = [candidate_sha]
        current = candidate_sha
        for _ in range(4096):
            parents = self._commit_parents(repository, current)
            if len(parents) != 1:
                raise WorkspaceConflict("serial candidate history contains a merge or unrelated ancestry")
            current = parents[0]
            lineage.append(current)
            if current == predecessor_sha:
                lineage.reverse()
                return lineage
        raise WorkspaceConflict("serial candidate history exceeds the bounded linear chain")

    def _validate_linear_successor(self, repository: Path, predecessor_sha: str, candidate_sha: str) -> None:
        """Require a repair candidate to be a forward-only direct-parent successor."""

        if len(predecessor_sha) != 40 or any(character not in "0123456789abcdef" for character in predecessor_sha):
            raise CandidateIntegrityError("candidate predecessor identity is invalid")
        self._linear_successor_lineage(repository, predecessor_sha, candidate_sha)

    def integrate_candidate(
        self,
        *,
        repository_root: Path,
        candidate_workspace: Path,
        candidate_branch: str,
        candidate_sha: str,
        expected_trunk_head: str,
        strategy: str,
        capsule: ExecutionCapsule | None = None,
    ) -> dict[str, object]:
        """Serialize and apply one controller-authorized Git integration."""

        _validate_raw_integration_chain(Path(repository_root))
        with self._mutation_lock():
            return self._integrate_candidate(
                repository_root=repository_root,
                candidate_workspace=candidate_workspace,
                candidate_branch=candidate_branch,
                candidate_sha=candidate_sha,
                expected_trunk_head=expected_trunk_head,
                strategy=strategy,
                capsule=capsule,
            )

    def _integrate_candidate(
        self,
        *,
        repository_root: Path,
        candidate_workspace: Path,
        candidate_branch: str,
        candidate_sha: str,
        expected_trunk_head: str,
        strategy: str,
        capsule: ExecutionCapsule | None = None,
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

        _validate_raw_integration_chain(Path(candidate_workspace))
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

        if capsule is not None:
            self._validate_candidate_history(capsule, candidate, candidate_sha)

        branch = self._git(candidate, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch != candidate_branch:
            raise WorkspaceConflict(f"candidate branch is {branch!r}, expected {candidate_branch!r}")
        candidate_head = self._git(candidate, "rev-parse", "--verify", "HEAD^{commit}")
        if candidate_head != candidate_sha:
            raise WorkspaceConflict("candidate workspace HEAD does not match the authorized commit")
        self._require_clean_checkout(candidate, label="candidate")
        ancestry = self._runner(("git", "merge-base", "--is-ancestor", candidate_sha, candidate_branch), candidate)
        if ancestry.returncode != 0:
            raise WorkspaceConflict("candidate commit is not an ancestor of its authorized branch")

        before = self._git(repository, "rev-parse", "--verify", "HEAD^{commit}")
        if before != expected_trunk_head:
            recovered = self._recover_applied_integration(
                repository=repository,
                candidate=candidate,
                candidate_branch=candidate_branch,
                candidate_sha=candidate_sha,
                expected_trunk_head=expected_trunk_head,
                actual_trunk_head=before,
                strategy=strategy,
            )
            if recovered is None:
                raise WorkspaceConflict("trunk HEAD changed before integration")
            return recovered
        self._require_clean_checkout(repository, label="trunk")

        if strategy == "fast_forward":
            ancestry = self._runner(
                ("git", "merge-base", "--is-ancestor", expected_trunk_head, candidate_sha), repository
            )
            if ancestry.returncode != 0:
                raise WorktreeError("Git integration preflight failed: candidate is not a fast-forward")
            expected_tree = self._git(repository, "rev-parse", "--verify", f"{candidate_sha}^{{tree}}")
            target_head = candidate_sha
        else:
            candidate_parents = self._commit_parents(candidate, candidate_sha)
            if strategy == "cherry_pick" and len(candidate_parents) != 1:
                raise WorkspaceConflict("cherry-pick integration requires a single-parent candidate")
            expected_tree = self._merge_tree(
                repository,
                expected_trunk_head,
                candidate_sha,
                merge_base=candidate_parents[0] if strategy == "cherry_pick" else None,
            )
            if expected_tree is None:
                raise WorktreeError("Git integration preflight failed: conflict-free tree is unavailable")
            parents = (
                ("-p", expected_trunk_head, "-p", candidate_sha) if strategy == "merge" else ("-p", expected_trunk_head)
            )
            target_head = self._git(
                repository,
                "commit-tree",
                expected_tree,
                *parents,
                "-m",
                f"codex-flow {strategy.replace('_', '-')} {candidate_sha}",
            )

        # Recheck immediately before moving the branch or checkout. Prepared
        # objects remain unreachable unless the expected-old-value CAS wins.
        if self._git(repository, "rev-parse", "--verify", "HEAD^{commit}") != expected_trunk_head:
            raise WorkspaceConflict("trunk HEAD changed after integration preflight")
        if self._git(candidate, "rev-parse", "--verify", "HEAD^{commit}") != candidate_sha:
            raise WorkspaceConflict("candidate HEAD changed after integration preflight")
        self._require_clean_checkout(repository, label="trunk")
        self._require_clean_checkout(candidate, label="candidate")

        trunk_ref = self._git(repository, "symbolic-ref", "--quiet", "HEAD")
        if not trunk_ref.startswith("refs/heads/"):
            raise WorkspaceConflict("integration trunk HEAD is not an exact branch authority")
        applied = self._runner(("git", "update-ref", trunk_ref, target_head, expected_trunk_head), repository)
        if applied.returncode != 0:
            raise WorkspaceConflict("trunk HEAD changed during integration CAS")
        self._synchronize_checkout_or_rollback(
            repository,
            trunk_ref=trunk_ref,
            target_head=target_head,
            rollback_head=expected_trunk_head,
            expected_checkout_head=expected_trunk_head,
        )
        after = self._git(repository, "rev-parse", "--verify", "HEAD^{commit}")
        if after == before:
            raise WorkspaceConflict("Git integration did not advance the trunk")
        parents = self._commit_parents(repository, after)
        tree = self._git(repository, "rev-parse", "--verify", f"{after}^{{tree}}")
        if strategy == "fast_forward":
            if after != candidate_sha:
                raise WorkspaceConflict("fast-forward integration produced an unexpected trunk HEAD")
        elif strategy == "merge":
            if parents != [before, candidate_sha] or tree != expected_tree:
                raise WorkspaceConflict("merge integration produced an unexpected Git post-state")
        else:
            if parents != [before] or tree != expected_tree:
                raise WorkspaceConflict("cherry-pick integration produced an unexpected Git post-state")
        self._require_clean_checkout(repository, label="integrated trunk")
        return {
            "before_trunk_head": before,
            "after_trunk_head": after,
            "candidate_sha": candidate_sha,
            "candidate_branch": candidate_branch,
            "candidate_workspace": str(candidate),
            "strategy": strategy,
            "parents": parents,
            "tree": tree,
        }

    def _recover_applied_integration(
        self,
        *,
        repository: Path,
        candidate: Path,
        candidate_branch: str,
        candidate_sha: str,
        expected_trunk_head: str,
        actual_trunk_head: str,
        strategy: str,
    ) -> dict[str, object] | None:
        """Recognize only the exact authorized Git effect after a crash.

        The durable outbox is written before Git changes.  If the harness
        exits after the strategy succeeds but before its receipt commits,
        replay reaches this read-only discriminator.  Unknown advancement is
        never treated as success.
        """

        parents = self._commit_parents(repository, actual_trunk_head)
        actual_tree = self._git(repository, "rev-parse", "--verify", f"{actual_trunk_head}^{{tree}}")
        if strategy == "fast_forward":
            ancestry = self._runner(
                ("git", "merge-base", "--is-ancestor", expected_trunk_head, candidate_sha),
                repository,
            )
            matches = actual_trunk_head == candidate_sha and ancestry.returncode == 0
        else:
            if strategy == "merge":
                expected_tree = self._merge_tree(repository, expected_trunk_head, candidate_sha)
                matches = (
                    parents == [expected_trunk_head, candidate_sha]
                    and expected_tree is not None
                    and actual_tree == expected_tree
                )
            else:
                candidate_parents = self._git(
                    candidate,
                    "rev-list",
                    "--parents",
                    "-n",
                    "1",
                    candidate_sha,
                ).split()
                if len(candidate_parents) != 2:
                    return None
                expected_tree = self._merge_tree(
                    repository,
                    expected_trunk_head,
                    candidate_sha,
                    merge_base=candidate_parents[1],
                )
                matches = (
                    parents == [expected_trunk_head] and expected_tree is not None and actual_tree == expected_tree
                )
        if not matches:
            return None
        trunk_ref = self._git(repository, "symbolic-ref", "--quiet", "HEAD")
        if not trunk_ref.startswith("refs/heads/"):
            raise WorkspaceConflict("integration trunk HEAD is not an exact branch authority")
        self._synchronize_checkout_or_rollback(
            repository,
            trunk_ref=trunk_ref,
            target_head=actual_trunk_head,
            rollback_head=expected_trunk_head,
            expected_checkout_head=expected_trunk_head,
        )
        self._require_clean_checkout(repository, label="recovered integrated trunk")
        return {
            "before_trunk_head": expected_trunk_head,
            "after_trunk_head": actual_trunk_head,
            "candidate_sha": candidate_sha,
            "candidate_branch": candidate_branch,
            "candidate_workspace": str(candidate),
            "strategy": strategy,
            "recovered": True,
            "parents": parents,
            "tree": actual_tree,
        }

    def _synchronize_checkout_or_rollback(
        self,
        repository: Path,
        *,
        trunk_ref: str,
        target_head: str,
        rollback_head: str,
        expected_checkout_head: str,
    ) -> None:
        """Converge an exact applied ref or CAS-roll it back before failure."""

        for _attempt in range(2):
            if self._checkout_matches_head(repository, target_head):
                return
            if not self._checkout_matches_head(repository, expected_checkout_head):
                raise WorkspaceConflict("integration checkout changed after integration CAS")
            checkout = self._runner(("git", "read-tree", "--reset", "-u", target_head), repository)
            if checkout.returncode == 0:
                return
        if self._checkout_matches_head(repository, target_head):
            return
        if not self._checkout_matches_head(repository, expected_checkout_head):
            raise WorkspaceConflict("integration checkout changed after integration CAS")
        rollback = self._runner(("git", "update-ref", trunk_ref, rollback_head, target_head), repository)
        if rollback.returncode != 0:
            raise WorkspaceConflict("trunk HEAD changed after integration CAS")
        self._require_clean_checkout(repository, label="rolled-back integration trunk")
        raise WorktreeError("Git integration checkout update failed and the ref was rolled back")

    def _checkout_matches_head(self, repository: Path, expected_head: str) -> bool:
        """Return whether index, tracked files and relevant untracked state match a commit."""

        index = self._runner(("git", "diff-index", "--cached", "--quiet", expected_head, "--"), repository)
        if index.returncode == 1:
            return False
        if index.returncode != 0:
            raise WorktreeError("Git integration index identity is unavailable")
        tracked = self._runner(("git", "diff-files", "--quiet", "--"), repository)
        if tracked.returncode == 1:
            return False
        if tracked.returncode != 0:
            raise WorktreeError("Git integration checkout identity is unavailable")
        untracked = self._runner(("git", "ls-files", "--others", "--exclude-standard"), repository)
        if untracked.returncode != 0:
            raise WorktreeError("Git integration untracked-state identity is unavailable")
        return not untracked.stdout.strip()

    def _commit_parents(self, repository: Path, commit: str) -> list[str]:
        values = self._git(repository, "rev-list", "--parents", "-n", "1", commit).split()
        if not values or values[0] != commit:
            raise WorktreeError("Git commit parent receipt is invalid")
        return values[1:]

    def _validate_candidate_history(self, capsule: ExecutionCapsule, workspace: Path, candidate_sha: str) -> None:
        """Prove every commit and changed path is owned by one capsule.

        Checking only the final tree is insufficient: a candidate can add and
        remove an unauthorized path while leaving no net diff.  Walk the full
        ancestry range and inspect each commit's tree diff, including every
        parent of a merge commit.
        """

        if capsule.workspace_path != workspace.resolve():
            raise CandidateIntegrityError("candidate workspace does not match the capsule")
        base_check = self._runner(("git", "merge-base", "--is-ancestor", capsule.base_sha, candidate_sha), workspace)
        if base_check.returncode != 0:
            raise CandidateIntegrityError("candidate commit does not descend from the capsule base")
        commits = self._git(workspace, "rev-list", "--reverse", f"{capsule.base_sha}..{candidate_sha}").splitlines()
        if not commits:
            raise CandidateIntegrityError("candidate commit is not ahead of the capsule base")
        mutable = tuple(Path(value) for value in capsule.mutable_paths)
        protected = tuple(Path(value) for value in capsule.protected_paths)

        def owned(path: str) -> bool:
            candidate = Path(path)
            if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() != path:
                raise CandidateIntegrityError("candidate commit contains a non-relative path")
            if any(candidate == item or item in candidate.parents for item in protected):
                raise CandidateIntegrityError("candidate commit changes a protected path")
            if not any(candidate == item or item in candidate.parents for item in mutable):
                raise CandidateIntegrityError("candidate commit changes a path outside capsule ownership")
            return True

        for commit in commits:
            if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
                raise CandidateIntegrityError("candidate commit history contains an invalid identity")
            raw_paths = self._git(
                workspace,
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-m",
                "-z",
                commit,
            )
            paths = tuple(item for item in raw_paths.split("\x00") if item)
            for path in paths:
                owned(path)

    def _merge_tree(
        self,
        repository: Path,
        left: str,
        right: str,
        *,
        merge_base: str | None = None,
    ) -> str | None:
        arguments = ["git", "merge-tree", "--write-tree"]
        if merge_base is not None:
            arguments.extend(("--merge-base", merge_base))
        arguments.extend((left, right))
        result = self._runner(tuple(arguments), repository)
        if result.returncode != 0:
            return None
        first_line = result.stdout.splitlines()[0].strip() if result.stdout.splitlines() else ""
        if len(first_line) != 40 or any(character not in "0123456789abcdef" for character in first_line):
            return None
        return first_line

    def _require_clean_checkout(self, path: Path, *, label: str) -> None:
        """Require no tracked or untracked bytes before an integration edge."""

        result = self._runner(("git", "status", "--porcelain", "--untracked-files=all"), path)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise WorktreeError(f"unable to inspect {label} topology: {detail}")
        if result.stdout:
            raise WorkspaceConflict(f"{label} workspace is not clean")
