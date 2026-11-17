"""Crawl GitHub call sites with unambiguous dependency-to-milestone evidence.

python github_crawler.py --library numpy --mapping-id 2 --repo owner/name
python github_crawler.py --library numpy --mapping-id 2 --max-repos 20

The second form needs GITHUB_TOKEN (or GH_TOKEN) for GitHub code search.
"""

import argparse
import ast
import base64
import binascii
import configparser
from concurrent.futures import ThreadPoolExecutor, as_completed
import functools
import hashlib
from http.client import HTTPException
import inspect
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import textwrap
import threading
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import yaml
from dotenv import load_dotenv
from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DISTRIBUTION = {"pytorch": "torch"}
VALID = {"active", "deprecated"}
LAST_SEARCH = 0.0
REPO_DEADLINE = None
# GitHub returns at most 1000 results per query, so each API query is split by file size.
SIZE_SHARDS = ("size:<2000", "size:2000..5999", "size:6000..19999", "size:>=20000")
# Parallel file/commit fetches while prefiltering search hits (GitHub allows far more
# concurrent reads; rate-limit replies are waited out per request).
PREFILTER_WORKERS = 8
# Extra code-search words for an API whose size-sharded queries hit the 1000-file cap:
# each word returns a different 1000 files. The class of a method target is added too.
LIBRARY_QUERY_TERMS = {"pandas": ("read_csv", "lambda")}
# Dependency files pinning the library to a missing cell, and then batched code search for
# target calls in those repositories (`repo:` qualifiers; GitHub allows 256 characters).
PIN_QUERY_LIMIT = 100
CONFIRM_QUERIES = 60
SEARCH_QUERY_LENGTH = 256
# Files kept per API from Sourcegraph (count:all), separate from --max-files: one query
# can return over 10000 files and most of them are filtered out by the AST prefilter.
SOURCEGRAPH_FILES = 20000
VENDOR_DIRS = {"site-packages", "dist-packages", ".eggs", "build", "dist"}
VENDOR_PATHSPECS = [f":(exclude,glob){pattern}" for pattern in (
    "**/site-packages/**", "**/dist-packages/**", "**/.eggs/**", "**/*.egg/**",
    "**/build/**", "**/dist/**")]
# Benchmark, synthetic or agent-output repositories: their code is planted or copied from
# evaluation tasks, not written by users, and would leak benchmark data into samples.
EXCLUDED_REPOS = {name.lower() for name in (
    "boranfurkan/cutoff",                     # planted outdated "seeds" for an upgrade agent
    "NizarIslah/GitChameleon",                # version-conditioned code generation benchmark
    "Geek-569/api-evo-200",                   # API-evolution benchmark
    "karankartikeya/PyLayer",                 # bench/tasks/numpy* migration tasks
    "MaCoredroid/Lumo_FlyWheel",              # agent run outputs on SWE-bench tasks
    "MaCoredroid/Lumo_FlyWheel-qwen-diffusion",
)}
EXCLUDED_REPO_PATTERN = re.compile(
    r"swe[-_]?bench|mini-swe-agent|^onerepoonemodel/|^openhands-commit0/", re.I)
DEPENDENCY_NAME = re.compile(
    r"(?:^|/)(?:requirements[^/]*\.(?:txt|in)|constraints[^/]*\.txt|"
    r"pyproject\.toml|setup\.(?:cfg|py)|Pipfile(?:\.lock)?|"
    r"(?:poetry|uv|pdm)\.lock|pylock\.toml|conda-lock\.ya?ml|environment\.ya?ml|"
    r"tox\.ini|tox\.toml|noxfile\.py|Dockerfile|\.github/workflows/[^/]+\.ya?ml)$",
    re.I,
)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # On Windows a virus scanner or indexer may briefly lock a file just written
    # ("[WinError 5] Access is denied" on replace); retry before giving up.
    for attempt in range(8):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.25 * (attempt + 1))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def library_roots():
    """Import roots of every library in the project; their source copies are not samples."""
    return {api.split(".", 1)[0] for path in DATA.glob("*/migration_evolution.json")
            for timeline in read_json(path) for api in timeline["seed"].values()}


def vendored(path, roots, tracked=None):
    """True for installed/built copies and for a copy of a project library (numpy, pandas...).

    A directory named like a library (``numpy/``) counts as a copy when it is a package
    (has ``__init__.py``); without the repository file list, the name alone decides.
    """
    parts = path.split("/")[:-1]
    if tracked is not None and path not in tracked:
        tracked = None
    for i, part in enumerate(parts):
        if part.lower() in VENDOR_DIRS or part.lower().endswith(".egg"):
            return True
        if part in roots and (tracked is None or
                              "/".join(parts[:i + 1]) + "/__init__.py" in tracked):
            return True
    return False


def excluded_repo(name):
    return bool(name) and (name.lower() in EXCLUDED_REPOS or bool(EXCLUDED_REPO_PATTERN.search(name)))


def sample_repo(sample):
    match = re.match(r"https://github\.com/([^/]+/[^/]+)/blob/", sample.get("source", ""))
    return match[1] if match else None


def sample_path(sample):
    source = sample.get("source", "")
    match = re.match(r"https://github\.com/[^/]+/[^/]+/blob/[^/]+/([^#]+)", source)
    if match:
        return unquote(match[1])
    return re.sub(r"#L\d+$", "", source).rsplit(":", 1)[-1]


def load_search_cache(path, signature, refresh=False):
    if refresh or not path.exists():
        return None
    try:
        payload = read_json(path)
        if (payload.get("signature") != signature or
                time.time() - payload.get("created", 0) > 24 * 3600):
            return None
        rows = payload["candidates"]
        for _, info in rows:
            for key in ("paths", "manifests", "observed", "spec_cells", "commits", "call_blobs"):
                info[key] = set(info.get(key, ()))
            info.setdefault("history_calls", 0)
        print(f"Using cached search results: {len(rows)} repositories", flush=True)
        return rows, payload.get("max_files")
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_search_cache(path, signature, candidates, max_files):
    rows = [(name, {key: sorted(value) if isinstance(value, set) else value
                    for key, value in info.items()}) for name, info in candidates]
    write_json(path, {"signature": signature, "created": time.time(),
                      "max_files": max_files, "candidates": rows})


class TimeLimit(RuntimeError):
    """The per-repository time budget is used up (not a read error)."""


def time_left(deadline, cap):
    if deadline is None:
        return cap
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeLimit("Repository time limit reached")
    return min(cap, remaining)


def kill_tree(process):
    """Kill a process and its children.

    On Windows, git.exe in Git\\cmd is a launcher: killing only it leaves the real git
    running and holding the output pipes, so communicate() waits until that git ends.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    process.kill()
    try:
        process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()


def run_process(command, label, max_seconds=None):
    by_deadline = False
    if REPO_DEADLINE is not None:
        max_seconds = time_left(REPO_DEADLINE, max_seconds or 300)
        # Callers often pass time_left(deadline, cap) already, so compare with the deadline.
        by_deadline = REPO_DEADLINE - time.monotonic() <= max_seconds + 0.5
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace")
    started = time.monotonic()
    while True:
        try:
            remaining = None if max_seconds is None else max_seconds - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                kill_tree(process)
                if by_deadline:
                    raise TimeLimit(f"Repository time limit reached while {label}")
                raise RuntimeError(f"{label} exceeded {max_seconds:.0f}s")
            stdout, stderr = process.communicate(timeout=min(20, remaining) if remaining else 20)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            print(f"  Still {label}...", flush=True)
        except KeyboardInterrupt:
            kill_tree(process)
            raise


def git(repo, *args, allow_missing=False, max_seconds=None):
    # core.longpaths: on Windows, paths over 260 characters fail with "Filename too long".
    result = run_process(["git", "-C", str(repo), "-c", "core.longpaths=true", *args],
                         f"running git {args[0]}",
                         max_seconds=max_seconds)
    if result.returncode and not allow_missing:
        raise RuntimeError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout if result.returncode == 0 else None


def fetch_blobs(repo, oids, deadline=None):
    """Fetch missing blobs of a partial clone in a few batched requests.

    Without this, git grep/log/show fetch every missing blob separately.
    """
    if not oids:
        return
    print(f"  Prefetching {len(oids)} blobs", flush=True)
    # Object IDs go on the command line in chunks (Windows limits its length).
    for start in range(0, len(oids), 500):
        result = run_process(
            ["git", "-C", str(repo), "-c", "fetch.negotiationAlgorithm=noop", "fetch",
             "origin", "--no-tags", "--no-write-fetch-head", "--recurse-submodules=no",
             "--filter=blob:none", *oids[start:start + 500]],
            "prefetching blobs", max_seconds=time_left(deadline, 120))
        if result.returncode:
            print(f"  Blob prefetch failed: {result.stderr.strip()[:200]}", flush=True)
            return


def missing_objects(repo, commit, deadline=None, walk=True):
    listed = git(repo, "rev-list", "--objects", "--missing=print",
                 *([] if walk else ["--no-walk"]), commit, max_seconds=time_left(deadline, 60))
    return {line[1:].strip() for line in listed.splitlines() if line.startswith("?")}


def prefetch_blobs(repo, tip, pathspecs, limit=0, deadline=None):
    """Prefetch every blob version of the given paths in the history (what git log -G
    and git show read). The raw diff needs only trees, which a blob:none clone has."""
    if not pathspecs:
        return
    cap = ["-n", str(limit)] if limit else []
    try:
        raw = git(repo, "log", "--raw", "--no-abbrev", "--format=", *cap, tip, "--",
                  *pathspecs, max_seconds=time_left(deadline, 60))
        oids = {oid for line in raw.splitlines() if line.startswith(":")
                for oid in line.split()[2:4] if oid.strip("0")}
        missing = missing_objects(repo, tip, deadline) if oids else set()
        fetch_blobs(repo, sorted(oids & missing), deadline)
    except RuntimeError as exc:
        print(f"  Blob prefetch stopped: {exc}", flush=True)


def prefetch_tree_blobs(repo, commit, wanted, deadline=None):
    """Prefetch the blobs of one commit's files whose path satisfies wanted(path)."""
    try:
        missing = missing_objects(repo, commit, deadline, walk=False)
        if not missing:
            return
        tree = git(repo, "ls-tree", "-r", "-z", commit, max_seconds=time_left(deadline, 60))
        oids = {meta.split()[2] for meta, _, path in
                (entry.partition("\t") for entry in tree.split("\0") if entry)
                if wanted(path)}
        fetch_blobs(repo, sorted(oids & missing), deadline)
    except RuntimeError as exc:
        print(f"  Blob prefetch stopped: {exc}", flush=True)


def terminate_tree(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process.kill()
    except OSError:
        pass


class BlobReader:
    """Read commit:path through one `git cat-file --batch` process per repository.

    Starting git once per file costs ~50 ms on Windows; a batch read costs ~1 ms.
    """

    def __init__(self, repo):
        self.repo = repo
        self.process = None

    def close(self):
        if self.process is not None:
            terminate_tree(self.process)
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            self.process = None

    def read(self, commit, path, deadline=None):
        """Return (blob id, text), or (None, None) when commit:path is not a file."""
        seconds = time_left(deadline, 60)
        if self.process is None or self.process.poll() is not None:
            self.process = subprocess.Popen(
                ["git", "-C", str(self.repo), "-c", "core.longpaths=true", "cat-file", "--batch"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        process, expired = self.process, []

        def stop():
            expired.append(True)
            terminate_tree(process)

        timer = threading.Timer(seconds, stop)
        timer.daemon = True
        timer.start()
        try:
            process.stdin.write(f"{commit}:{path}\n".encode("utf-8"))
            process.stdin.flush()
            header = process.stdout.readline()
            if not header:
                raise OSError("git cat-file stopped")
            if header.rstrip().endswith((b" missing", b" ambiguous")):
                return None, None
            oid, kind, size = header.split()
            data = process.stdout.read(int(size) + 1)
            if len(data) != int(size) + 1:
                raise OSError("git cat-file stopped")
            if kind != b"blob":
                return None, None
            return oid.decode(), data[:-1].decode("utf-8", errors="replace")
        except (OSError, ValueError) as exc:
            self.close()
            if expired and deadline is not None and deadline - time.monotonic() <= 0.5:
                raise TimeLimit(f"Repository time limit reached while reading {path}") from exc
            if expired:
                raise RuntimeError(f"reading {commit[:12]}:{path} exceeded {seconds:.0f}s") from exc
            raise RuntimeError(f"Cannot read {commit[:12]}:{path}: {exc}") from exc
        finally:
            timer.cancel()


def github_search(token, query, limit, endpoint="code"):
    global LAST_SEARCH
    found, page = [], 1
    per_page = min(50 if endpoint == "code" else 100, limit)
    waits = 0
    while len(found) < limit:
        delay = max(0, LAST_SEARCH + 7 - time.monotonic())
        if delay:
            time.sleep(delay)
        url = f"https://api.github.com/search/{endpoint}?" + urlencode({
            "q": query, "per_page": per_page, "page": page,
        })
        request = Request(url, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "api-evolution-step9",
        })
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.load(response)
                remaining = int(response.headers.get("X-RateLimit-Remaining", "1"))
                reset = int(response.headers.get("X-RateLimit-Reset", "0"))
                LAST_SEARCH = time.monotonic()
        except HTTPError as exc:
            headers = exc.headers or {}
            detail = exc.read().decode("utf-8", errors="replace")
            # Secondary (burst) limit: wait and retry the same page instead of stopping.
            secondary = exc.code in (403, 429) and "secondary rate limit" in detail.lower()
            limited = exc.code in (403, 429) and (
                headers.get("X-RateLimit-Remaining") == "0" or headers.get("Retry-After"))
            if not (secondary or limited) or waits >= 5:
                raise RuntimeError(f"GitHub {endpoint} search {exc.code}: {detail[:300]}") from exc
            retry_after = headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = int(retry_after) + 1
            elif headers.get("X-RateLimit-Remaining") == "0":
                delay = max(1, int(headers.get("X-RateLimit-Reset", "0") or 0) - time.time() + 1)
            else:
                delay = 60
            delay = min(delay, 3700)
            waits += 1
            print(f"  GitHub rate limit: waiting {int(delay)}s", flush=True)
            time.sleep(delay)
            LAST_SEARCH = time.monotonic()
            continue
        waits = 0
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise RuntimeError(f"GitHub {endpoint} search returned an invalid response")
        items = payload["items"]
        print(f"  Search page {page}: {len(items)} results", flush=True)
        found.extend(item for item in items)
        if len(items) < per_page:
            break
        page += 1
        if len(found) >= 1000:
            break
        if remaining == 0:
            delay = max(1, reset - time.time() + 1)
            print(f"  GitHub rate limit: waiting {int(delay)}s", flush=True)
            time.sleep(delay)
    return found[:limit]


def github_json(request, attempts=5):
    """JSON of a GitHub REST request; waits out rate limits, None on other failures.

    Without the wait, files fetched after the hourly quota runs out would be silently
    dropped from the candidates.
    """
    for _ in range(attempts):
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            headers = exc.headers or {}
            try:
                detail = exc.read(2000).decode("utf-8", errors="replace").lower()
            except (OSError, ValueError, AttributeError):
                detail = ""
            # The secondary (concurrency) limit may come without Retry-After.
            secondary = exc.code in (403, 429) and "secondary rate limit" in detail
            limited = exc.code in (403, 429) and (
                headers.get("X-RateLimit-Remaining") == "0" or headers.get("Retry-After"))
            if not (limited or secondary):
                return None
            retry_after = headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = int(retry_after) + 1
            elif headers.get("X-RateLimit-Remaining") == "0":
                delay = max(1, int(headers.get("X-RateLimit-Reset", "0") or 0) - time.time() + 1)
            else:
                delay = 60
            delay = min(delay, 3700)
            print(f"  GitHub rate limit: waiting {int(delay)}s", flush=True)
            time.sleep(delay)
        except (HTTPException, OSError, ValueError):
            return None
    return None


def github_content(token, url):
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc.lower() != "api.github.com":
            return None
        safe_url = urlunsplit((parsed.scheme, parsed.netloc,
                              quote(parsed.path, safe="/%"),
                              quote(parsed.query, safe="=&%+"), ""))
    except ValueError:
        return None
    item = github_json(Request(safe_url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "api-evolution-step9",
    }))
    if not isinstance(item, dict) or item.get("encoding") != "base64":
        return None
    if not isinstance(item.get("size", 0), int) or item.get("size", 0) > 384_000:
        return None
    if not isinstance(item.get("content"), str):
        return None
    try:
        return base64.b64decode(item["content"]).decode("utf-8", errors="replace")
    except (ValueError, binascii.Error):
        return None


RAW_SKIP = object()


def raw_text(token, name, ref, path, attempts=5):
    """File text from raw.githubusercontent.com, which does not spend the REST API quota.

    RAW_SKIP when GitHub says the file is not there (404) or it is larger than the
    contents API limit (384 kB), None when it could not be read.
    """
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name) or
            not isinstance(ref, str) or not ref or not isinstance(path, str) or not path):
        return None
    url = f"https://raw.githubusercontent.com/{name}/{quote(ref, safe='')}/{quote(path)}"
    headers = {"User-Agent": "api-evolution-step9"}
    if token:
        headers["Authorization"] = f"token {token}"
    for attempt in range(attempts):
        try:
            with urlopen(Request(url, headers=headers), timeout=30) as response:
                data = response.read(384_001)
            if len(data) > 384_000:
                return RAW_SKIP
            return data.decode("utf-8", errors="replace")
        except HTTPError as exc:
            if exc.code == 404:
                return RAW_SKIP
            if exc.code not in (403, 429) and exc.code < 500:
                return None
            retry_after = (exc.headers or {}).get("Retry-After")
            delay = int(retry_after) + 1 if retry_after and retry_after.isdigit() else 5 * 2 ** attempt
            time.sleep(min(delay, 300))
        except (HTTPException, OSError, ValueError):
            time.sleep(2 ** attempt)
    return None


