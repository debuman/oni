"""Read public GitHub metadata shared by builds and scheduled change detection."""

import json
import os
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen

OWNER = "debuman"
API = "https://api.github.com"
MAX_RESPONSE = 8 * 1024 * 1024


def request(url):
    if not url.startswith(API + "/"):
        raise ValueError("Only the GitHub API may receive the API token")
    headers = {"User-Agent": "oni.sqzer.com", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2026-03-10"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + token
    with urlopen(Request(url, headers=headers), timeout=90) as response:
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise RuntimeError("GitHub API response exceeds the 8 MiB limit")
    return data


def pages(endpoint):
    separator = "&" if "?" in endpoint else "?"
    for page in range(1, 1001):
        batch = json.loads(request(f"{API}{endpoint}{separator}per_page=100&page={page}"))
        if not isinstance(batch, list):
            raise ValueError("Expected a GitHub API list")
        yield from batch
        if len(batch) < 100:
            return
    raise RuntimeError("GitHub pagination exceeded 100,000 entries")


def validate_repository(repo):
    name = repo["name"]
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
            or name.lower() in {".", "..", ".git", "licenses", "style.css", "index.html", "sync.json"}):
        raise ValueError(f"Unsafe or reserved repository name: {name!r}")
    if repo["private"] or repo["owner"]["login"].lower() != OWNER.lower():
        raise ValueError(f"Only public repositories owned by {OWNER} are allowed")
    if repo["clone_url"] != f"https://github.com/{OWNER}/{name}.git":
        raise ValueError(f"Unexpected repository URL for {name}")
    if repo.get("full_name") != f"{OWNER}/{name}":
        raise ValueError(f"Unexpected repository identity for {name}")


def repositories():
    repos = [repo for repo in pages(f"/users/{OWNER}/repos?type=owner&sort=full_name&direction=asc")
             if not repo["private"] and repo["owner"]["login"].lower() == OWNER.lower()]
    for repo in repos:
        validate_repository(repo)
    return sorted(repos, key=lambda repo: repo["name"].lower())


def pull_requests(repo):
    validate_repository(repo)
    pulls = []
    for pull in pages(f"/repos/{OWNER}/{repo['name']}/pulls?state=all&sort=updated&direction=desc"):
        if type(pull["number"]) is not int or pull["number"] <= 0:
            raise ValueError("Invalid pull request number")
        pulls.append({
            "number": pull["number"], "title": pull["title"], "state": pull["state"],
            "draft": bool(pull.get("draft")), "merged_at": pull.get("merged_at"),
            "updated_at": pull["updated_at"], "author": (pull.get("user") or {}).get("login", "deleted"),
            "head": pull["head"]["ref"], "head_sha": pull["head"]["sha"], "base": pull["base"]["ref"],
        })
    return sorted(pulls, key=lambda pull: pull["number"])


def references(repo):
    validate_repository(repo)
    try:
        # With no suffix this endpoint returns all refs; it is not paginated.
        refs = json.loads(request(f"{API}/repos/{OWNER}/{repo['name']}/git/matching-refs/"))
    except HTTPError as error:
        try:
            empty = error.code == 409 and json.loads(error.read(MAX_RESPONSE)).get("message") == "Git Repository is empty."
        finally:
            error.close()
        if empty:
            return []
        raise
    if not isinstance(refs, list):
        raise ValueError("Expected a GitHub reference list")
    return sorted([[ref["ref"], ref["object"]["sha"]] for ref in refs
                   if ref["ref"].startswith(("refs/heads/", "refs/tags/"))])
