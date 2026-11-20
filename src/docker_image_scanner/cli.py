"""Command-line entry point. Output is safe to pipe into other tools."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .gitstore import DEFAULT_REMOTES, DOCKER_REMOTE, PROJECTS, GitError, locked_state
from .policy import Policy
from .scan import fresh_inputs, load, read_time, report, save


def _days(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a nonnegative number of days") from None
    if not 0 <= result <= 36500:
        raise argparse.ArgumentTypeError("days must be between 0 and 36500")
    return result


def _family(value: str) -> str:
    value = value.removeprefix("release/")
    if not re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value):
        raise argparse.ArgumentTypeError("use a release family such as v4.2")
    return value


def _bindings(values: list[str], *, multiple: bool = False) -> dict:
    result: dict = {}
    for value in values:
        name, separator, setting = value.partition("=")
        if not separator or name not in PROJECTS or not setting:
            raise ValueError("expected REPO=VALUE, where REPO is " + ", ".join(PROJECTS))
        if multiple:
            result.setdefault(name, set()).add(setting)
        elif name in result:
            raise ValueError(f"duplicate setting for {name}")
        else:
            result[name] = setting
    return {k: sorted(v) for k, v in result.items()} if multiple else result


def _branch_bindings(values: list[str], *, multiple: bool = False) -> dict:
    raw = _bindings(values, multiple=multiple)
    if multiple:
        return {project: sorted({v.removeprefix("refs/heads/") for v in branches})
                for project, branches in raw.items()}
    return {project: v.removeprefix("refs/heads/") for project, v in raw.items()}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="docker-image-scanner", description="List required Flexiv build images from published Git refs")
    default_state = Path(os.environ.get("DOCKER_IMAGE_SCANNER_STATE_DIR", "~/.docker-image-scanner"))
    p.add_argument("--state-dir", type=Path, default=default_state, help="scanner-owned Git cache and snapshots")
    commands = p.add_subparsers(dest="command", required=True)
    live = commands.add_parser("scan", help="fetch current remote branches and read their image declarations")
    replay = commands.add_parser("replay", help="recompute a report against recorded inputs without network access")
    for mode in (live, replay):
        mode.add_argument("--state-dir", type=Path, default=argparse.SUPPRESS)
        mode.add_argument("--json", action="store_true", help="include inputs, warnings, and evidence as JSON")
    live.add_argument("--active-release", type=_family, metavar="vMAJOR.MINOR")
    live.add_argument("--integration-days", type=_days, default=30)
    live.add_argument("--feature-days", type=_days, default=30)
    live.add_argument("--as-of", type=read_time, metavar="TIMESTAMP", help="freeze cutoff time (ISO 8601 with timezone)")
    live.add_argument("--save", action="store_true", help="pin Git objects and save a replayable snapshot")
    live.add_argument("--anchor", action="append", default=[], metavar="REPO=BRANCH", help="override the active-release anchor for one repo")
    live.add_argument("--include", action="append", default=[], metavar="REPO=BRANCH", help="include exact branch in active-release selection")
    live.add_argument("--exclude", action="append", default=[], metavar="REPO=BRANCH", help="exclude exact branch only from active-release selection")
    live.add_argument("--repo", action="append", default=[], metavar="REPO=REMOTE", help="override Git remote (e.g. an absolute test mirror path)")
    live.add_argument("--docker-remote", default=DOCKER_REMOTE, metavar="REMOTE", help="override the shared Docker submodule remote")
    replay.add_argument("snapshot", help="saved snapshot ID or path to a JSON report")
    return p


def _emit(document: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(document, indent=2, sort_keys=True))
    else:
        for item in document["required_images"]:
            print(item["image"])
        for note in document["warnings"]:
            print(f"warning: {note}", file=sys.stderr)
        for problem in document["errors"]:
            print(f"incomplete: {problem}", file=sys.stderr)
        if document["snapshot_id"]:
            print(f"snapshot: {document['snapshot_id']}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    state = args.state_dir.expanduser().resolve()
    try:
        with locked_state(state):
            if args.command == "scan":
                if not args.active_release and (args.anchor or args.include or args.exclude):
                    raise ValueError("--anchor, --include and --exclude require --active-release")
                policy = Policy(integration_days=args.integration_days, feature_days=args.feature_days,
                                active_release=args.active_release, anchors=_branch_bindings(args.anchor),
                                includes=_branch_bindings(args.include, multiple=True),
                                excludes=_branch_bindings(args.exclude, multiple=True))
                remotes = DEFAULT_REMOTES | _bindings(args.repo)
                inputs = fresh_inputs(state, remotes, args.docker_remote, policy,
                                      args.as_of or datetime.now(timezone.utc))
                document, pins = report(state, inputs, docker_remote=args.docker_remote,
                                        project_remotes=remotes,
                                        snapshot_id=uuid4().hex if args.save else None)
                if args.save:
                    try:
                        save(state, document, pins)
                    except (OSError, GitError) as exc:
                        document["errors"].append(f"could not save snapshot: {exc}")
                        document["snapshot_id"] = None
                        document["complete"] = False
            else:
                old = load(state, args.snapshot)
                document, _ = report(state, old["inputs"], snapshot_id=old.get("snapshot_id"))
            _emit(document, args.json)
    except (GitError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(f"docker-image-scanner: {exc}", file=sys.stderr)
        return 2
    return 0 if document["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