def file_text(token, name, ref, path, api_url=None):
    """Text of name:path at ref; the REST contents API is only a fallback for raw failures."""
    text = raw_text(token, name, ref, path)
    if text is RAW_SKIP:
        return None
    if text is None and api_url:
        return github_content(token, api_url)
    return text


def github_commit(token, name, sha):
    """(first parent sha or None, [(filename, patch)]) of one commit; None when unavailable."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return None
    item = github_json(Request(f"https://api.github.com/repos/{name}/commits/{sha}", headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "api-evolution-step9",
    }))
    if not isinstance(item, dict):
        return None
    parents = [p.get("sha") for p in item.get("parents") or [] if isinstance(p, dict)]
    parent = parents[0] if parents and isinstance(parents[0], str) else None
    files = [(f["filename"], f.get("patch") or "") for f in item.get("files") or []
             if isinstance(f, dict) and isinstance(f.get("filename"), str)]
    return parent, files


def sourcegraph_search(query):
    """(owner/name, path) of public GitHub files matching a Sourcegraph query; [] on failure.

    GitHub code search only indexes each repository's default branch and caps every
    query at 1000 files; Sourcegraph is a second, independent index.
    """
    request = Request("https://sourcegraph.com/.api/search/stream?q=" + quote(query),
                      headers={"Accept": "text/event-stream", "User-Agent": "api-evolution-step9"})
    found, event = [], None
    try:
        with urlopen(request, timeout=180) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: ") and event == "matches":
                    for match in json.loads(line[6:]):
                        repo, path = match.get("repository", ""), match.get("path")
                        if (match.get("type") == "content" and isinstance(path, str)
                                and isinstance(repo, str) and repo.startswith("github.com/")):
                            found.append((repo[len("github.com/"):], path))
    except (HTTPError, HTTPException, OSError, ValueError) as exc:
        print(f"  Sourcegraph search failed: {exc}", flush=True)
    return list(dict.fromkeys(found))


def spec_cells(specs, timelines):
    """Milestone cells ("mapping_id:version") that dependency specs pin unambiguously."""
    cells = set()
    for timeline in timelines:
        versions = [m["version"] for m in timeline["milestones"]]
        for spec in specs:
            index = containing_milestone(spec, versions)
            if index is not None:
                cells.add(f"{timeline['mapping_id']}:{versions[index]}")
    return cells


def library_releases(distribution):
    """Final release versions of a distribution on PyPI ([] when PyPI is unreachable)."""
    request = Request(f"https://pypi.org/pypi/{quote(canonicalize_name(distribution))}/json",
                      headers={"User-Agent": "api-evolution-step9"})
    try:
        with urlopen(request, timeout=30) as response:
            releases = json.load(response).get("releases", {})
    except (HTTPException, OSError, ValueError):
        return []
    versions = []
    for value in releases:
        try:
            version = Version(value)
        except ValueError:
            continue
        if not version.is_prerelease and not version.is_devrelease:
            versions.append(version)
    return sorted(versions)


def pin_minors(distribution, timelines, wanted):
    """"major.minor" of every release inside a missing cell, for `"pandas==2.2"` searches.

    Without PyPI, each missing milestone's own major.minor is used.
    """
    cells = set().union(*wanted.values()) if wanted else set()
    releases = library_releases(distribution) if cells else []
    minors = {}
    for timeline in timelines:
        versions = [m["version"] for m in timeline["milestones"]]
        for i, version in enumerate(versions):
            if f"{timeline['mapping_id']}:{version}" not in cells:
                continue
            try:
                start = Version(version)
                end = Version(versions[i + 1]) if i + 1 < len(versions) else None
            except ValueError:
                continue
            inside = [r for r in releases if r >= start and (end is None or r < end)] or [start]
            for release in inside:
                minors.setdefault(release.release[:2], f"{release.major}.{release.minor}")
    return [minors[key] for key in sorted(minors)]


def patch_hit_lines(patch, pattern):
    """Line numbers of removed (old side) and added (new side) patch lines matching pattern."""
    removed, added, old, new = set(), set(), 0, 0
    for line in patch.splitlines():
        hunk = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk:
            old, new = int(hunk[1]), int(hunk[2])
        elif line.startswith("-"):
            if pattern.search(line):
                removed.add(old)
            old += 1
        elif line.startswith("+"):
            if pattern.search(line):
                added.add(new)
            new += 1
        elif not line.startswith("\\"):
            old += 1
            new += 1
    return removed, added


def commit_evidence(token, name, sha, targets, roots, counterparts=None):
    """Python files whose patch in this commit adds or removes an AST-resolved target call.

    The patch only preselects lines naming a target leaf; the file itself is then parsed
    at the parent (removed lines) and at the commit (added lines) with the same resolver
    as the repository scan, so jnp.cumproduct, cupy.cumproduct, `def cumproduct(` or an
    `np` that may be torch._numpy are not counted.
    """
    leaves = {target.rsplit(".", 1)[-1] for target in targets}
    leaf_call = re.compile(r"\b(" + "|".join(map(re.escape, sorted(leaves))) + r")\s*\(")
    commit = github_commit(token, name, sha)
    if commit is None:
        return None
    parent, files = commit
    paths, observed, count = set(), set(), 0
    for filename, patch in files:
        if not filename.endswith(".py") or vendored(filename, roots):
            continue
        removed, added = patch_hit_lines(patch, leaf_call)
        for ref, lines in ((parent, removed), (sha, added)):
            if not lines or ref is None:
                continue

            def read(path, ref=ref):
                return file_text(token, name, ref, path,
                                 f"https://api.github.com/repos/{name}/contents/{quote(path)}?ref={ref}")

            content = read(filename)
            if content is None:
                continue
            calls, used = migration_calls(content, targets, counterparts or {})
            if not used:
                candidates = reexport_candidates(content, filename, targets)
                rebinds = reexport_rebinds(candidates, targets, read) if candidates else {}
                if rebinds:
                    calls, used = migration_calls(content, targets, counterparts or {}, rebinds)
            calls = single_version_calls(calls, used, counterparts or {})
            hits = [call for call in calls
                    if lines & set(range(call[4].lineno, (call[4].end_lineno or call[4].lineno) + 1))]
            if hits:
                paths.add(filename)
                count += len(hits)
                observed.update(call[6] for call in hits)
    return {"paths": sorted(paths), "history_calls": count, "observed": sorted(observed)}


def discover_candidates(token, targets, timelines, library, limit, cache, wanted):
    candidates = defaultdict(lambda: {"paths": set(), "manifests": set(), "head_calls": 0,
                                      "history_calls": 0, "commits": set(),
                                      "observed": set(), "spec_cells": set(),
                                      "call_blobs": set(), "repo_size": None})
    roots = library_roots()
    counterparts = pair_counterparts(timelines)
    shard_limit = min(1000, -(-limit // len(SIZE_SHARDS)))
    leaf_roots = sorted({(target.rsplit(".", 1)[-1], target.split(".", 1)[0]) for target in targets})
    # The import root (numpy, torch, sklearn...) must also appear in the file.
    queries = [("code", f"{leaf} {root} language:Python {shard}", shard_limit)
               for leaf, root in leaf_roots for shard in SIZE_SHARDS]
    # Sourcegraph: a second code index (also beyond GitHub's 1000-file cap). The root goes
    # in file:has.content() so it only filters files: as a plain term every occurrence
    # would count as a match and use up the result limit. A method of a class is called
    # as `.leaf(`; `\b` would also match functions of that name (e.g. builtin map()).
    # An indexer (call_syntax kind "indexer", e.g. DataFrame.loc) is used as `.leaf[key]`
    # or `.leaf(axis=...)[key]`, so `[` is accepted after the name as well as `(`.
    functions = {(target.rsplit(".", 1)[-1], target.split(".", 1)[0]) for target in targets
                 if not any(part[:1].isupper() for part in target.split(".")[1:-1])}
    indexers = {(target.rsplit(".", 1)[-1], target.split(".", 1)[0]) for target in targets
                if any(state["name"] == target
                       and (state.get("call_syntax") or {}).get("kind") == "indexer"
                       for timeline in timelines for milestone in timeline["milestones"]
                       for state in milestone["api_states"])}
    for leaf, root in leaf_roots:
        before = r"\b" if (leaf, root) in functions else r"\."
        after = r"[\[(]" if (leaf, root) in indexers else r"\("
        queries.append(("sourcegraph", f"lang:python file:has.content({root}) "
                                       f"/{before}{re.escape(leaf)}{after}/ "
                                       f"fork:yes archived:yes count:all", SOURCEGRAPH_FILES))
    # Code search only sees each default branch at HEAD; commit search finds repositories
    # whose history added or removed the call (e.g. migrations to the replacement API).
    commit_queries = []
    for timeline in timelines:
        old, new = timeline["seed"]["deprecated_api"], timeline["seed"]["replacement_api"]
        if old in targets or new in targets:
            old_leaf, new_leaf = old.rsplit(".", 1)[-1], new.rsplit(".", 1)[-1]
            # Migration commits (they often raise the dependency pin in the same change).
            commit_queries += [f"{old_leaf} {new_leaf}", f"{old_leaf} {old.split('.', 1)[0]}",
                               f"{old_leaf} deprecated", f"{old_leaf} FutureWarning",
                               f"replace {old_leaf}"]
    queries.extend(("commits", query, min(limit, 1000)) for query in dict.fromkeys(commit_queries))
    distribution = DISTRIBUTION.get(library, library)
    queries.extend(("dependency", f"{distribution} filename:{filename}", min(limit, 30))
                   for filename in ("requirements.txt", "pyproject.toml"))
    # Repositories whose requirements pin the library inside a missing cell; their calls
    # are then looked up with batched `repo:` code searches (the "confirm" step).
    queries.extend(("dependency", f'"{distribution}=={minor}" filename:requirements.txt',
                    min(limit, PIN_QUERY_LIMIT))
                   for minor in pin_minors(distribution, timelines, wanted))
    queries.append(("confirm", None, None))
    variant_terms = {}
    for target in targets:
        parts = target.split(".")
        terms = [part for part in parts[1:-1] if part[:1].isupper()]
        terms += LIBRARY_QUERY_TERMS.get(library, ())
        variant_terms.setdefault((parts[-1], parts[0]), []).extend(terms)
    varied = set()
    prefilter_path = cache / "prefilter_cache.json"
    # cells-v3: DataFrame receiver typing and version shims changed the call evidence.
    prefilter_signature = digest(("cells-v3", library, sorted(targets),
                                  [(t["mapping_id"], [m["version"] for m in t["milestones"]])
                                   for t in timelines]))
    try:
        saved = read_json(prefilter_path)
        evidence_cache = (saved["files"] if saved.get("signature") == prefilter_signature
                          and isinstance(saved.get("files"), dict) else {})
    except (OSError, ValueError, KeyError, TypeError):
        evidence_cache = {}
    commit_tries, commit_seen = Counter(), set()

    def file_evidence(kind, name, ref, path, url):
        content = file_text(token, name, ref, path, url)
        if content is None:
            return None
        if kind == "dependency":
            return {"spec_cells": sorted(spec_cells(dependency_specs(path, content, library),
                                                    timelines))}
        calls, used = migration_calls(content, targets, counterparts)
        calls = single_version_calls(calls, used, counterparts)
        return {"head_calls": len(calls), "observed": sorted({call[6] for call in calls})}

    def apply(kind, name, hit, evidence):
        info = candidates[name]
        if kind == "commits":
            if evidence["paths"]:
                info["commits"].add(hit)
                info["paths"].update(evidence["paths"])
                info["history_calls"] += evidence["history_calls"]
                info["observed"].update(evidence["observed"])
        elif kind == "dependency":
            info["spec_cells"].update(evidence.get("spec_cells", []))
        elif evidence.get("head_calls"):
            info["head_calls"] += evidence["head_calls"]
            info["observed"].update(evidence.get("observed", []))
            # Identical call files (same blob) in forks/mirrors give the same samples.
            info["call_blobs"].add(hit)

    def confirm_queries():
        """Batched `repo:` code searches for target calls in repositories whose dependency
        files pin the library inside a cell still missing samples for that target.

        Cells take turns (smaller repositories first) so each missing cell gets repositories.
        """
        groups = defaultdict(list)
        for name, info in candidates.items():
            if excluded_repo(name):
                continue
            for target in targets:
                if target in info["observed"]:
                    continue  # its calls are already known
                for cell in info["spec_cells"] & wanted.get(target, set()):
                    groups[(target, cell)].append(name)
        turns = {key: iter(sorted(names, key=lambda name: (
                     candidates[name]["repo_size"] is None, candidates[name]["repo_size"] or 0,
                     name)))
                 for key, names in sorted(groups.items())}
        batches, built, seen = defaultdict(list), [], set()

        def query(target, names):
            leaf, root = target.rsplit(".", 1)[-1], target.split(".", 1)[0]
            return f"{leaf} {root} language:Python" + "".join(f" repo:{name}" for name in names)

        while turns and len(built) < CONFIRM_QUERIES:
            for key in list(turns):
                name = next(turns[key], None)
                if name is None:
                    del turns[key]
                    continue
                target = key[0]
                if (target, name) in seen:
                    continue
                seen.add((target, name))
                batch = batches[target]
                if batch and len(query(target, [*batch, name])) > SEARCH_QUERY_LENGTH:
                    built.append(("confirm-code", query(target, batch), 100))
                    batch.clear()
                    if len(built) >= CONFIRM_QUERIES:
                        break
                batch.append(name)
        for target, batch in batches.items():
            if batch and len(built) < CONFIRM_QUERIES:
                built.append(("confirm-code", query(target, batch), 100))
        return built

    index = 0
    while index < len(queries):
        kind, query, query_limit = queries[index]
        index += 1
        if kind == "confirm":
            batches = confirm_queries()
            queries[index:index] = batches
            print(f"Pinned-dependency repositories: {len(batches)} batched code searches "
                  f"for their API calls", flush=True)
            continue
        unreadable = vendored_hits = 0
        print(f"Search {index}/{len(queries)}: {query}", flush=True)
        if kind == "confirm-code":
            try:
                hits = github_search(token, query, query_limit)
            except RuntimeError as exc:
                # e.g. 422 when a listed repository was renamed, deleted or made private.
                print(f"  Skipped: {str(exc)[:200]}", flush=True)
                continue
            for name in re.findall(r"repo:(\S+)", query):
                candidates[name]["call_searched"] = True
            kind = "code"
        elif kind == "sourcegraph":
            hits = [{"repository": {"full_name": name}, "path": path,
                     "url": f"https://api.github.com/repos/{name}/contents/{quote(path)}"}
                    for name, path in sourcegraph_search(query)
                    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name)][:query_limit]
            kind = "code"
        elif kind == "commits":
            hits = github_search(token, query, query_limit, endpoint="commits")
        else:
            hits = github_search(token, query, query_limit)
            leaf_root = tuple(query.split()[:2])
            if (kind == "code" and " size:" in query and len(hits) >= 1000
                    and leaf_root not in varied):
                # A capped size shard: more files exist than one query returns.
                varied.add(leaf_root)
                queries.extend(("code", f"{leaf_root[0]} {term} {leaf_root[1]} language:Python",
                                shard_limit)
                               for term in dict.fromkeys(variant_terms.get(leaf_root, ())))
        print(f"  {len(hits)} matching {'commits' if kind == 'commits' else 'files'}", flush=True)
        # Selection is sequential (it decides what to fetch); fetches run in parallel and
        # their evidence only adds to sets and counters, so the order does not matter.
        jobs, queued = [], defaultdict(list)

        def queue(cache_key, job_kind, name, hit, fetch):
            # One fetch per key; forks returning the same commit/file share its evidence.
            if cache_key is not None and cache_key in queued:
                queued[cache_key].append((job_kind, name, hit))
                return
            if cache_key is not None:
                queued[cache_key].append((job_kind, name, hit))
            jobs.append((cache_key, job_kind, name, hit, fetch))

        for item in hits:
            if not isinstance(item, dict) or not isinstance(item.get("repository"), dict):
                continue
            if kind == "commits":
                name, sha = item["repository"].get("full_name"), item.get("sha")
                if not isinstance(name, str) or not isinstance(sha, str):
                    continue
                # A few commits per repository are enough to find its call files.
                if (name, sha) in commit_seen or commit_tries[name] >= 3:
                    continue
                commit_seen.add((name, sha))
                commit_tries[name] += 1
                # A commit id fixes the file contents, so forks sharing it share the evidence.
                cache_key = f"commit-v3:{sha}"
                if cache_key in evidence_cache:
                    apply(kind, name, sha, evidence_cache[cache_key])
                else:
                    queue(cache_key, kind, name, sha, functools.partial(
                        commit_evidence, token, name, sha, targets, roots, counterparts))
                continue
            name, path = item["repository"].get("full_name"), item.get("path")
            if not isinstance(name, str) or not isinstance(path, str):
                continue
            if vendored(path, roots):
                vendored_hits += 1
                continue
            size = item["repository"].get("size")
            if isinstance(size, int):
                candidates[name]["repo_size"] = size
            if kind == "dependency":
                if not DEPENDENCY_NAME.search(path):
                    continue
                candidates[name]["manifests"].add(path)
            elif path.endswith(".py"):
                candidates[name]["paths"].add(path)
            else:
                continue
            url = item.get("git_url") or item.get("url")
            sha = item.get("sha") if isinstance(item.get("sha"), str) else None
            blob = f"{path}\0{sha or ''}"  # Sourcegraph hits carry no blob id
            if kind == "code" and sha:
                # Call evidence depends only on the file contents, which a blob id fixes,
                # so forks and mirrors holding the same file share one fetch.
                cache_key = f"code-blob:{sha}"
            else:
                # "dependency-v3": marker-aware parsing changed dependency evidence.
                prefix = "dependency-v3" if kind == "dependency" else kind
                cache_key = f"{prefix}:{url}" if isinstance(url, str) else None
            if cache_key in evidence_cache:
                apply(kind, name, blob, evidence_cache[cache_key])
                continue
            ref = re.search(r"/blob/([^/]+)/", item.get("html_url") or "")
            queue(cache_key, kind, name, blob, functools.partial(
                file_evidence, kind, name, ref[1] if ref else "HEAD", path, url))
        if jobs:
            print(f"  Prefetching {len(jobs)} uncached {'commits' if kind == 'commits' else 'files'}",
                  flush=True)
        pending_cache = 0
        with ThreadPoolExecutor(PREFILTER_WORKERS) as pool:
            futures = {pool.submit(job[4]): job for job in jobs}
            for done, future in enumerate(as_completed(futures), 1):
                cache_key, job_kind, name, hit, _ = futures[future]
                evidence = future.result()
                if done % 100 == 0:
                    print(f"  Prefiltered {done}/{len(jobs)}", flush=True)
                if evidence is None:
                    unreadable += 1
                    continue
                if cache_key is None:
                    apply(job_kind, name, hit, evidence)
                else:
                    evidence_cache[cache_key] = evidence
                    pending_cache += 1
                    for sharer in queued[cache_key]:
                        apply(*sharer, evidence)
                if pending_cache >= 50:
                    write_json(prefilter_path, {"signature": prefilter_signature,
                                                "files": evidence_cache})
                    pending_cache = 0
        if pending_cache:
            write_json(prefilter_path, {"signature": prefilter_signature,
                                        "files": evidence_cache})
        if vendored_hits:
            print(f"  {vendored_hits} vendored/library-copy files ignored", flush=True)
        if unreadable:
            print(f"  {unreadable} files could not be prefetched", flush=True)
    if cache.is_dir():
        for folder in cache.iterdir():
            if folder.is_dir() and "__" in folder.name and is_repo_root(folder):
                remote = git(folder, "config", "--get", "remote.origin.url", allow_missing=True) or ""
                match = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?\s*$", remote)
                if match:
                    candidates[match[1]]
    roots_wanted = [(name, filename) for name, info in candidates.items()
                    if (info["head_calls"] or info["history_calls"]) and not info["spec_cells"]
                    for filename in ("requirements.txt", "pyproject.toml")
                    if filename not in info["manifests"]]
    with ThreadPoolExecutor(PREFILTER_WORKERS) as pool:
        futures = {pool.submit(file_text, token, name, "HEAD", filename,
                               f"https://api.github.com/repos/{name}/contents/{filename}"):
                   (name, filename) for name, filename in roots_wanted}
        for future in as_completed(futures):
            name, filename = futures[future]
            content = future.result()
            if content is None:
                continue
            info = candidates[name]
            info["manifests"].add(filename)
            info["spec_cells"].update(spec_cells(dependency_specs(filename, content, library),
                                                 timelines))
    return rank_candidates(candidates.items(), library, wanted, timelines)


def rank_candidates(candidates, library, wanted, timelines):
    """wanted maps each target API to the "mapping_id:version" cells that still lack samples."""
    order = {f"{timeline['mapping_id']}:{milestone['version']}": index
             for timeline in timelines
             for index, milestone in enumerate(timeline["milestones"])}
    all_wanted = set().union(*wanted.values()) if wanted else set()

    def head_level(info):
        """0: the HEAD dependency is in a missing cell of a called target; 1: HEAD version
        unknown; 2: only cells older than HEAD are missing (only history can fill them);
        3: every missing cell is newer than HEAD (nothing in the repository can fill it)."""
        level = 3 if info["observed"] else 1
        for target in info["observed"]:
            cells = wanted.get(target, set())
            if info["spec_cells"] & cells:
                return 0
            for mid in {cell.split(":", 1)[0] for cell in cells}:
                heads = [order[cell] for cell in info["spec_cells"]
                         if cell in order and cell.split(":", 1)[0] == mid]
                if not heads:
                    level = min(level, 1)
                elif any(order.get(cell, -1) < max(heads) for cell in cells
                         if cell.split(":", 1)[0] == mid):
                    level = min(level, 2)
        return level

    def priority(info):
        # Calls at HEAD (AST-checked) or added/removed in a commit found by commit search.
        if info["head_calls"] or info.get("history_calls"):
            return (0, 1, 2, 6)[head_level(info)]
        if info["paths"] and info["spec_cells"]:
            return 3
        if info["paths"]:
            return 4
        if info["spec_cells"]:
            return 5
        return 7
    ordered = sorted(candidates, key=lambda item: (
        priority(item[1]),
        # Dependency pinned in a missing cell first; pinned repositories whose batched
        # code search found no target call last.
        not item[1]["spec_cells"] & all_wanted,
        bool(item[1].get("call_searched")) and not item[1]["head_calls"],
        item[0].rsplit("/", 1)[-1].lower().startswith(library.lower()),
        -len(item[1]["observed"]), -item[1]["head_calls"], -item[1].get("history_calls", 0),
        -len(item[1]["paths"]),
        item[1]["repo_size"] if item[1]["repo_size"] is not None else float("inf"),
        item[0]))
    # Forks/mirrors whose call evidence is the same (the same call files at HEAD, i.e. the
    # same blobs, or only the same commit-history call) give the same samples, which
    # sample_key merges anyway; keep the best-ranked one.
    seen, merged = set(), 0
    kept = []
    for item in ordered:
        info = item[1]
        key = None
        blobs = {}
        for entry in info.get("call_blobs", ()):
            path, _, blob = entry.partition("\0")
            if blob:
                blobs[path] = blob
            else:
                blobs.setdefault(path, None)
        if info["head_calls"] and blobs and all(blobs.values()):
            key = ("head", frozenset(blobs.items()), frozenset(info["paths"]),
                   frozenset(info.get("commits", ())))
        elif not info["head_calls"] and info.get("commits"):
            key = ("history", frozenset(info["commits"]), frozenset(info["paths"]))
        if key is not None:
            if key in seen:
                merged += 1
                continue
            seen.add(key)
        kept.append(item)
    ordered = kept
    if merged:
        print(f"Merged {merged} repositories sharing the same call files or commit-history "
              f"call with a higher-ranked repository", flush=True)
    source_only = deque(item for item in ordered if priority(item[1]) == 4)
    dependency_only = deque(item for item in ordered if priority(item[1]) == 5)
    ranked = [item for item in ordered if priority(item[1]) < 4]
    while source_only or dependency_only:
        for _ in range(3):
            if source_only:
                ranked.append(source_only.popleft())
        if dependency_only:
            ranked.append(dependency_only.popleft())
    ranked.extend(item for item in ordered if priority(item[1]) >= 6)
    print(f"Ranked {len(ranked)} repositories: "
          f"{sum(bool(info['head_calls']) for _, info in ranked)} with AST calls, "
          f"{sum(bool(info.get('history_calls')) for _, info in ranked)} with calls in commit history, "
          f"{sum(bool(info['spec_cells']) for _, info in ranked)} with usable version evidence, "
          f"{sum(priority(info) == 0 for _, info in ranked)} matching a missing cell, "
          f"{sum(priority(info) == 2 for _, info in ranked)} with calls only for cells older than HEAD, "
          f"{sum(priority(info) == 6 for _, info in ranked)} with calls for no reachable cell",
          flush=True)
    return ranked


def is_repo_root(folder):
    root = git(folder, "rev-parse", "--show-toplevel", allow_missing=True)
    return bool(root and Path(root.strip()).resolve() == folder.resolve())


def clone_repo(name, cache, deadline=None):
    if Path(name).is_dir():
        folder = Path(name).resolve()
        if not is_repo_root(folder):
            raise RuntimeError(f"Not a complete Git repository: {folder}")
        remote = git(folder, "config", "--get", "remote.origin.url", allow_missing=True) or ""
        match = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?\s*$", remote)
        return folder, match[1] if match else str(folder)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
        raise ValueError(f"Invalid GitHub repository: {name}")
    folder = cache / name.replace("/", "__")
    if folder.exists() and not is_repo_root(folder):
        cache_root = cache.resolve()
        if (folder.is_symlink() or folder.resolve().parent != cache_root or
                {entry.name for entry in folder.iterdir()} != {".git"} or
                not (folder / ".git").is_dir() or (folder / ".git").is_symlink()):
            raise RuntimeError(f"Invalid cache directory; inspect it before retrying: {folder}")
        print(f"  Removing incomplete clone: {folder}", flush=True)
        shutil.rmtree(folder)
    if not folder.exists():
        folder.parent.mkdir(parents=True, exist_ok=True)
        result = run_process(
            ["git", "clone", "--filter=blob:none", "--no-checkout", "--single-branch",
             f"https://github.com/{name}.git", str(folder)], f"cloning {name}",
            max_seconds=time_left(deadline, 300))
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
    else:
        remote = git(folder, "config", "--get", "remote.origin.url", allow_missing=True) or ""
        if not re.search(r"github\.com[:/]" + re.escape(name) + r"(?:\.git)?\s*$", remote, re.I):
            raise RuntimeError(f"Cached repository has a different origin: {folder}")
        git(folder, "fetch", "--quiet", "origin", max_seconds=time_left(deadline, 180))
    if not is_repo_root(folder):
        raise RuntimeError(f"Incomplete Git repository: {folder}")
    return folder, name


def head_revision(repo):
    remote = git(repo, "rev-parse", "refs/remotes/origin/HEAD", allow_missing=True)
    return (remote or git(repo, "rev-parse", "HEAD")).strip()


def parsed_requirement(text, library):
    """(specifier, marker) of a requirement of library, or None."""
    text = text.strip()
    if not text or text.startswith(("#", "-")):
        return None
    try:
        req = Requirement(text)
    except InvalidRequirement:
        return None
    if canonicalize_name(req.name) != canonicalize_name(DISTRIBUTION.get(library, library)) or req.url:
        return None
    return str(req.specifier), req.marker


def requirement_spec(text, library):
    parsed = parsed_requirement(text, library)
    # A marker-specific requirement alone cannot be assigned without the environment.
    if parsed is None or parsed[1] is not None:
        return None
    return parsed[0] or None


PYTHON_GRID = ("2.7", *(f"3.{minor}" for minor in range(5, 15)))
PLATFORM_GRID = (("linux", "Linux", "x86_64", "posix"), ("linux", "Linux", "aarch64", "posix"),
                 ("win32", "Windows", "AMD64", "nt"), ("darwin", "Darwin", "x86_64", "posix"),
                 ("darwin", "Darwin", "arm64", "posix"))


def marker_hull(marked, python=None):
    """One spec covering marker-specific alternatives, or None.

    ``numpy==1.22.0; python_version<="3.10"`` plus ``numpy==1.24.3; python_version>"3.10"``
    gives ``>=1.22.0,<=1.24.3``: every environment of the grid installs a version inside it.
    If some environment matches none of the markers, nothing is known there, so None.
    ``python`` limits the Python versions (a package's Requires-Python).
    """
    allowed = None
    if python:
        try:
            allowed = SpecifierSet(python)
        except InvalidSpecifier:
            allowed = None
    intervals = []
    for version in PYTHON_GRID:
        if allowed is not None and not allowed.contains(version + ".0", prereleases=True):
            continue
        for sys_platform, system, machine, os_name in PLATFORM_GRID:
            environment = {**default_environment(), "python_version": version,
                           "python_full_version": version + ".0", "sys_platform": sys_platform,
                           "platform_system": system, "platform_machine": machine,
                           "os_name": os_name, "implementation_name": "cpython",
                           "platform_python_implementation": "CPython", "extra": ""}
            try:
                applicable = [spec for spec, marker in marked if marker.evaluate(environment)]
            except Exception:  # undefined marker variables; packaging raises several types
                return None
            if not applicable:
                return None
            if not any(applicable):
                return None  # an unversioned requirement applies here
            interval = spec_interval(",".join(spec for spec in applicable if spec))
            if interval is None:
                return None
            if interval != "empty":
                intervals.append(interval)
    if not intervals or any(lower is None for lower, _, _ in intervals):
        lower = None
    else:
        lower = min(lower for lower, _, _ in intervals)
    if not intervals or any(upper is None for _, upper, _ in intervals):
        upper = None
    else:
        upper = max(upper for _, upper, _ in intervals)
        upper_open = all(strict for _, value, strict in intervals if value == upper)
    parts = ([f">={lower}"] if lower is not None else []) + (
        [f"{'<' if upper_open else '<='}{upper}"] if upper is not None else [])
    return ",".join(parts) or None


def unpinned_requirement(text, library):
    """True for a plain requirement of the library without a version or marker."""
    text = text.split(" #", 1)[0].strip()
    if not text or text.startswith(("#", "-")):
        return False
    try:
        req = Requirement(text)
    except InvalidRequirement:
        return False
    return (canonicalize_name(req.name) == canonicalize_name(DISTRIBUTION.get(library, library))
            and not req.url and req.marker is None and not str(req.specifier))


def requirement_lines(value, library, python=None):
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list):
        return []
    specs, marked = [], []
    for line in value:
        if not isinstance(line, str):
            continue
        parsed = parsed_requirement(line.split(" #", 1)[0], library)
        if parsed is None:
            continue
        if parsed[1] is not None:
            marked.append(parsed)
        elif parsed[0]:
            specs.append(parsed[0])
    if marked and (hull := marker_hull(marked, python)):
        specs.append(hull)
    return specs


def dependency_specs(path, content, library):
    """Return only explicit dependency constraints; dynamic declarations are skipped."""
    name = Path(path).name
    try:
        if re.fullmatch(r"(?:requirements[^/]*\.(?:txt|in)|constraints[^/]*\.txt)", name, re.I):
            return requirement_lines(content, library)
        if name in {"pyproject.toml", "tox.toml", "poetry.lock", "uv.lock", "pdm.lock", "pylock.toml", "Pipfile"}:
            doc = tomllib.loads(content)
            if name in {"poetry.lock", "uv.lock", "pdm.lock", "pylock.toml"}:
                return [f"=={item['version']}" for item in
                        [*doc.get("package", []), *doc.get("packages", [])]
                        if canonicalize_name(item.get("name", "")) == canonicalize_name(DISTRIBUTION.get(library, library))
                        and isinstance(item.get("version"), str)]
            if name == "Pipfile":
                result = []
                for group in ("packages", "dev-packages"):
                    for dep, value in doc.get(group, {}).items():
                        if canonicalize_name(dep) == canonicalize_name(DISTRIBUTION.get(library, library)):
                            spec = value if isinstance(value, str) else value.get("version", "")
                            if spec not in ("", "*"):
                                result.append(spec)
                return result
            result = requirement_lines(doc.get("project", {}).get("dependencies", []), library)
            if name == "tox.toml":
                result.extend(requirement_lines(doc.get("env_run_base", {}).get("deps", []), library))
                for group in doc.get("env", {}).values():
                    result.extend(requirement_lines(group.get("deps", []), library))
            for group in doc.get("project", {}).get("optional-dependencies", {}).values():
                result.extend(requirement_lines(group, library))
            for group in doc.get("dependency-groups", {}).values():
                result.extend(requirement_lines(group, library))
            poetry = doc.get("tool", {}).get("poetry", {})
            for group in [poetry.get("dependencies", {}),
                          *(x.get("dependencies", {}) for x in poetry.get("group", {}).values())]:
                for dep, value in group.items():
                    if canonicalize_name(dep) == canonicalize_name(DISTRIBUTION.get(library, library)):
                        spec = value if isinstance(value, str) else value.get("version", "")
                        if spec not in ("", "*"):
                            result.append(spec)
            return result
        if name == "Pipfile.lock":
            doc = json.loads(content)
            return [entry["version"] for group in ("default", "develop")
                    for dep, entry in doc.get(group, {}).items()
                    if canonicalize_name(dep) == canonicalize_name(DISTRIBUTION.get(library, library))
                    and isinstance(entry.get("version"), str)]
        if name in {"conda-lock.yml", "conda-lock.yaml"}:
            doc = yaml.safe_load(content) or {}
            versions = {item["version"] for item in doc.get("package", [])
                        if isinstance(item, dict)
                        and canonicalize_name(item.get("name", "")) == canonicalize_name(DISTRIBUTION.get(library, library))
                        and isinstance(item.get("version"), str)}
            return ["==" + versions.pop()] if len(versions) == 1 else []
        if name == "setup.cfg" or name == "tox.ini":
            config = configparser.ConfigParser(interpolation=None)
            config.read_string(content)
            result = []
            for section in config.sections():
                for key in ("install_requires", "deps"):
                    if config.has_option(section, key):
                        result.extend(requirement_lines(config.get(section, key), library))
            return result
        if name in {"environment.yml", "environment.yaml"}:
            doc = yaml.safe_load(content) or {}
            result = []
            for item in doc.get("dependencies", []):
                if isinstance(item, dict):
                    result.extend(requirement_lines(item.get("pip", []), library))
                elif isinstance(item, str):
                    direct = requirement_spec(item, library)
                    if direct:
                        result.append(direct)
                        continue
                    dep, sep, version = item.partition("=")
                    if canonicalize_name(dep) == canonicalize_name(DISTRIBUTION.get(library, library)) and sep:
                        result.append("==" + version.lstrip("=").split("=", 1)[0])
            return result
        if name == "setup.py" or name == "noxfile.py":
            tree = ast.parse(content)
            result = []
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg in {"install_requires", "extras_require"}:
                    try:
                        value = ast.literal_eval(node.value)
                    except (ValueError, TypeError, SyntaxError, MemoryError):
                        continue
                    groups = value.values() if isinstance(value, dict) else [value]
                    for group in groups:
                        result.extend(requirement_lines(group, library))
                if (name == "noxfile.py" and isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "install"):
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            result.extend(requirement_lines([arg.value], library))
            return result
        if name == "Dockerfile" or path.startswith(".github/workflows/"):
            # Only explicit install commands, not arbitrary mentions in comments.
            commands = re.findall(r"(?:pip|pip3|conda|mamba)\s+install\s+([^\n]+)", content)
            result = []
            for command in commands:
                try:
                    tokens = shlex.split(command, comments=True)
                except ValueError:
                    tokens = command.split()
                result.extend(requirement_lines(tokens, library))
            return result
    except (ValueError, TypeError, AttributeError, KeyError, yaml.YAMLError,
            configparser.Error, SyntaxError):
        return []
    return []


PYPI_CACHE = {"path": None, "data": None, "unsaved": 0}


def save_pypi_cache():
    """Write pending PyPI entries; a failed write only loses cache, never the crawl."""
    if not PYPI_CACHE["unsaved"] or not PYPI_CACHE["path"]:
        return
    try:
        write_json(PYPI_CACHE["path"], PYPI_CACHE["data"])
        PYPI_CACHE["unsaved"] = 0
    except OSError as exc:
        print(f"  PyPI cache not saved yet: {exc}", flush=True)
# pip-installed manifests only: conda builds carry their own (different) dependency metadata.
PIP_MANIFEST = re.compile(r"(?:requirements[^/]*\.(?:txt|in)|setup\.(?:py|cfg)|pyproject\.toml|Pipfile)$",
                          re.I)
PIN_CANDIDATE = re.compile(r"(?<![\w.\-])([A-Za-z][A-Za-z0-9._\-]*)(?:\[[^\]\n]*\])?\s*={1,3}\s*[\"']?v?\d")
INCLUDE_LINE = re.compile(r"^\s*-(r|c|requirement|constraint)\s+([^\s#]+)", re.M)


def pypi_requirement_specs(name, version, library):
    """Constraints on library from the default (non-extra) requirements of name==version."""
    key = f"{canonicalize_name(name)}=={version}"
    if PYPI_CACHE["data"] is None:
        path = PYPI_CACHE["path"]
        try:
            PYPI_CACHE["data"] = read_json(path) if path and path.exists() else {}
        except (OSError, ValueError):
            PYPI_CACHE["data"] = {}
    data = PYPI_CACHE["data"]
    if library in data.get(key, {}):
        return data[key][library]
    request = Request(f"https://pypi.org/pypi/{quote(canonicalize_name(name))}/{quote(version)}/json",
                      headers={"User-Agent": "api-evolution-step9"})
    try:
        with urlopen(request, timeout=30) as response:
            info = json.load(response).get("info", {})
    except HTTPError as exc:
        if exc.code != 404:
            return []
        info = {}
    except (HTTPException, OSError, ValueError):
        return []  # not cached: retried next time
    requires = info.get("requires_dist") or []
    specs = requirement_lines([r for r in requires if isinstance(r, str)], library,
                              info.get("requires_python"))
    data.setdefault(key, {})[library] = specs
    PYPI_CACHE["unsaved"] += 1
    if PYPI_CACHE["unsaved"] >= 25:
        save_pypi_cache()
    return specs


def codependency_specs(path, content, library):
    """Library constraints implied by exactly pinned co-dependencies (pip resolves them together)."""
    if not PIP_MANIFEST.search(Path(path).name):
        return []
    own = canonicalize_name(DISTRIBUTION.get(library, library))
    result = []
    for name in sorted(set(PIN_CANDIDATE.findall(content)))[:200]:
        if canonicalize_name(name) == own:
            continue
        pins = {interval[0] for spec in dependency_specs(path, content, name)
                if isinstance(interval := spec_interval(spec), tuple)
                and interval[0] is not None and interval[0] == interval[1]}
        if len(pins) == 1:
            result.extend(pypi_requirement_specs(name, str(pins.pop()), library))
    return result


def manifest_specs(read, path, library, seen=None, codeps=False, content=None):
    """Specs of library in a manifest; read(path) returns another file of the same commit."""
    seen = set() if seen is None else seen
    if path in seen or Path(path).name.lower().startswith("constraints"):
        return []
    seen.add(path)
    if content is None:
        content = read(path)
    if content is None:
        return []
    specs = dependency_specs(path, content, library)
    if codeps:
        specs.extend(codependency_specs(path, content, library))
    if re.fullmatch(r"requirements[^/]*\.(?:txt|in)", Path(path).name, re.I):
        constraints = []
        for match in INCLUDE_LINE.finditer(content):
            included = posixpath.normpath(posixpath.join(posixpath.dirname(path), match[2]))
            if included.startswith("../") or included.startswith("/"):
                continue
            if match[1] in {"r", "requirement"}:
                specs.extend(manifest_specs(read, included, library, seen, codeps))
            else:
                text = read(included)
                if text:
                    constraints.extend(requirement_lines(text, library))
        if constraints:
            # A constraint file pins the version of an unpinned requirement.
            required = any(unpinned_requirement(line, library) for line in content.splitlines())
            specs = ([base + "," + constraint for base in specs for constraint in constraints]
                     if specs else constraints if required else [])
    return specs


def manifests_for_source(manifests, source):
    parents = {""}
    current = posixpath.dirname(source)
    while current:
        parents.add(current)
        current = posixpath.dirname(current)
    return [path for path in manifests
            if posixpath.dirname(path) in parents or path.startswith(".github/workflows/")]


def spec_clauses(spec):
    """Parse ==, >, >=, <, <=, !=, ~= and ==X.* into clauses; None if unsupported.

    != only removes single versions, so spec_interval ignores it except against an exact pin.
    """
    clauses = re.findall(r"(===|==|>=|<=|>|<|!=|~=)\s*([^,\s]+)", spec)
    if not clauses or ",".join(op + value for op, value in clauses) != re.sub(r"\s+", "", spec):
        return None
    result = []
    try:
        for op, value in clauses:
            if op == "~=" or (op == "==" and value.endswith(".*")):
                # ~=1.21.0 -> >=1.21.0,<1.22 ; ~=1.21 -> >=1.21,<2 ; ==1.21.* -> >=1.21,<1.22
                lower = Version(value[:-2] if op == "==" else value)
                release = lower.release if op == "==" else lower.release[:-1]
                if not release:
                    return None
                upper = Version(".".join(map(str, (*release[:-1], release[-1] + 1))))
                result.extend(((">=", lower), ("<", upper)))
            elif op in {"==", ">", ">=", "<", "<=", "!="} and "*" not in value:
                result.append((op, Version(value)))
            elif op == "!=":
                continue  # !=1.22.* only removes versions from the interval
            else:
                return None
    except ValueError:
        return None
    return result


def spec_interval(spec):
    """Return (lower, upper, upper_strict), "empty" for contradictory clauses, or None."""
    clauses = spec_clauses(spec)
    if clauses is None:
        return None
    exact = {value for op, value in clauses if op == "=="}
    if len(exact) > 1:
        return "empty"
    if exact:
        candidate = exact.pop()
        if not all(candidate > value if op == ">" else
                   candidate >= value if op == ">=" else
                   candidate < value if op == "<" else
                   candidate <= value if op == "<=" else
                   candidate != value if op == "!=" else True
                   for op, value in clauses):
            return "empty"
        return candidate, candidate, False
    lowers = [(value, op == ">") for op, value in clauses if op in {">", ">="}]
    uppers = [(value, op == "<") for op, value in clauses if op in {"<", "<="}]
    lower = max((value for value, _ in lowers), default=None)
    lower_open = any(strict for value, strict in lowers if value == lower)
    upper = min((value for value, _ in uppers), default=None)
    upper_open = any(strict for value, strict in uppers if value == upper)
    if (lower is not None and upper is not None and
            (upper < lower or upper == lower and (lower_open or upper_open))):
        return "empty"
    if lower is not None and lower == upper and ("!=", lower) in clauses:
        return "empty"  # >=X,<=X,!=X
    return lower, upper, upper_open


def containing_milestone(spec, versions):
    """A spec must fit wholly within one half-open milestone interval."""
    interval = spec_interval(spec)
    if not isinstance(interval, tuple) or interval[0] is None:
        return None
    lower, upper, upper_open = interval
    try:
        milestones = [Version(value) for value in versions]
    except ValueError:
        return None
    for i, start in enumerate(milestones):
        end = milestones[i + 1] if i + 1 < len(milestones) else None
        if lower < start:
            continue
        if end is None or (upper is not None and
                           (upper < end or upper == end and upper_open)):
            return i
    return None


def split_parameters(signature):
    """Extract parameter names/defaults from Python and PyTorch C-style signatures."""
    left = signature.find("(")
    if left < 0:
        return []
    depth, quote_char, escaped, parts, start = 0, None, False, [], left + 1
    for i in range(left + 1, len(signature)):
        char = signature[i]
        if quote_char:
            if char == quote_char and not escaped:
                quote_char = None
            escaped = char == "\\" and not escaped
        elif char in "'\"":
            quote_char = char
        elif char in "[{(":
            depth += 1
        elif char in "]})":
            if char == ")" and depth == 0:
                parts.append(signature[start:i].strip())
                break
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(signature[start:i].strip())
            start = i + 1
    return parts


def signature_matches(signature, call, target):
    parts = split_parameters(signature)
    if parts == [""]:
        return not call.args and not call.keywords
    if not parts:
        return False
    positional_only = "/" in parts
    keyword_only = False
    params = []
    for part in parts:
        if part == "/":
            positional_only = False
            continue
        if part == "*":
            keyword_only = True
            continue
        if not part:
            continue
        prefix = "**" if part.startswith("**") else "*" if part.startswith("*") else ""
        raw = part[len(prefix):]
        name = re.split(r"[:=]", raw, maxsplit=1)[0].strip()
        if not name.isidentifier():
            return False
        default = object() if "=" in raw else inspect.Parameter.empty
        kind = (inspect.Parameter.VAR_KEYWORD if prefix == "**" else
                inspect.Parameter.VAR_POSITIONAL if prefix == "*" else
                inspect.Parameter.KEYWORD_ONLY if keyword_only else
                inspect.Parameter.POSITIONAL_ONLY if positional_only else
                inspect.Parameter.POSITIONAL_OR_KEYWORD)
        try:
            params.append(inspect.Parameter(name, kind, default=default))
        except ValueError:
            return False
        if prefix == "*":
            keyword_only = True
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(
            keyword.arg is None for keyword in call.keywords):
        return False
    # Bound class methods hide self/cls from the call site.
    if any(segment[:1].isupper() for segment in target.split(".")[:-1]) and params:
        if params[0].name in {"self", "cls"}:
            params.pop(0)
    try:
        inspect.Signature(params).bind(*([None] * len(call.args)),
                                       **{item.arg: None for item in call.keywords})
        return True
    except (ValueError, TypeError):
        return False


def call_matches(state, node, target):
    if state["status"] not in VALID:
        return False
    if isinstance(node, ast.Subscript):
        syntax = state.get("call_syntax") or {}
        return syntax.get("kind") == "indexer"
    if not isinstance(node, ast.Call):
        return False
    if any(signature_matches(sig, node, target) for sig in state.get("signatures", [])):
        return True
    syntax = state.get("call_syntax") or {}
    return (syntax.get("kind") == "ufunc" and
            len(node.args) == syntax.get("required_inputs") and
            not any(isinstance(arg, ast.Starred) for arg in node.args) and
            all(kw.arg == "out" for kw in node.keywords))


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def import_aliases(nodes, stars=None):
    """Imports of a block, including those nested in try/if/with; star modules go to stars."""
    result = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for item in node.names:
                result[item.asname or item.name] = item.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for item in node.names:
                if item.name != "*":
                    result[item.asname or item.name] = node.module + "." + item.name
                elif stars is not None:
                    stars.append(node.module)
        elif isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith)) or \
                type(node).__name__ == "TryStar":
            for field in ("body", "orelse", "finalbody"):
                result.update(import_aliases(getattr(node, field, []), stars))
            for handler in getattr(node, "handlers", []):
                result.update(import_aliases(handler.body, stars))
    return result


def resolve(name, aliases):
    if not name:
        return None
    root, _, rest = name.partition(".")
    resolved = aliases.get(root, root) + ("." + rest if rest else "")
    if resolved == "pandas.DataFrame.style":
        return "pandas.io.formats.style.Styler"
    if resolved.startswith("pandas.DataFrame.style."):
        return resolved.replace("pandas.DataFrame.style.",
                                "pandas.io.formats.style.Styler.", 1)
    return resolved


def inferred_type(name):
    """Type of the object a call returns, for calls whose result type is fixed."""
    if name and re.fullmatch(r"pandas\.read_(?:csv|table|excel|json|parquet|feather|sql|sql_query|sql_table)", name):
        return "pandas.DataFrame"
    # The constructor and its alternative constructors always build a DataFrame.
    if name in {"pandas.DataFrame", "pandas.DataFrame.from_dict", "pandas.DataFrame.from_records"}:
        return "pandas.DataFrame"
    return None


DATAFRAME = "pandas.DataFrame"
# DataFrame methods that always return a DataFrame (None only with inplace=True).
DATAFRAME_METHODS = frozenset((
    "abs", "add_prefix", "add_suffix", "applymap", "assign", "astype", "bfill", "clip", "copy",
    "corr", "drop", "drop_duplicates", "dropna", "ffill", "fillna", "head", "isna", "isnull",
    "map", "mask", "merge", "notna", "notnull", "reindex", "rename", "replace", "reset_index",
    "round", "select_dtypes", "set_index", "sort_index", "sort_values", "tail", "transpose",
    "where"))


def returned_type(call, aliases):
    """Type a call surely returns: constructors/readers, or a DataFrame method on a DataFrame."""
    owner = inferred_type(resolve(dotted(call.func), aliases))
    if owner:
        return owner
    func = call.func
    if (isinstance(func, ast.Attribute) and func.attr in DATAFRAME_METHODS and
            receiver_type(func.value, aliases) == DATAFRAME and
            not any(item.arg is None or (item.arg == "inplace" and not (
                isinstance(item.value, ast.Constant) and item.value.value is False))
                for item in call.keywords)):
        return DATAFRAME
    return None


def receiver_type(expression, aliases):
    """pandas.DataFrame when the expression surely evaluates to a DataFrame, else None.

    pd.read_csv(p), pd.DataFrame(d).T.fillna(""), df.reset_index() for a DataFrame df.
    """
    if isinstance(expression, ast.Call):
        return returned_type(expression, aliases)
    if isinstance(expression, ast.Attribute) and expression.attr == "T":
        return DATAFRAME if receiver_type(expression.value, aliases) == DATAFRAME else None
    name = dotted(expression)
    return DATAFRAME if name and resolve(name, aliases) == DATAFRAME else None


def call_name(node, aliases):
    """Canonical name of a Call/Subscript target (df.map -> pandas.DataFrame.map)."""
    expression = node.func if isinstance(node, ast.Call) else node.value
    name = dotted(expression)
    canonical = resolve(name, aliases)
    if isinstance(expression, ast.Attribute):
        # pd.DataFrame(data).map(f): the receiver is the object the expression returns.
        owner = receiver_type(expression.value, aliases)
        if owner:
            canonical = f"{owner}.{expression.attr}"
    return name, canonical


def owned_nodes(function):
    """Nodes of a function body, without nested functions and classes."""
    pending = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


def local_types(function, inherited, imports, parameters):
    """Aliases inside a function, with the types of local names that hold one type everywhere.

    A name is typed by an annotation or by an assignment (df = pd.read_csv(p), also inside
    if/try/with; df = df.fillna(0)). It keeps the type only when every binding of it in the
    function (assignments, loop targets, imports; a parameter counts as one) is typed;
    otherwise (df = df["a"], for df in ...) it may hold another type, e.g. a Series.
    """
    stores = Counter(item.id for item in ast.walk(function)
                     if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store))
    stores.update(arg.arg for arg in parameters)
    stores.update(list(imports))
    # A name bound in the function is local: it does not keep an enclosing scope's alias.
    base = {key: value for key, value in inherited.items() if key not in stores}
    base.update(imports)
    assignments = sorted((item for item in owned_nodes(function)
                          if isinstance(item, (ast.Assign, ast.AnnAssign))),
                         key=lambda item: (item.lineno, item.col_offset))
    unsafe = set()
    while True:
        current = {key: value for key, value in base.items() if key not in unsafe}
        typed = Counter()
        for arg in parameters:
            if arg.annotation and arg.arg not in unsafe:
                annotation = resolve(dotted(arg.annotation), current)
                if annotation:
                    current[arg.arg] = annotation
                    typed[arg.arg] += 1
        for item in assignments:
            value = item.value
            if isinstance(value, ast.Call):
                inferred = returned_type(value, current)
            else:
                inferred = resolve(dotted(value), current) if value is not None else None
            if not inferred:
                continue
            for target in (item.targets if isinstance(item, ast.Assign) else [item.target]):
                if isinstance(target, ast.Name) and target.id not in unsafe:
                    current[target.id] = inferred
                    typed[target.id] += 1
        found = {name for name, count in typed.items() if stores[name] > count} - unsafe
        if not found:
            return current
        unsafe |= found


def source_calls(content, targets, rebinds=None):
    """Yield one call and its enclosing function; unresolved receiver types are skipped.

    rebinds maps module-level names to what a repository module re-exports
    (see reexport_rebinds), e.g. {"np": "numpy"}.
    """
    try:
        tree = ast.parse(content)
    except (SyntaxError, UnicodeError, ValueError):
        # ValueError: null bytes (binary or UTF-16 files named *.py) before Python 3.12.
        return
    stars = []
    module_aliases = import_aliases(tree.body, stars)
    module_aliases.update(rebinds or {})
    # Module names defined locally hide names from ``from module import *``.
    module_defined = {node.name for node in tree.body
                      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    module_defined.update(target.id for node in tree.body if isinstance(node, ast.Assign)
                          for target in node.targets if isinstance(target, ast.Name))
    lines = content.splitlines(keepends=True)
    functions = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []
            self.alias_stack = [module_aliases]

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            for child in node.body:
                self.visit(child)
            self.stack.pop()

        def visit_FunctionDef(self, node):
            self.function(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def function(self, node):
            self.stack.append(node.name)
            parameters = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            aliases = local_types(node, self.alias_stack[-1], import_aliases(node.body), parameters)
            self.alias_stack.append(aliases)
            local_names = {arg.arg for arg in parameters}
            local_names.update(item.id for item in ast.walk(node)
                               if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store))
            functions.append((node, ".".join(self.stack), aliases, local_names))
            for child in node.body:
                self.visit(child)
            self.alias_stack.pop()
            self.stack.pop()

    Visitor().visit(tree)

    for function, qualname, aliases, local_names in functions:
        source = "".join(lines[function.lineno - 1:function.end_lineno])
        for node in owned_nodes(function):
            if not isinstance(node, (ast.Call, ast.Subscript)):
                continue
            name, canonical = call_name(node, aliases)
            call_aliases = aliases
            if canonical not in targets and name and stars:
                root = name.split(".", 1)[0]
                if root not in aliases and root not in local_names and root not in module_defined:
                    module = next((module for module in reversed(stars)
                                   if f"{module}.{name}" in targets), None)
                    if module:
                        canonical = f"{module}.{name}"
                        call_aliases = {**aliases, root: f"{module}.{root}"}
            if canonical not in targets:
                continue
            snippet = ast.get_source_segment(content, node)
            if not snippet:
                continue
            prefix = "".join(lines[function.lineno - 1:node.lineno - 1])
            if not source.startswith(prefix):
                continue
            yield qualname, source, prefix, snippet, node, call_aliases, canonical, node.lineno


def pair_counterparts(timelines):
    """{api: the other API of every migration pair that contains it}."""
    result = defaultdict(set)
    for timeline in timelines:
        old, new = timeline["seed"]["deprecated_api"], timeline["seed"]["replacement_api"]
        result[old].add(new)
        result[new].add(old)
    return dict(result)


def migration_calls(content, targets, counterparts, rebinds=None):
    """(calls to targets, {(qualname, function source): pair APIs that function calls}).

    The map lets callers drop calls in version shims such as
    `df.map(f) if hasattr(df, "map") else df.applymap(f)`: a function that calls both
    APIs of a pair is written for both versions, so it is neither outdated nor up-to-date.
    """
    others = {other for target in targets for other in counterparts.get(target, ())}
    calls = list(source_calls(content, set(targets) | others, rebinds))
    used = defaultdict(set)
    for call in calls:
        used[(call[0], call[1])].add(call[6])
    return [call for call in calls if call[6] in targets], dict(used)


def calls_counterpart(call, used, other):
    return other in used.get((call[0], call[1]), ())


def single_version_calls(calls, used, counterparts):
    """Calls that are not in a shim for at least one pair of their API (for ranking)."""
    return [call for call in calls
            if not counterparts.get(call[6]) or
            any(not calls_counterpart(call, used, other) for other in counterparts[call[6]])]


def sample_calls_api(sample, api):
    """Whether a saved sample's function also calls api (resolved with its alias dict)."""
    try:
        tree = ast.parse(textwrap.dedent(sample.get("function", "")))
    except (SyntaxError, ValueError):
        return False
    function = next((node for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    if function is None:
        return False
    aliases = sample.get("alias dict") or {}
    return any(call_name(node, aliases)[1] == api for node in owned_nodes(function)
               if isinstance(node, (ast.Call, ast.Subscript)))


def from_imports(nodes):
    """(level, module, name, local name) of `from ... import name` in a block (try/if/with too)."""
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            for item in node.names:
                if item.name != "*":
                    yield node.level, node.module, item.name, item.asname or item.name
        elif isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith)) or \
                type(node).__name__ == "TryStar":
            for field in ("body", "orelse", "finalbody"):
                yield from from_imports(getattr(node, field, []))
            for handler in getattr(node, "handlers", []):
                yield from from_imports(handler.body)


def reexport_candidates(content, path, targets):
    """Module-level `from <module> import X` where X is the receiver of a target-named call.

    Returns [(local name, imported name, candidate module files)] for imports that do not
    already resolve to a target library, e.g. `from pint.compat import np` used as
    `np.cumproduct(...)`. Absolute modules are looked up under every parent directory
    of the file (repositories often keep packages below a subfolder).
    """
    leaves = {target.rsplit(".", 1)[-1] for target in targets}
    roots = {target.split(".", 1)[0] for target in targets}
    if not any(leaf in content for leaf in leaves):
        return []
    try:
        tree = ast.parse(content)
    except (SyntaxError, UnicodeError, ValueError):
        return []
    receivers = set()
    for node in ast.walk(tree):
        name = dotted(node.func) if isinstance(node, ast.Call) else None
        if name and "." in name and name.rsplit(".", 1)[1] in leaves:
            receivers.add(name.split(".", 1)[0])
    direct = import_aliases(tree.body)
    folder = posixpath.dirname(path)
    parents, current = [], folder
    while True:
        parents.append(current)
        if not current:
            break
        current = posixpath.dirname(current)
    result = []
    for level, module, name, local in from_imports(tree.body):
        if local not in receivers or direct.get(local, "").split(".", 1)[0] in roots:
            continue
        if level:  # from .compat import np / from .. import np
            base = folder
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            if not module:
                result.append((local, name, [posixpath.join(base, "__init__.py")]))
                continue
            stems = [posixpath.join(base, *module.split("."))]
        elif module:
            stems = [posixpath.join(parent, *module.split(".")) for parent in parents]
        else:
            continue
        files = [file for stem in stems for file in (f"{stem}.py", posixpath.join(stem, "__init__.py"))]
        result.append((local, name, files))
    return result


def reexport_rebinds(candidates, targets, read):
    """{local name: library name} when the imported module itself imports the library.

    One level only: pint/compat.py doing `import numpy as np` makes
    `from pint.compat import np` a numpy alias. read(path) returns file text or None.
    """
    roots = {target.split(".", 1)[0] for target in targets}
    rebinds = {}
    for local, name, files in candidates:
        for file in files:
            text = read(file)
            if text is None:
                continue
            try:
                aliases = import_aliases(ast.parse(text).body)
            except (SyntaxError, UnicodeError, ValueError):
                break
            real = aliases.get(name)
            if real and real.split(".", 1)[0] in roots:
                rebinds[local] = real
            break  # the first existing file is the module Python would import
    return rebinds


def repo_files(repo, commit):
    return git(repo, "ls-tree", "-r", "--name-only", commit).splitlines()


def candidate_paths(repo, commit, targets, limit, full_scan=False, deadline=None,
                    python_files=None, scan_seconds=60, keep=None):
    if not full_scan:
        if python_files is None:
            python_files = sum(path.endswith(".py") for path in repo_files(repo, commit))
        if python_files > 200:
            print(f"  Skipping full scan of {python_files} Python files; using GitHub search paths",
                  flush=True)
            return []
    prefetch_tree_blobs(repo, commit, lambda path: path.endswith(".py") and
                        (keep is None or keep(path)), deadline)
    args = ["grep", "-l", "-F"]
    for leaf in sorted({target.rsplit(".", 1)[-1] for target in targets}):
        args.extend(("-e", leaf))
    result = run_process(["git", "-C", str(repo), *args, commit, "--", "*.py",
                          *VENDOR_PATHSPECS],
                         "searching repository files",
                         max_seconds=time_left(deadline, 180 if full_scan else scan_seconds))
    if result.returncode == 1:
        return []
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    paths = []
    for line in result.stdout.splitlines():
        path = line.split(":", 1)[1]
        if keep is not None and not keep(path):
            continue
        paths.append(path)
        if len(paths) >= limit:
            break
    return paths


def history_candidate_paths(repo, tip, targets, limit, deadline=None, scan_seconds=45,
                            status=None, keep=None):
    pattern = "|".join(re.escape(target.rsplit(".", 1)[-1]) for target in targets)
    prefetch_blobs(repo, tip, ["*.py", *VENDOR_PATHSPECS], deadline=deadline)
    try:
        paths = git(repo, "log", tip, f"-G{pattern}", "--name-only", "--format=",
                    "--", "*.py", *VENDOR_PATHSPECS,
                    max_seconds=time_left(deadline, scan_seconds)).splitlines()
    except RuntimeError as exc:
        if status is not None:
            status["complete"] = False
            status["preparation_complete"] = False
        print(f"  Historical file search stopped: {exc}", flush=True)
        return []
    return list(dict.fromkeys(path for path in paths if path.endswith(".py")
                              and (keep is None or keep(path))))[:limit]


def historical_manifests(repo, tip, limit, deadline=None, status=None):
    cap = ["-n", str(limit)] if limit else []
    patterns = [
        ":(glob)**/requirements*.txt", ":(glob)**/requirements*.in",
        ":(glob)**/constraints*.txt", ":(glob)**/pyproject.toml",
        ":(glob)**/setup.py", ":(glob)**/setup.cfg",
        ":(glob)**/Pipfile", ":(glob)**/Pipfile.lock",
        ":(glob)**/*.lock", ":(glob)**/pylock.toml",
        ":(glob)**/conda-lock.y*ml", ":(glob)**/environment.y*ml",
        ":(glob)**/tox.ini", ":(glob)**/tox.toml",
        ":(glob)**/noxfile.py", ":(glob)**/Dockerfile",
        ":(glob).github/workflows/*.yml", ":(glob).github/workflows/*.yaml",
        *VENDOR_PATHSPECS,
    ]
    try:
        paths = git(repo, "log", tip, "--name-only", "--format=", *cap,
                    "--", *patterns, max_seconds=time_left(deadline, 30)).splitlines()
    except RuntimeError as exc:
        if status is not None:
            status["complete"] = False
            status["preparation_complete"] = False
        print(f"  Historical dependency search stopped: {exc}", flush=True)
        return []
    return list(dict.fromkeys(path for path in paths if DEPENDENCY_NAME.search(path)))


def repo_commits(repo, tip, source_paths, manifests, targets, limit, deadline=None,
                 head_has_call=False, status=None):
    if not source_paths:
        return []
    cap = ["-n", str(limit)] if limit else []
    pattern = "|".join(re.escape(target.rsplit(".", 1)[-1]) for target in targets)
    focused = []
    parents = {}
    def parse_commits(lines):
        commits = []
        for line in lines:
            parts = line.split()
            if parts:
                commits.append(parts[0])
                parents[parts[0]] = parts[1:]
        return commits
    focused_complete = True
    try:
        focused = parse_commits(git(repo, "log", tip, f"-G{pattern}", "--format=%H %P", *cap,
                                    "--", *source_paths,
                                    max_seconds=time_left(deadline, 45)).splitlines())
        if not focused:
            leaf = next(iter(sorted(targets))).rsplit(".", 1)[-1]
            focused = parse_commits(git(repo, "log", tip, f"-S{leaf}", "--format=%H %P", *cap,
                                        "--", *source_paths,
                                        max_seconds=time_left(deadline, 30)).splitlines())
    except RuntimeError as exc:
        focused_complete = False
        if status is not None:
            status["complete"] = False
            status["preparation_complete"] = False
        print(f"  Focused history search stopped: {exc}", flush=True)
    if focused_complete and not focused and not head_has_call:
        print("  No API call at HEAD or API change in file history", flush=True)
        return []
    dependency = (parse_commits(git(repo, "log", tip, "--format=%H %P", *cap, "--", *manifests,
                                    max_seconds=time_left(deadline, 60)).splitlines())
                  if manifests else [])
    changed = (git(repo, "log", tip, "--format=%H", *cap, "--", *source_paths,
                   max_seconds=time_left(deadline, 60)).splitlines()
               if head_has_call else [])
    fallback = changed[:20]
    ordered = []
    for commit in [*focused[:20], *dependency[:20], *focused[20:],
                   *dependency[20:], *fallback]:
        if commit not in ordered:
            ordered.append(commit)
        if commit in focused or commit in dependency:
            if parents.get(commit) and parents[commit][0] not in ordered:
                ordered.append(parents[commit][0])
        if limit and len(ordered) >= limit:
            break
    return ordered[:limit] if limit else ordered


def normalized_code(text):
    """Code identity that ignores formatting and comments."""
    try:
        return ast.dump(ast.parse(textwrap.dedent(text)))
    except (SyntaxError, ValueError):
        return " ".join(text.split())


def sample_key(sample):
    """Same code for the same API pair, wherever (repository, commit) it was found."""
    return (normalized_code(sample["function"]), normalized_code(sample["api_call"]),
            tuple(sample["deprecated api"]), sample["replacement api"])


def near_key(sample, version):
    """Revisions of one function with the same call count once per milestone."""
    source = sample.get("source", "")
    origin = re.sub(r"/blob/[0-9a-f]{40}/", "/blob/", source).split("#", 1)[0]
    origin = re.sub(r"@[0-9a-f]{40}:", ":", origin)
    name = re.search(r"^\s*(?:async\s+)?def\s+(\w+)", sample["function"], re.M)
    return (origin, name[1] if name else "", normalized_code(sample["api_call"]),
            tuple(sample["deprecated api"]), sample["replacement api"], version)


def process_repo(repo, *args, shared=None, **kwargs):
    """See _process_repo; shared holds blob-id caches reused across repositories (mirrors)."""
    reader = BlobReader(repo)
    try:
        return _process_repo(repo, *args, reader=reader,
                             shared=shared if shared is not None else {}, **kwargs)
    finally:
        reader.close()


def _process_repo(repo, name, source_paths, known_manifests, timelines, library,
                  max_commits, deadline, samples, existing, need, near_seen,
                  status=None, resume=None, reader=None, shared=None, loose=None):
    """need maps (mapping_id, kind, version) to the samples still missing in that cell.

    loose (see new_loose) collects calls whose version the dependencies do not fix; they
    are kept without versions and never count toward a cell.
    """
    stats = Counter()
    head = head_revision(repo)
    by_target = defaultdict(list)
    for timeline in timelines:
        for kind, target in (("O", timeline["seed"]["deprecated_api"]),
                             ("U", timeline["seed"]["replacement_api"])):
            if any(count > 0 for (mid, cell_kind, _), count in need.items()
                   if mid == timeline["mapping_id"] and cell_kind == kind):
                by_target[target].append((kind, timeline))
    targets = set(by_target)
    targets_key = tuple(sorted(targets))
    counterparts = pair_counterparts(timelines)
    roots = library_roots()
    # Keyed by blob id, so identical files in other commits or mirrored repositories reuse
    # the parse; a requirements file with -r/-c includes is keyed by commit instead.
    ast_cache = shared.setdefault("ast", {})
    manifest_cache = shared.setdefault("manifest", {})
    validation_cache = {}
    eligible_unique = set()

    def source_snapshot(commit, path):
        """(blob id, target calls, {function: pair APIs it calls}) of one file at a commit."""
        oid, content = reader.read(commit, path, deadline)
        if oid is None:
            return None, [], {}
        key = (oid, targets_key)
        if key not in ast_cache:
            ast_cache[key] = migration_calls(content, targets, counterparts)
        else:
            stats["ast_reused"] += 1
        calls, used = ast_cache[key]
        if not used:
            # `from pint.compat import np`: follow one in-repository re-export.
            found = (oid, path, targets_key, "reexports")
            if found not in ast_cache:
                ast_cache[found] = reexport_candidates(content, path, targets)
            if ast_cache[found]:
                rebinds = reexport_rebinds(ast_cache[found], targets,
                                           lambda file: reader.read(commit, file, deadline)[1])
                if rebinds:
                    rebound = (oid, targets_key, tuple(sorted(rebinds.items())))
                    if rebound not in ast_cache:
                        ast_cache[rebound] = migration_calls(content, targets, counterparts, rebinds)
                    calls, used = ast_cache[rebound]
                    stats["reexport_calls"] += len(calls)
        return oid, calls, used

    def commit_specs(commit, nearby, codeps):
        specs = []
        for manifest in nearby:
            oid, content = reader.read(commit, manifest, deadline)
            if oid is None:
                continue
            includes = (re.fullmatch(r"requirements[^/]*\.(?:txt|in)", Path(manifest).name, re.I)
                        and INCLUDE_LINE.search(content))
            key = (manifest, commit if includes else oid, codeps, library)
            if key not in manifest_cache:
                manifest_cache[key] = manifest_specs(
                    lambda path: reader.read(commit, path, deadline)[1],
                    manifest, library, codeps=codeps, content=content)
            specs.extend(manifest_cache[key])
        return sorted({spec for spec in specs if spec_interval(spec) is not None})

    def assignable(specs):
        combined = ",".join(specs)
        return bool(specs) and any(
            containing_milestone(combined, [m["version"] for m in timeline["milestones"]]) is not None
            for timeline in timelines)

    if resume:
        manifests = resume["manifests"]
        commits = resume["commits"]
        start_index = resume["next_index"]
        print(f"  Resuming commit {start_index + 1}/{len(commits)}", flush=True)
    else:
        tracked = set(repo_files(repo, head))

        def relevant(path):
            # Only manifests that manifests_for_source() can pick for a source file matter.
            return (not vendored(path, roots, tracked) and
                    any(manifests_for_source([path], source) for source in source_paths))

        manifests = sorted(p for p in set(known_manifests) |
                           {p for p in tracked if DEPENDENCY_NAME.search(p)} if relevant(p))
        prefetch_blobs(repo, head, [*source_paths, *manifests], max_commits, deadline)
        head_has_call = False
        for path in source_paths:
            try:
                _, calls, _ = source_snapshot(head, path)
                head_has_call |= bool(calls)
            except TimeLimit:
                raise
            except RuntimeError as exc:
                if status is not None:
                    status["complete"] = False
                    status["preparation_complete"] = False
                head_has_call = True
                print(f"  Cannot inspect HEAD {path}: {exc}", flush=True)
        if not manifests:
            print("  No dependency files at HEAD; checking calls without version evidence", flush=True)
        at_head = set(manifests)
        manifests = sorted(path for path in
                           at_head | set(historical_manifests(repo, head, max_commits,
                                                              deadline, status))
                           if relevant(path))
        prefetch_blobs(repo, head, [p for p in manifests if p not in at_head],
                       max_commits, deadline)
        print("  Reading commit history", flush=True)
        commits = repo_commits(repo, head, source_paths, manifests, targets, max_commits,
                               deadline, head_has_call, status)
        start_index = 0
    print(f"  Checking commits {start_index + 1}-{len(commits)}/{len(commits)}", flush=True)
    if status is not None:
        status["resume"] = {"paths": source_paths, "manifests": manifests,
                            "commits": commits, "next_index": start_index,
                            "preparation_complete": status.get("preparation_complete", True)}
    old_targets = {timeline["seed"]["deprecated_api"] for timeline in timelines}
    new_targets = {timeline["seed"]["replacement_api"] for timeline in timelines}
    last_progress = time.monotonic()
    distinct_calls = set()
    reason_keys = ("no_manifest", "no_version_spec", "version_conflict",
                   "version_not_single_interval", "both_apis", "api_unavailable",
                   "signature_mismatch", "eligible", "old_eligible", "new_eligible")
    for index in range(start_index, len(commits)):
        number, commit = index + 1, commits[index]
        if time.monotonic() >= deadline:
            if status is not None:
                status["complete"] = False
            print(f"  Repository time limit reached after {number - 1} commits", flush=True)
            break
        if not any(count > 0 for count in need.values()):
            print("  All missing cells filled", flush=True)
            break
        if time.monotonic() - last_progress >= 20:
            print(f"  Commits {number - 1}/{len(commits)}; eligible={stats['eligible']}", flush=True)
            last_progress = time.monotonic()
        failed_commit = False
        for path in source_paths:
            try:
                oid, calls, used = source_snapshot(commit, path)
                stats["calls"] += len(calls)
                stats["old_calls"] += sum(target in old_targets for *_, target, _ in calls)
                stats["new_calls"] += sum(target in new_targets for *_, target, _ in calls)
                distinct_calls.update((path, qualname, function, call, target)
                                      for qualname, function, _, call, _, _, target, _ in calls)
                if not calls:
                    continue
                nearby_manifests = manifests_for_source(manifests, path)
                specs = commit_specs(commit, nearby_manifests, False)
                if nearby_manifests and not assignable(specs):
                    # Pinned co-dependencies narrow the library version that pip can install.
                    widened = commit_specs(commit, nearby_manifests, True)
                    if assignable(widened):
                        stats["codependency_calls"] += len(calls)
                    specs = widened
            except TimeLimit:
                if status is not None:
                    status["complete"] = False
                print(f"  Repository time limit reached after {number - 1} commits", flush=True)
                failed_commit = True
                break
            except RuntimeError as exc:
                if status is not None:
                    status["complete"] = False
                stats["read_error"] += 1
                print(f"Skipped {name}@{commit[:12]}:{path}: {exc}")
                failed_commit = True
                break
            validation_key = (path, oid, bool(nearby_manifests), tuple(specs))
            if validation_key in validation_cache:
                outcome = validation_cache[validation_key]
                stats.update(outcome)
                stats["duplicate"] += outcome["eligible"]
                stats["reused_validations"] += len(calls)
                continue
            before_validation = {key: stats[key] for key in reason_keys}

            def remember():
                validation_cache[validation_key] = Counter(
                    {key: stats[key] - before_validation[key] for key in reason_keys})

            def make_item(entry, kind, timeline, sample_versions):
                _, function, probe, call, _, aliases, target, line = entry
                seed = timeline["seed"]
                return {
                    "function": function,
                    "source": (f"https://github.com/{name}/blob/{commit}/{quote(path)}#L{line}"
                               if "/" in name and not Path(name).is_dir()
                               else f"{name}@{commit}:{path}#L{line}"),
                    "reference dict": {},
                    "alias dict": {k: v for k, v in aliases.items() if k != v},
                    "probing input": probe,
                    "deprecated api": [seed["deprecated_api"]],
                    "replacement api": seed["replacement_api"],
                    "expected call": target,
                    "category": "outdated" if kind == "O" else "up-to-dated",
                    "api_call": call,
                    "versions": sample_versions,
                }

            def counterpart_called(entry, timeline):
                seed = timeline["seed"]
                other = (seed["replacement_api"] if entry[6] == seed["deprecated_api"]
                         else seed["deprecated_api"])
                return calls_counterpart(entry, used, other)

            def keep_loose(entry, kind, timeline):
                """A call whose version the dependencies do not fix: kept without versions
                when it matches the API signature at some milestone where the API exists."""
                if loose is None:
                    return
                mid, target, node = timeline["mapping_id"], entry[6], entry[4]
                if loose["room"][(mid, kind)] <= 0:
                    return
                if not any(state["name"] == target and call_matches(state, node, target)
                           for milestone in timeline["milestones"]
                           for state in milestone["api_states"]):
                    return
                item = make_item(entry, kind, timeline, [])
                key, near = sample_key(item), near_key(item, None)
                if (key in existing[kind] or key in samples[kind] or key in loose["samples"][kind]
                        or near in loose["dependency_near"][kind] or near in loose["near"][kind]):
                    return
                loose["samples"][kind][key] = item
                loose["near"][kind][near] = key
                loose["room"][(mid, kind)] -= 1
                loose["added"][kind] += 1
                stats[f"not_dependency_{kind}"] += 1

            def keep_all_loose():
                for entry in calls:
                    for kind, timeline in by_target[entry[6]]:
                        if not counterpart_called(entry, timeline):
                            keep_loose(entry, kind, timeline)

            if not nearby_manifests:
                stats["no_manifest"] += len(calls)
                keep_all_loose()
                remember()
                continue
            if not specs:
                stats["no_version_spec"] += len(calls)
                keep_all_loose()
                remember()
                continue
            # All constraints that apply to the file must hold together.
            combined = ",".join(specs)
            if spec_interval(combined) == "empty":
                stats["version_conflict"] += len(calls)
                keep_all_loose()
                remember()
                continue
            for entry in calls:
                qualname, function, probe, call, node, aliases, target, line = entry
                for kind, timeline in by_target[target]:
                    if counterpart_called(entry, timeline):
                        stats["both_apis"] += 1
                        continue
                    milestones = timeline["milestones"]
                    versions = [m["version"] for m in milestones]
                    i = containing_milestone(combined, versions)
                    if i is None:
                        stats["version_not_single_interval"] += 1
                        keep_loose(entry, kind, timeline)
                        continue
                    state = next((s for s in milestones[i]["api_states"] if s["name"] == target), None)
                    if state is None or state["status"] not in VALID:
                        stats["api_unavailable"] += 1
                        continue
                    if not call_matches(state, node, target):
                        stats["signature_mismatch"] += 1
                        continue
                    stats["eligible"] += 1
                    stats["old_eligible" if kind == "O" else "new_eligible"] += 1
                    item = make_item(entry, kind, timeline, [versions[i]])
                    key = sample_key(item)
                    eligible_unique.add((kind, key, versions[i]))
                    current = samples[kind].get(key)
                    if key in existing[kind] or (current is not None and
                                                 versions[i] in current["versions"]):
                        stats["duplicate"] += 1
                        continue
                    near = near_key(item, versions[i])
                    if near in near_seen[kind]:
                        stats["near_duplicate"] += 1
                        continue
                    cell = (timeline["mapping_id"], kind, versions[i])
                    if need.get(cell, 0) <= 0:
                        stats["cell_full"] += 1
                        continue
                    if current is None:
                        samples[kind][key] = item
                    else:
                        current["versions"].append(versions[i])
                    near_seen[kind].add(near)
                    need[cell] -= 1
                    if loose is not None:
                        # The call now has dependency evidence: it leaves the loose set.
                        loose["dependency_near"][kind].add(near_key(item, None))
                        drop_loose(loose, kind, key, near_key(item, None))
            remember()
        if failed_commit:
            break
        if status is not None and status.get("resume") is not None:
            status["resume"]["next_index"] = index + 1
    if status is not None and status.get("resume") is not None:
        remaining = len(commits) - status["resume"]["next_index"]
        if remaining and any(count > 0 for count in need.values()):
            status["complete"] = False
            print(f"  {remaining} commits pending; continuation saved", flush=True)
        else:
            if not status["resume"]["preparation_complete"]:
                status["complete"] = False
            status["resume"] = None
    stats["distinct_calls"] = len(distinct_calls)
    stats["unique_eligible"] = len(eligible_unique)
    return stats


def dependency_output(folder, kind):
    """Samples whose milestone comes from the repository's dependency files."""
    return folder / f"{kind}_crawled_dependency.json"


def not_dependency_output(folder, kind):
    """Calls whose version the dependency files do not fix; their versions stay empty."""
    return folder / f"{kind}_crawled_not_dependency.json"


def new_loose():
    """samples: kind -> sample_key -> sample; near: kind -> near_key(sample, None) -> key;
    dependency_near: near_key(sample, None) of samples with dependency evidence;
    room: (mapping_id, kind) -> samples that may still be added; added: new per kind."""
    return {"samples": {"O": {}, "U": {}}, "near": {"O": {}, "U": {}},
            "dependency_near": {"O": set(), "U": set()}, "room": Counter(),
            "added": Counter(), "moved": Counter()}


def drop_loose(loose, kind, key, near):
    """Remove the loose sample with this key or revision of the same function call."""
    for stale in {key, loose["near"][kind].get(near)}:
        item = loose["samples"][kind].pop(stale, None)
        if item is not None:
            loose["near"][kind].pop(near_key(item, None), None)
            loose["moved"][kind] += 1


def write_samples(output, rows):
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)


