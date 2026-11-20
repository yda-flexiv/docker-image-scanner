"""Read-only branch retention policy for a snapshot of repository refs."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache

_INTEGRATION = frozenset({"develop", "v4.x_develop"})
_FEATURE_PREFIXES = ("bug/", "bugfix/", "task/", "feature/", "fix/")
_RELEASE_PREFIX = "release/"
_RELEASE = re.compile(r"v([0-9]+)\.([0-9]+)(?:[._-].+)?")
_MINIMUM_MINOR = {"3": 11, "4": 2}

_History = list[tuple[str, int]]
_Selection = tuple[str, str, str]


@dataclass(frozen=True)
class Policy:
    integration_days: int = 30
    feature_days: int = 30
    active_release: str | None = None
    anchors: dict[str, str] = field(default_factory=dict)
    includes: dict[str, list[str]] = field(default_factory=dict)
    excludes: dict[str, list[str]] = field(default_factory=dict)


def version_family(tag: str, family: str) -> bool:
    """Match a version exactly or with a delimited patch/suffix."""
    return tag == family or tag.startswith((family + ".", family + "_", family + "-"))


def _branch_kind(branch: str) -> str:
    if branch in _INTEGRATION:
        return "integration"
    if branch.startswith(_RELEASE_PREFIX):
        match = _RELEASE.fullmatch(branch.removeprefix(_RELEASE_PREFIX))
        minimum = _MINIMUM_MINOR.get(match[1]) if match else None
        if minimum is None or match is None:
            return "unknown-release"
        # Compare minor numbers without converting arbitrary-length ref names to int.
        minor = match[2].lstrip("0") or "0"
        threshold = str(minimum)
        if (len(minor), minor) < (len(threshold), threshold):
            return "old-release"
        return "supported-release"
    if branch.startswith(_FEATURE_PREFIXES):
        return "feature"
    return "unclassified"


def select(
    repo: str,
    tips: dict[str, str],
    as_of: datetime,
    policy: Policy,
    history: Callable[[str], _History],
    *,
    tip_time: Callable[[str], int] | None = None,
) -> tuple[list[_Selection], list[str], list[str]]:
    """Select retention roots and report warnings and incomplete evaluations.

    Histories are newest first in first-parent traversal order, not date order.
    The history cache is bounded because many branch tips can have long shared history.
    """
    selected: set[_Selection] = set()
    warnings: set[str] = set()
    errors: set[str] = set()
    kinds = {branch: _branch_kind(branch) for branch in sorted(tips)}

    @lru_cache(maxsize=16)
    def cached_history(sha: str) -> _History | str:
        try:
            rows = history(sha)
            if not rows or rows[0][0] != sha:
                raise RuntimeError("first-parent history did not start at the tip")
            return rows
        except RuntimeError as exc:
            return str(exc) or "history unavailable"

    def load(branch: str) -> _History | None:
        sha = tips[branch]
        result = cached_history(sha)
        if isinstance(result, str):
            errors.add(f"{branch}: cannot read first-parent history for {sha}: {result}")
            return None
        return result

    integration_cutoff = (as_of - timedelta(days=policy.integration_days)).timestamp()
    feature_cutoff = (as_of - timedelta(days=policy.feature_days)).timestamp()
    for branch, kind in kinds.items():
        sha = tips[branch]
        if kind == "integration":
            selected.add((branch, sha, "integration-tip"))
            rows = load(branch)
            if rows is not None:
                selected.update(
                    (branch, commit, "integration-window")
                    for commit, timestamp in rows[1:]
                    if timestamp >= integration_cutoff
                )
        elif kind == "feature":
            try:
                rows = None if tip_time else load(branch)
                timestamp = tip_time(sha) if tip_time else rows[0][1] if rows else None
            except RuntimeError as exc:
                errors.add(f"{branch}: cannot read tip timestamp for {sha}: {exc}")
                timestamp = None
            if timestamp is None:
                selected.add((branch, sha, "feature-tip-history-unavailable"))
            elif timestamp >= feature_cutoff:
                selected.add((branch, sha, "recent-feature-tip"))
        elif kind != "old-release":
            selected.add((branch, sha, f"{kind}-tip"))
            if kind == "unknown-release":
                warnings.add(f"{branch}: unknown release version; protecting tip")
            elif kind == "unclassified":
                warnings.add(f"{branch}: unclassified branch; protecting tip")

    active, notes, failures = _select_active(repo, tips, kinds, policy, load)
    return sorted(selected | active), sorted(warnings | notes), sorted(errors | failures)


def _select_active(
    repo: str,
    tips: dict[str, str],
    kinds: dict[str, str],
    policy: Policy,
    load: Callable[[str], _History | None],
) -> tuple[set[_Selection], set[str], set[str]]:
    active: set[_Selection] = set()
    warnings: set[str] = set()
    errors: set[str] = set()
    if policy.active_release is None:
        return active, warnings, errors

    family = policy.active_release.removeprefix(_RELEASE_PREFIX)
    anchor = policy.anchors.get(repo, _RELEASE_PREFIX + family)
    excluded = set(policy.excludes.get(repo, ()))
    included = (set(policy.includes.get(repo, ())) & tips.keys()) - excluded
    active.update((branch, tips[branch], "active-release-include") for branch in included)
    if anchor not in tips:
        errors.add(f"missing active-release anchor {anchor!r}")
        return active, warnings, errors
    if anchor not in excluded:
        active.add((anchor, tips[anchor], "active-release-anchor"))

    anchor_rows = load(anchor)
    if anchor_rows is None:
        return active, warnings, errors

    def in_family(branch: str) -> bool:
        return branch.startswith(_RELEASE_PREFIX) and version_family(branch[len(_RELEASE_PREFIX):], family)

    competitors = {
        branch for branch, kind in kinds.items() if branch != anchor and (
            kind == "integration" or (kind == "supported-release" and not in_family(branch))
        )
    }
    competitor_commits: set[str] = set()
    complete = bool(competitors)
    for branch in sorted(competitors):
        rows = load(branch)
        if rows is None:
            complete = False
        else:
            competitor_commits.update(sha for sha, _ in rows)

    # Once two first-parent chains meet they share a suffix. Only the
    # anchor's prefix before the first shared commit can prove derivation.
    release_only = set()
    shared_base = None
    for sha, _ in anchor_rows:
        if sha in competitor_commits:
            shared_base = sha
            break
        release_only.add(sha)

    def ambiguous(branch: str, reason: str) -> None:
        active.add((branch, tips[branch], "active-release-ambiguous"))
        warnings.add(f"{branch}: ambiguous active-release ancestry ({reason}); protecting tip")

    skip = {anchor} | competitors | excluded | included
    for branch, kind in kinds.items():
        if branch in skip or kind == "old-release":
            continue
        rows = load(branch)
        if rows is None:
            if in_family(branch):
                ambiguous(branch, "first-parent history unavailable")
            continue

        # A candidate whose first link is to a competitor beyond the common
        # base is not a boundary tie. An older common trunk commit alone is
        # not evidence, even if the candidate itself has novel commits.
        overlap = next((sha for sha, _ in rows if sha in release_only or sha in competitor_commits), None)
        if overlap in release_only:
            if complete:
                active.add((branch, tips[branch], "active-release-derived"))
            else:
                ambiguous(branch, "competing first-parent histories unavailable")
        elif shared_base is not None and overlap == shared_base and tips[branch] != shared_base:
            ambiguous(branch, "branches meet at the shared divergence commit")
        elif in_family(branch):
            ambiguous(branch, "release family name without release-only first-parent evidence")

    return active, warnings, errors
