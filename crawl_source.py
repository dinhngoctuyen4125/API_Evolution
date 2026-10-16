"""Keep one Git source checkout per library and switch it to curated versions."""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from enrich_pypi_versions import REPOSITORIES


ROOT_DIR = Path(__file__).resolve().parent
INPUT_PATH = ROOT_DIR / "data" / "pypi_versions_with_github_diy.json"
CACHE_DIR = ROOT_DIR / "cache"
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")


def git(*args, cwd=None, check=True, capture=True):
    command = ["git", *map(str, args)]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git is not installed or is absent from PATH") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Git command failed ({' '.join(command)}): {detail}")
    return result


def github_url(repository):
    return f"https://github.com/{repository}.git"


def remote_identity(url):
    url = url.strip().rstrip("/")
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([\w.-]+/[\w.-]+?)(?:\.git)?",
        url,
        re.I,
    )
    return ("github", match.group(1).lower()) if match else ("other", url)


def source_path(library):
    return CACHE_DIR / library / "source"


def validate_source(path, expected_url):
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"Source path is not a Git directory: {path}")
    top_level = git("-C", path, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(top_level).resolve() != path.resolve():
        raise RuntimeError(f"Source path is not the root of its own Git repository: {path}")
    actual_url = git("-C", path, "remote", "get-url", "origin").stdout.strip()
    if remote_identity(actual_url) != remote_identity(expected_url):
        raise RuntimeError(f"Unexpected origin for {path}: {actual_url}")


def ensure_source(library, repository_url=None):
    if library not in REPOSITORIES:
        raise ValueError(f"Unsupported library: {library}")
    expected_url = repository_url or github_url(REPOSITORIES[library])
    path = source_path(library)
    if path.exists() or path.is_symlink():
        validate_source(path, expected_url)
        return path, False

    library_dir = path.parent
    library_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".source-", dir=library_dir) as staging_dir:
        staging_path = Path(staging_dir) / "repo"
        print(f"Cloning {library} from {expected_url}", flush=True)
        git("clone", "--filter=blob:none", "--no-checkout", expected_url, staging_path,
            capture=False)
        if sys.platform == "win32":
            git("-C", staging_path, "config", "core.longpaths", "true")
        default_sha = git("-C", staging_path, "rev-parse", "HEAD").stdout.strip()
        git("-C", staging_path, "checkout", "--detach", default_sha, capture=False)
        validate_source(staging_path, expected_url)
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"Source path appeared during clone: {path}")
        staging_path.rename(path)
    return path, True


def load_version(library, version):
    records = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"{INPUT_PATH} must contain a JSON array")
    matches = [record for record in records if isinstance(record, dict)
               and record.get("library") == library and record.get("version") == version]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one curated version for {library} {version}")
    record = matches[0]
    sha = record.get("commit_sha")
    if not isinstance(sha, str) or not FULL_SHA.fullmatch(sha):
        raise ValueError(f"Missing or invalid commit SHA for {library} {version}")
    expected_source = f"https://github.com/{REPOSITORIES[library]}/tree/{sha}"
    if record.get("source_url") != expected_source:
        raise ValueError(f"Source URL disagrees with commit SHA for {library} {version}")
    return record


def has_commit(path, sha):
    return git("-C", path, "cat-file", "-e", f"{sha}^{{commit}}",
               check=False).returncode == 0


def fetch_commit(path, record):
    sha = record["commit_sha"]
    if has_commit(path, sha):
        return
    tag = record.get("tag")
    if isinstance(tag, str) and tag:
        print(f"Fetching tag {tag} for {sha}", flush=True)
        fetched = git("-C", path, "fetch", "--filter=blob:none", "origin",
                      f"refs/tags/{tag}", check=False)
        if fetched.returncode == 0:
            tag_sha = git("-C", path, "rev-parse", "FETCH_HEAD^{commit}").stdout.strip()
            if tag_sha.lower() != sha.lower():
                raise RuntimeError(f"Tag {tag} no longer points to curated SHA {sha}")
    if not has_commit(path, sha):
        print(f"Fetching commit {sha}", flush=True)
        git("-C", path, "fetch", "--filter=blob:none", "origin", sha)
    if not has_commit(path, sha):
        raise RuntimeError(f"Commit {sha} is unavailable in {path}")


def checkout_version(path, record):
    if git("-C", path, "status", "--porcelain", "--untracked-files=all").stdout.strip():
        raise RuntimeError(f"Source checkout has local changes: {path}")
    sha = record["commit_sha"]
    fetch_commit(path, record)
    current = git("-C", path, "rev-parse", "HEAD").stdout.strip()
    if current.lower() != sha.lower():
        git("-C", path, "checkout", "--detach", sha, capture=False)
    actual = git("-C", path, "rev-parse", "HEAD").stdout.strip()
    if actual.lower() != sha.lower():
        raise RuntimeError(f"Checkout mismatch for {path}: expected {sha}, got {actual}")
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--library", action="append", choices=sorted(REPOSITORIES),
                           help="Prepare one library; repeat for more libraries")
    selection.add_argument("--all", action="store_true", help="Prepare all eight libraries")
    parser.add_argument("--version", help="Checkout a curated version (requires one --library)")
    args = parser.parse_args()
    if args.version and (args.all or len(args.library or []) != 1):
        parser.error("--version requires exactly one --library")

    libraries = list(REPOSITORIES) if args.all else list(dict.fromkeys(args.library))
    record = load_version(libraries[0], args.version) if args.version else None
    failures = 0
    for library in libraries:
        try:
            path, cloned = ensure_source(library)
            sha = checkout_version(path, record) if record else git(
                "-C", path, "rev-parse", "HEAD"
            ).stdout.strip()
            action = "Cloned" if cloned else "Ready"
            print(f"{action}: {library} -> {path} @ {sha}")
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            failures += 1
            print(f"Failed: {library}: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
