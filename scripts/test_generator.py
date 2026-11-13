#!/usr/bin/env python3
"""Exercise the Rust generator against local Git fixtures; no network is used."""
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
from readme_assets import ReadmeLinks, link_readme_assets

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / ".cache/cargo/release/sqzer-stagit"


def run(*args, cwd=None, env=None):
    return subprocess.check_output([str(arg) for arg in args], cwd=cwd, env=env, timeout=60)


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        self.links.extend(value for key, value in attrs if key == "href")

def assert_local_links(out):
    for page in out.rglob("*.html"):
        parser = Links()
        parser.feed(page.read_text())
        for href in parser.links:
            url = urlsplit(href)
            if url.scheme or url.netloc or not url.path:
                continue
            if url.path.endswith(("style.css", "favicon.ico", "favicon.png", "archive.tar.gz")):
                continue
            assert (page.parent / unquote(url.path)).is_file(), (page, href)


def regressions(root, env):
    def fixture(name, files):
        repo = root / name
        repo.mkdir()
        run("git", "init", "-q", "--initial-branch=main", repo)
        run("git", "-C", repo, "config", "user.name", "Tester")
        run("git", "-C", repo, "config", "user.email", "test@example.invalid")
        for name, content in files.items():
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        commit(repo, "Initial")
        return repo

    def commit(repo, subject):
        run("git", "-C", repo, "add", "-A")
        run("git", "-C", repo, "commit", "-qm", subject, env=env)
        return run("git", "-C", repo, "rev-parse", "HEAD").decode().strip()

    def generate(repo):
        out = root / (repo.name + "-output")
        run(BINARY, "-u", "https://git.example/fixture/", "--output", out, repo)
        assert_local_links(out)
        return out

    repo = fixture("renames", {"src/old 한 글#?.txt": "one\ntwo\n", "source.txt": "source content\n"})
    run("git", "-C", repo, "mv", "src/old 한 글#?.txt", "src/new 한 글#?.txt")
    renamed = commit(repo, "Rename only")
    (repo / "copy.txt").write_text((repo / "source.txt").read_text())
    (repo / "source.txt").write_text("source content\nnew line\n")
    copied = commit(repo, "Copy and modify")
    out = generate(repo)
    page = (out / "commit" / (renamed + ".html")).read_text()
    assert 'class="R"' in page
    assert "old 한 글#?.txt -&gt; src/new 한 글#?.txt" in page
    assert "1 file changed, 0 insertion(+), 0 deletion(-)" in page
    assert 'class="C"' in (out / "commit" / (copied + ".html")).read_text()

    for name, files, expected, excluded in (
        ("lowercase-txt", {"readme.txt": "LOWERCASE TEXT\n"}, "LOWERCASE TEXT", None),
        ("lowercase", {"readme": "LOWERCASE README\n"}, "LOWERCASE README", None),
        ("readme-order", {"README": "PRIMARY PLAIN\n", "README.md": "# SECONDARY MD\n"},
         "PRIMARY PLAIN", "SECONDARY MD"),
        ("readme-md-order", {"README.md": "# PRIMARY MD\n", "README.txt": "SECONDARY TEXT\n"},
         "<h1>PRIMARY MD</h1>", "SECONDARY TEXT"),
    ):
        page = (generate(fixture(name, files)) / "README.html").read_text()
        assert expected in page
        assert excluded is None or excluded not in page

    repo = fixture("release-tags", {"README.md": "# Main\n"})
    run("git", "-C", repo, "checkout", "-qb", "release")
    (repo / "release.txt").write_text("Release preparation\n")
    release_parent = commit(repo, "Prepare separate release")
    (repo / "release.txt").write_text("Release finished\n")
    release = commit(repo, "Separate release")
    run("git", "-C", repo, "tag", "v-special")
    run("git", "-C", repo, "tag", "-am", "Annotated release", "v-annotated", env=env)
    run("git", "-C", repo, "checkout", "-q", "main")
    blob = run("git", "-C", repo, "rev-parse", "HEAD:README.md").decode().strip()
    run("git", "-C", repo, "tag", "v-blob", blob)
    out = generate(repo)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    feed = ET.parse(out / "tags.xml").getroot()
    entries = feed.findall("a:entry", ns)
    assert len(entries) == 2
    assert len({entry.findtext("a:id", namespaces=ns) for entry in entries}) == 2
    assert {entry.findtext("a:title", namespaces=ns) for entry in entries} == {
        "[v-special] Separate release", "[v-annotated] Separate release",
    }
    assert feed.find("a:updated", ns) is not None
    for oid in (release_parent, release):
        assert (out / "commit" / (oid + ".html")).is_file()
    for entry in entries:
        href = entry.find("a:link", ns).attrib["href"]
        assert href.endswith(f"/commit/{release}.html")
    assert release not in (out / "log.html").read_text()

    repo = fixture("empty-and-merge", {"a.txt": "base\n", "README.md": "# Main\n",
                   "LICENSE.txt": "license\n", ".gitmodules": "# no modules\n"})
    run("git", "-C", repo, "commit", "--allow-empty", "-qm", "Empty commit", env=env)
    empty = run("git", "-C", repo, "rev-parse", "HEAD").decode().strip()
    (repo / "a.txt").chmod(0o755)
    run("git", "-C", repo, "update-index", "--chmod=+x", "a.txt")
    run("git", "-C", repo, "commit", "-qm", "Mode only", env=env)
    mode = run("git", "-C", repo, "rev-parse", "HEAD").decode().strip()
    run("git", "-C", repo, "checkout", "-qb", "topic")
    (repo / "topic.txt").write_text("topic\n")
    commit(repo, "Topic change")
    run("git", "-C", repo, "checkout", "-q", "main")
    (repo / "main.txt").write_text("main\n")
    commit(repo, "Main change")
    run("git", "-C", repo, "merge", "--no-ff", "-qm", "Merge topic", "topic", env=env)
    merged = run("git", "-C", repo, "rev-parse", "HEAD").decode().strip()
    out = generate(repo)
    assert "0 files changed, 0 insertion(+), 0 deletion(-)" in (out / f"commit/{empty}.html").read_text()
    assert "1 file changed, 0 insertion(+), 0 deletion(-)" in (out / f"commit/{mode}.html").read_text()
    assert "1 file changed, 1 insertion(+), 0 deletion(-)" in (out / f"commit/{merged}.html").read_text()
    page = (out / "file/a.txt.html").read_text()
    assert 'href="../file/LICENSE.txt.html"' in page
    assert 'href="../file/.gitmodules.html"' in page

    # Stream markers must not collide with raw numstat paths or source lines.
    unusual = "stream\noni-end-of-diff\nfile.txt"
    (repo / unusual).write_text("oni-end-of-diff\n<source> & text\n")
    streamed = commit(repo, "Marker-like filename and content")
    out = generate(repo)
    assert (out / "file" / (unusual + ".html")).is_file()
    page = (out / f"commit/{streamed}.html").read_text()
    assert "+oni-end-of-diff</a>" in page
    assert "&lt;source&gt; &amp; text" in page

    repo = fixture("readme-links", {"README.md": "# Links\n\n[docs](docs/?q=1#top)\n"
                   "[nested](docs/nested)\n[file](docs/nested/a.txt)\n[missing](absent)\n",
                   "docs/nested/a.txt": "text\n", "docs-other/b.txt": "text\n"})
    out = root / "readme-links-output"
    run(BINARY, "--output", out, repo)
    metadata = {"full_name": "owner/repo", "default_branch": "main"}
    parser = ReadmeLinks(out, repo, metadata)
    assert parser.resolve_link("docs/?q=1#top", False) == "https://github.com/owner/repo/tree/main/docs?q=1#top"
    assert parser.resolve_link("docs/nested", False) == "https://github.com/owner/repo/tree/main/docs/nested"
    assert parser.resolve_link("doc", False) == "doc"
    assert parser.resolve_link("absent", False) == "absent"
    assert parser.resolve_link("../docs", False) == "../docs"
    link_readme_assets(out, repo, metadata)
    assert 'href="file/docs/nested/a.txt.html"' in (out / "README.html").read_text()

    # More than a pipe buffer, an empty blob, and repeated requests exercise
    # the persistent binary protocol and child process cleanup.
    payload = b"\x89PNG\r\n\x1a\n" + b"\0\xffdata\n" * 20000
    (repo / "image #?.png").write_bytes(payload)
    (repo / "empty.bin").write_bytes(b"")
    commit(repo, "Image assets")
    image_out = root / "image-output"
    image_out.mkdir()
    parser = ReadmeLinks(image_out, repo, metadata)
    try:
        assert parser.resolve_link("image%20%23%3F.png?q=1#image", True) == "raw/image%20%23%3F.png?q=1#image"
        process = parser.objects
        assert process is not None
        assert parser.resolve_link("empty.bin", True) == ""
        assert parser.blob(parser.blobs["empty.bin"]) == b""
        assert parser.resolve_link("image%20%23%3F.png", True) == "raw/image%20%23%3F.png"
        assert parser.objects is process
        assert (image_out / "raw/image #?.png").read_bytes() == payload
        assert not (image_out / "raw/empty.bin").exists()
    finally:
        parser.close()
    assert process.poll() == 0

    parser = ReadmeLinks(image_out, repo, metadata)
    try:
        try:
            parser.blob("0" * 40)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Missing blobs must fail")
        failed_process = parser.objects
    finally:
        parser.close()
    assert failed_process.poll() is not None

    parser = ReadmeLinks(image_out, repo, metadata)
    parser.blob(parser.blobs["empty.bin"])
    stopped_process = parser.objects
    stopped_process.kill()
    stopped_process.wait()
    try:
        parser.close()
    except RuntimeError:
        pass
    else:
        raise AssertionError("Failed Git processes must be reported")
    assert parser.objects is None


