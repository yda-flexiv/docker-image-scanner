from collections import Counter
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from unittest import TestCase

from docker_image_scanner.policy import Policy, select, version_family


AS_OF = datetime(2026, 11, 20, tzinfo=timezone.utc)


def at_age(days: int, seconds: int = 0) -> int:
    return int((AS_OF - timedelta(days=days, seconds=seconds)).timestamp())


class PolicyTests(TestCase):
    def run_policy(self, tips, histories, policy=None, repo="test-repo"):
        calls = Counter()

        def history(sha):
            calls[sha] += 1
            value = histories[sha]
            if isinstance(value, RuntimeError):
                raise value
            return value

        selected, warnings, errors = select(repo, tips, AS_OF, policy or Policy(), history)
        return selected, warnings, errors, calls

    def test_version_family_matches_only_at_delimiter(self):
        for tag in ("v4.2", "v4.2.1", "v4.2_hotfix", "v4.2-rc1"):
            with self.subTest(tag=tag):
                self.assertTrue(version_family(tag, "v4.2"))
        for tag in ("v4.20", "v4.2rc", "v4.21.0", "prefix-v4.2", "v4.1"):
            with self.subTest(tag=tag):
                self.assertFalse(version_family(tag, "v4.2"))

    def test_policy_is_frozen_with_independent_override_mappings(self):
        first, second = Policy(), Policy()
        with self.assertRaises(FrozenInstanceError):
            first.feature_days = 7
        for field in ("anchors", "includes", "excludes"):
            self.assertIsNot(getattr(first, field), getattr(second, field))
        self.assertEqual((30, 30, None), (first.integration_days, first.feature_days, first.active_release))

    def test_integration_retains_tip_and_window_across_clock_skew(self):
        tips = {"develop": "d", "v4.x_develop": "i"}
        histories = {
            "d": [("d", at_age(31)), ("old", at_age(50)), ("boundary", at_age(30)),
                  ("just-outside", at_age(30, 1)), ("new", at_age(1))],
            "i": [("i", at_age(90))],
        }
        selected, warnings, errors, _ = self.run_policy(tips, histories)
        self.assertEqual(
            {("develop", "d", "integration-tip"), ("v4.x_develop", "i", "integration-tip"),
             ("develop", "boundary", "integration-window"), ("develop", "new", "integration-window")},
            set(selected),
        )
        self.assertEqual(([], []), (warnings, errors))
        selected, _, _, _ = self.run_policy(tips, histories, Policy(integration_days=7))
        self.assertNotIn(("develop", "boundary", "integration-window"), selected)

    def test_release_thresholds_are_numeric_and_major_specific(self):
        kept = (
            "release/v3.11", "release/v3.18.9", "release/v3.200-hotfix", "release/v4.2",
            "release/v4.2.1", "release/v4.10_beta", "release/v4.12-rc3",
        )
        old = ("release/v3.9", "release/v3.10.99-rc", "release/v4.0", "release/v4.1_hotfix")
        unknown = ("release/v5.1", "release/v2.99", "release/stable", "release/v4.2x")
        tips = {branch: f"sha-{branch}" for branch in kept + old + unknown}
        selected, warnings, errors, calls = self.run_policy(tips, {})
        self.assertEqual(
            {(branch, tips[branch], "supported-release-tip") for branch in kept}
            | {(branch, tips[branch], "unknown-release-tip") for branch in unknown},
            set(selected),
        )
        self.assertEqual(len(unknown), len(warnings))
        self.assertTrue(all(any(branch in message for message in warnings) for branch in unknown))
        self.assertEqual([], errors)
        self.assertEqual({}, calls)

    def test_features_select_only_recent_tips_and_unclassified_protects_tip(self):
        tips = {f"{prefix}/work": "recent" for prefix in ("bug", "bugfix", "task", "feature", "fix")}
        tips.update({"bug/stale": "stale", "experiment": "unknown"})
        histories = {
            "recent": [("recent", at_age(6)), ("parent", at_age(3))],
            "stale": [("stale", at_age(7, 1)), ("newer-parent", at_age(2))],
        }
        selected, warnings, errors, calls = self.run_policy(tips, histories, Policy(feature_days=7))
        self.assertEqual({branch for branch in tips if branch != "bug/stale"}, {b for b, _, _ in selected})
        self.assertEqual({"recent", "unknown"}, {sha for _, sha, _ in selected})
        self.assertEqual(1, len(warnings))
        self.assertIn("experiment", warnings[0])
        self.assertEqual([], errors)
        self.assertEqual({"recent": 1, "stale": 1}, calls)

    def test_history_errors_are_memoized_and_other_work_continues(self):
        tips = {"develop": "broken", "bug/work": "broken", "release/v4.2": "ok"}
        selected, _, errors, calls = self.run_policy(tips, {"broken": RuntimeError("missing object")})
        self.assertEqual({"broken": 1}, calls)
        self.assertEqual({("develop", "broken"), ("bug/work", "broken"), ("release/v4.2", "ok")},
                         {(branch, sha) for branch, sha, _ in selected})
        self.assertTrue(errors)
        self.assertTrue(all("missing object" in error for error in errors))

    def test_active_release_uses_first_parent_beyond_common_trunk(self):
        paths = {
            "release/v4.2": ("anchor", "release-only", "fork", "trunk"),
            "develop": ("integration", "fork", "trunk"),
            "release/v4.20": ("other-release", "fork", "trunk"),
            "release/v4.2.1": ("patch", "release-only", "fork", "trunk"),
            "task/derived": ("derived", "release-only", "fork", "trunk"),
            "task/plausible": ("plausible", "fork", "trunk"),
            "task/trunk-only": ("unrelated", "trunk"),
            # The merge's release parent is not part of the callback's chain.
            "task/merged-release-as-second-parent": ("merge", "integration", "fork", "trunk"),
            "release/v4.1-old": ("old", "release-only", "fork", "trunk"),
        }
        tips = {branch: shas[0] for branch, shas in paths.items()}
        histories = {shas[0]: [(sha, at_age(60)) for sha in shas] for shas in paths.values()}
        policy = Policy(active_release="v4.2")
        selected, warnings, errors, calls = self.run_policy(tips, histories, policy)
        active = {row for row in selected if row[2].startswith("active-release")}
        self.assertEqual({
            ("release/v4.2", "anchor", "active-release-anchor"),
            ("release/v4.2.1", "patch", "active-release-derived"),
            ("task/derived", "derived", "active-release-derived"),
            ("task/plausible", "plausible", "active-release-ambiguous"),
        }, active)
        self.assertEqual(1, len(warnings))
        self.assertIn("task/plausible", warnings[0])
        self.assertIn("ambiguous", warnings[0])
        self.assertEqual([], errors)
        self.assertNotIn("old", calls)
        self.assertTrue(all(count == 1 for count in calls.values()))
        reverse = dict(reversed(list(tips.items())))
        self.assertEqual((selected, warnings, errors), self.run_policy(reverse, histories, policy)[:3])

    def test_other_supported_release_can_make_ancestry_ambiguous(self):
        paths = {
            "release/v4.2": ("anchor", "overlap", "fork", "trunk"),
            "release/v4.20": ("outside-family", "overlap", "fork", "trunk"),
            "release/v3.10": ("not-supported", "anchor", "overlap", "fork", "trunk"),
            "v4.x_develop": ("integration", "fork", "trunk"),
            "bug/unknown-source": ("ambiguous", "overlap", "fork", "trunk"),
            "bug/known-source": ("proven", "anchor", "overlap", "fork", "trunk"),
            "bug/competing-source": ("competing", "outside-family", "overlap", "fork", "trunk"),
        }
        tips = {branch: shas[0] for branch, shas in paths.items()}
        histories = {shas[0]: [(sha, at_age(60)) for sha in shas] for shas in paths.values()}
        selected, warnings, errors, _ = self.run_policy(tips, histories, Policy(active_release="v4.2"))
        self.assertIn(("bug/unknown-source", "ambiguous", "active-release-ambiguous"), selected)
        self.assertIn(("bug/known-source", "proven", "active-release-derived"), selected)
        self.assertNotIn("bug/competing-source", {branch for branch, _, _ in selected})
        self.assertEqual(1, len(warnings))
        self.assertEqual([], errors)

    def test_missing_exact_anchor_does_not_fall_back_and_keeps_baseline_and_include(self):
        tips = {"release/v4.2.1": "patch", "task/manual": "manual"}
        histories = {"manual": [("manual", at_age(60))]}
        policy = Policy(active_release="v4.2", includes={"test-repo": ["task/manual"]})
        selected, warnings, errors, calls = self.run_policy(tips, histories, policy)
        self.assertEqual({
            ("release/v4.2.1", "patch", "supported-release-tip"),
            ("task/manual", "manual", "active-release-include"),
        }, set(selected))
        self.assertEqual([], warnings)
        self.assertEqual(1, len(errors))
        self.assertIn("'release/v4.2'", errors[0])
        self.assertEqual({"manual": 1}, calls)

    def test_active_overrides_are_repo_specific_and_leave_baseline_untouched(self):
        paths = {
            "release/v4.2-override": ("override", "fork", "trunk"),
            "develop": ("integration", "fork", "trunk"),
            "bug/excluded": ("excluded", "override", "fork", "trunk"),
            "task/overridden": ("included", "trunk"),
            "task/other-repo": ("other-repo", "trunk"),
        }
        tips = {branch: shas[0] for branch, shas in paths.items()}
        histories = {shas[0]: [(sha, at_age(60)) for sha in shas] for shas in paths.values()}
        histories["excluded"][0] = ("excluded", at_age(1))
        policy = Policy(
            active_release="release/v4.2",
            anchors={"test-repo": "release/v4.2-override"},
            includes={"test-repo": ["task/overridden", "bug/excluded"], "elsewhere": ["task/other-repo"]},
            excludes={"test-repo": ["bug/excluded", "develop"], "elsewhere": ["task/overridden"]},
        )
        selected, warnings, errors, _ = self.run_policy(tips, histories, policy)
        self.assertEqual({
            ("release/v4.2-override", "override", "supported-release-tip"),
            ("release/v4.2-override", "override", "active-release-anchor"),
            ("task/overridden", "included", "active-release-include"),
            ("bug/excluded", "excluded", "recent-feature-tip"),
            ("develop", "integration", "integration-tip"),
        }, set(selected))
        self.assertEqual(([], []), (warnings, errors))
        disabled = Policy(includes=policy.includes, excludes=policy.excludes, anchors=policy.anchors)
        rows, _, _, _ = self.run_policy(tips, histories, disabled)
        self.assertFalse(any(reason.startswith("active-release") for _, _, reason in rows))

    def test_unavailable_competitor_is_incomplete_not_positive_evidence(self):
        tips = {"develop": "broken", "release/v4.2": "anchor", "task/possible": "possible"}
        histories = {
            "broken": RuntimeError("cannot traverse"),
            "anchor": [("anchor", at_age(60))],
            "possible": [("possible", at_age(60)), ("anchor", at_age(60))],
        }
        selected, warnings, errors, calls = self.run_policy(tips, histories, Policy(active_release="v4.2"))
        self.assertIn(("task/possible", "possible", "active-release-ambiguous"), selected)
        self.assertIn("competing", warnings[0])
        self.assertIn("develop", errors[0])
        self.assertEqual(1, calls["broken"])
