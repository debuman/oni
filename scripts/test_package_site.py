#!/usr/bin/env python3
"""Verify deployment packaging preserves repository paths without leaking metadata."""

from pathlib import Path
import tarfile
import tempfile
import unittest

from package_site import package


class PackageTests(unittest.TestCase):
    def test_dot_prefixed_repositories_and_paths_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public"
            (public / ".github/file").mkdir(parents=True)
            (public / "index.html").write_text("catalog")
            (public / ".github/index.html").write_text("repository")
            (public / ".github/file/.gitignore.html").write_text("source view")
            output = root / "artifact.tar"
            package(public, output)
            with tarfile.open(output) as archive:
                self.assertIn(".github/index.html", archive.getnames())
                self.assertIn(".github/file/.gitignore.html", archive.getnames())
                self.assertTrue(all(member.isfile() or member.isdir() for member in archive))
            with self.assertRaises(ValueError):
                package(public, public / "artifact.tar")
            (public / ".git").mkdir()
            (public / ".git/config").write_text("private metadata")
            with self.assertRaises(ValueError):
                package(public, output)

    def test_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public"
            public.mkdir()
            (public / "index.html").write_text("catalog")
            outside = root / "private.txt"
            outside.write_text("private")
            (public / "linked.txt").symlink_to(outside)
            with self.assertRaises(ValueError):
                package(public, root / "artifact.tar")


if __name__ == "__main__":
    unittest.main(verbosity=2)
