# Docker Image Scanner

List Flexiv build images that must be kept because they are referenced by published Git revisions. The scanner does not inspect your current directory as a Git worktree, contact Harbor, run pipeline scripts, pull images, or delete anything in the registry. A missing image in the report is not an authorization to delete it.

## Install and run

Requires Python 3.10+, Git, and a Unix-like OS. Install once from the source repository; afterwards the command can run from any directory:

```sh
uv tool install ~/ws/utils/docker-image-scanner  # or: pipx install the same path
cd ~/Documents
docker-image-scanner scan
docker-image-scanner scan --active-release v4.2 --save --json > images.json
docker-image-scanner replay images.json --json
```

Live scans fetch branch heads from `FlexivTeam/flexiv_sw`, `FlexivTeam/flexiv_sw_base`, and `FlexivTeam/flexiv_sw_uiapp` on Bitbucket using your existing Git configuration and credentials. The shared Docker submodule repository is fetched only when a pinned commit is needed. Initial fetches can be large. Use `--state-dir /absolute/path` or `DOCKER_IMAGE_SCANNER_STATE_DIR` to change the default `~/.docker-image-scanner` cache. Remote read-only local mirrors can be explicitly selected with `--repo flexiv_sw=/path/to/mirror.git` (repeat for other repos) and `--docker-remote /path/to/docker.git`.

The target repository is `hub.flexiv.cloud/flexiv_sw_env/flexiv_thirdparty2_for_flexiv_sw_base`. Default output is one full image reference per line; warnings and errors go to stderr. JSON includes each image's source lines, repo, branch, revision, and retention reasons. Exit `0` means the declared scope was scanned, `1` means partial results (check `errors` in JSON), and `2` means an invocation or local state failure. A successful scan can still have warnings.

## Retention policy

| Branch | Revisions to scan |
| --- | --- |
| `release/v3.11` and later v3 releases; `release/v4.2` and later v4 releases | Tip, including patch and suffix variants. Explicitly older v3/v4 releases are excluded. |
| `develop`, `v4.x_develop` | Tip plus first-parent commits within 30 days, configurable via `--integration-days`. |
| `bug/*`, `bugfix/*`, `task/*`, `feature/*`, `fix/*` | Tip if its Git committer timestamp is within `--feature-days` (default 30). |
| Other branches | Tip; flagged for review. |

With `--active-release v4.2`, the scanner additionally checks tips of branches inferred to derive from `release/v4.2`, regardless of age. Git has no branch-creation provenance: we use first-parent relationships with the active release and competing lines, not a second-parent merge or a shared pre-release trunk. Plausible ambiguous branches are included and flagged. A missing anchor makes the scan incomplete. Use `--anchor flexiv_sw=release/v4.2_certification` to override an anchor, and repeat `--include REPO=BRANCH` or `--exclude REPO=BRANCH` to correct active-release classification. These overrides do not cancel ordinary retention. A tag outside the active release family remains required if a retained revision references it; the scanner reports the mismatch.

## Sources and replay

The scanner reads declared global, step, and referenced service images from `bitbucket-pipelines.yml`. It also reads the historical `docker_config.sh` from the exact `docker` gitlink at each retained revision. A legacy Docker image independent of CI is kept alongside the CI image; newer configs simply derive theirs from CI. Arbitrary shell expansion and image pulls inside pipeline scripts are outside scope. An unresolved declared image that could reference the target, or an unavailable required Git object, is reported as incomplete.

`scan --save` writes a report under the state directory and pins the Git objects for that scan. `replay` accepts the saved ID or a JSON report and recomputes from its recorded branch tips, policy, and cutoff time using only the same local cache. `--save` is what guarantees pins are created; an exported report alone does not contain Git objects. Saved snapshots are not automatically pruned.

## Development

```sh
python3 -m pip install -e .
python3 -m unittest discover -s tests -v
```

Tests use temporary Git repositories and do not need Bitbucket or Harbor.
