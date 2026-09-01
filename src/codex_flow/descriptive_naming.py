#!/usr/bin/env python3
"""Validate names introduced by a change against the descriptive-name rule.

The validator intentionally works on a caller-provided changed-name set.  It
does not rewrite or reinterpret existing protocol values and immutable review
evidence; those are explicit compatibility boundaries.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path


class DescriptiveNameError(ValueError):
    """Raised when a changed path or identifier is milestone-coupled."""


# Numeric suffixes identify temporary sequencing labels while leaving domain
# words such as ``milestone_id`` and ``model_facing_projection`` available.
_COUPLED_NAME = re.compile(
    r"(?:^|[-_.])(?:h|s)\d+[A-Za-z]*(?=$|[-_.])|"
    r"(?:^|[-_.])milestone[-_.]?\d+(?=$|[-_.])|"
    r"(?:^|[-_.])task[-_.]?\d+(?=$|[-_.])|"
    r"(?:^|[-_.])thread[-_.]?\d+(?=$|[-_.])|"
    r"(?:^|[-_.])model[-_.]?\d+(?=$|[-_.])",
    re.IGNORECASE,
)

PERSISTED_PROTOCOL_NAMES = frozenset(
    {
        "--milestone-id",
        "milestone_id",
        "model-facing",
        "model-facing-projection",
        "codex-flow/h4/v1",
        "codex-flow/h5-workflow-control-medium/v1",
        "codex-flow/h6-production-pilots/v1",
        "h1-sdk-sentinel/v1",
    }
)

# These are the one forward-only public surface.  They are kept as explicit
# data so source/wheel validators can distinguish a removed API from a frozen
# protocol token or historical evidence path.
SEMANTIC_PUBLIC_MODULES = frozenset(
    {
        "review_pilots.py",
        "workflow_control_pilot.py",
        "production_pilots.py",
        "sdk_compatibility_sentinel.py",
        "controller_recovery_sentinel.py",
    }
)
REMOVED_PUBLIC_MODULES = frozenset(
    {
        "h4_pilot.py",
        "h5_pilot.py",
        "h6_pilot.py",
        "sentinel.py",
        "controller_sentinel.py",
    }
)
SEMANTIC_PUBLIC_IDENTIFIERS = frozenset(
    {
        "ReviewWorkflow",
        "review_workflow",
        "ReviewLifecycleResult",
        "write_review_artifact",
        "record_review_transition",
        "review_lifecycle",
        "record_review_fact",
        "record_review_recovery",
        "record_review_decision_request",
        "record_review_decision_response",
        "record_review_budget",
        "PilotError",
        "build_visible_worker_capsule",
        "run_review_pilot",
        "run_multi_authority_review_pilot",
        "run_workflow_control_pilot",
        "run_production_pilots",
        "write_production_evidence",
        "run_sdk_compatibility_sentinel",
        "write_sdk_compatibility_evidence",
        "run_controller_recovery_sentinel",
        "write_controller_recovery_evidence",
    }
)
REMOVED_PUBLIC_IDENTIFIERS = frozenset(
    {
        "H4WalkingSkeleton",
        "h4_walking_skeleton",
        "H4LifecycleResult",
        "write_h4_review_artifact",
        "record_h4_transition",
        "h4_lifecycle",
        "record_h4_fact",
        "record_h4_recovery",
        "record_h4_decision_request",
        "record_h4_decision_response",
        "record_h4_budget",
        "H6PilotError",
        "app_native_visible_pilot_capsule",
        "run_h6_pilots",
        "write_h6_evidence",
        "run_h5_medium_pilot",
        "write_h5_evidence",
        "run_h4_objective_pilot",
        "run_h4_multi_authority_pilot",
        "run_real_sentinel",
        "write_controller_evidence",
        "run_controller_sentinel",
        "write_evidence",
    }
)


def _is_protocol_exception(value: str) -> bool:
    return value in PERSISTED_PROTOCOL_NAMES or value.startswith("codex-flow/")


def coupling_reason(value: str, *, allow_protocol: bool = False) -> str | None:
    """Return a concise violation reason, or ``None`` when the name is valid."""

    protocol_token = value.rsplit(".", 1)[0]
    if allow_protocol and (
        _is_protocol_exception(value)
        or re.fullmatch(r"(?:h|s)\d+", protocol_token, re.IGNORECASE) is not None
        or re.fullmatch(r"(?:model|milestone)-\d+", protocol_token, re.IGNORECASE) is not None
    ):
        return None
    if _COUPLED_NAME.search(value):
        return "temporary milestone/task/thread/model sequence coupling"
    return None


def validate_path_name(
    path: str | Path,
    *,
    immutable_paths: Iterable[str | Path] = (),
    protocol_paths: Iterable[str | Path] = (),
) -> None:
    """Validate one path with exact caller-owned provenance exceptions."""

    path_text = str(path).replace("\\", "/")
    if path_text in {str(value).replace("\\", "/") for value in immutable_paths}:
        return
    protocol_path = path_text in {str(value).replace("\\", "/") for value in protocol_paths}
    for component in Path(path_text).parts:
        reason = coupling_reason(component, allow_protocol=protocol_path)
        if reason is not None:
            raise DescriptiveNameError(f"{path_text!r}: {reason}")


def validate_identifier_name(identifier: str, *, allow_protocol: bool = False) -> None:
    """Validate a Python/public identifier or persisted protocol name."""

    reason = coupling_reason(identifier, allow_protocol=allow_protocol)
    if reason is not None:
        raise DescriptiveNameError(f"{identifier!r}: {reason}")


def validate_changed_names(
    paths: Iterable[str | Path],
    identifiers: Iterable[str] = (),
    *,
    immutable_paths: Iterable[str | Path] = (),
    protocol_paths: Iterable[str | Path] = (),
    protocol_names: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return all violations in a changed-name set, in stable input order.

    ``protocol_names`` is an explicit, caller-owned exception list for values
    whose numbered identity is persisted protocol rather than a canonical name.
    Immutable and persisted paths are exact caller-owned exception lists.
    """

    allowed_protocol = set(protocol_names)
    allowed_immutable_paths = {str(value).replace("\\", "/") for value in immutable_paths}
    allowed_protocol_paths = {str(value).replace("\\", "/") for value in protocol_paths}
    violations: list[str] = []
    for path in paths:
        path_text = str(path).replace("\\", "/")
        if path_text in allowed_immutable_paths:
            continue
        protocol_path = path_text in allowed_protocol_paths
        for component in Path(path_text.replace("\\", "/")).parts:
            if component in allowed_protocol:
                continue
            reason = coupling_reason(component, allow_protocol=protocol_path)
            if reason is not None:
                violations.append(f"{path_text}: {reason}")
                break
    for identifier in identifiers:
        if identifier in allowed_protocol:
            continue
        reason = coupling_reason(identifier)
        if reason is not None:
            violations.append(f"{identifier}: {reason}")
    return tuple(violations)


def changed_paths_from_git(root: Path) -> tuple[str, ...]:
    """Collect tracked changed and untracked paths without changing the index."""

    tracked = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", "HEAD", "--"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return tuple(dict.fromkeys((*tracked, *untracked)))


__all__ = [
    "PERSISTED_PROTOCOL_NAMES",
    "REMOVED_PUBLIC_IDENTIFIERS",
    "REMOVED_PUBLIC_MODULES",
    "SEMANTIC_PUBLIC_IDENTIFIERS",
    "SEMANTIC_PUBLIC_MODULES",
    "DescriptiveNameError",
    "changed_paths_from_git",
    "coupling_reason",
    "validate_changed_names",
    "validate_identifier_name",
    "validate_path_name",
]
