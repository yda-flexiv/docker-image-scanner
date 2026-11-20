"""Bare Git caches owned by the scanner; never discover the invoking checkout."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
KEY = re.compile(r"[a-z_]+-[0-9a-f]{20}\Z")
SNAPSHOT_ID = re.compile(r"[0-9a-f]{32}\Z")

PROJECTS = ("flexiv_sw", "flexiv_sw_base", "flexiv_sw_uiapp")
DEFAULT_REMOTES = {name: f"git@bitbucket.org:FlexivTeam/{name}.git" for name in PROJECTS}
DOCKER_REMOTE = "git@bitbucket.org:FlexivTeam/flexiv_sw_docker_build.git"


def remote_key(name: str, remote: str) -> str:
    return f"{name}-{hashlib.sha256(remote.encode()).hexdigest()[:20]}"


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in tuple(env):
        if key in {
            "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_NAMESPACE", "GIT_QUARANTINE_PATH", "GIT_CONFIG",
            "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_PREFIX",
            "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE", "GIT_IMPLICIT_WORK_TREE",
        } or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_TRACE")):
            env.pop(key)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1")
    return env


class GitError(RuntimeError):
    pass


class BareRepo:
    def __init__(self, state: Path, key: str, *, create: bool = False) -> None:
        if not KEY.fullmatch(key):
            raise ValueError("invalid cache key")
        self.path = state / "repos" / (key + ".git")
        if create and not (self.path / "HEAD").is_file():
            self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
            result = subprocess.run(
                ["git", "init", "--bare", "--quiet", str(self.path)],
                cwd=state, env=_environment(), capture_output=True,
            )
            if result.returncode:
                raise GitError("could not initialize bare Git cache")
        if not (self.path / "HEAD").is_file():
            raise GitError("bare repository is missing from cache")

    def git(self, *args: str, data: bytes | None = None) -> bytes:
        result = subprocess.run(
            ["git", "--git-dir", str(self.path), "-c", "gc.auto=0", *args],
            cwd=self.path, env=_environment(), input=data, capture_output=True,
        )
        if result.returncode:
            # Git errors can include credential-bearing URLs or headers.
            raise GitError(f"git {args[0]} failed (exit {result.returncode})")
        return result.stdout

    def refresh(self, remote: str, *, tags: bool = False, blobless: bool = False) -> None:
        refspecs = ["+refs/heads/*:refs/scanner/heads/*"]
        if tags:
            refspecs.append("+refs/tags/*:refs/scanner/tags/*")
        options = ("--filter=blob:none",) if blobless else ()
        self.git("fetch", "--quiet", "--atomic", "--prune", "--no-tags", "--no-recurse-submodules",
                 *options, "--", remote, *refspecs)

    def hydrate(self, remote: str, blobs: set[str]) -> None:
        missing = self.missing_blobs(blobs)
        if missing:
            self.git("fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--stdin", "--",
                     remote, data="".join(oid + "\n" for oid in sorted(missing)).encode())

    def missing_blobs(self, blobs: set[str]) -> set[str]:
        if not blobs:
            return set()
        for oid in blobs:
            self._check_sha(oid)
        output = self.git("cat-file", "--batch-check=%(objectname) %(objecttype)",
                          data="".join(oid + "\n" for oid in sorted(blobs)).encode())
        return {oid for oid, kind in (line.split() for line in output.decode().splitlines()) if kind != "blob"}

    def tips(self) -> dict[str, str]:
        prefix = "refs/scanner/heads/"
        output = self.git("for-each-ref", "--format=%(objectname) %(refname)", prefix)
        return {
            ref.removeprefix(prefix): sha
            for sha, ref in (line.split(" ", 1) for line in output.decode().splitlines())
        }

    def history(self, sha: str) -> list[tuple[str, int]]:
        self._check_sha(sha)
        return [
            (commit.decode(), int(timestamp))
            for commit, timestamp in map(bytes.split, self.git(
                "log", "--first-parent", "--no-show-signature", "--no-notes", "--format=%H %ct", sha, "--",
            ).splitlines())
        ]

    def tip_time(self, sha: str) -> int:
        self._check_sha(sha)
        # Read only the commit. In a blobless cache, git show may consult omitted
        # objects (notably when inspecting merges) even with patch output disabled.
        header = self.git("cat-file", "commit", sha).split(b"\n\n", 1)[0]
        for line in header.splitlines():
            if line.startswith(b"committer "):
                try:
                    return int(line.rsplit(b" ", 2)[-2])
                except (IndexError, ValueError):
                    break
        raise GitError("invalid committer timestamp in Git commit")

    def entry(self, sha: str, path: str) -> tuple[str, str] | None:
        self._check_sha(sha)
        value = self.git("ls-tree", "-z", sha, "--", path)
        if not value:
            return None
        meta, _ = value.split(b"\t", 1)
        mode, _, oid = meta.decode().split()
        return mode, oid

    def text(self, entry: tuple[str, str]) -> str:
        mode, oid = entry
        if mode not in {"100644", "100755"}:
            raise GitError("expected a regular Git blob")
        self._check_sha(oid)
        try:
            return self.git("cat-file", "blob", oid).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise GitError("file is not valid UTF-8") from None

    def has_commit(self, sha: str) -> bool:
        self._check_sha(sha)
        try:
            return self.git("cat-file", "-t", sha).strip() == b"commit"
        except GitError:
            return False

    def fetch_commit(self, remote: str, sha: str) -> None:
        self._check_sha(sha)
        self.git("fetch", "--quiet", "--no-tags", "--", remote, sha)

    def pin(self, snapshot: str, commits: set[str]) -> None:
        if not SNAPSHOT_ID.fullmatch(snapshot):
            raise ValueError("invalid snapshot ID")
        for sha in commits:
            self._check_sha(sha)
        if commits:
            self.git("update-ref", "--stdin", data="".join(
                f"update refs/scanner/snapshots/{snapshot}/{sha} {sha}\n"
                for sha in sorted(commits)
            ).encode())

    @staticmethod
    def _check_sha(sha: str) -> None:
        if not SHA.fullmatch(sha):
            raise ValueError("invalid Git object ID")


@contextmanager
def locked_state(path: Path) -> Iterator[None]:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (path / ".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield
