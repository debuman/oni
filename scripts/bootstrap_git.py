#!/usr/bin/env python3
"""Build a pinned, patched Git locally without modifying the system installation."""

import hashlib
import os
from pathlib import Path
import subprocess
import tarfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
VERSION = "2.56.1"
SHA256 = "f0b0bf89264b9bd2e1c640363e7529643989da76281600473c50745cd34a607f"


def main():
    if os.name == "nt":
        raise SystemExit("Run this script inside WSL Ubuntu")
    cache = ROOT / ".cache/secure-git"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"git-{VERSION}.tar.xz"
    if not archive.exists():
        with urlopen(f"https://www.kernel.org/pub/software/scm/git/{archive.name}", timeout=90) as response:
            data = response.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024:
            raise RuntimeError("Git source archive exceeds the download limit")
        archive.write_bytes(data)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError("Git source checksum does not match the pinned upstream release")
    source = cache / f"git-{VERSION}"
    if not source.exists():
        with tarfile.open(archive) as bundle:
            bundle.extractall(cache, filter="data")
    prefix = cache / "installed"
    options = [f"prefix={prefix}", "NO_GETTEXT=YesPlease", "NO_TCLTK=YesPlease",
               "NO_PERL=YesPlease", "NO_PYTHON=YesPlease", "NO_EXPAT=YesPlease"]
    if not Path("/usr/include/x86_64-linux-gnu/curl/curl.h").exists():
        # Obtain Ubuntu's authenticated development package without root access.
        packages = cache / "curl-dev"
        packages.mkdir(exist_ok=True)
        subprocess.run(["apt-get", "download", "libcurl4-openssl-dev"], cwd=packages, check=True, timeout=120)
        candidates = list(packages.glob("libcurl4-openssl-dev_*.deb"))
        if len(candidates) != 1:
            raise RuntimeError("Expected exactly one curl development package")
        subprocess.run(["dpkg-deb", "-x", candidates[0], packages], check=True, timeout=60)
        options.extend([f"CURL_CFLAGS=-I{packages}/usr/include/x86_64-linux-gnu",
                        "CURL_LIBCURL=/usr/lib/x86_64-linux-gnu/libcurl.so.4"])
    subprocess.run(["make", "-j", str(min(os.cpu_count() or 2, 8)), *options, "all"], cwd=source, check=True, timeout=900)
    subprocess.run(["make", *options, "install"], cwd=source, check=True, timeout=120)
    subprocess.run([prefix / "bin/git", "--version"], check=True)


if __name__ == "__main__":
    main()