def main():
    with tempfile.TemporaryDirectory(prefix="sqzer-stagit-") as directory:
        root = Path(directory)
        repo = root / "fixture"
        repo.mkdir()
        run("git", "init", "-q", "--initial-branch=main", repo)
        run("git", "-C", repo, "config", "user.name", "Tester & team")
        run("git", "-C", repo, "config", "user.email", "test@example.invalid")
        (repo / ".git/description").write_text("A <test> & repository\n")
        (repo / ".git/owner").write_text("debuman\n")
        (repo / ".git/url").write_text("https://example.invalid/fixture.git\n")
        (repo / "README.md").write_text("# Hello\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n- [x] done\n\n<details><summary>More</summary>yes</details>\n")
        (repo / "LICENSE").write_text("Fixture license\n")
        (repo / "gone.txt").write_text("removed later\n")
        (repo / "src").mkdir()
        (repo / "src/한 글#?.txt").write_text("<tag> & value\nlast line")
        (repo / "binary.dat").write_bytes(b"\0\xffbinary")
        run("git", "-C", repo, "add", ".")
        env = dict(os.environ, GIT_AUTHOR_DATE="2026-01-01T00:00:00+0000",
                   GIT_COMMITTER_DATE="2026-01-01T00:00:00+0000")
        run("git", "-C", repo, "commit", "-qm", "Initial <&> commit", env=env)
        run("git", "-C", repo, "tag", "-am", "Release", "v1", env=env)
        (repo / "gone.txt").unlink()
        (repo / "src/한 글#?.txt").write_text("updated\n")
        run("git", "-C", repo, "add", "-A")
        env.update(GIT_AUTHOR_DATE="2026-02-01T00:00:00+0000",
                   GIT_COMMITTER_DATE="2026-02-01T00:00:00+0000")
        run("git", "-C", repo, "commit", "-qm", "Second commit", env=env)
        run("git", "-C", repo, "branch", "topic")
        out = root / "output"
        run(BINARY, "-l", "1", "-u", "https://git.example/fixture/",
            "--output", out, repo)
        log = (out / "log.html").read_text()
        assert "1 more commits remaining" in log
        assert len(list((out / "commit").glob("*.html"))) == 2
        assert 'href="commit/' in log
        readme = (out / "README.html").read_text()
        assert "<h1>Hello</h1>" in readme and "<table>" in readme
        assert 'type="checkbox"' in readme and "<details>" in readme
        file_page = (out / "file/src/한 글#?.txt.html").read_text()
        assert 'href="../../README.html"' in file_page and 'id="l1"' in file_page
        assert "Binary file." in (out / "file/binary.dat.html").read_text()
        refs = (out / "refs.html").read_text()
        assert "topic" in refs and "v1" in refs and "Tester &amp; team" in refs
        for filename in ("atom.xml", "tags.xml"):
            ET.parse(out / filename)
        assert_local_links(out)
        empty = root / "empty"
        run("git", "init", "-q", "--bare", empty)
        empty_out = root / "empty-output"
        run(BINARY, "--output", empty_out, empty)
        assert (empty_out / "log.html").is_file()
        assert not (empty_out / "README.html").exists()
        for filename in ("atom.xml", "tags.xml"):
            assert ET.parse(empty_out / filename).getroot().find(
                "{http://www.w3.org/2005/Atom}updated") is not None
        index = run(BINARY, "--index", empty, repo).decode()
        assert index.index(">fixture</a>") < index.index(">empty</a>")
        assert "A &lt;test&gt; &amp; repository" in index
        failed = subprocess.run([str(BINARY), str(root / "missing")], capture_output=True)
        assert failed.returncode != 0
        regressions(root, env)
        print("Passed: Markdown, history limit, commits, Unicode paths, deleted files, binary files, refs, Atom feeds, empty repository, index order, missing input, exact renames/copies, README names/order, unmerged release tags and commit links, empty/mode-only/merge diffs, cached navigation, README links, streamed diff boundaries, batched image bytes and child cleanup.")


if __name__ == "__main__":
    main()
