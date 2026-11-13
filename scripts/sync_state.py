"""Compare GitHub with the last successfully published Pages snapshot."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from html import escape
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import github_data

SCHEMA = 1
SITE = "https://oni.sqzer.com"


def fingerprint(repos, pulls, refs=None):
    # Counts such as stars and watchers do not change any mirrored page.
    state = [{key: repo.get(key) for key in
              ("id", "name", "description", "default_branch", "pushed_at", "updated_at")}
             | {"pulls": pulls[repo["name"]], "refs": (refs or {}).get(repo["name"], [])} for repo in repos]
    state.sort(key=lambda repo: repo["name"].lower())
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def manifest(repos, pulls, now=None, *, refs=None):
    return {"schema": SCHEMA, "owner": github_data.OWNER,
            "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
            "fingerprint": fingerprint(repos, pulls, refs)}


def needs_build(previous, current, now=None):
    if not isinstance(previous, dict):
        return True
    if any(previous.get(key) != current[key] for key in ("schema", "owner", "fingerprint")):
        return True
    try:
        generated = datetime.fromisoformat(previous["generated_at"])
        age = (now or datetime.now(timezone.utc)) - generated
    except (KeyError, TypeError, ValueError):
        return True
    return age < timedelta(0)


def published_manifest():
    # Do not send GITHUB_TOKEN to the Pages origin. Bypass its cached old manifest.
    nonce = datetime.now(timezone.utc).timestamp()
    req = Request(f"{SITE}/sync.json?check={nonce}",
                  headers={"User-Agent": "oni-sync", "Cache-Control": "no-cache"})
    try:
        with urlopen(req, timeout=30) as response:
            data = response.read(64 * 1024 + 1)
    except HTTPError as error:
        error.close()
        if error.code == 404:
            return None
        raise
    if len(data) > 64 * 1024:
        raise ValueError("Published sync manifest exceeds the size limit")
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        return None


def render_pulls(repo, pulls):
    def text(value):
        return escape(str(value), quote=True)

    rows = []
    for pull in sorted(pulls, key=lambda item: (item["updated_at"], item["number"]), reverse=True):
        number = pull["number"]
        if type(number) is not int or number <= 0:
            raise ValueError("Invalid pull request number")
        state = "merged" if pull["merged_at"] else (
            "draft" if pull["state"] == "open" and pull["draft"] else pull["state"])
        url = f"https://github.com/{github_data.OWNER}/{repo['name']}/pull/{number}"
        rows.append(f'<tr><td><a href="{text(url)}">#{number}</a></td>'
                    f'<td>{text(pull["title"])}</td><td>{text(state)}</td>'
                    f'<td>{text(pull["author"])}</td><td>{text(pull["updated_at"])}</td></tr>')
    if not rows:
        return "<h2>Pull requests</h2>\n<p>No pull requests.</p>\n"
    return ('<h2>Pull requests</h2>\n<p>Open, closed, and merged pull requests. '
            'Follow the number for the discussion and diff on GitHub.</p>\n'
            '<div class="pull-requests"><table class="listing" id="pulls"><thead><tr>'
            '<th>number</th><th>title</th><th>state</th><th>author</th><th>updated (UTC)</th>'
            '</tr></thead><tbody>\n' + "\n".join(rows) + '</tbody></table></div>\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-output", type=Path, default=os.environ.get("GITHUB_OUTPUT"))
    args = parser.parse_args()
    previous = published_manifest()
    repos = github_data.repositories()
    # Read sequentially to avoid bursts against the secondary API rate limit.
    pulls = {repo["name"]: github_data.pull_requests(repo) for repo in repos}
    refs = {repo["name"]: github_data.references(repo) for repo in repos}
    changed = needs_build(previous, manifest(repos, pulls, refs=refs))
    print("GitHub changed; rebuild required" if changed else "Published snapshot is current")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"changed={str(changed).lower()}\n")


if __name__ == "__main__":
    main()
