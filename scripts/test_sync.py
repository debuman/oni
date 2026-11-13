#!/usr/bin/env python3
"""Exercise synchronization without changing any GitHub repository."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
from io import BytesIO
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import build
import github_data
import sync_state

NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)


def repository(name="sample"):
    return {"id": 1, "name": name, "full_name": f"debuman/{name}", "private": False,
            "owner": {"login": "debuman"}, "clone_url": f"https://github.com/debuman/{name}.git",
            "description": "Example", "default_branch": "main",
            "pushed_at": NOW.isoformat(), "updated_at": NOW.isoformat()}


def pull():
    return {"number": 1, "title": "A change", "state": "open", "draft": False,
            "merged_at": None, "updated_at": NOW.isoformat(), "author": "contributor",
            "head": "feature", "head_sha": "a" * 40, "base": "main"}


class StateTests(unittest.TestCase):
    def test_branch_and_tag_updates_do_not_depend_on_repository_timestamps(self):
        repos, pulls = [repository()], {"sample": []}
        refs = {"sample": [["refs/heads/main", "a" * 40]]}
        previous = sync_state.manifest(repos, pulls, NOW, refs=refs)
        for changed in ([], [["refs/heads/main", "b" * 40]],
                        refs["sample"] + [["refs/tags/v1", "a" * 40]]):
            current = sync_state.manifest(repos, pulls, NOW, refs={"sample": changed})
            self.assertTrue(sync_state.needs_build(previous, current, NOW))

    def test_no_change_skips_build_without_expiring(self):
        current = sync_state.manifest([repository()], {"sample": [pull()]}, NOW)
        for elapsed in (timedelta(minutes=5), timedelta(days=1), timedelta(days=365)):
            with self.subTest(elapsed=elapsed):
                self.assertFalse(sync_state.needs_build(current, current, NOW + elapsed))
        self.assertTrue(sync_state.needs_build(current, current, NOW - timedelta(seconds=1)))

    def test_push_metadata_and_pull_changes_trigger_build(self):
        repos, pulls = [repository()], {"sample": [pull()]}
        previous = sync_state.manifest(repos, pulls, NOW)
        for field, value in {"id": 2, "description": "new", "default_branch": "next",
                             "pushed_at": "new push", "updated_at": "metadata changed"}.items():
            with self.subTest(field=field):
                changed = deepcopy(repos)
                changed[0][field] = value
                self.assertTrue(sync_state.needs_build(previous, sync_state.manifest(changed, pulls, NOW), NOW))
        for field, value in {"state": "closed", "draft": True, "merged_at": NOW.isoformat(),
                             "head_sha": "b" * 40, "title": "edited", "updated_at": "new"}.items():
            with self.subTest(field=field):
                changed = deepcopy(pulls)
                changed["sample"][0][field] = value
                self.assertTrue(sync_state.needs_build(previous, sync_state.manifest(repos, changed, NOW), NOW))
        for changed in ({"sample": []}, {"sample": [pull(), dict(pull(), number=2)]}):
            self.assertTrue(sync_state.needs_build(previous, sync_state.manifest(repos, changed, NOW), NOW))

    def test_discovery_removal_rename_and_empty_account(self):
        old = sync_state.manifest([repository()], {"sample": []}, NOW)
        for repos in ([], [repository("renamed")], [repository(), repository("new")]):
            with self.subTest(repos=repos):
                state = sync_state.manifest(repos, {repo["name"]: [] for repo in repos}, NOW)
                self.assertTrue(sync_state.needs_build(old, state, NOW))
        with patch.object(github_data, "pages", return_value=[repository(), dict(repository("private"), private=True)]):
            self.assertEqual(github_data.repositories(), [repository()])

    def test_missing_old_or_invalid_manifest_forces_build(self):
        current = sync_state.manifest([], {}, NOW)
        for previous in (None, [], {}, dict(current, schema=0), dict(current, generated_at="bad"),
                         dict(current, generated_at="2026-11-13"), dict(current, owner="other")):
            with self.subTest(previous=previous):
                self.assertTrue(sync_state.needs_build(previous, current, NOW))

    def test_repository_order_and_unrendered_counts_do_not_trigger_build(self):
        repos = [repository("a"), repository("b")]
        pulls = {"a": [], "b": []}
        previous = sync_state.manifest(repos, pulls, NOW)
        repos.reverse()
        repos[0]["stargazers_count"] = 100
        self.assertFalse(sync_state.needs_build(previous, sync_state.manifest(repos, pulls, NOW), NOW))

    def test_manifest_404_bootstraps_but_service_failure_is_not_a_deletion(self):
        with patch.object(sync_state, "urlopen", side_effect=HTTPError("url", 404, "missing", {}, None)):
            self.assertIsNone(sync_state.published_manifest())
        with patch.object(sync_state, "urlopen", side_effect=HTTPError("url", 503, "down", {}, None)):
            with self.assertRaises(HTTPError):
                sync_state.published_manifest()

    def test_pages_request_never_carries_token(self):
        with patch.dict("os.environ", {"GITHUB_TOKEN": "test-secret"}), patch.object(sync_state, "urlopen") as opened:
            opened.return_value.__enter__.return_value.read.return_value = b'{}'
            sync_state.published_manifest()
            req = opened.call_args.args[0]
            self.assertIsNone(req.get_header("Authorization"))
            self.assertIn("/sync.json?check=", req.full_url)

    def test_cli_skips_unchanged_snapshot_older_than_a_day(self):
        previous = sync_state.manifest([repository()], {"sample": []}, NOW - timedelta(days=365))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with patch("sys.argv", ["sync_state.py", "--github-output", str(output)]), \
                    patch.object(sync_state, "published_manifest", return_value=previous), \
                    patch.object(github_data, "repositories", return_value=[repository()]), \
                    patch.object(github_data, "pull_requests", return_value=[]), \
                    patch.object(github_data, "references", return_value=[]):
                sync_state.main()
                self.assertEqual(output.read_text(), "changed=false\n")

    def test_cli_writes_output_and_api_failure_never_marks_snapshot_current(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with patch("sys.argv", ["sync_state.py", "--github-output", str(output)]), \
                    patch.object(sync_state, "published_manifest", return_value=None), \
                    patch.object(github_data, "repositories", return_value=[repository()]), \
                    patch.object(github_data, "pull_requests", return_value=[]), \
                    patch.object(github_data, "references", return_value=[]):
                sync_state.main()
                self.assertEqual(output.read_text(), "changed=true\n")
                output.unlink()
                with patch.object(github_data, "pull_requests", side_effect=RuntimeError("rate limited")):
                    with self.assertRaises(RuntimeError):
                        sync_state.main()
                    self.assertFalse(output.exists())


class ApiTests(unittest.TestCase):
    def test_refs_include_branch_and_tag_changes_and_handle_empty_repositories(self):
        refs = [{"ref": name, "object": {"sha": "a" * 40}} for name in
                ("refs/tags/v1", "refs/heads/main", "refs/pull/1/merge")]
        with patch.object(github_data, "request", return_value=json.dumps(refs)):
            self.assertEqual(github_data.references(repository()),
                             [["refs/heads/main", "a" * 40], ["refs/tags/v1", "a" * 40]])
        error = HTTPError("url", 409, "empty", {}, BytesIO(b'{"message":"Git Repository is empty."}'))
        with patch.object(github_data, "request", side_effect=error):
            self.assertEqual(github_data.references(repository()), [])
        for status in (403, 404, 409, 503):
            error = HTTPError("url", status, "failed", {}, BytesIO(b'{"message":"not empty"}'))
            with patch.object(github_data, "request", side_effect=error), self.assertRaises(HTTPError):
                github_data.references(repository())

    def test_pagination_and_invalid_responses(self):
        first = [{"number": n} for n in range(100)]
        with patch.object(github_data, "request", side_effect=[json.dumps(first), '[{"number":100}]']) as req:
            self.assertEqual(len(list(github_data.pages("/example?state=all"))), 101)
            self.assertIn("&per_page=100&page=2", req.call_args.args[0])
        with patch.object(github_data, "request", return_value='{"message":"error"}'):
            with self.assertRaises(ValueError):
                list(github_data.pages("/example"))

    def test_all_pr_states_and_stable_order(self):
        raw = {**pull(), "user": {"login": "contributor"},
               "head": {"ref": "feature", "sha": "a" * 40}, "base": {"ref": "main"}}
        with patch.object(github_data, "pages", return_value=[dict(raw, number=2), raw]) as pages:
            self.assertEqual([item["number"] for item in github_data.pull_requests(repository())], [1, 2])
            self.assertIn("state=all", pages.call_args.args[0])

    def test_foreign_private_or_unsafe_repositories_are_rejected(self):
        for repo in (repository(".."), repository(".git"), repository("sync.json"), repository("licenses"),
                     repository("../outside"), dict(repository(), private=True),
                     dict(repository(), clone_url="https://elsewhere.invalid/repo.git"),
                     dict(repository(), owner={"login": "other"})):
            with self.subTest(repo=repo):
                with self.assertRaises(ValueError):
                    github_data.validate_repository(repo)
        github_data.validate_repository(repository(".github"))
        with self.assertRaises(ValueError):
            github_data.request("https://oni.sqzer.com/")

    def test_empty_online_account_replaces_old_metadata(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(build, "CACHE", Path(directory)), \
                patch.object(github_data, "repositories", return_value=[]):
            (Path(directory) / "repositories.json").write_text(json.dumps([repository()]))
            self.assertEqual(build.repositories(False), [])
            self.assertEqual(json.loads((Path(directory) / "repositories.json").read_text()), [])


class Elements(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.elements = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class RenderingTests(unittest.TestCase):
    def test_pr_text_is_escaped_and_links_are_derived(self):
        malicious = dict(pull(), title='<img src=x onerror=alert(1)> 한국어', author='<script>bad</script>')
        html = sync_state.render_pulls(repository(), [malicious])
        self.assertIn("&lt;img", html)
        self.assertIn("한국어", html)
        self.assertNotIn("<script", html)
        self.assertEqual([attrs["href"] for tag, attrs in Elements(html).elements if tag == "a"],
                         ["https://github.com/debuman/sample/pull/1"])
        for kwargs, state in (({}, "open"), ({"draft": True}, "draft"),
                              ({"state": "closed", "draft": True}, "closed"),
                              ({"state": "closed", "merged_at": NOW.isoformat()}, "merged")):
            self.assertIn(f"<td>{state}</td>", sync_state.render_pulls(repository(), [dict(pull(), **kwargs)]))
        self.assertIn("No pull requests", sync_state.render_pulls(repository(), []))

    @unittest.skipUnless(build.BINARY.is_file(), "Build the Rust generator first")
    def test_empty_repository_archive_and_pr_navigation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "sample.git"
            subprocess.run(["git", "init", "--bare", "--quiet", "--initial-branch=main", repo], check=True)
            fragment = root / "pulls-fragment.html"
            fragment.write_text(sync_state.render_pulls(repository(), [pull()]))
            out = root / "site"
            build.generate_repository(repository(), repo, out, sync_state.SITE, pulls_page=fragment)
            self.assertIn('href="pulls.html"', (out / "log.html").read_text())
            self.assertIn("A change", (out / "pulls.html").read_text())
            self.assertIn("script-src", (out / "pulls.html").read_text())
            with tarfile.open(out / "archive.tar.gz") as archive:
                self.assertTrue(archive.getmembers()[0].isdir())

    def test_failed_generation_preserves_current_site_and_sync_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".cache").mkdir()
            (root / "public").mkdir()
            (root / "public/sync.json").write_text("old snapshot")
            with patch.object(build, "ROOT", root), patch.object(build, "CACHE", root / ".cache"), \
                    patch.object(build, "site_assets"), \
                    patch.object(build, "generate_repository", side_effect=RuntimeError("fetch failed")):
                with self.assertRaises(RuntimeError):
                    build.generate([repository()], [root / "repo.git"], sync_state.SITE,
                                   pulls={"sample": []}, sync_manifest={"new": True})
            self.assertEqual((root / "public/sync.json").read_text(), "old snapshot")


if __name__ == "__main__":
    unittest.main(verbosity=2)