def save_outputs(folder, samples, loose=None):
    for kind in ("O", "U"):
        for item in samples[kind].values():
            item["versions"] = sorted(set(item["versions"]), key=Version)
        write_samples(dependency_output(folder, kind), list(samples[kind].values()))
        if loose is not None:
            write_samples(not_dependency_output(folder, kind),
                          list(loose["samples"][kind].values()))


def normalize_probing_input(sample):
    prefix = sample.get("probing input", "")
    if prefix and not prefix.endswith("\n"):
        sample["probing input"] = prefix[:prefix.rfind("\n") + 1]
    return sample


def cell_needs(timelines, baseline):
    """Missing samples per (mapping_id, kind, version).

    The goal of a pair is its largest cell in the step-8 data; cells whose API is not
    active/deprecated at the milestone (removed, absent, unknown) are not crawled.
    """
    need = {}
    for timeline in timelines:
        old, new = timeline["seed"]["deprecated_api"], timeline["seed"]["replacement_api"]
        counts = Counter((kind, version) for kind in ("O", "U") for item in baseline[kind]
                         if old in item.get("deprecated api", [])
                         and item.get("replacement api") == new
                         for version in set(item.get("versions", [])))
        cells = [(kind, milestone["version"]) for milestone in timeline["milestones"]
                 for kind, api in (("O", old), ("U", new))
                 for state in milestone["api_states"]
                 if state["name"] == api and state["status"] in VALID]
        goal = max((counts[cell] for cell in cells), default=0)
        for kind, version in cells:
            need[(timeline["mapping_id"], kind, version)] = goal - counts[(kind, version)]
    return need


