#!/usr/bin/env python3
"""Pinned local dependencies for Linux x86_64/WSL. No system files changed.

Prerequisites: Python >=3.12 + venv/dev, clang 21, libffi-dev, git, npm.
Use --matched to build the comparison binding against our exact transport.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "dependencies.json").read_text())

def run(*args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, **kwargs)

def checkout(name, spec):
    path = ROOT / ".deps" / name
    if not path.exists():
        run("git", "init", path)
        run("git", "-C", path, "remote", "add", "origin", spec["repository"])
        run("git", "-C", path, "fetch", "--depth", "1", "origin", spec["commit"])
        run("git", "-C", path, "checkout", "--detach", "FETCH_HEAD")
    actual = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    if actual != spec["commit"]:
        raise RuntimeError(f"Refusing to change existing {path}; expected {spec['commit']}, got {actual}")
    return path

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--matched", action="store_true")
    args = p.parse_args()
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise SystemExit("This lock currently supports Linux x86_64 (including WSL).")
    deps = ROOT / ".deps"; deps.mkdir(exist_ok=True)
    archive = deps / "curl.tar.gz"
    curl = LOCK["curl_impersonate"]
    if not archive.exists():
        urllib.request.urlretrieve(curl["url"], archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != curl["sha256"]:
        raise RuntimeError("curl archive SHA-256 mismatch; refusing extraction")
    if not (deps / "curl/libcurl-impersonate.so").exists():
        with tarfile.open(archive) as tar:
            tar.extractall(deps / "curl", filter="data")
    checkout("bend", LOCK["bend"])
    run("npm", "install", "--prefix", deps / "bun", "--ignore-scripts", "--no-audit", "--no-fund",
        "@oven/bun-linux-x64-baseline@" + LOCK["bun"])
    run(sys.executable, "-m", "venv", deps / "venv")
    run(deps / "venv/bin/pip", "install", "-r", ROOT / "requirements-test.txt")
    if args.matched:
        source = checkout("curl_cffi", LOCK["curl_cffi_matched"])
        run(sys.executable, "-m", "venv", deps / "venv-matched")
        env = {**os.environ, "IMPERSONATE_BUILD_DIR": str(deps / "curl"), "IMPERSONATE_LINK_TYPE": "dynamic"}
        run(deps / "venv-matched/bin/pip", "install", source, "pytest==9.1.1", "h2==4.4.1", "cryptography==50.0.1", env=env)
    print("Dependencies ready. Run: python3 scripts/build.py")

if __name__ == "__main__": main()
