#!/usr/bin/env python3
"""Package only generated regular files, preserving dot-prefixed repository names."""

import argparse
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024


def package(public, output):
    public = public.resolve()
    output = output.resolve()
    if output.is_relative_to(public):
        raise ValueError("Place the deployment archive outside the generated site")
    if not (public / "index.html").is_file():
        raise ValueError("The generated site has no index.html")
    paths = sorted(public.rglob("*"))
    total = 0
    for path in paths:
        if path.is_symlink() or path.is_junction() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"Deployment contains a link or special file: {path}")
        if ".git" in path.relative_to(public).parts:
            raise ValueError("Git metadata must never be deployed")
        if path.is_file():
            total += path.stat().st_size
        total += 2048
    if total >= MAX_ARCHIVE_BYTES:
        raise ValueError("Generated site exceeds the 1 GiB deployment limit")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w", format=tarfile.PAX_FORMAT, dereference=True) as archive:
        for path in paths:
            archive.add(path, arcname=path.relative_to(public).as_posix(), recursive=False)
    if output.stat().st_size >= MAX_ARCHIVE_BYTES:
        raise ValueError("Deployment archive exceeds the 1 GiB limit")
    print(f"Packaged {len(paths)} entries into {output} ({output.stat().st_size:,} bytes)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package(ROOT / "public", args.output)


if __name__ == "__main__":
    main()
