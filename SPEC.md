# Docker Image Scanner — v1 Specification

## Problem Statement

Flexiv engineers need to clean up old Harbor build images without removing images that are still required by published source revisions. A tag’s age or an absence of recent CI runs does not establish that it can be removed. Long-lived releases may use old images, integration branches may need recent historical revisions, and active release work can live on feature branches. Some older revisions also configure local Docker builds through an exact submodule commit, independently of the CI image.

A one-off search of current checkouts cannot apply these rules consistently across repos or explain a result once branches advance. Engineers need a repeatable, read-only list of required image references with evidence they can verify before making their own cleanup decisions.

## Solution

Provide a standalone `docker-image-scanner` CLI that reads published Bitbucket branches in `FlexivTeam/flexiv_sw`, `FlexivTeam/flexiv_sw_base`, and `FlexivTeam/flexiv_sw_uiapp` using existing Git credentials. It applies the retention policy below and lists Required Images for `hub.flexiv.cloud/flexiv_sw_env/flexiv_thirdparty2_for_flexiv_sw_base`. It does not inspect the invoking worktree, contact Harbor, or delete anything. Engineers can save a scan and replay it offline using the pinned Git inputs.

A **Required Image** is a reference found at a retained revision. It does not imply that anyone ran the image recently. Conversely, an image missing from this report is not necessarily safe to delete.

## User Stories

1. As a release engineer, I want a standalone command I can run from any directory, so that the scan does not depend on the repo or branch I happen to have checked out.
2. As a release engineer, I want all three published Bitbucket repos scanned by default, so that I do not miss a consumer in a different product repo.
3. As a developer, I want the scanner to reuse my existing Git credentials, so that I do not need a separate account, token, or registry login.
4. As a developer, I want the scanner to own a configurable cache, so that no fetch changes my worktrees and I can choose where the cache lives.
5. As a release maintainer, I want each Retained Release Branch’s tip protected regardless of age, so that older supported releases remain buildable.
6. As a release maintainer, I want the v3.11 and v4.2 minimums enforced separately within their major versions, so that patch and suffixed variants qualify without sweeping in explicitly excluded older releases.
7. As a release maintainer, I want unfamiliar release versions flagged and their tips protected, so that a new naming convention cannot silently drop a dependency.
8. As an integration maintainer, I want `develop` and `v4.x_develop` tips always protected, so that inactivity never drops the current build image.
9. As an integration maintainer, I want recent first-parent revisions protected for a configurable window, so that I can rebuild recent mainline states without preserving every intermediate commit from merged branches.
10. As a feature developer, I want an Active Feature Branch’s tip protected during a configurable window, so that I can continue my work without keeping every image from the branch’s earlier revisions.
11. As a cleanup reviewer, I want an Unclassified Branch’s tip included and flagged, so that nonstandard names do not hide a dependency.
12. As a release maintainer, I want to supply an optional Active Release when needed, so that Release-Derived Branches keep their tip images even after the normal feature window expires.
13. As a release maintainer, I want a branch that forked before the release tip advanced to remain eligible, so that release maintenance does not depend on rebasing every branch.
14. As a release maintainer, I want a second-parent merge of release history not to count by itself, so that unrelated integrations are not treated as release work.
15. As a cleanup reviewer, I want plausibly Release-Derived Branches retained with an ambiguity warning, so that uncertain Git provenance does not silently exclude an image.
16. As a release maintainer, I want to override the per-repo release anchor and correct active-release classification, so that I can account for exceptional branch layouts.
17. As a release maintainer, I want active-release overrides to leave ordinary retention intact, so that excluding a branch from one rule does not remove another valid reason to keep it.
18. As a release maintainer, I want a cross-version tag kept if a Release-Derived Branch references it, so that a naming mismatch does not break builds.
19. As a cleanup reviewer, I want image declarations from global defaults, steps, and referenced services inspected, so that pipeline overrides and sidecars are not missed.
20. As a local builder, I want the scanner to read the exact Docker submodule revision pinned by each retained commit, so that historical local build settings remain visible.
21. As a cleanup reviewer, I want both the CI and local Docker image kept when they differ, so that either consumer can still build.
22. As a cleanup reviewer, I want one deduplicated image reference per line by default, so that I can review or compare the list with other tools.
23. As a cleanup reviewer, I want JSON evidence pointing to each repo, branch, commit, source, and retention reason, so that I can verify each decision independently.
24. As a cleanup reviewer, I want partial results and a nonzero status when inputs are missing or unresolved, so that I can distinguish an incomplete scan from an empty or complete result.
25. As a release engineer, I want a saved snapshot to freeze the policy, cutoff time, and branch tips and preserve the necessary objects, so that I can recover the evidence after refs move or disappear.
26. As a release engineer, I want to replay a snapshot without contacting Bitbucket, so that I can reproduce a result while offline or after remote access changes.
27. As a developer testing the scanner, I want explicit Git remote overrides, so that I can use fixtures or mirrors without changing the default scope.
28. As a cleanup reviewer, I want the scanner to stop at reporting requirements, so that deciding what to remove stays a separate, manual action.

## Implementation Decisions

