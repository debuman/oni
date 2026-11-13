#!/usr/bin/env python3
"""Check generated pages, local links, feeds, archives, and active-content boundaries."""

from html.parser import HTMLParser
import json
from pathlib import Path
import tarfile
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

from readme_assets import image_type

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"


class Audit(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.page = page
        self.links = 0
        self.csp = False

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        assert tag not in {"script", "iframe", "object", "embed", "base", "form", "svg", "math"}, (self.page, tag)
        assert not any(name.startswith("on") or name in {"srcdoc", "style"} for name in attrs), self.page
        if tag == "meta" and attrs.get("http-equiv", "").lower() == "content-security-policy":
            assert "script-src 'none'" in attrs.get("content", ""), self.page
            self.csp = True
        for key, value in attributes:
            if key not in ("href", "src") or not value:
                continue
            url = urlsplit(value)
            assert url.scheme.lower() in ("", "http", "https", "mailto"), (self.page, value)
            if url.scheme or url.netloc or not url.path:
                continue
            self.links += 1
            target = PUBLIC / unquote(url.path).lstrip("/") if url.path.startswith("/") else self.page.parent / unquote(url.path)
            target.resolve().relative_to(PUBLIC.resolve())
            assert target.exists(), (self.page, value)


def main():
    report = json.loads((ROOT / ".cache/last-build.json").read_text())
    pages = list(PUBLIC.rglob("*.html"))
    links = 0
    for page in pages:
        parser = Audit(page)
        parser.feed(page.read_text(encoding="utf-8"))
        assert parser.csp, page
        links += parser.links
    for name in report["repositories"]:
        for feed in ("atom.xml", "tags.xml"):
            ET.parse(PUBLIC / name / feed)
        with tarfile.open(PUBLIC / name / "archive.tar.gz") as archive:
            assert archive.getmembers(), name
        for asset in (PUBLIC / name / "raw").rglob("*"):
            if asset.is_file():
                assert image_type(asset.name, asset.read_bytes()), asset
    result = {"repositories": len(report["repositories"]), "html_pages": len(pages),
              "local_links": links, "missing_links": 0, "unsafe_elements": 0,
              "archives": len(report["repositories"])}
    destination = ROOT / ".cache/security-audit/site.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
