"""Compile a deterministic retention report from a frozen set of Git inputs."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .gitstore import PROJECTS, SHA, SNAPSHOT_ID, BareRepo, GitError, remote_key
from .images import TARGET, docker_images, pipeline_images
from .policy import Policy, select, version_family

PIPELINE = "bitbucket-pipelines.yml"
DOCKER_CONFIG = "docker/docker_config.sh"
SCHEMA = 1


def read_time(value: str) -> datetime:
    date = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
    if date.tzinfo is None:
        raise ValueError("the timestamp must have an explicit timezone (e.g. Z)")
    return date.astimezone(timezone.utc)


def fresh_inputs(state: Path, remotes: dict[str, str], docker_remote: str, policy: Policy,
                 as_of: datetime) -> dict:
    projects = {}
    for name in PROJECTS:
        key = remote_key(name, remotes[name])
        try:
            repo = BareRepo(state, key, create=True)
            repo.refresh(remotes[name], blobless=True)
            projects[name] = {"cache_key": key, "tips": repo.tips()}
        except GitError as exc:
            projects[name] = {"cache_key": key, "tips": {}, "error": str(exc)}
    return {
        "target": TARGET,
        "as_of": as_of.astimezone(timezone.utc).isoformat(),
        "policy": asdict(policy),
        "projects": projects,
        "docker_cache_key": remote_key("docker", docker_remote),
        "unavailable_docker_commits": {},
    }


class DockerSources:
    def __init__(self, state: Path, inputs: dict, remote: str | None) -> None:
        self.state = state
        self.inputs = inputs
        self.remote = remote
        self.cache: BareRepo | None = None
        self.refreshed = False
        self.seen: set[str] = set()
        self.results: dict[str, tuple[list[tuple[str, int, str]], list[str]]] = {}

    def parse(self, sha: str) -> tuple[list[tuple[str, int, str]], list[str]]:
        if sha not in self.results:
            try:
                cache = self._obtain(sha)
                self.seen.add(sha)
                entry = cache.entry(sha, "docker_config.sh")
                result = docker_images(cache.text(entry)) if entry else ([], ["missing Docker config"])
            except GitError as exc:
                result = [], [str(exc)]
            self.results[sha] = result
        return self.results[sha]

    def _obtain(self, sha: str) -> BareRepo:
        unavailable = self.inputs["unavailable_docker_commits"]
        if sha in unavailable:
            raise GitError(unavailable[sha])
        try:
            return self._load(sha)
        except GitError as exc:
            if self.remote:
                unavailable[sha] = str(exc)
            raise

    def _load(self, sha: str) -> BareRepo:
        if self.cache is None:
            self.cache = BareRepo(self.state, self.inputs["docker_cache_key"], create=self.remote is not None)
        if self.cache.has_commit(sha):
            return self.cache
        if self.remote is None:
            raise GitError("pinned Docker commit missing from local snapshot cache")
        if not self.refreshed:
            self.refreshed = True
            # A commit may be reachable from another advertised ref or fetchable by ID.
            try:
                self.cache.refresh(self.remote, tags=True)
            except GitError:
                pass
        if not self.cache.has_commit(sha):
            self.cache.fetch_commit(self.remote, sha)
        if not self.cache.has_commit(sha):
            raise GitError("pinned Docker commit unavailable")
        return self.cache


def report(state: Path, inputs: dict, *, docker_remote: str | None = None,
           project_remotes: dict[str, str] | None = None,
           snapshot_id: str | None = None) -> tuple[dict, dict[str, set[str]]]:
    if inputs["target"] != TARGET:
        raise ValueError("snapshot has an unsupported target image repository")
    as_of = read_time(inputs["as_of"])
    policy = Policy(**inputs["policy"])
    docker = DockerSources(state, inputs, docker_remote)
    groups: dict[str, list[dict]] = defaultdict(list)
    errors: set[str] = set()
    warnings: set[str] = set()
    pins: dict[str, set[str]] = defaultdict(set)

    for name in PROJECTS:
        record = inputs["projects"][name]
        if "error" in record:
            errors.add(f"{name}: {record['error']}")
            continue
        key = record["cache_key"]
        tips = record["tips"]
        if any(not SHA.fullmatch(sha) for sha in tips.values()):
            raise ValueError("invalid branch tip in snapshot")
        try:
            repo = BareRepo(state, key)
        except GitError as exc:
            errors.add(f"{name}: {exc}")
            continue
        pins[key].update(tips.values())
        selections, notes, failures = select(name, tips, as_of, policy, repo.history, tip_time=repo.tip_time)
        warnings.update(f"{name}: {note}" for note in notes)
        errors.update(f"{name}: {failure}" for failure in failures)
        by_commit: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for branch, sha, reason in selections:
            by_commit[sha][branch].add(reason)
        parsed_pipelines: dict[str, tuple[list[tuple[str, int, str]], list[str]]] = {}

        def collect(parsed: tuple[list[tuple[str, int, str]], list[str]], path: str,
                    sha: str, branches: dict[str, set[str]], submodule: str | None = None) -> None:
            records, problems = parsed
            context = f"{name}@{sha}:{path}" + (f" ({submodule})" if submodule else "")
            errors.update(f"{context}: {problem}" for problem in problems)
            for branch, reasons in sorted(branches.items()):
                for image, line, kind in records:
                    entry = {"repo": name, "branch": branch, "commit": sha, "path": path,
                             "line": line, "source": kind, "reasons": sorted(reasons)}
                    if submodule:
                        entry["docker_commit"] = submodule
                    groups[image].append(entry)
                    if policy.active_release and any("active-release" in reason for reason in reasons):
                        tag = image.split("@", 1)[0].partition(":")[2]
                        if not version_family(tag, policy.active_release.removeprefix("release/")):
                            warnings.add(f"{name}:{branch}: active-release branch references {image}")

        pipeline_entries = {}
        for sha in sorted(by_commit):
            try:
                entry = repo.entry(sha, PIPELINE)
                if entry is not None:
                    pipeline_entries[sha] = entry
            except GitError as exc:
                errors.add(f"{name}@{sha}:{PIPELINE}: {exc}")
        if project_remotes:
            try:
                repo.hydrate(project_remotes[name], {
                    oid for mode, oid in pipeline_entries.values() if mode in {"100644", "100755"}
                })
            except GitError:
                # A single missing blob should not suppress the others; retry individually below.
                pass
        for sha, branches in sorted(by_commit.items()):
            try:
                entry = pipeline_entries.get(sha)
                if entry is not None:
                    if entry[1] not in parsed_pipelines:
                        try:
                            text = repo.text(entry)
                        except GitError:
                            if not project_remotes or entry[0] not in {"100644", "100755"}:
                                raise
                            repo.hydrate(project_remotes[name], {entry[1]})
                            text = repo.text(entry)
                        parsed_pipelines[entry[1]] = pipeline_images(text)
                    collect(parsed_pipelines[entry[1]], PIPELINE, sha, branches)
            except GitError as exc:
                errors.add(f"{name}@{sha}:{PIPELINE}: {exc}")
            try:
                gitlink = repo.entry(sha, "docker")
                if gitlink is not None and gitlink[0] == "160000":
                    collect(docker.parse(gitlink[1]), DOCKER_CONFIG, sha, branches, gitlink[1])
            except GitError as exc:
                errors.add(f"{name}@{sha}:{DOCKER_CONFIG}: {exc}")

    if docker.seen:
        pins[inputs["docker_cache_key"]].update(docker.seen)
    return {
        "schema_version": SCHEMA,
        "snapshot_id": snapshot_id,
        "complete": not errors,
        "inputs": inputs,
        "required_images": [
            {"image": image, "evidence": sorted(groups[image], key=lambda e: (
                e["repo"], e["branch"], e["commit"], e["path"], e["line"], e["source"],
            ))}
            for image in sorted(groups)
        ],
        "warnings": sorted(warnings),
        "errors": sorted(errors),
    }, pins


def save(state: Path, document: dict, pins: dict[str, set[str]]) -> None:
    snapshot = document["snapshot_id"]
    if not SNAPSHOT_ID.fullmatch(snapshot):
        raise ValueError("invalid snapshot ID")
    for key, commits in pins.items():
        BareRepo(state, key).pin(snapshot, commits)
    folder = state / "snapshots"
    folder.mkdir(mode=0o700, exist_ok=True)
    draft = folder / ("." + uuid4().hex + ".tmp")
    with draft.open("x", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
    draft.replace(folder / (snapshot + ".json"))


def load(state: Path, locator: str) -> dict:
    path = state / "snapshots" / (locator + ".json") if SNAPSHOT_ID.fullmatch(locator) else Path(locator)
    with path.open(encoding="utf-8") as handle:
        old = json.load(handle)
    if old.get("schema_version") != SCHEMA:
        raise ValueError("unsupported report schema")
    return old
