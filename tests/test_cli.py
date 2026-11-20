import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

IMAGE = "hub.flexiv.cloud/flexiv_sw_env/flexiv_thirdparty2_for_flexiv_sw_base"
PACKAGE = Path(__file__).resolve().parents[1] / "src"
PROJECTS = ("flexiv_sw", "flexiv_sw_base", "flexiv_sw_uiapp")


class ScannerTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "cache"
        self.documents = self.root / "Documents"
        self.documents.mkdir()
        self.docker = self.make_repo("docker")
        (self.docker / "docker_config.sh").write_text(f'DOCKER_IMAGE="{IMAGE}:legacy"\n')
        self.docker_sha = self.commit(self.docker)
        self.projects = {name: self.make_repo(name) for name in PROJECTS}
        for name, repo in self.projects.items():
            self.pipeline(repo, name + "-integration")
            self.commit(repo)
            self.git(repo, "checkout", "-qb", "release/v4.2")
            self.pipeline(repo, "v4.2-release")
            if name == PROJECTS[0]:
                self.git(repo, "update-index", "--add", "--cacheinfo", f"160000,{self.docker_sha},docker")
            self.commit(repo)

    def git(self, repo, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="Scanner Test", GIT_COMMITTER_NAME="Scanner Test",
                   GIT_AUTHOR_EMAIL="test@example.invalid", GIT_COMMITTER_EMAIL="test@example.invalid",
                   GIT_AUTHOR_DATE="2026-10-01T00:00:00Z", GIT_COMMITTER_DATE="2026-10-01T00:00:00Z")
        return subprocess.check_output(["git", "-C", str(repo), *args], env=env, text=True,
                                       stderr=subprocess.PIPE).strip()

    def make_repo(self, name):
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-q", "--initial-branch=develop")
        self.git(repo, "config", "uploadpack.allowFilter", "true")
        self.git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
        return repo

    def commit(self, repo):
        source = "docker_config.sh" if repo == self.docker else "bitbucket-pipelines.yml"
        self.git(repo, "add", "--", source)
        self.git(repo, "commit", "-qm", "fixture")
        return self.git(repo, "rev-parse", "HEAD")

    def pipeline(self, repo, tag):
        (repo / "bitbucket-pipelines.yml").write_text(f"image:\n  name: {IMAGE}:{tag}\n")

    def cli(self, *args, replay=False):
        command = [sys.executable, "-m", "docker_image_scanner.cli", "--state-dir", str(self.state),
                   "replay" if replay else "scan", "--json"]
        command.extend(args)
        if not replay:
            command.extend(["--as-of", "2026-11-20T00:00:00Z", "--docker-remote", str(self.docker)])
            for name, repo in self.projects.items():
                command.extend(["--repo", f"{name}={repo.as_uri()}"])
        return subprocess.run(command, cwd=self.documents, text=True, capture_output=True,
                              env=dict(os.environ, PYTHONPATH=str(PACKAGE), GIT_DIR="/ignored/by/scanner"))

    def test_scan_and_replay_after_ref_deletion_and_remote_loss(self):
        repo = self.projects[PROJECTS[0]]
        self.git(repo, "checkout", "-qb", "task/forked-before-release-advanced")
        self.pipeline(repo, "v4.1-old-toolchain")
        feature_sha = self.commit(repo)
        self.git(repo, "checkout", "-q", "release/v4.2")
        self.pipeline(repo, "v4.2-release-new-tip")
        self.commit(repo)
        result = self.cli("--active-release", "v4.2", "--save")
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        saved = json.loads(result.stdout)
        required = {item["image"]: item["evidence"] for item in saved["required_images"]}
        self.assertIn(IMAGE + ":legacy", required)
        self.assertEqual(self.docker_sha, required[IMAGE + ":legacy"][0]["docker_commit"])
        self.assertEqual(feature_sha, required[IMAGE + ":v4.1-old-toolchain"][0]["commit"])
        self.git(repo, "branch", "-D", "task/forked-before-release-advanced")
        refresh = self.cli("--active-release", "v4.2")
        self.assertEqual(0, refresh.returncode, refresh.stderr + refresh.stdout)
        self.assertNotIn(IMAGE + ":v4.1-old-toolchain", refresh.stdout)
        for source in (*self.projects.values(), self.docker):
            self.assertEqual(self.root, source.resolve().parent)
            source.rename(source.with_name(source.name + "-offline"))
        repeated = self.cli(saved["snapshot_id"], replay=True)
        self.assertEqual(0, repeated.returncode, repeated.stderr + repeated.stdout)
        self.assertEqual(saved, json.loads(repeated.stdout))

    def test_blobless_fetch_hydrates_pipeline_but_not_unrelated_blobs(self):
        name = PROJECTS[0]
        repo = self.projects[name]
        unrelated = repo / "unrelated.txt"
        unrelated.write_text("Unused source contents\n")
        self.git(repo, "add", "--", unrelated.name)
        self.commit(repo)
        blob = self.git(repo, "rev-parse", "HEAD:unrelated.txt")
        result = self.cli()
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        key = json.loads(result.stdout)["inputs"]["projects"][name]["cache_key"]
        check = subprocess.run(
            ["git", "--git-dir", str(self.state / "repos" / (key + ".git")), "cat-file", "-e", blob],
            capture_output=True, env=dict(os.environ, GIT_NO_LAZY_FETCH="1"),
        )
        self.assertNotEqual(0, check.returncode, "blobless scan fetched unrelated source contents")

    def test_missing_repo_reports_partial_results(self):
        self.projects[PROJECTS[1]] = self.root / "missing"
        result = self.cli()
        self.assertEqual(1, result.returncode, result.stderr + result.stdout)
        document = json.loads(result.stdout)
        self.assertFalse(document["complete"])
        self.assertTrue(any(PROJECTS[1] in problem for problem in document["errors"]))
        self.assertTrue(document["required_images"])

    def test_missing_docker_commit_does_not_hide_pipeline_image(self):
        repo = self.projects[PROJECTS[0]]
        self.git(repo, "update-index", "--add", "--cacheinfo", "160000," + "a" * 40 + ",docker")
        self.commit(repo)
        result = self.cli()
        self.assertEqual(1, result.returncode, result.stderr + result.stdout)
        document = json.loads(result.stdout)
        self.assertIn(IMAGE + ":v4.2-release", [i["image"] for i in document["required_images"]])
        self.assertTrue(any("docker/docker_config.sh" in problem for problem in document["errors"]))
