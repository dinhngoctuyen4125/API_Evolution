"""Crawl GitHub call sites with unambiguous dependency-to-milestone evidence.

python github_crawler.py --library numpy --mapping-id 2 --repo owner/name
python github_crawler.py --library numpy --mapping-id 2 --max-repos 20

The second form needs GITHUB_TOKEN (or GH_TOKEN) for GitHub code search.
"""

import argparse
import ast
import base64
import configparser
import hashlib
import inspect
import json
import os
import posixpath
import re
import shutil
import subprocess
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import yaml
from dotenv import load_dotenv
from packaging.requirements import InvalidRequirement, Requirement
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
    temporary.replace(path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


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
            for key in ("paths", "manifests", "observed"):
                info[key] = set(info[key])
        print(f"Using cached search results: {len(rows)} repositories", flush=True)
        return rows
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_search_cache(path, signature, candidates):
    rows = [(name, {key: sorted(value) if isinstance(value, set) else value
                    for key, value in info.items()}) for name, info in candidates]
    write_json(path, {"signature": signature, "created": time.time(), "candidates": rows})


def time_left(deadline, cap):
    if deadline is None:
        return cap
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("Repository time limit reached")
    return min(cap, remaining)


def run_process(command, label, max_seconds=None):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace")
    started = time.monotonic()
    while True:
        try:
            remaining = None if max_seconds is None else max_seconds - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                process.kill()
                process.communicate()
                raise RuntimeError(f"{label} exceeded {max_seconds}s")
            stdout, stderr = process.communicate(timeout=min(20, remaining) if remaining else 20)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            print(f"  Still {label}...", flush=True)
        except KeyboardInterrupt:
            process.kill()
            process.communicate()
            raise


def git(repo, *args, allow_missing=False, max_seconds=None):
    result = run_process(["git", "-C", str(repo), *args], f"running git {args[0]}",
                         max_seconds=max_seconds)
    if result.returncode and not allow_missing:
        raise RuntimeError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout if result.returncode == 0 else None


def git_file(repo, commit, path):
    result = run_process(["git", "-C", str(repo), "show", f"{commit}:{path}"],
                         f"reading {path}", max_seconds=60)
    if result.returncode == 0:
        return result.stdout
    present = git(repo, "ls-tree", "--name-only", commit, "--", path)
    if path in present.splitlines():
        raise RuntimeError(f"Cannot read {commit}:{path}: {result.stderr.strip()}")
    return None


def github_search(token, query, limit):
    global LAST_SEARCH
    found, page = [], 1
    per_page = min(50, limit)
    while len(found) < limit:
        delay = max(0, LAST_SEARCH + 7 - time.monotonic())
        if delay:
            time.sleep(delay)
        url = "https://api.github.com/search/code?" + urlencode({
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
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub code search {exc.code}: {detail[:300]}") from exc
        items = payload.get("items", [])
        print(f"  Search page {page}: {len(items)} files", flush=True)
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


def github_content(token, url):
    request = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "api-evolution-step9",
    })
    try:
        with urlopen(request, timeout=30) as response:
            item = json.load(response)
    except (HTTPError, OSError, ValueError):
        return None
    if item.get("encoding") != "base64" or item.get("size", 0) > 384_000:
        return None
    try:
        return base64.b64decode(item["content"]).decode("utf-8", errors="replace")
    except (KeyError, ValueError):
        return None


def discover_candidates(token, targets, timelines, library, max_files, cache):
    candidates = defaultdict(lambda: {"paths": set(), "manifests": set(),
                                  "head_calls": 0, "observed": set(), "version_specs": 0,
                                  "repo_size": None})
    leaves = dict.fromkeys(target.rsplit(".", 1)[-1] for target in targets)
    queries = [(leaf, f"{leaf} language:Python", max_files) for leaf in leaves]
    distribution = DISTRIBUTION.get(library, library)
    version_sets = [[m["version"] for m in timeline["milestones"]] for timeline in timelines]
    queries.extend((kind, f"{distribution} filename:{filename}", min(max_files, 30)) for kind, filename in
                   (("dependency", "requirements.txt"), ("dependency", "pyproject.toml")))
    content_cache = {}
    for index, (kind, query, limit) in enumerate(queries, 1):
        print(f"Search {index}/{len(queries)}: {query}", flush=True)
        hits = github_search(token, query, limit)
        print(f"  {len(hits)} matching files", flush=True)
        for hit_number, item in enumerate(hits, 1):
            name, path = item["repository"]["full_name"], item["path"]
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
            url = item.get("url")
            if url not in content_cache:
                content_cache[url] = github_content(token, url) if url else None
            content = content_cache[url]
            if content:
                if kind == "dependency":
                    candidates[name]["version_specs"] += sum(
                        any(containing_milestone(spec, versions) is not None
                            for versions in version_sets)
                        for spec in dependency_specs(path, content, library))
                else:
                    calls = list(source_calls(content, set(targets)))
                    candidates[name]["head_calls"] += len(calls)
                    candidates[name]["observed"].update(call[6] for call in calls)
            if hit_number % 10 == 0:
                print(f"  Prefiltered {hit_number}/{len(hits)} files", flush=True)
    if cache.is_dir():
        for folder in cache.iterdir():
            if folder.is_dir() and "__" in folder.name and is_repo_root(folder):
                remote = git(folder, "config", "--get", "remote.origin.url", allow_missing=True) or ""
                match = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?\s*$", remote)
                if match:
                    candidates[match[1]]
    for name, info in candidates.items():
        if not info["head_calls"] or info["version_specs"]:
            continue
        for filename in ("requirements.txt", "pyproject.toml"):
            if filename in info["manifests"]:
                continue
            url = f"https://api.github.com/repos/{name}/contents/{filename}"
            content = github_content(token, url)
            if content is None:
                continue
            info["manifests"].add(filename)
            info["version_specs"] += sum(
                any(containing_milestone(spec, versions) is not None
                    for versions in version_sets)
                for spec in dependency_specs(filename, content, library))
    def priority(info):
        if info["head_calls"] and info["version_specs"]:
            return 0
        if info["paths"] and info["version_specs"]:
            return 1
        if info["head_calls"]:
            return 2
        if info["paths"]:
            return 3
        if info["version_specs"]:
            return 4
        return 5
    ordered = sorted(candidates.items(), key=lambda item: (
        priority(item[1]),
        -len(item[1]["observed"]), -item[1]["head_calls"],
        -len(item[1]["paths"]),
        item[1]["repo_size"] if item[1]["repo_size"] is not None else float("inf"),
        item[0]))
    source_only = deque(item for item in ordered if priority(item[1]) == 3)
    dependency_only = deque(item for item in ordered if priority(item[1]) == 4)
    ranked = [item for item in ordered if priority(item[1]) < 3]
    while source_only or dependency_only:
        for _ in range(3):
            if source_only:
                ranked.append(source_only.popleft())
        if dependency_only:
            ranked.append(dependency_only.popleft())
    ranked.extend(item for item in ordered if priority(item[1]) == 5)
    print(f"Ranked {len(ranked)} repositories: "
          f"{sum(bool(info['head_calls']) for _, info in ranked)} with AST calls, "
          f"{sum(bool(info['version_specs']) for _, info in ranked)} with usable version evidence, "
          f"{sum(bool(info['head_calls'] and info['version_specs']) for _, info in ranked)} with both",
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


def requirement_spec(text, library):
    text = text.strip()
    if not text or text.startswith(("#", "-")):
        return None
    try:
        req = Requirement(text)
    except InvalidRequirement:
        return None
    if canonicalize_name(req.name) != canonicalize_name(DISTRIBUTION.get(library, library)) or req.url:
        return None
    # Marker-specific requirements cannot be assigned without the environment.
    if req.marker is not None:
        return None
    return str(req.specifier) or None


def requirement_lines(value, library):
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list):
        return []
    return [spec for line in value
            if isinstance(line, str)
            if (spec := requirement_spec(line.split(" #", 1)[0], library))]


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
                result.extend(requirement_lines(command.split(), library))
            return result
    except (ValueError, TypeError, AttributeError, KeyError, yaml.YAMLError,
            configparser.Error, SyntaxError):
        return []
    return []


def manifest_specs(repo, commit, path, library, seen=None):
    seen = set() if seen is None else seen
    if path in seen or Path(path).name.lower().startswith("constraints"):
        return []
    seen.add(path)
    content = git_file(repo, commit, path)
    if content is None:
        return []
    specs = dependency_specs(path, content, library)
    if re.fullmatch(r"requirements[^/]*\.(?:txt|in)", Path(path).name, re.I):
        constraints = []
        for line in content.splitlines():
            match = re.match(r"\s*-(r|c|requirement|constraint)\s+([^\s#]+)", line)
            if not match:
                continue
            included = posixpath.normpath(posixpath.join(posixpath.dirname(path), match[2]))
            if included.startswith("../") or included.startswith("/"):
                continue
            if match[1] in {"r", "requirement"}:
                specs.extend(manifest_specs(repo, commit, included, library, seen))
            else:
                text = git_file(repo, commit, included)
                if text:
                    constraints.extend(requirement_lines(text, library))
        if constraints:
            specs = [base + "," + constraint for base in specs for constraint in constraints]
    return specs


def manifests_for_source(manifests, source):
    parents = {""}
    current = posixpath.dirname(source)
    while current:
        parents.add(current)
        current = posixpath.dirname(current)
    return [path for path in manifests
            if posixpath.dirname(path) in parents or path.startswith(".github/workflows/")]


def containing_milestone(spec, versions):
    """A spec must fit wholly within one half-open milestone interval."""
    clauses = re.findall(r"(===|==|>=|<=|>|<|!=|~=)\s*([^,\s]+)", spec)
    if not clauses or ",".join(op + value for op, value in clauses) != re.sub(r"\s+", "", spec):
        return None
    if any(op not in {"==", ">", ">=", "<", "<="} or "*" in value
           for op, value in clauses):
        return None
    try:
        parsed = [(op, Version(value)) for op, value in clauses]
        milestones = [Version(value) for value in versions]
    except ValueError:
        return None
    exact = [value for op, value in parsed if op == "=="]
    if exact:
        if len(set(exact)) != 1:
            return None
        candidate = exact[0]
        if not all(candidate > value if op == ">" else
                   candidate >= value if op == ">=" else
                   candidate < value if op == "<" else
                   candidate <= value if op == "<=" else True
                   for op, value in parsed):
            return None
        return next((i for i, start in enumerate(milestones)
                     if candidate >= start and (i + 1 == len(milestones)
                                                or candidate < milestones[i + 1])), None)
    lowers = [(value, op == ">") for op, value in parsed if op in {">", ">="}]
    uppers = [(value, op == "<") for op, value in parsed if op in {"<", "<="}]
    if not lowers:
        return None
    lower = max(value for value, _ in lowers)
    lower_open = any(strict for value, strict in lowers if value == lower)
    upper = min((value for value, _ in uppers), default=None)
    upper_open = (any(strict for value, strict in uppers if value == upper)
                  if upper is not None else False)
    if upper is not None and (upper < lower or upper == lower and (lower_open or upper_open)):
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


def import_aliases(nodes):
    result = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for item in node.names:
                result[item.asname or item.name] = item.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for item in node.names:
                if item.name != "*":
                    result[item.asname or item.name] = node.module + "." + item.name
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
    if name and re.fullmatch(r"pandas\.read_(?:csv|table|excel|json|parquet|feather|sql|sql_query|sql_table)", name):
        return "pandas.DataFrame"
    return None


def source_calls(content, targets):
    """Yield one call and its enclosing function; unresolved receiver types are skipped."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, UnicodeError):
        return
    module_aliases = import_aliases(tree.body)
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
            aliases = dict(self.alias_stack[-1])
            aliases.update(import_aliases(node.body))
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                aliases.pop(arg.arg, None)
                if arg.annotation:
                    annotation = resolve(dotted(arg.annotation), aliases)
                    if annotation:
                        aliases[arg.arg] = annotation
            for child in node.body:
                if isinstance(child, (ast.Assign, ast.AnnAssign)):
                    value = child.value
                    inferred = (inferred_type(resolve(dotted(value.func), aliases))
                                if isinstance(value, ast.Call) else
                                resolve(dotted(value), aliases) if value else None)
                    if inferred:
                        assigned = child.targets if isinstance(child, ast.Assign) else [child.target]
                        for item in assigned:
                            if isinstance(item, ast.Name):
                                aliases[item.id] = inferred
            self.alias_stack.append(aliases)
            functions.append((node, ".".join(self.stack), aliases))
            for child in node.body:
                self.visit(child)
            self.alias_stack.pop()
            self.stack.pop()

    Visitor().visit(tree)
    def owned_nodes(function):
        pending = list(function.body)
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            yield node
            pending.extend(ast.iter_child_nodes(node))

    for function, qualname, aliases in functions:
        source = "".join(lines[function.lineno - 1:function.end_lineno])
        for node in owned_nodes(function):
            if not isinstance(node, (ast.Call, ast.Subscript)):
                continue
            expression = node.func if isinstance(node, ast.Call) else node.value
            canonical = resolve(dotted(expression), aliases)
            if canonical not in targets:
                continue
            snippet = ast.get_source_segment(content, node)
            if not snippet:
                continue
            prefix = "".join(lines[function.lineno - 1:node.lineno - 1])
            if not source.startswith(prefix):
                continue
            yield qualname, source, prefix, snippet, node, aliases, canonical, node.lineno


def repo_files(repo, commit):
    return git(repo, "ls-tree", "-r", "--name-only", commit).splitlines()


def candidate_paths(repo, commit, targets, limit, full_scan=False, deadline=None,
                    python_files=None, scan_seconds=60):
    if not full_scan:
        if python_files is None:
            python_files = sum(path.endswith(".py") for path in repo_files(repo, commit))
        if python_files > 200:
            print(f"  Skipping full scan of {python_files} Python files; using GitHub search paths",
                  flush=True)
            return []
    args = ["grep", "-l", "-F"]
    for leaf in sorted({target.rsplit(".", 1)[-1] for target in targets}):
        args.extend(("-e", leaf))
    result = run_process(["git", "-C", str(repo), *args, commit, "--", "*.py"],
                         "searching repository files",
                         max_seconds=time_left(deadline, 180 if full_scan else scan_seconds))
    if result.returncode == 1:
        return []
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    paths = []
    for line in result.stdout.splitlines():
        path = line.split(":", 1)[1]
        paths.append(path)
        if len(paths) >= limit:
            break
    return paths


def history_candidate_paths(repo, tip, targets, limit, deadline=None, scan_seconds=45,
                            status=None):
    pattern = "|".join(re.escape(target.rsplit(".", 1)[-1]) for target in targets)
    try:
        paths = git(repo, "log", tip, f"-G{pattern}", "--name-only", "--format=",
                    "--", "*.py", max_seconds=time_left(deadline, scan_seconds)).splitlines()
    except RuntimeError as exc:
        if status is not None:
            status["complete"] = False
        print(f"  Historical file search stopped: {exc}", flush=True)
        return []
    return list(dict.fromkeys(path for path in paths if path.endswith(".py")))[:limit]


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
    ]
    try:
        paths = git(repo, "log", tip, "--name-only", "--format=", *cap,
                    "--", *patterns, max_seconds=time_left(deadline, 30)).splitlines()
    except RuntimeError as exc:
        if status is not None:
            status["complete"] = False
        print(f"  Historical dependency search stopped: {exc}", flush=True)
        return []
    return list(dict.fromkeys(path for path in paths if DEPENDENCY_NAME.search(path)))


def repo_commits(repo, tip, source_paths, manifests, targets, limit, deadline=None,
                 head_has_call=False, status=None):
    if not source_paths:
        return []
    cap = ["-n", str(limit)] if limit else []
    changed = git(repo, "log", tip, "--format=%H", *cap, "--",
                  *sorted(set(source_paths + manifests)),
                  max_seconds=time_left(deadline, 60)).splitlines()
    dependency = (git(repo, "log", tip, "--format=%H", *cap, "--", *manifests,
                      max_seconds=time_left(deadline, 60)).splitlines()
                  if manifests else [])
    pattern = "|".join(re.escape(target.rsplit(".", 1)[-1]) for target in targets)
    focused = []
    focused_complete = True
    try:
        focused = git(repo, "log", tip, f"-G{pattern}", "--format=%H", *cap,
                      "--", *source_paths, max_seconds=time_left(deadline, 45)).splitlines()
        if not focused:
            leaf = next(iter(sorted(targets))).rsplit(".", 1)[-1]
            focused = git(repo, "log", tip, f"-S{leaf}", "--format=%H", *cap,
                          "--", *source_paths, max_seconds=time_left(deadline, 30)).splitlines()
    except RuntimeError as exc:
        focused_complete = False
        if status is not None:
            status["complete"] = False
        print(f"  Focused history search stopped: {exc}", flush=True)
    if focused_complete and not focused and not head_has_call:
        print("  No API call at HEAD or API change in file history", flush=True)
        return []
    fallback = changed[:min(limit, 20)] if focused and limit else changed
    ordered = []
    for commit in [*focused, *dependency, *fallback]:
        if commit not in ordered:
            ordered.append(commit)
        if commit in focused or commit in dependency:
            parent = git(repo, "show", "-s", "--format=%P", commit).strip().split()
            if parent and parent[0] not in ordered:
                ordered.append(parent[0])
        if limit and len(ordered) >= limit:
            break
    return ordered[:limit] if limit else ordered


def sample_key(sample):
    source = sample.get("source", "")
    origin = re.sub(r"/blob/[0-9a-f]{40}/", "/blob/", source).split("#", 1)[0]
    origin = re.sub(r"@[0-9a-f]{40}:", ":", origin)
    return (origin, *content_key(sample))


def content_key(sample):
    return (sample["function"].strip(), sample["api_call"].strip(),
            tuple(sample["deprecated api"]), sample["replacement api"])


def process_repo(repo, name, source_paths, known_manifests, timelines, library,
                 max_commits, deadline, samples, existing, status=None):
    stats = Counter()
    head = head_revision(repo)
    targets = {api for timeline in timelines for api in timeline["seed"].values()}
    ast_cache = {}
    manifest_cache = {}
    validation_cache = {}
    eligible_unique = set()

    def source_snapshot(commit, path):
        oid = git(repo, "rev-parse", f"{commit}:{path}", allow_missing=True,
                  max_seconds=time_left(deadline, 20))
        if not oid:
            return None, []
        oid = oid.strip()
        if oid not in ast_cache:
            content = git_file(repo, commit, path)
            ast_cache[oid] = list(source_calls(content, targets)) if content else []
        else:
            stats["ast_reused"] += 1
        return oid, ast_cache[oid]

    tracked = repo_files(repo, head)
    manifests = sorted(set(known_manifests) |
                       {p for p in tracked if DEPENDENCY_NAME.search(p)})
    manifests = sorted(set(manifests) |
                       set(historical_manifests(repo, head, max_commits, deadline, status)))
    if not manifests:
        print("  No dependency files at HEAD; checking calls without version evidence", flush=True)
    head_has_call = False
    for path in source_paths:
        try:
            _, calls = source_snapshot(head, path)
            head_has_call |= bool(calls)
        except RuntimeError as exc:
            if status is not None:
                status["complete"] = False
            head_has_call = True
            print(f"  Cannot inspect HEAD {path}: {exc}", flush=True)
    print("  Reading commit history", flush=True)
    commits = repo_commits(repo, head, source_paths, manifests, targets, max_commits,
                           deadline, head_has_call, status)
    print(f"  Checking {len(commits)} source/dependency commits", flush=True)
    by_target = defaultdict(list)
    for timeline in timelines:
        for kind, target in (("O", timeline["seed"]["deprecated_api"]),
                             ("U", timeline["seed"]["replacement_api"])):
            by_target[target].append((kind, timeline))
    old_targets = {timeline["seed"]["deprecated_api"] for timeline in timelines}
    new_targets = {timeline["seed"]["replacement_api"] for timeline in timelines}
    last_progress = time.monotonic()
    distinct_calls = set()
    reason_keys = ("no_manifest", "no_version_spec", "version_not_single_interval",
                   "api_unavailable", "signature_mismatch", "eligible",
                   "old_eligible", "new_eligible")
    for number, commit in enumerate(commits, 1):
        if time.monotonic() >= deadline:
            if status is not None:
                status["complete"] = False
            print(f"  Repository time limit reached after {number - 1} commits", flush=True)
            break
        if time.monotonic() - last_progress >= 20:
            print(f"  Commits {number - 1}/{len(commits)}; eligible={stats['eligible']}", flush=True)
            last_progress = time.monotonic()
        for path in source_paths:
            try:
                oid, calls = source_snapshot(commit, path)
                stats["calls"] += len(calls)
                stats["old_calls"] += sum(target in old_targets for *_, target, _ in calls)
                stats["new_calls"] += sum(target in new_targets for *_, target, _ in calls)
                distinct_calls.update((path, qualname, function, call, target)
                                      for qualname, function, _, call, _, _, target, _ in calls)
                if not calls:
                    continue
                nearby_manifests = manifests_for_source(manifests, path)
                specs = []
                for manifest in nearby_manifests:
                    cache_key = (commit, manifest)
                    if cache_key not in manifest_cache:
                        manifest_cache[cache_key] = manifest_specs(repo, commit, manifest, library)
                    specs.extend(manifest_cache[cache_key])
                specs = sorted(set(specs))
            except RuntimeError as exc:
                if status is not None:
                    status["complete"] = False
                stats["read_error"] += 1
                print(f"Skipped {name}@{commit[:12]}:{path}: {exc}")
                continue
            validation_key = (path, oid, bool(nearby_manifests), tuple(specs))
            if validation_key in validation_cache:
                outcome = validation_cache[validation_key]
                stats.update(outcome)
                stats["duplicate"] += outcome["eligible"]
                stats["reused_validations"] += len(calls)
                continue
            before_validation = {key: stats[key] for key in reason_keys}
            if not nearby_manifests:
                stats["no_manifest"] += len(calls)
                validation_cache[validation_key] = Counter(
                    {key: stats[key] - before_validation[key] for key in reason_keys})
                continue
            if not specs:
                stats["no_version_spec"] += len(calls)
                validation_cache[validation_key] = Counter(
                    {key: stats[key] - before_validation[key] for key in reason_keys})
                continue
            for qualname, function, probe, call, node, aliases, target, line in calls:
                for kind, timeline in by_target[target]:
                    milestones = timeline["milestones"]
                    versions = [m["version"] for m in milestones]
                    indices = {i for spec in specs if (i := containing_milestone(spec, versions)) is not None}
                    if not indices:
                        stats["version_not_single_interval"] += 1
                    for i in sorted(indices):
                        state = next((s for s in milestones[i]["api_states"] if s["name"] == target), None)
                        if state is None or state["status"] not in VALID:
                            stats["api_unavailable"] += 1
                            continue
                        if not call_matches(state, node, target):
                            stats["signature_mismatch"] += 1
                            continue
                        stats["eligible"] += 1
                        stats["old_eligible" if kind == "O" else "new_eligible"] += 1
                        seed = timeline["seed"]
                        item = {
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
                            "versions": [versions[i]],
                        }
                        key = sample_key(item)
                        eligible_unique.add((kind, key, versions[i]))
                        if content_key(item) in existing[kind]:
                            stats["duplicate"] += 1
                            continue
                        current = samples[kind].get(key)
                        if current is None:
                            samples[kind][key] = item
                        elif versions[i] not in current["versions"]:
                            current["versions"].append(versions[i])
                        else:
                            stats["duplicate"] += 1
            validation_cache[validation_key] = Counter(
                {key: stats[key] - before_validation[key] for key in reason_keys})
    stats["distinct_calls"] = len(distinct_calls)
    stats["unique_eligible"] = len(eligible_unique)
    return stats


def save_outputs(folder, samples):
    for kind in ("O", "U"):
        for item in samples[kind].values():
            item["versions"] = sorted(set(item["versions"]), key=Version)
        output = folder / f"{kind}_crawled.json"
        temporary = output.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(list(samples[kind].values()), ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        temporary.replace(output)


def normalize_probing_input(sample):
    prefix = sample.get("probing input", "")
    if prefix and not prefix.endswith("\n"):
        sample["probing input"] = prefix[:prefix.rfind("\n") + 1]
    return sample


def main():
    parser = argparse.ArgumentParser(description="Crawl GitHub API calls with version evidence.")
    parser.add_argument("--library", required=True)
    parser.add_argument("--mapping-id", type=int)
    parser.add_argument("--repo", help="GitHub owner/name or a local Git repository for a pilot")
    parser.add_argument("--max-repos", type=int, default=20)
    parser.add_argument("--repo-offset", type=int, default=0,
                        help="Skip this many ranked repositories")
    parser.add_argument("--max-files", type=int, default=100,
                        help="Maximum GitHub code results per API (50 per page)")
    parser.add_argument("--max-commits", type=int, default=500,
                        help="Recent source/dependency commits per repository; 0 means all")
    parser.add_argument("--target-samples", type=int, default=0,
                        help="Stop after this many new valid samples; 0 uses max-repos only")
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
    if (args.max_repos < 1 or args.repo_offset < 0 or args.max_files < 1 or
            args.max_files > 1000 or args.max_commits < 0 or args.target_samples < 0 or
            args.max_repo_seconds < 1):
        parser.error("Invalid crawl limit: max-repos/max-files/max-repo-seconds > 0; others >= 0; max-files <= 1000")
    if args.repo and args.repo_offset:
        parser.error("--repo-offset only applies to GitHub search, not --repo")
    folder = DATA / args.library
    timelines = [t for t in read_json(folder / "migration_evolution.json")
                 if args.mapping_id is None or t["mapping_id"] == args.mapping_id]
    if not timelines:
        parser.error("No matching migration timeline")
    existing, samples = {}, {}
    for kind in ("O", "U"):
        previous = read_json(folder / f"{kind}_modified.json")
        existing[kind] = {content_key(x) for x in previous}
        output = folder / f"{kind}_crawled.json"
        rows = read_json(output) if output.exists() else []
        samples[kind] = {sample_key(x): normalize_probing_input(x) for x in rows}
    # Logging-only edits must not invalidate the existing search and processed-repo cache.
    # Update this signature when discovery or validation behavior changes.
    code_hash = "17481fb76c186debcd10aba8a775e951298b73dd2d3c313dc7d993fbfb09bfe9"
    timeline_hash = digest(timelines)
    ledger_path = args.cache_dir / "processed.json"
    try:
        ledger = read_json(ledger_path) if ledger_path.exists() else {}
        if not isinstance(ledger, dict):
            ledger = {}
    except (OSError, ValueError):
        ledger = {}
    if args.repo:
        candidates = [(args.repo, {"paths": set(), "manifests": set(), "head_calls": 0})]
    else:
        load_dotenv(ROOT / ".env")
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            parser.error("Set GITHUB_TOKEN or GH_TOKEN for GitHub code search")
        targets_in_order = list(dict.fromkeys(
            [t["seed"]["deprecated_api"] for t in timelines] +
            [t["seed"]["replacement_api"] for t in timelines]))
        search_signature = digest((code_hash, timeline_hash, args.library,
                                   targets_in_order, args.max_files))
        search_path = args.cache_dir / "search_cache.json"
        candidates = load_search_cache(search_path, search_signature, args.refresh_search)
        if candidates is None:
            candidates = discover_candidates(token, targets_in_order, timelines,
                                             args.library, args.max_files, args.cache_dir)
            if not args.dry_run:
                save_search_cache(search_path, search_signature, candidates)
    targets = {api for t in timelines for api in t["seed"].values()}
    selected = candidates[args.repo_offset:]
    print(f"Found {len(selected)} ranked repositories after offset {args.repo_offset}; "
          f"processing up to {args.max_repos} unprocessed repositories", flush=True)
    starting_counts = {kind: len(samples[kind]) for kind in ("O", "U")}
    starting_total = sum(starting_counts.values())
    attempted = 0
    skipped = 0
    outputs_existed_at_start = all((folder / f"{kind}_crawled.json").exists()
                                   for kind in ("O", "U"))
    for rank, (name, candidate) in enumerate(selected, 1):
        if attempted >= args.max_repos:
            break
        repo_started = time.monotonic()
        print(f"[rank {rank}] {name}: syncing repository "
              f"(AST={candidate['head_calls']}, version_evidence={candidate.get('version_specs', 0)})",
              flush=True)
        status = {"complete": True}
        completed = False
        try:
            deadline = time.monotonic() + args.max_repo_seconds
            repo, display = clone_repo(name, args.cache_dir, deadline)
            head = head_revision(repo)
            ledger_key = f"{args.library}:{','.join(str(t['mapping_id']) for t in timelines)}:{display}"
            scan_signature = digest((code_hash, timeline_hash, args.library,
                                     args.max_files, args.max_commits,
                                     args.max_repo_seconds, args.full_repo_scan,
                                     sorted(candidate["paths"]), sorted(candidate["manifests"])))
            previous = ledger.get(ledger_key, {})
            if (not args.dry_run and not args.rescan and outputs_existed_at_start and
                    previous.get("head") == head and
                    previous.get("signature") == scan_signature):
                skipped += 1
                quality = "complete" if previous.get("complete", True) else "partial"
                print(f"  Already processed at this commit ({quality}); skipping", flush=True)
                continue
            attempted += 1
            print(f"  Processing {attempted}/{args.max_repos} new repositories", flush=True)
            paths = set(candidate["paths"])
            python_files = sum(p.endswith(".py") for p in repo_files(repo, head))
            print("  Finding API call files", flush=True)
            try:
                paths.update(candidate_paths(repo, head, targets, args.max_files,
                                             args.full_repo_scan, deadline, python_files,
                                             60 if paths else 20))
            except RuntimeError as exc:
                status["complete"] = False
                print(f"  Current-file search stopped: {exc}", flush=True)
            if (args.full_repo_scan or python_files <= 200 or
                    (not paths and candidate.get("version_specs", 0))):
                paths.update(history_candidate_paths(repo, head, targets,
                                                     args.max_files, deadline,
                                                     45 if paths else 25, status))
            time_left(deadline, 1)
            before = {kind: len(samples[kind]) for kind in ("O", "U")}
            if paths:
                stats = process_repo(repo, display, sorted(paths), candidate["manifests"],
                                     timelines, args.library, args.max_commits, deadline,
                                     samples, existing, status)
            else:
                print("  No candidate Python files found", flush=True)
                stats = Counter()
            print(f"{display}: files={len(paths)}, calls={stats['calls']}, "
                  f"distinct_calls={stats['distinct_calls']}, "
                  f"eligible_occurrences={stats['eligible']}, "
                  f"unique_eligible={stats['unique_eligible']}, "
                  f"new O={len(samples['O']) - before['O']}, "
                  f"new U={len(samples['U']) - before['U']}, "
                  f"seconds={time.monotonic() - repo_started:.1f}", flush=True)
            print(f"  Old/new: old_calls={stats['old_calls']}, new_calls={stats['new_calls']}, "
                  f"old_eligible_occurrences={stats['old_eligible']}, "
                  f"new_eligible_occurrences={stats['new_eligible']}", flush=True)
            if stats["reused_validations"]:
                print(f"  Reused validation for {stats['reused_validations']} calls", flush=True)
            rejected = ", ".join(f"{key}={stats[key]}" for key in (
                "no_manifest", "no_version_spec", "version_not_single_interval",
                "api_unavailable", "signature_mismatch", "duplicate", "read_error")
                if stats[key])
            if rejected:
                print(f"  Reasons: {rejected}", flush=True)
            completed = True
        except (RuntimeError, OSError) as exc:
            print(f"Skipped {name}: {exc}", flush=True)
        if not args.dry_run:
            save_outputs(folder, samples)
            if completed:
                ledger[ledger_key] = {"head": head, "signature": scan_signature,
                                      "complete": status["complete"]}
                write_json(ledger_path, ledger)
                if not status["complete"]:
                    print("  Partial scan saved; use --rescan or larger limits to revisit", flush=True)
        if (args.target_samples and
                sum(len(samples[kind]) for kind in ("O", "U")) - starting_total >= args.target_samples):
            print(f"Reached target of {args.target_samples} new samples", flush=True)
            break
    if not args.dry_run and not selected:
        save_outputs(folder, samples)
    print(f"Processed {attempted} repositories; skipped {skipped} already processed", flush=True)
    print(f"Batch: new O={len(samples['O']) - starting_counts['O']}, "
          f"new U={len(samples['U']) - starting_counts['U']}, "
          f"seconds={time.monotonic() - run_started:.1f}", flush=True)
    for kind in ("O", "U"):
        output = folder / f"{kind}_crawled.json"
        print(f"{output}: {len(samples[kind])} samples" +
              (" (dry run)" if args.dry_run else ""), flush=True)


if __name__ == "__main__":
    main()