- **CLI and storage.** Ship an installable Python CLI in a separate repository. Live `scan` refreshes published heads in scanner-owned bare Git caches; it does not discover repositories from the current directory. Use `~/.docker-image-scanner` by default and accept `--state-dir` or `DOCKER_IMAGE_SCANNER_STATE_DIR`. Allow explicit read-only remotes or mirrors for development. Use blobless fetch where supported and retrieve the pipeline blobs needed for the scan.
- **Baseline release retention.** Protect tips of v3 release branches from v3.11 and v4 release branches from v4.2, including patch and delimiter-suffixed variants. Exclude explicitly older releases in those majors; do not reclassify them as Unclassified Branches. Protect and warn on unfamiliar release versions or naming patterns.
- **Integration and feature retention.** Always protect Integration Branch tips. Also protect first-parent commits within the integration window. For `bug/*`, `bugfix/*`, `task/*`, `feature/*`, and `fix/*`, protect only the feature tip if its Git committer timestamp is within the feature window. Both windows default to 30 days and can be configured independently. Evaluate against a recorded, timezone-aware scan time. Protect other Unclassified Branch tips and warn.
- **Active Release.** An optional version family such as `v4.2` anchors to the exact `release/v4.2` ref in each repo by default. Allow per-repo anchor overrides. A missing anchor makes the result incomplete without stopping other scans. Active Release selection is additive; it protects qualifying tips irrespective of age, not their earlier revisions. With no Active Release, only the baseline rules apply.
- **Release-Derived Branch inference.** Use connection to the anchor’s release-only first-parent history relative to competing integration and retained release refs. Do not count a second-parent merge or shared pre-release trunk as affirmative evidence. Protect plausible ambiguous tips and warn. Per-repo include/exclude overrides affect only Active Release selection. A release-family tag matches the exact version or a continuation beginning with `.`, `-`, or `_`; use it to flag mismatches, never to drop an otherwise Required Image or to protect an unreferenced tag.
- **Image declarations.** Read the retained revision’s canonical Bitbucket Pipelines YAML for the global image, step images (including nested groups and merge queues), and referenced service images. Resolve ordinary YAML aliases and merges. Read the historical local Docker configuration at the exact pinned Docker gitlink; parse known forms without executing shell. Preserve independent CI and local Docker references if both exist. Modern Docker config that reads the pipeline does not introduce an independent pin. A genuinely absent Docker gitlink is not itself an error.
- **Reports.** Keep only references to the exact target repository, preserving the declared tag or digest form without resolving it against Harbor. Return a stable, deduplicated text list by default and diagnostics on stderr. JSON reports include the frozen inputs, protection evidence, warnings, and errors. Exit `0` when the declared scan scope completes, `1` when results are partial, and `2` on invocation or local state failure. Unavailable required inputs or unresolved declarations that could point to the target must not be silently skipped.
- **Snapshots.** `scan --save` stores a report and pins the branch-tip and Docker objects needed for replay in the scanner cache. `replay` accepts a snapshot ID or a report and re-evaluates the recorded branch tips, cutoff time, and policy from local objects only. A JSON export is not a portable object archive. Do not fetch replacements for missing replay inputs or automatically prune saved snapshots.

## Testing Decisions

- Use the published **CLI** as the primary test seam. Launch it from a non-repo directory with temporary Git remotes, exercise `scan` and `replay`, and assert documented output, evidence, exit status, and isolation from the caller’s Git environment. Do not assert the private sequence of Git commands. The existing CLI integration suite is the prior art.
- Cover the critical replay scenario end to end: save a scan, advance or delete a branch and refresh the live cache, make the remotes unavailable, then replay the original snapshot and compare results. Cover a missing repo and missing Docker commit, with confirmed images still emitted.
- Use focused **retention policy** tests for release thresholds, aged integration tips, first-parent windows across skewed timestamps, feature age, old forks, second-parent merges, ambiguous ancestry, missing anchors, and override precedence. Assert selected revisions, reasons, and diagnostics, not implementation details of the graph walk.
- Use focused **image reader** tests for global, step, service, and merge-queue declarations; aliases and merges; historical local Docker configurations; modern pipeline-derived values; unrelated registries; and unresolved expressions. Assert extracted references and evidence locations, including redacted diagnostics, without executing configuration.
- Keep routine tests offline and independent of live credentials. A live Bitbucket smoke test is useful for remote protocol and historical data compatibility, but is not a substitute for the deterministic fixtures.

## Out of Scope

- Listing or deleting Harbor tags; deciding which unlisted images can safely be deleted.
- Measuring actual CI or container usage, resolving mutable tags to current registry digests, or replacing existing tags.
- Executing pipeline scripts, evaluating arbitrary shell, or scanning image references used only by shell commands.
- Reading unpublished branches, uncommitted files, or the current worktree’s configuration.
- Proving an exact branch-creation origin from Git history, or protecting unreferenced images solely because their tags resemble the active version.
- Automatically expiring saved snapshots or packaging their object data as a portable archive.

## Further Notes

- Review warnings even when the scan completes. Review errors before using any partial list to inform cleanup. Active-release classification intentionally favors retention over certainty when ancestry plausibly supports more than one origin.
- At the time of this specification, the v1 implementation had 31 passing tests. A live `v4.2` scan covered 1,168 published branches across the three repos, reported 10 Required Image references and no scan errors, and produced byte-identical JSON on replay with SSH disabled. These counts are verification evidence, not expected future results.
