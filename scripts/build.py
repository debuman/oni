#!/usr/bin/env python3
"""Generate a local stagit site from debuman's public repositories."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import github_data
from readme_assets import link_readme_assets
from sync_state import manifest, render_pulls

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache"
OWNER = github_data.OWNER
VERSION = "sqzer-stagit 0.1.0"
SOURCE = ROOT / "generator"
BINARY = CACHE / "cargo" / "release" / "sqzer-stagit"
ASSETS = ("style.css", "favicon.ico", "favicon.png")


def run(*args, **kwargs):
    timeout = kwargs.pop("timeout", 300)
    command = [str(arg) for arg in args]
    process = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        # Stop the entire local process group, including generator Git children.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def configure_git():
    local = CACHE / "secure-git/installed/bin"
    if (local / "git").is_file():
        os.environ["PATH"] = str(local) + os.pathsep + os.environ["PATH"]
    version = subprocess.check_output(["git", "--version"], text=True, timeout=10).strip()
    match = re.search(r"git version (\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (2, 56, 1):
        raise RuntimeError("Git 2.56.1 or newer is required. Run python3 scripts/bootstrap_git.py inside WSL")
    print(f"Using {version}", flush=True)


def build_tool():
    run("cargo", "build", "--release", "--offline", "--locked",
        "--target-dir", CACHE / "cargo", cwd=SOURCE)


def repositories(offline):
    metadata = CACHE / "repositories.json"
    if offline:
        repos = json.loads(metadata.read_text(encoding="utf-8"))
    else:
        # An authoritative empty list must withdraw all previously public repositories.
        repos = github_data.repositories()
        metadata.write_text(json.dumps(repos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for repo in repos:
        github_data.validate_repository(repo)
        run("git", "check-ref-format", f"refs/heads/{repo['default_branch']}", stdout=subprocess.DEVNULL)
    return sorted(repos, key=lambda repo: repo["name"].lower())


def sync_repo(repo, offline):
    target = CACHE / "repos" / (repo["name"] + ".git")
    if not target.exists():
        if offline:
            raise RuntimeError(f"Missing local repository: {target}")
        run("git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null",
            "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "http.sslVerify=true",
            "-c", "transfer.fsckObjects=true", "clone", "--bare", "--quiet",
            "--", repo["clone_url"], target)
    elif not offline:
        run("git", "-C", target, "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null",
            "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "http.sslVerify=true",
            "-c", "fetch.fsckObjects=true", "-c", "fetch.recurseSubmodules=false", "fetch", "--quiet",
            "--prune", "--prune-tags", repo["clone_url"], "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
    # These local copies are used only as generator input.
    run("git", "-C", target, "config", "remote.origin.pushurl", "DISABLED")
    run("git", "-C", target, "symbolic-ref", "HEAD", f"refs/heads/{repo['default_branch']}")
    for name, value in {
        "description": repo.get("description") or "",
        "owner": OWNER,
        "url": repo["clone_url"],
    }.items():
        (target / name).write_text(value.replace("\n", " ").strip() + "\n", encoding="utf-8")
    print(f"Ready: {repo['name']}", flush=True)
    return target


def site_assets(staging):
    for asset in ASSETS:
        shutil.copyfile(ROOT / "theme" / asset, staging / asset)
    notices = staging / "licenses"
    notices.mkdir()
    shutil.copyfile(ROOT / "LICENSE", notices / "sqzer-stagit.txt")
    for notice in (ROOT / "third-party").glob("*LICENSE*"):
        shutil.copyfile(notice, notices / notice.name)


def generate_repository(repo, path, output, base_url, *, pulls_page=None):
    output.mkdir()
    options = ["--pulls-page", pulls_page] if pulls_page is not None else []
    run("prlimit", "--as=2147483648", "--cpu=120", "--", BINARY, "-l", "50", "-u",
        f"{base_url.rstrip('/')}/{repo['name']}/", *options, path, cwd=output, timeout=180)
    if has_head(path):
        run("git", "-C", path, "archive", "--format=tar.gz",
            f"--prefix={repo['name']}/", f"--output={output / 'archive.tar.gz'}", "HEAD")
    else:
        # An empty public repository must not prevent discovery of other repositories.
        with tarfile.open(output / "archive.tar.gz", "w:gz") as archive:
            entry = tarfile.TarInfo(repo["name"] + "/")
            entry.type = tarfile.DIRTYPE
            entry.mode = 0o755
            archive.addfile(entry)
    link_readme_assets(output, path, repo)
    for asset in ASSETS:
        shutil.copyfile(ROOT / "theme" / asset, output / asset)


def has_head(path):
    return subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", "--quiet", "HEAD"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0


def generate_index(staging, paths):
    with (staging / "index.html").open("w", encoding="utf-8") as index:
        run("prlimit", "--as=2147483648", "--cpu=120", "--", BINARY, "--index", *paths, stdout=index, timeout=180)


def generate(repos, paths, base_url, *, pulls=None, sync_manifest=None):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    staging = CACHE / ("build-" + stamp)
    staging.mkdir()
    site_assets(staging)
    with tempfile.TemporaryDirectory(prefix="pulls-", dir=CACHE) as fragments:
        for number, (repo, path) in enumerate(zip(repos, paths), 1):
            output = staging / repo["name"]
            print(f"[{number}/{len(repos)}] Generating {repo['name']}", flush=True)
            page = None
            if pulls is not None:
                page = Path(fragments) / (repo["name"] + ".html")
                page.write_text(render_pulls(repo, pulls[repo["name"]]), encoding="utf-8")
            generate_repository(repo, path, output, base_url, pulls_page=page)
    generate_index(staging, paths)
    if sync_manifest is not None:
        (staging / "sync.json").write_text(json.dumps(sync_manifest, indent=2) + "\n", encoding="utf-8")
    public = ROOT / "public"
    if public.exists():
        # Keep the previous preview as a local backup; never delete source files.
        public.rename(CACHE / ("previous-" + stamp))
    staging.rename(public)
    report = {
        "generated_at": stamp,
        "stagit_version": VERSION,
        "base_url": base_url,
        "intended_domain": "oni.sqzer.com",
        "repositories": [repo["name"] for repo in repos],
    }
    (CACHE / "last-build.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {len(repos)} repositories in {public}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Rebuild from the local snapshot without fetching GitHub")
    parser.add_argument("--base-url", default="http://127.0.0.1:4174", help="Absolute URL for Atom feed links")
    args = parser.parse_args()
    if os.name == "nt":
        parser.error("Run this script on Linux or inside WSL")
    CACHE.mkdir(exist_ok=True)
    (CACHE / "repos").mkdir(exist_ok=True)
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    configure_git()
    build_tool()
    repos = repositories(args.offline)
    print(f"Public repositories: {len(repos)}", flush=True)
    pulls_file = CACHE / "pull-requests.json"
    if args.offline:
        pulls = json.loads(pulls_file.read_text(encoding="utf-8")) if pulls_file.exists() else None
    else:
        pulls = {repo["name"]: github_data.pull_requests(repo) for repo in repos}
        pulls_file.write_text(json.dumps(pulls, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Capture refs before fetching: a push during the fetch will be seen again by
    # the next poll, never acknowledged by a marker ahead of the rendered source.
    refs = {repo["name"]: github_data.references(repo) for repo in repos} if not args.offline else None
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda repo: sync_repo(repo, args.offline), repos))
    generate(repos, paths, args.base_url, pulls=pulls,
             sync_manifest=manifest(repos, pulls, refs=refs) if not args.offline else None)


if __name__ == "__main__":
    main()
