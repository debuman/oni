"""Resolve repository-local links and images without changing README markup."""

from html import escape
from html.parser import HTMLParser
import posixpath
import subprocess
from urllib.parse import quote, unquote, urlsplit, urlunsplit

MAX_IMAGE_BYTES = 32 * 1024 * 1024


def image_type(name, data):
    suffix = name.rsplit(".", 1)[-1].lower()
    valid = {
        "png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpg": data.startswith(b"\xff\xd8\xff"),
        "jpeg": data.startswith(b"\xff\xd8\xff"),
        "gif": data.startswith((b"GIF87a", b"GIF89a")),
        "webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        "ico": data.startswith(b"\x00\x00\x01\x00"),
        "avif": data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"),
    }
    return valid.get(suffix, False)


def contained_path(root, name):
    if (name.startswith("/") or '\\' in name or ':' in name
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise ValueError("Unsafe repository asset path")
    root = root.resolve()
    current = root
    for part in name.split("/"):
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise ValueError("Asset paths must not contain links")
    current.resolve().relative_to(root)
    return current


class ReadmeLinks(HTMLParser):
    def __init__(self, output, git_dir, repo):
        super().__init__(convert_charrefs=False)
        self.output = output
        self.git_dir = git_dir
        self.repo = repo
        self.parts = []
        self.objects = None
        listing = subprocess.check_output(["git", "-C", str(git_dir), "ls-tree", "-rz", "HEAD"])
        self.blobs = {}
        self.directories = set()
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            if kind == "blob" and mode in ("100644", "100755"):
                name = name.decode("utf-8")
                self.blobs[name] = oid
                # Index ancestors once, so every README directory link is a
                # set lookup instead of a scan of all repository files.
                parent = posixpath.dirname(name)
                while parent and parent not in self.directories:
                    self.directories.add(parent)
                    parent = posixpath.dirname(parent)

    def blob(self, oid):
        if self.objects is None:
            self.objects = subprocess.Popen(
                ["git", "-C", str(self.git_dir), "cat-file", "--batch"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            )
        self.objects.stdin.write(oid.encode("ascii") + b"\n")
        self.objects.stdin.flush()
        header = self.objects.stdout.readline().split()
        if len(header) != 3 or header[0] != oid.encode("ascii") or header[1] != b"blob":
            raise RuntimeError("Invalid Git blob response")
        size = int(header[2])
        if size < 0 or size > MAX_IMAGE_BYTES:
            raise RuntimeError("Git image blob exceeds the 32 MiB limit")
        data = self.objects.stdout.read(size)
        if len(data) != size or self.objects.stdout.read(1) != b"\n":
            raise RuntimeError("Truncated Git blob response")
        return data

    def close(self):
        try:
            super().close()
        finally:
            if self.objects is not None:
                objects, self.objects = self.objects, None
                try:
                    objects.stdin.close()
                except BrokenPipeError:
                    pass
                finally:
                    objects.stdout.close()
                if objects.wait() != 0:
                    raise RuntimeError("Git blob process failed")

    def resolve_link(self, value, is_image):
        try:
            url = urlsplit(value)
        except ValueError:
            return ""
        if url.scheme or url.netloc or not url.path or url.path.startswith("/"):
            return value
        name = posixpath.normpath(unquote(url.path))
        if name.startswith("../") or name == "..":
            return value
        try:
            existing = contained_path(self.output, name)
        except ValueError:
            return value
        if existing.is_file():
            return value
        if is_image and name in self.blobs:
            # SVG is an active document when opened directly. Keep it off this
            # origin; GitHub's raw host is separate from the generated site.
            if name.lower().endswith(".svg"):
                oid = subprocess.check_output(["git", "-C", str(self.git_dir), "rev-parse", "HEAD"], timeout=30).decode().strip()
                target = f"https://raw.githubusercontent.com/{self.repo['full_name']}/{oid}/{quote(name, safe='/')}"
                result = urlsplit(target)
                return urlunsplit((result.scheme, result.netloc, result.path, url.query, url.fragment))
            if name.rsplit(".", 1)[-1].lower() not in ("png", "jpg", "jpeg", "gif", "webp", "ico", "avif"):
                return ""
            asset = contained_path(self.output, "raw/" + name)
            asset.parent.mkdir(parents=True, exist_ok=True)
            if not asset.exists():
                data = self.blob(self.blobs[name])
                if not image_type(name, data):
                    return ""
                with asset.open("xb") as output:
                    output.write(data)
            target = "raw/" + quote(name, safe="/")
        elif name in self.blobs and (self.output / "file" / (name + ".html")).is_file():
            target = "file/" + quote(name, safe="/") + ".html"
        elif name.rstrip("/") in self.directories:
            target = (f"https://github.com/{self.repo['full_name']}/tree/"
                      f"{quote(self.repo['default_branch'], safe='')}/{quote(name, safe='/')}")
        else:
            return value
        result = urlsplit(target)
        return urlunsplit((result.scheme, result.netloc, result.path, url.query, url.fragment))

    def start_tag(self, tag, attrs, closed=False):
        updated = []
        changed = False
        for key, value in attrs:
            replacement = self.resolve_link(value, key == "src") if value and key in ("src", "href") else value
            changed |= replacement != value
            updated.append((key, replacement))
        if not changed:
            self.parts.append(self.get_starttag_text())
            return
        attributes = "".join(" " + key + (f'="{escape(value, quote=True)}"' if value is not None else "")
                             for key, value in updated)
        self.parts.append("<" + tag + attributes + (" />" if closed else ">"))

    def handle_starttag(self, tag, attrs):
        self.start_tag(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self.start_tag(tag, attrs, closed=True)

    def handle_endtag(self, tag):
        self.parts.append("</" + tag + ">")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append("&" + name + ";")

    def handle_charref(self, name):
        self.parts.append("&#" + name + ";")

    def handle_comment(self, data):
        self.parts.append("<!--" + data + "-->")

    def handle_decl(self, decl):
        self.parts.append("<!" + decl + ">")


def link_readme_assets(output, git_dir, repo):
    page = output / "README.html"
    if not page.exists():
        return
    document = page.read_text(encoding="utf-8")
    marker = '<article class="readme">\n'
    if marker not in document:
        return
    prefix, body = document.split(marker, 1)
    body, suffix = body.rsplit('</article>', 1)
    parser = ReadmeLinks(output, git_dir, repo)
    try:
        parser.feed(body)
    finally:
        parser.close()
    page.write_text(prefix + marker + "".join(parser.parts) + '</article>' + suffix, encoding="utf-8")
