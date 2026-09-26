#!/usr/bin/env python3
"""Fetch the pinned documents, test ROMs and source trees into reference/ and tests/test_roms/.

Nothing fetched here is committed (see .gitignore). Two pin files drive it:

- ``scripts/pinned_files.json``: one entry per file, with its URL, SHA-256 and
  kind. Kinds ``document``, ``manual``, ``hardware-capture`` and ``test-rom`` must
  match their hash exactly; a mismatch is an error and the file is not kept.
  Kinds ``live-wiki`` and ``live-forum`` are snapshots of pages that change
  (wiki sources, forum threads); a mismatch there is reported as a
  warning and the new bytes are kept, because the snapshot date is what the
  docs cite.
- ``scripts/pinned_repos.json``: one entry per git repository, cloned shallow at
  the pinned revision (sparse where the entry lists directories in ``sparse``, or
  gitignore-style patterns in ``sparse_patterns``, such as ``!*.jpg`` to leave a
  repository's die photographs out).

Entries marked ``large`` (over 50 MB: audio recordings, the FM document bundle)
are fetched only with ``--large``.

Forum threads are also converted to plain text next to their HTML.

    python scripts/fetch_references.py            # everything missing
    python scripts/fetch_references.py --verify   # hash what is present, fetch nothing
    python scripts/fetch_references.py --no-repos # files only
    python scripts/fetch_references.py --large    # also the large recordings and bundles
    python scripts/fetch_references.py --no-repos --only tests/test_roms/nemesis/ \
        --only reference/images/                  # what CI needs, and nothing else
    python scripts/fetch_references.py --no-repos --repo Nuked-OPN2
                                                  # one source tree, no files
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "scripts" / "pinned_files.json"
REPOS = ROOT / "scripts" / "pinned_repos.json"
LIVE = {"live-wiki", "live-forum"}
USER_AGENT = "Mozilla/5.0 (megadrive-python reference fetch)"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


#: Seconds to wait before each retry of a mirror. The Wayback Machine refuses
#: connections for a while after a burst -- three CI jobs fetching at once were enough
#: (2026-09-26: two files failed in each of two jobs with retries of 5 and 10 s).
MIRROR_BACKOFF = (10, 20, 40, 60, 60)
#: Seconds between two mirror downloads, so that one fetch is not a burst of its own.
MIRROR_PACE = 2


def download(url: str, backoff: tuple[int, ...] = ()) -> bytes:
    for wait in (*backoff, None):
        try:
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=120) as response:
                return response.read()
        except OSError:
            if wait is None:
                raise
            time.sleep(wait)
    raise AssertionError("unreachable")


def fetch(entry: dict) -> bytes:
    """An entry's bytes: from its URL, else from its ``mirror`` -- the Wayback
    Machine's raw capture of the same URL (``/web/<timestamp>id_/<url>``), chosen
    because its SHA-1 in the archive's index equals the pinned file's and checked
    against the SHA-256 here like any other download. Nemesis's site went down on
    2026-09-26 and took milestone 2 and 3's judges with it for every fresh clone
    and CI run; a mirror of the identical bytes is not a change of source."""
    try:
        return download(entry["url"])
    except OSError as error:
        mirror = entry.get("mirror")
        if not mirror:
            raise
        print(f"note: {entry['url']}: {error}; trying its mirror")
        time.sleep(MIRROR_PACE)
        return download(mirror, backoff=MIRROR_BACKOFF)


def forum_text(raw: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", "", raw, flags=re.DOTALL)
    text = html.unescape(re.sub(r"<br\s*/?>", "\n", text))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"(\s*\n\s*){2,}", "\n\n", text)


def fetch_files(verify_only: bool, large: bool, only: list[str] | None = None) -> int:
    problems = 0
    for entry in json.loads(FILES.read_text(encoding="utf-8")):
        if only and not any(entry["path"].startswith(prefix) for prefix in only):
            continue
        path = ROOT / entry["path"]
        if entry.get("large") and not large and not path.exists():
            continue
        live = entry["kind"] in LIVE
        if path.exists():
            if sha256(path) != entry["sha256"]:
                level = "warning" if live else "ERROR"
                print(f"{level}: {entry['path']} does not match its pin")
                problems += not live
            continue
        if verify_only:
            print(f"missing: {entry['path']}")
            problems += 1
            continue
        try:
            data = fetch(entry)
        except OSError as error:
            print(f"ERROR: {entry.get('mirror') or entry['url']}: {error}")
            problems += 1
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest != entry["sha256"] and not live:
            print(f"ERROR: {entry['url']} has SHA-256 {digest}, pinned {entry['sha256']}; not kept")
            problems += 1
            continue
        if digest != entry["sha256"]:
            print(f"warning: {entry['path']} changed since its {entry.get('date', '')} snapshot")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(data)
        Path(handle.name).replace(path)
        if entry["kind"] == "live-forum":
            path.with_suffix(".txt").write_text(
                forum_text(data.decode("utf-8", "replace")), encoding="utf-8"
            )
        print(f"fetched {entry['path']}")
    return problems


def fetch_repos(verify_only: bool, only: list[str] | None = None) -> int:
    problems = 0
    for entry in json.loads(REPOS.read_text(encoding="utf-8")):
        if only and not any(entry["path"].endswith("/" + name) for name in only):
            continue
        path = ROOT / entry["path"]
        if path.exists():
            head = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            if head != entry["rev"]:
                print(f"ERROR: {entry['path']} is at {head}, pinned {entry['rev']}")
                problems += 1
            continue
        if verify_only:
            print(f"missing: {entry['path']}")
            problems += 1
            continue
        path.mkdir(parents=True)
        git = ["git", "-C", str(path)]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "remote", "add", "origin", entry["url"]], check=True)
        if entry.get("sparse"):
            subprocess.run([*git, "sparse-checkout", "set", *entry["sparse"]], check=True)
        elif entry.get("sparse_patterns"):
            subprocess.run(
                [*git, "sparse-checkout", "set", "--no-cone", *entry["sparse_patterns"]],
                check=True,
            )
        subprocess.run(
            [*git, "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", entry["rev"]],
            check=True,
        )
        subprocess.run([*git, "checkout", "-q", "FETCH_HEAD"], check=True)
        print(f"cloned {entry['path']} at {entry['rev'][:12]}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--verify", action="store_true", help="check what is present; fetch nothing"
    )
    parser.add_argument("--no-repos", action="store_true", help="skip the git repositories")
    parser.add_argument("--no-files", action="store_true",
                        help="skip the pinned files (with --repo: just those repos)")  # fmt: skip
    parser.add_argument("--large", action="store_true", help="also the entries over 50 MB")
    parser.add_argument("--only", action="append",
                        help="only the files whose path starts with this (repeatable)")  # fmt: skip
    parser.add_argument("--repo", action="append",
                        help="only these repositories, by name (repeatable)")  # fmt: skip
    args = parser.parse_args()
    problems = 0 if args.no_files else fetch_files(args.verify, args.large, args.only)
    if not args.no_repos or args.repo:
        problems += fetch_repos(args.verify, args.repo)
    print("all pins satisfied" if not problems else f"{problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
