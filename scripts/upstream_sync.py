#!/usr/bin/env python3
"""Rebase the icloudpd customizations onto a newer upstream release.

The build repository never carries upstream source.  All it carries is

    custom/icloudpd-custom.patch     the diff applied to the upstream sources
    custom/guided_setup.py           new module - the interactive wizard
    custom/remember.py               new module - the minimal remembered state

plus two workflows that pin the upstream commit in their `env` block and
hard-code, in their "Verify" step, the sha256 of every patched file and of
those two added modules, all *after* the patch has been applied.

The patch is no longer cli.py alone (it also carries the indexing wait and the
exit-code fix), and this script still only rebases cli.py. It therefore refuses
to run at all while the patch touches anything else - see PATCHED_EXTRA - rather
than rewriting the patch from the merged cli.py and dropping the rest.

When upstream cuts a release, three things have to move together:

    1. the four pins in both workflows
       (UPSTREAM_SHA, ICLOUDPD_VERSION, ICLOUDPD_COMMIT_SHA, ICLOUDPD_COMMIT_TS)
    2. the patch itself, rebased onto the new src/icloudpd/cli.py
    3. the sha256 values the Verify step asserts

This script moves all three, and nothing else.

The rebase is a genuine three-way merge (git merge-file):

    base   = upstream cli.py at the currently pinned commit
    ours   = base + custom/icloudpd-custom.patch      <- our customizations
    theirs = upstream cli.py at the target commit

so upstream edits that do not overlap our hunks are carried over silently,
and a genuine overlap is reported as a conflict rather than guessed at.  A
conflict changes nothing on disk and exits 2, so it can never silently
produce a wrong binary.

Exit codes
    0   synced, or already up to date
    2   the rebase conflicted - nothing was written
    3   the rebased patch failed to reproduce the expected bytes
    10  --check only: a newer upstream release exists
    1   anything else
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import difflib
import hashlib
import http.client
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

UPSTREAM_SLUG = "icloud-photos-downloader/icloud_photos_downloader"
GITHUB_API = "https://api.github.com"
CODELOAD = "https://codeload.github.com"

CLI_REL = "src/icloudpd/cli.py"
PATCH_REL = "custom/icloudpd-custom.patch"

# Where a conflicted merge is parked so a human can edit it in place.  It lives
# outside custom/ on purpose: it is scratch, not something to carry in the repo.
CONFLICT_REL = "cli.py.conflict"

# Files we add that upstream does not have, mapped to where they land in the tree.
CARRIED = {
    "custom/guided_setup.py": "src/icloudpd/guided_setup.py",
    "custom/remember.py": "src/icloudpd/remember.py",
}

WORKFLOWS = (
    ".github/workflows/build-win-exe.yml",
    ".github/workflows/build-mac-bin.yml",
    ".github/workflows/build-linux-bin.yml",
)

# Workflows that carry the upstream pins but assert no sha256 - the image build
# proves the customizations landed by smoke-testing the pushed image instead of
# by hashing files. They still have to move when the pin does, otherwise the
# image silently keeps building the old upstream.
PIN_ONLY_WORKFLOWS = (".github/workflows/docker-build.yml",)

# Upstream files the patch modifies on top of cli.py. The workflows assert a hash
# for each of them too, so a rebase has to carry them along.
PATCHED_EXTRA = (
    "src/icloudpd/base.py",
    "src/icloudpd/config.py",
    "src/pyicloud_ipd/base.py",
    "src/pyicloud_ipd/services/photos.py",
    "src/starters/icloudpd.py",
)

# Every file the Verify step asserts a hash for.
VERIFIED = (CLI_REL, *PATCHED_EXTRA, *CARRIED.values())

# The pin keys, and how each one is rendered back into the workflow's env block.
ENV_SPEC = {
    "UPSTREAM_SHA": lambda v: v,
    "ICLOUDPD_VERSION": lambda v: f'"{v}"',
    "ICLOUDPD_COMMIT_SHA": lambda v: f'"{v}"',
    "ICLOUDPD_COMMIT_TS": lambda v: f'"{v}"',
}

# Modules our code imports from the upstream tree, checked for existence after a rebase.
INTRA_PACKAGES = ("icloudpd", "pyicloud_ipd", "foundation")

VERIFY_LINE_RE = re.compile(
    r'^(\s*echo ")([0-9a-f]{64})(  (?:src|foundation)/[A-Za-z0-9_/]+\.py" \|.*)$',
    re.M,
)


class SyncError(Exception):
    """Something the operator has to look at."""


class Conflict(SyncError):
    """The three-way merge did not resolve cleanly."""


def log(msg: str = "") -> None:
    print(msg, flush=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    return sha256_bytes(path.read_bytes())


# --------------------------------------------------------------------------- #
# upstream
# --------------------------------------------------------------------------- #


def _request(url: str, token: str | None, attempts: int = 4) -> bytes:
    """GET a URL with urllib, retrying the transient failures a flaky network throws.

    This is only the fallback path: prefer `http_get`, which shells out to curl.
    """
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "icloudpd-upstream-sync")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429) and attempt < attempts:
                last = exc
                time.sleep(2 * attempt)
                continue
            raise SyncError(f"GET {url} -> HTTP {exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            # IncompleteRead, connection resets and truncated bodies all land here.
            last = exc
            if attempt < attempts:
                time.sleep(2 * attempt)
    raise SyncError(f"GET {url} -> gave up after {attempts} attempts: {last!r}")


def http_get(url: str, token: str | None) -> bytes:
    """GET `url`.

    curl is preferred when it is installed: it retries a partial transfer on its own
    and survives the intercepting proxies that cut a urllib read short.  urllib is
    the fallback for machines without curl.
    """
    curl = shutil.which("curl")
    if not curl:
        return _request(url, token)

    args = [
        curl,
        "-fsSL",
        "--retry",
        "4",
        "--retry-delay",
        "2",
        "--retry-all-errors",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "User-Agent: icloudpd-upstream-sync",
    ]
    if token:
        args += ["-H", f"Authorization: Bearer {token}"]
    args.append(url)

    proc = subprocess.run(args, capture_output=True, check=False)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SyncError(f"GET {url} -> curl: {err[-1] if err else proc.returncode}")
    return proc.stdout


def _get_json(url: str, token: str | None) -> dict:
    return json.loads(http_get(url, token).decode("utf-8"))


def latest_release_tag(token: str | None) -> str:
    data = _get_json(f"{GITHUB_API}/repos/{UPSTREAM_SLUG}/releases/latest", token)
    tag = data.get("tag_name")
    if not tag:
        raise SyncError("upstream has no published release")
    return tag


def resolve_ref(ref: str, token: str | None) -> dict:
    """Resolve a tag, branch or sha into the concrete commit we will build from."""
    data = _get_json(f"{GITHUB_API}/repos/{UPSTREAM_SLUG}/commits/{ref}", token)
    if "sha" not in data:
        raise SyncError(f"cannot resolve upstream ref {ref!r}: {str(data)[:200]}")

    commit = data["commit"]
    stamp = commit["committer"]["date"].replace("Z", "+00:00")
    when = dt.datetime.fromisoformat(stamp)

    tag = ""
    if re.fullmatch(r"v?\d+\.\d+\.\d+", ref):
        tag = ref if ref.startswith("v") else f"v{ref}"

    return {
        "ref": ref,
        "sha": data["sha"],
        "sha_short": data["sha"][:7],
        "commit_ts": int(when.timestamp()),
        "date": when.astimezone(dt.timezone.utc).strftime("%Y-%m-%d"),
        "tag": tag,
    }


def _is_readable_tar(path: pathlib.Path) -> bool:
    """Cheap integrity check: a truncated download blows up while listing members."""
    try:
        with tarfile.open(path) as tar:
            return tar.next() is not None
    except (tarfile.TarError, OSError, EOFError):
        return False


def download(url: str, dest: pathlib.Path) -> None:
    """Fetch `url` to `dest`. The whole body is held in memory; the tarballs are ~25 MB."""
    dest.write_bytes(http_get(url, None))


def fetch_tree(sha: str, dest: pathlib.Path, cache: pathlib.Path) -> pathlib.Path:
    """Download and extract the upstream tarball for `sha`; cached by sha.

    The result is normalised so that `dest/<sha>/pyproject.toml` always exists,
    whatever the tarball's own top-level directory happens to be called.
    """
    tree = dest / sha
    if (tree / "pyproject.toml").is_file():
        return tree

    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"{sha}.tar.gz"
    if archive.is_file() and not _is_readable_tar(archive):
        log(f"  the cached tarball for {sha[:12]} is corrupt; fetching it again")
        archive.unlink()
    if not archive.is_file():
        log(f"  downloading upstream {sha[:12]} ...")
        download(f"{CODELOAD}/{UPSTREAM_SLUG}/tar.gz/{sha}", archive)
        if not _is_readable_tar(archive):
            archive.unlink()
            raise SyncError(f"the downloaded tarball for {sha} is truncated")

    dest.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(tempfile.mkdtemp(prefix="extract-", dir=dest))
    try:
        with tarfile.open(archive) as tar:
            try:
                tar.extractall(staging, filter="data")  # py3.12+
            except TypeError:
                tar.extractall(staging)  # noqa: S202

        roots = [p for p in staging.iterdir() if p.is_dir()]
        if len(roots) != 1 or not (roots[0] / "pyproject.toml").is_file():
            raise SyncError(f"unexpected tarball layout for {sha}")

        shutil.rmtree(tree, ignore_errors=True)
        shutil.move(str(roots[0]), str(tree))
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return tree


def upstream_version(tree: pathlib.Path) -> str:
    text = (tree / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        raise SyncError("cannot read version from upstream pyproject.toml")
    return match.group(1)


# --------------------------------------------------------------------------- #
# three-way merge
# --------------------------------------------------------------------------- #


def three_way_merge(ours: str, base: str, theirs: str) -> tuple[str, int]:
    """Merge `theirs` into `ours`, with `base` as the common ancestor.

    Returns the merged text and the number of conflict regions (0 = clean).
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        paths = {}
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            paths[name] = tmpdir / name
            paths[name].write_text(text, encoding="utf-8")

        proc = subprocess.run(
            [
                "git",
                "merge-file",
                "--stdout",
                "--diff3",
                "-L",
                "customized",
                "-L",
                "upstream (pinned)",
                "-L",
                "upstream (target)",
                str(paths["ours"]),
                str(paths["base"]),
                str(paths["theirs"]),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode < 0:
            raise SyncError(f"git merge-file failed: {proc.stderr.strip()}")
        return proc.stdout, proc.returncode


def conflict_report(merged: str) -> str:
    """Pull the conflicted regions out of a --diff3 merge result."""
    out: list[str] = []
    inside = False
    for line in merged.splitlines():
        if line.startswith("<<<<<<<"):
            inside = True
            out.append("  " + line)
        elif line.startswith(">>>>>>>"):
            out.append("  " + line)
            inside = False
            out.append("")
        elif inside:
            out.append("  " + line)
    return "\n".join(out)


def patched_rels(patch_text: str) -> list[str]:
    """Every upstream file the patch touches, in the order the hunks appear."""
    rels: list[str] = []
    for match in re.finditer(r"^--- a/(\S+)", patch_text, re.M):
        if match.group(1) not in rels:
            rels.append(match.group(1))
    return rels


def unified_diff(old: str, new: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )


# --------------------------------------------------------------------------- #
# static compatibility check
# --------------------------------------------------------------------------- #


def _defined_names(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def intra_package_imports(text: str) -> list[tuple[str, str]]:
    """Every `from icloudpd.x import y` style import, as (module, name) pairs."""
    found: list[tuple[str, str]] = []
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in INTRA_PACKAGES:
                for alias in node.names:
                    found.append((node.module, alias.name))
    return found


def modules_we_provide() -> set[str]:
    """Dotted module names the customizations themselves bring into the tree.

    `check_symbols` must not complain that upstream "lacks" a module we add.
    """
    modules: set[str] = set()
    for tree_rel in (CLI_REL, *CARRIED.values()):
        parts = list(pathlib.PurePosixPath(tree_rel).parts)
        if parts and parts[0] == "src":
            parts = parts[1:]
        if not parts:
            continue
        parts[-1] = parts[-1].removesuffix(".py")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules.add(".".join(parts))
    return modules


def check_symbols(sources: dict[str, str], tree: pathlib.Path) -> list[str]:
    """Verify every upstream symbol our code imports still exists in `tree`."""
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    ours = modules_we_provide()

    for label, text in sources.items():
        try:
            pairs = intra_package_imports(text)
        except SyntaxError as exc:
            problems.append(f"{label}: not valid Python ({exc})")
            continue
        for module, name in pairs:
            if name == "*" or module in ours or f"{module}.{name}" in ours:
                continue
            if (module, name) in seen:
                continue
            seen.add((module, name))
            rel = pathlib.Path("src") / pathlib.Path(*module.split(".")).with_suffix(".py")
            target = tree / rel
            if not target.is_file():
                pkg = tree / "src" / pathlib.Path(*module.split(".")) / "__init__.py"
                if pkg.is_file():
                    target = pkg
                else:
                    problems.append(f"{label}: imports `{module}` which no longer exists upstream")
                    continue
            try:
                if name not in _defined_names(target):
                    problems.append(
                        f"{label}: imports `{name}` from `{module}`, but {rel} no longer defines it"
                    )
            except SyntaxError as exc:
                problems.append(f"{target}: not valid Python ({exc})")
    return problems


# --------------------------------------------------------------------------- #
# workflow rewriting
# --------------------------------------------------------------------------- #


def workflow_text(root: pathlib.Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def read_pins(text: str, rel: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for key in ENV_SPEC:
        match = re.search(rf"^(\s*{key}:\s*)(\S+)\s*$", text, re.M)
        if not match:
            raise SyncError(f"{rel}: cannot find the {key} pin")
        pins[key] = match.group(2).strip('"')
    return pins


def read_expected_hashes(text: str, rel: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for match in VERIFY_LINE_RE.finditer(text):
        tail = match.group(3)
        path = re.search(r"(?:src|foundation)/[A-Za-z0-9_/]+\.py", tail)
        if path:
            hashes[path.group(0)] = match.group(2)
    missing = [p for p in VERIFIED if p not in hashes]
    if missing:
        raise SyncError(f"{rel}: the Verify step has no hash for {', '.join(missing)}")
    return hashes


def _set_env_line(text: str, key: str, value: str, rel: str) -> str:
    """Replace the value of one `KEY: value` line inside a workflow's env block."""
    pattern = rf"^(\s*{key}:\s*)\S+[ \t]*$"
    matches = list(re.finditer(pattern, text, re.M))
    if len(matches) != 1:
        raise SyncError(f"{rel}: expected exactly one {key} line, found {len(matches)}")
    match = matches[0]
    return text[: match.start()] + match.group(1) + value + text[match.end() :]


def rewrite_workflow(text: str, rel: str, pins: dict[str, str], hashes: dict[str, str]) -> str:
    for key, render in ENV_SPEC.items():
        text = _set_env_line(text, key, render(pins[key]), rel)

    def swap(match: re.Match[str]) -> str:
        found = re.search(r"(?:src|foundation)/[A-Za-z0-9_/]+\.py", match.group(3))
        digest = hashes.get(found.group(0)) if found else None
        return match.group(1) + (digest or match.group(2)) + match.group(3)

    text = VERIFY_LINE_RE.sub(swap, text)
    return text


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def resolve_root(explicit: str | None) -> pathlib.Path:
    candidates = []
    if explicit:
        candidates.append(pathlib.Path(explicit).resolve())
    else:
        here = pathlib.Path(__file__).resolve()
        candidates.extend([here.parent.parent, pathlib.Path.cwd()])
        candidates.extend(pathlib.Path.cwd().parents)
    for path in candidates:
        if (path / PATCH_REL).is_file() or (path / WORKFLOWS[0]).is_file():
            return path
    raise SyncError("cannot find the repository root (expected custom/ and .github/workflows/)")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebase the icloudpd customizations onto a newer upstream release.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", help="repository root (auto-detected by default)")
    parser.add_argument("--from", dest="source", help="upstream ref we are currently pinned to")
    parser.add_argument(
        "--to", dest="target", help="upstream ref to move to (default: latest release)"
    )
    parser.add_argument("--check", action="store_true", help="only report whether a sync is needed")
    parser.add_argument("--dry-run", action="store_true", help="do the merge, write nothing")
    parser.add_argument("--resolve", metavar="FILE", help="finish a sync using this hand-merged cli.py")
    parser.add_argument("--force", action="store_true", help="re-run even when already up to date")
    parser.add_argument("--cache", help="directory holding the downloaded upstream tarballs")
    parser.add_argument("--report", help="write a machine-readable JSON summary here")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    global log
    if args.quiet:
        log = lambda msg="": None  # noqa: E731

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    report: dict = {"synced": False, "updated": False, "exit_code": 0}
    conflict_text = ""

    try:
        root = resolve_root(args.root)
        report["root"] = str(root)

        # ---- where we are now -------------------------------------------------
        workflow_texts = {rel: workflow_text(root, rel) for rel in WORKFLOWS + PIN_ONLY_WORKFLOWS}
        pins = {rel: read_pins(text, rel) for rel, text in workflow_texts.items()}
        baseline = {
            rel: read_expected_hashes(text, rel)
            for rel, text in workflow_texts.items()
            if rel in WORKFLOWS
        }

        current_sha = pins[WORKFLOWS[0]]["UPSTREAM_SHA"]
        if args.source:
            current_sha = resolve_ref(args.source, token)["sha"]

        report["from"] = {
            "sha": current_sha,
            "version": pins[WORKFLOWS[0]]["ICLOUDPD_VERSION"],
        }

        # ---- where we want to be ---------------------------------------------
        target_ref = args.target or latest_release_tag(token)
        target = resolve_ref(target_ref, token)
        report["to"] = {
            "ref": target_ref,
            "sha": target["sha"],
            "tag": target["tag"],
            "date": target["date"],
        }

        log(f"pinned : {current_sha[:12]}  (icloudpd {pins[WORKFLOWS[0]]['ICLOUDPD_VERSION']})")
        log(f"target : {target['sha'][:12]}  {target['tag']}  ({target['date']})")

        if target["sha"] == current_sha and not args.force:
            log("\nalready up to date - nothing to do.")
            report["message"] = "already up to date"
            return 0

        # The rebase below is cli.py only. If the patch has grown past that, running
        # it would rewrite the patch from the merged cli.py alone and silently drop
        # every other customization - a wrong binary that still builds green. Refuse
        # instead. (While we are pinned to the newest release this is never reached,
        # so the weekly check stays quiet.)
        patch_file = root / PATCH_REL
        if patch_file.is_file():
            extra = [rel for rel in patched_rels(patch_file.read_text(encoding="utf-8")) if rel != CLI_REL]
            if extra:
                raise SyncError(
                    f"{PATCH_REL} now covers {', '.join([CLI_REL, *extra])}. This script only "
                    f"rebases {CLI_REL}; carrying the rest over needs a hand. Move the pins, "
                    f"re-generate the patch against the new upstream tree, and update the "
                    f"Verify hashes - or generalise this script to merge every patched file."
                )

        if args.check:
            log("\na newer upstream release is available.")
            report["updated"] = True
            report["message"] = "update available"
            return 10

        # ---- fetch both trees -------------------------------------------------
        scratch = (
            pathlib.Path(args.cache)
            if args.cache
            else pathlib.Path(tempfile.mkdtemp(prefix="icloudpd-sync-"))
        )
        cache = scratch / "tarballs"
        log("\nfetching upstream trees ...")
        old_tree = fetch_tree(current_sha, scratch, cache)
        new_tree = fetch_tree(target["sha"], scratch, cache)

        ours_old = (old_tree / CLI_REL).read_text(encoding="utf-8")
        theirs = (new_tree / CLI_REL).read_text(encoding="utf-8")

        patch_path = root / PATCH_REL
        if patch_path.is_file():
            # The patch is the canonical form: it is what CI applies.
            ours = apply_patch_to(ours_old, patch_path.read_text(encoding="utf-8"), CLI_REL)
        else:
            # running inside a full source checkout: the working file is the truth
            ours = (root / CLI_REL).read_text(encoding="utf-8")
        report["upstream_cli_changed"] = ours_old != theirs

        # If this is a full checkout too, the mirror must agree with the patch or the
        # two representations have drifted and a rebase would silently fork them.
        mirror_cli = root / CLI_REL
        if (
            mirror_cli.is_file()
            and patch_path.is_file()
            and mirror_cli.read_text(encoding="utf-8") != ours
        ):
            pinned_hash = baseline[WORKFLOWS[0]].get(CLI_REL, "")
            actual_hash = sha256_file(mirror_cli)
            if actual_hash != pinned_hash and sha256_bytes(ours.encode("utf-8")) != pinned_hash:
                raise SyncError(
                    f"{CLI_REL} and {PATCH_REL} disagree, and neither matches the hash the "
                    f"workflow verifies. Reconcile them before syncing."
                )
            log("  note: the working copy of cli.py differs from the patch; using the patch.")

        # ---- rebase ----------------------------------------------------------
        if args.resolve:
            resolution = pathlib.Path(args.resolve)
            if not resolution.is_file():
                raise SyncError(f"--resolve {args.resolve}: no such file")
            merged = resolution.read_text(encoding="utf-8")
            for marker in ("<<<<<<<", "|||||||", "=======", ">>>>>>>"):
                if marker in merged:
                    raise SyncError(
                        f"--resolve {args.resolve} still contains conflict markers ({marker})"
                    )
            conflicts = 0
            log(f"using the hand-merged {args.resolve} instead of merging.")
        else:
            log("rebasing the patch onto the new cli.py ...")
            merged, conflicts = three_way_merge(ours, ours_old, theirs)

        if conflicts:
            conflict_text = conflict_report(merged)
            report["message"] = f"{conflicts} conflicted region(s) in {CLI_REL}"
            report["conflicts"] = conflicts
            # Leave the half-merged file behind so the fix is an edit, not a re-derivation.
            conflict_path = root / CONFLICT_REL
            conflict_path.parent.mkdir(parents=True, exist_ok=True)
            conflict_path.write_text(merged, encoding="utf-8")
            report["conflict_file"] = str(conflict_path)

            log(f"\nCONFLICT: git could not merge {conflicts} region(s) by itself.")
            log(f"Nothing else was written.  The half-merged file is at {CONFLICT_REL}:\n")
            log(conflict_text)
            log("\nTo finish by hand:")
            log(f"  1. edit {CONFLICT_REL} and resolve the markers")
            log("  2. re-run this script with the same options plus")
            log(f"     --resolve {CONFLICT_REL}")
            return conflict_out(report, args)

        if not args.resolve:
            if ours_old != theirs:
                log("  upstream changed cli.py; the merge came out clean.")
            else:
                log("  upstream did not touch cli.py.")

        try:
            ast.parse(merged)
        except SyntaxError as exc:
            report["message"] = f"the merged cli.py is not valid Python: {exc}"
            log(f"\nthe merged file is not valid Python: {exc}")
            return conflict_out(report, args)

        # ---- static compatibility of the modules we add ----------------------
        carried_sources = {}
        for carried_rel, tree_rel in CARRIED.items():
            local = root / carried_rel
            if not local.is_file():
                local = root / tree_rel
            if local.is_file():
                carried_sources[carried_rel] = local.read_text(encoding="utf-8")
        carried_sources["cli.py (rebased)"] = merged

        log("checking that every upstream symbol our code imports still exists ...")
        problems = check_symbols(carried_sources, new_tree)
        if problems:
            report["message"] = "upstream API moved: " + "; ".join(problems)
            report["problems"] = problems
            log("\nupstream refactored something we depend on:")
            for problem in problems:
                log(f"  - {problem}")
            log("\nNothing was written.")
            return conflict_out(report, args)
        log("  all symbols still present.")

        # ---- new patch, new hashes -------------------------------------------
        new_patch = unified_diff(theirs, merged, CLI_REL)
        if not new_patch:
            report["message"] = "the rebased patch is empty - refusing to write it"
            log("\nthe rebased patch is empty; that would silently drop the customizations.")
            return conflict_out(report, args)

        staged: dict[pathlib.Path, str] = {patch_path: new_patch}
        new_hashes: dict[str, str] = {CLI_REL: sha256_bytes(merged.encode("utf-8"))}
        for carried_rel, tree_rel in CARRIED.items():
            new_hashes[tree_rel] = sha256_bytes(carried_sources[carried_rel].encode("utf-8"))
            staged[root / carried_rel] = carried_sources[carried_rel]

        # Keep a full source checkout (if this is one) in step with the rebased result,
        # so a local build produces exactly what CI would.
        if (root / CLI_REL).is_file():
            staged[root / CLI_REL] = merged

        version = upstream_version(new_tree)
        if target["tag"] and target["tag"].lstrip("v") != version:
            log(f"  note: tag says {target['tag']} but pyproject says {version}; using {version}")

        new_pins = {
            "UPSTREAM_SHA": target["sha"],
            "ICLOUDPD_VERSION": version,
            "ICLOUDPD_COMMIT_SHA": target["sha_short"],
            "ICLOUDPD_COMMIT_TS": str(target["commit_ts"]),
        }
        for rel, text in workflow_texts.items():
            staged[root / rel] = rewrite_workflow(text, rel, new_pins, new_hashes)

        # ---- prove the new patch actually reproduces those bytes -------------
        log("verifying the rebased patch against a clean checkout of the new upstream ...")
        rebuilt = apply_patch_to(theirs, new_patch, CLI_REL)
        if sha256_bytes(rebuilt.encode("utf-8")) != new_hashes[CLI_REL]:
            report["message"] = "the rebased patch does not reproduce the expected bytes"
            log("\nthe rebased patch does not reproduce the expected bytes.")
            return conflict_out(report, args)
        log("  the patch applies cleanly and reproduces byte-identical output.")

        report["synced"] = True
        report["updated"] = True
        report["pins"] = new_pins
        report["hashes"] = new_hashes
        report["message"] = (
            f"rebased the customizations onto {target['tag'] or target['sha_short']} ({version})"
        )

        if args.dry_run:
            log("\n--dry-run: the following would change")
            for path in sorted(staged):
                log(f"  {path.relative_to(root)}")
            return 0

        for path, text in staged.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        log(f"\nwrote {len(staged)} file(s):")
        for path in sorted(staged):
            log(f"  {path.relative_to(root)}")

        if scratch.exists() and not args.cache:
            shutil.rmtree(scratch, ignore_errors=True)

        log(f"\nsynced to {target['tag'] or target['sha_short']}  (icloudpd {version})")
        return 0

    except SyncError as exc:
        report["message"] = str(exc)
        report["exit_code"] = 1
        log(f"\nerror: {exc}")
        return 1
    finally:
        if args.report:
            report["exit_code"] = report.get("exit_code", 0)
            pathlib.Path(args.report).write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )


def conflict_out(report: dict, args: argparse.Namespace) -> int:
    report["exit_code"] = report.get("exit_code") or 3
    return report["exit_code"]


def apply_patch_to(original: str, patch: str, rel: str) -> str:
    """Apply a unified diff to a single file's text (no git required)."""
    lines = original.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    hunks = re.split(r"^@@", patch, flags=re.M)[1:]
    if not hunks:
        raise SyncError(f"the patch has no hunks for {rel}")

    offset = 0
    for hunk in hunks:
        header, _, body = hunk.partition("\n")
        match = re.match(r"\s*-(\d+)(?:,(\d+))?\s+\+(\d+)", header)
        if not match:
            raise SyncError(f"cannot parse hunk header: @@{header}")
        start = int(match.group(1))
        count = int(match.group(2) or 1)

        want = [ln[1:] for ln in body.splitlines() if ln[:1] in (" ", "-")]
        if len(want) != count:
            raise SyncError(f"hunk @@{header}: expected {count} context lines, found {len(want)}")

        # `@@ -l,0 +... @@` inserts *after* line l; every other hunk replaces from line l.
        at = (start if count == 0 else start - 1) + offset
        have = lines[at : at + count]
        if [ln.rstrip("\n") for ln in have] != [ln.rstrip("\n") for ln in want]:
            raise SyncError(f"the patch does not apply: context mismatch at line {at + 1} of {rel}")

        replacement = [ln[1:] + "\n" for ln in body.splitlines() if ln[:1] in (" ", "+")]
        lines[at : at + count] = replacement
        offset += len(replacement) - count

    return "".join(lines)


if __name__ == "__main__":
    sys.exit(main())