def print_needs(timelines, need):
    for timeline in timelines:
        seed = timeline["seed"]
        for kind, api in (("O", seed["deprecated_api"]), ("U", seed["replacement_api"])):
            cells = [f"{m['version']}:{max(need[key], 0)}" for m in timeline["milestones"]
                     if (key := (timeline["mapping_id"], kind, m["version"])) in need]
            print(f"  #{timeline['mapping_id']} {kind} {api} missing: "
                  f"{', '.join(cells) or 'no crawlable cell'}", flush=True)


def main():
    global REPO_DEADLINE
    parser = argparse.ArgumentParser(description="Crawl GitHub API calls with version evidence.")
    parser.add_argument("--library", required=True)
    parser.add_argument("--mapping-id", type=int)
    parser.add_argument("--repo", help="GitHub owner/name or a local Git repository for a pilot")
    parser.add_argument("--max-repos", type=int, default=20)
    parser.add_argument("--repo-offset", type=int, default=0,
                        help="Skip this many ranked repositories")
    parser.add_argument("--max-files", type=int, default=100,
                        help=f"Initial GitHub code results per API, split over {len(SIZE_SHARDS)} "
                             f"file-size shards (expands if needed)")
    parser.add_argument("--max-commits", type=int, default=500,
                        help="Recent source/dependency commits per repository; 0 means all")
    parser.add_argument("--target-samples", type=int, default=0,
                        help="Also stop after filling this many missing cell slots; "
                             "0 stops only when every missing cell is filled or max-repos is reached")
    parser.add_argument("--max-repo-seconds", type=int, default=300)
    parser.add_argument("--full-repo-scan", action="store_true",
                        help="Scan every Python file, including very large repositories")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "cache" / "step9")
    parser.add_argument("--refresh-search", action="store_true",
                        help="Ignore the 24-hour GitHub search cache")
    parser.add_argument("--rescan", action="store_true",
                        help="Reprocess repositories even when HEAD and crawl settings are unchanged")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_started = time.monotonic()
    max_search = 1000 * len(SIZE_SHARDS)
    if (args.max_repos < 1 or args.repo_offset < 0 or args.max_files < 1 or
            args.max_files > max_search or args.max_commits < 0 or args.target_samples < 0 or
            args.max_repo_seconds < 1):
        parser.error(f"Invalid crawl limit: max-repos/max-files/max-repo-seconds > 0; "
                     f"others >= 0; max-files <= {max_search}")
    if args.repo and args.repo_offset:
        parser.error("--repo-offset only applies to GitHub search, not --repo")
    folder = DATA / args.library
    timelines = [t for t in read_json(folder / "migration_evolution.json")
                 if args.mapping_id is None or t["mapping_id"] == args.mapping_id]
    if not timelines:
        parser.error("No matching migration timeline")
    roots = library_roots()
    pairs = {(t["seed"]["deprecated_api"], t["seed"]["replacement_api"]): t["mapping_id"]
             for t in timelines}
    existing, samples, baseline = {}, {}, {}
    for kind in ("O", "U"):
        baseline[kind] = read_json(folder / f"{kind}_modified.json")
        existing[kind] = {sample_key(x) for x in baseline[kind]}
    need = cell_needs(timelines, baseline)
    near_seen = {"O": set(), "U": set()}
    pruned = Counter()
    for kind in ("O", "U"):
        output = dependency_output(folder, kind)
        samples[kind] = {}
        for row in (read_json(output) if output.exists() else []):
            row = normalize_probing_input(row)
            mid = pairs.get((row["deprecated api"][0], row["replacement api"]))
            if mid is not None:
                # Earlier crawls are re-checked against the current rules and cell goals.
                if vendored(sample_path(row), roots):
                    pruned["vendored"] += len(row["versions"])
                    continue
                if excluded_repo(sample_repo(row)):
                    pruned["benchmark_repo"] += len(row["versions"])
                    continue
                old_api, new_api = row["deprecated api"][0], row["replacement api"]
                if sample_calls_api(row, new_api if row.get("expected call") == old_api else old_api):
                    pruned["both_apis"] += len(row["versions"])
                    continue
                kept = []
                for version in row["versions"]:
                    cell, near = (mid, kind, version), near_key(row, version)
                    if need.get(cell, 0) > 0 and near not in near_seen[kind]:
                        kept.append(version)
                        near_seen[kind].add(near)
                        need[cell] -= 1
                    else:
                        pruned["over_goal"] += 1
                if not kept:
                    continue
                row["versions"] = kept
            key = sample_key(row)
            if key in samples[kind]:
                samples[kind][key]["versions"].extend(
                    v for v in row["versions"] if v not in samples[kind][key]["versions"])
            else:
                samples[kind][key] = row
    if pruned:
        print(f"Removed earlier crawled sample versions: {dict(pruned)}", flush=True)
    # Not-dependency samples: same checks except the cells; a call that has a sample with
    # dependency evidence (same code, or another revision of the same function call) or a
    # step-8 sample is dropped from this set.
    loose = new_loose()
    pruned_loose = Counter()
    for kind in ("O", "U"):
        loose["dependency_near"][kind].update(near_key(row, None) for row in samples[kind].values())
        output = not_dependency_output(folder, kind)
        for row in (read_json(output) if output.exists() else []):
            row = normalize_probing_input(row)
            key, near = sample_key(row), near_key(row, None)
            if pairs.get((row["deprecated api"][0], row["replacement api"])) is not None:
                old_api, new_api = row["deprecated api"][0], row["replacement api"]
                if vendored(sample_path(row), roots):
                    pruned_loose["vendored"] += 1
                    continue
                if excluded_repo(sample_repo(row)):
                    pruned_loose["benchmark_repo"] += 1
                    continue
                if sample_calls_api(row, new_api if row.get("expected call") == old_api else old_api):
                    pruned_loose["both_apis"] += 1
                    continue
                if (key in existing[kind] or key in samples[kind]
                        or near in loose["dependency_near"][kind]):
                    pruned_loose["has_dependency_or_step8_sample"] += 1
                    continue
            if key in loose["samples"][kind] or near in loose["near"][kind]:
                pruned_loose["duplicate"] += 1
                continue
            loose["samples"][kind][key] = row
            loose["near"][kind][near] = key
    if pruned_loose:
        print(f"Removed earlier not-dependency samples: {dict(pruned_loose)}", flush=True)
    print("Missing samples per cell (version:count):", flush=True)
    print_needs(timelines, need)
    # At most as many not-dependency samples per API as that API still misses in its cells.
    room_report = []
    for timeline in timelines:
        mid, seed = timeline["mapping_id"], timeline["seed"]
        for kind, api in (("O", seed["deprecated_api"]), ("U", seed["replacement_api"])):
            cap = sum(max(count, 0) for (cell_mid, cell_kind, _), count in need.items()
                      if cell_mid == mid and cell_kind == kind)
            held = sum(row["deprecated api"][0] == seed["deprecated_api"]
                       and row["replacement api"] == seed["replacement_api"]
                       for row in loose["samples"][kind].values())
            loose["room"][(mid, kind)] = max(cap - held, 0)
            room_report.append(f"#{mid} {kind} {held}/{cap}")
    print(f"Not-dependency samples (kept/limit = missing samples of the API): "
          f"{', '.join(room_report)}", flush=True)

    def wanted_cells():
        wanted = defaultdict(set)
        for timeline in timelines:
            for kind, api in (("O", timeline["seed"]["deprecated_api"]),
                              ("U", timeline["seed"]["replacement_api"])):
                wanted[api].update(f"{mid}:{version}" for (mid, cell_kind, version), count
                                   in need.items()
                                   if mid == timeline["mapping_id"] and cell_kind == kind
                                   and count > 0)
        return {api: cells for api, cells in wanted.items() if cells}

    def missing_total():
        return sum(max(count, 0) for count in need.values())

    starting_missing = missing_total()
    wanted = wanted_cells()
    targets = set(wanted)
    if not targets:
        print("Every crawlable cell already has enough samples", flush=True)
        if not args.dry_run and (pruned or pruned_loose):
            save_outputs(folder, samples, loose)
        return
    # Preserve compatible processed-repo entries; candidate paths also enter scan_signature.
    # Bump when validation changes require rechecking prior results.
    # v3: marker-aware specs, pinned co-dependencies, Sourcegraph and commit-search candidates.
    code_hash = "b7e2d94c1a6f83e05d9c2b7a4f1e8d63c0a5b9f2e7d4c1a8b6f3e0d9c2a5b8f1"
    # Bump to recheck processed repositories without discarding the search cache.
    # v4: != clauses, one-level re-exported library aliases (from pint.compat import np).
    # v5: local names typed inside if/try/with and guarded against every rebinding,
    # DataFrame-returning methods and chains, version shims (both APIs of a pair).
    # v6: calls without a dependency-fixed version are kept in *_crawled_not_dependency.json.
    validation_hash = "v6"
    # Bump to redo the candidate search without rechecking processed repositories.
    # s2: commit-search evidence is AST-checked at the parent/commit instead of a patch regex.
    # s3: DataFrame receiver typing, version shims, blob ids for merging identical forks.
    # s4: pinned-dependency searches with batched `repo:` call searches, extra code-search
    # words for capped APIs, migration commit queries.
    # s5: Sourcegraph query with file:has.content(root), `.leaf(` for methods, count:all.
    # s6: Sourcegraph query accepts `.leaf[` for indexers (DataFrame.loc).
    search_hash = "s6"
    PYPI_CACHE["path"] = args.cache_dir / "pypi_requires.json"
    shared_cache = {}
    timeline_hash = digest(timelines)
    ledger_path = args.cache_dir / "processed.json"
    try:
        ledger = read_json(ledger_path) if ledger_path.exists() else {}
        if not isinstance(ledger, dict):
            ledger = {}
    except (OSError, ValueError):
        ledger = {}
    if args.repo:
        candidates = [(args.repo, {"paths": set(), "manifests": set(), "head_calls": 0,
                                   "spec_cells": set()})]
    else:
        load_dotenv(ROOT / ".env")
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            parser.error("Set GITHUB_TOKEN or GH_TOKEN for GitHub code search")
        targets_in_order = [api for t in timelines for api in
                            (t["seed"]["deprecated_api"], t["seed"]["replacement_api"])
                            if api in targets]
        targets_in_order = list(dict.fromkeys(targets_in_order))
        search_signature = digest((code_hash, search_hash, timeline_hash, args.library,
                                   targets_in_order, args.max_files))
        search_path = args.cache_dir / "search_cache.json"
        cached = load_search_cache(search_path, search_signature, args.refresh_search)
        search_limit = max(args.max_files, cached[1] or 0) if cached else args.max_files
        if cached is None:
            candidates = discover_candidates(token, targets_in_order, timelines,
                                             args.library, search_limit, args.cache_dir, wanted)
            if not args.dry_run:
                save_search_cache(search_path, search_signature, candidates, search_limit)
        else:
            candidates = rank_candidates(cached[0], args.library, wanted, timelines)
        ledger_prefix = f"{args.library}:{','.join(str(t['mapping_id']) for t in timelines)}:"
        processed_names = {key[len(ledger_prefix):] for key in ledger
                           if key.startswith(ledger_prefix)}
        if (search_limit < max_search and
                not any(info["head_calls"] or info.get("history_calls") for name, info in candidates
                        if name not in processed_names)):
            search_limit = min(search_limit * 2, max_search)
            print(f"No unprocessed repositories with API calls; expanding search to "
                  f"{search_limit} files per API", flush=True)
            candidates = discover_candidates(token, targets_in_order, timelines,
                                             args.library, search_limit, args.cache_dir, wanted)
            if not args.dry_run:
                save_search_cache(search_path, search_signature, candidates, search_limit)
    if not args.repo:
        excluded = [name for name, _ in candidates if excluded_repo(name)]
        if excluded:
            print(f"Excluded {len(excluded)} benchmark/synthetic repositories", flush=True)
        candidates = [item for item in candidates if not excluded_repo(item[0])]
    selected = candidates[args.repo_offset:]
    print(f"Found {len(selected)} ranked repositories after offset {args.repo_offset}; "
          f"processing up to {args.max_repos} unprocessed repositories", flush=True)
    starting_counts = {kind: len(samples[kind]) for kind in ("O", "U")}
    attempted = 0
    skipped = 0
    # Only the dependency files: the not-dependency files are created by the first v6 run.
    outputs_existed_at_start = all(dependency_output(folder, kind).exists()
                                   for kind in ("O", "U"))
    for rank, (name, candidate) in enumerate(selected, 1):
        if attempted >= args.max_repos:
            break
        REPO_DEADLINE = None
        repo_started = time.monotonic()
        print(f"[rank {rank}] {name}: syncing repository "
              f"(AST={candidate['head_calls']}, "
              f"version_cells={len(candidate.get('spec_cells', ()))})", flush=True)
        status = {"complete": True, "preparation_complete": True}
        completed = False
        try:
            deadline = time.monotonic() + args.max_repo_seconds
            REPO_DEADLINE = deadline
            repo, display = clone_repo(name, args.cache_dir, deadline)
            head = head_revision(repo)
            ledger_key = f"{args.library}:{','.join(str(t['mapping_id']) for t in timelines)}:{display}"
            scan_signature = digest((code_hash, validation_hash, timeline_hash, args.library,
                                     args.max_files, args.max_commits,
                                     args.max_repo_seconds, args.full_repo_scan,
                                     sorted(candidate["paths"]), sorted(candidate["manifests"])))
            previous = ledger.get(ledger_key, {})
            same_scan = (not args.dry_run and not args.rescan and outputs_existed_at_start and
                         previous.get("head") == head and
                         previous.get("signature") == scan_signature)
            if same_scan and previous.get("complete", True):
                skipped += 1
                print("  Already processed at this commit (complete); skipping", flush=True)
                continue
            resume = previous.get("resume") if same_scan else None
            if not isinstance(resume, dict) or not all(
                    key in resume for key in ("paths", "manifests", "commits", "next_index")):
                resume = None
            if resume:
                status["preparation_complete"] = resume.get("preparation_complete", True)
            attempted += 1
            print(f"  Processing {attempted}/{args.max_repos} repositories", flush=True)
            if resume:
                paths = set(resume["paths"])
            else:
                tracked = set(repo_files(repo, head))

                def keep(path):
                    return not vendored(path, roots, tracked)

                paths = set(candidate["paths"])
                python_files = sum(p.endswith(".py") for p in tracked)
                print("  Finding API call files", flush=True)
                try:
                    paths.update(candidate_paths(repo, head, targets, args.max_files,
                                                 args.full_repo_scan, deadline, python_files,
                                                 60 if paths else 20, keep))
                except RuntimeError as exc:
                    status["complete"] = False
                    status["preparation_complete"] = False
                    print(f"  Current-file search stopped: {exc}", flush=True)
                if (args.full_repo_scan or python_files <= 200 or
                        (not paths and candidate.get("spec_cells"))):
                    paths.update(history_candidate_paths(repo, head, targets,
                                                         args.max_files, deadline,
                                                         45 if paths else 25, status, keep))
                ignored = {p for p in paths if not keep(p)}
                if ignored:
                    print(f"  Ignoring {len(ignored)} vendored/library-copy files", flush=True)
                paths -= ignored
            time_left(deadline, 1)
            before = {kind: len(samples[kind]) for kind in ("O", "U")}
            if paths:
                stats = process_repo(repo, display, sorted(paths), candidate["manifests"],
                                     timelines, args.library, args.max_commits, deadline,
                                     samples, existing, need, near_seen, status, resume,
                                     shared=shared_cache, loose=loose)
            else:
                print("  No candidate Python files found", flush=True)
                stats = Counter()
            print(f"{display}: files={len(paths)}, calls={stats['calls']}, "
                  f"distinct_calls={stats['distinct_calls']}, "
                  f"eligible_occurrences={stats['eligible']}, "
                  f"unique_eligible={stats['unique_eligible']}, "
                  f"new O={len(samples['O']) - before['O']}, "
                  f"new U={len(samples['U']) - before['U']}, "
                  f"new O_not_dependency={stats['not_dependency_O']}, "
                  f"new U_not_dependency={stats['not_dependency_U']}, "
                  f"seconds={time.monotonic() - repo_started:.1f}", flush=True)
            print(f"  Old/new: old_calls={stats['old_calls']}, new_calls={stats['new_calls']}, "
                  f"old_eligible_occurrences={stats['old_eligible']}, "
                  f"new_eligible_occurrences={stats['new_eligible']}", flush=True)
            if stats["reused_validations"]:
                print(f"  Reused validation for {stats['reused_validations']} calls", flush=True)
            if stats["reexport_calls"]:
                print(f"  Library alias re-exported by a repository module for "
                      f"{stats['reexport_calls']} calls", flush=True)
            if stats["codependency_calls"]:
                print(f"  Version evidence from pinned co-dependencies for "
                      f"{stats['codependency_calls']} calls", flush=True)
            rejected = ", ".join(f"{key}={stats[key]}" for key in (
                "no_manifest", "no_version_spec", "version_conflict",
                "version_not_single_interval", "both_apis", "api_unavailable",
                "signature_mismatch", "duplicate", "near_duplicate", "cell_full", "read_error")
                if stats[key])
            if rejected:
                print(f"  Reasons: {rejected}", flush=True)
            # Eligible calls over this scan and the earlier runs it continues.
            eligible = stats["eligible"] + (previous.get("eligible", 0) if resume else 0)
            if not status["complete"] and not eligible and time.monotonic() >= deadline:
                # The whole time budget went by without one valid call: continuing the
                # same commit list is unlikely to pay off, so the scan is closed here.
                status["complete"] = True
                status["resume"] = None
                print("  No eligible calls within the repository time limit; not continuing",
                      flush=True)
            completed = True
        except (RuntimeError, OSError) as exc:
            print(f"Skipped {name}: {exc}", flush=True)
        save_pypi_cache()
        if not args.dry_run:
            save_outputs(folder, samples, loose)
            if completed:
                ledger[ledger_key] = {"head": head, "signature": scan_signature,
                                      "complete": status["complete"], "eligible": eligible}
                if status.get("resume"):
                    ledger[ledger_key]["resume"] = status["resume"]
                write_json(ledger_path, ledger)
                if not status["complete"]:
                    print("  Partial scan saved; the next run will continue or retry", flush=True)
        if not missing_total():
            print("All missing cells are filled", flush=True)
            break
        if args.target_samples and starting_missing - missing_total() >= args.target_samples:
            print(f"Reached target of {args.target_samples} filled cell slots", flush=True)
            break
    if not args.dry_run and not selected:
        save_outputs(folder, samples, loose)
    REPO_DEADLINE = None
    print(f"Processed {attempted} repositories; skipped {skipped} already processed", flush=True)
    print(f"Batch: new O={len(samples['O']) - starting_counts['O']}, "
          f"new U={len(samples['U']) - starting_counts['U']}, "
          f"filled cell slots={starting_missing - missing_total()}, "
          f"new O_not_dependency={loose['added']['O']}, "
          f"new U_not_dependency={loose['added']['U']}, "
          f"not_dependency moved to dependency={sum(loose['moved'].values())}, "
          f"seconds={time.monotonic() - run_started:.1f}", flush=True)
    print("Still missing per cell (version:count):", flush=True)
    print_needs(timelines, need)
    suffix = " (dry run)" if args.dry_run else ""
    for kind in ("O", "U"):
        print(f"{dependency_output(folder, kind)}: {len(samples[kind])} samples{suffix}",
              flush=True)
    for kind in ("O", "U"):
        print(f"{not_dependency_output(folder, kind)}: {len(loose['samples'][kind])} samples "
              f"without versions{suffix}", flush=True)


if __name__ == "__main__":
    main()
