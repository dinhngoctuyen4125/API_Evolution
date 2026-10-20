"""Find source-observed Python API changes between curated releases.

Only intervals with a relevant candidate go to the main output. Intervals that
cannot be checked reliably go to a separate unresolved output. This script does
not read release notes or claim that an implementation diff changes behavior.
"""

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from pathlib import Path

from crawl_source import validate_source
from enrich_pypi_versions import REPOSITORIES


ROOT = Path(__file__).resolve().parent
PAIRS_PATH = ROOT / "data" / "api_mapping_pairs.json"
VERSIONS_PATH = ROOT / "data" / "pypi_versions_with_github_diy.json"
DEFAULT_OUTPUT = ROOT / "data" / "source_change_candidates.json"
DEFAULT_UNRESOLVED = ROOT / "data" / "source_trace_unresolved.json"
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
ROOT_MODULE = {"pytorch": "torch", "scikit-learn": "sklearn"}
MAX_IMPORT_DEPTH = 24


def git(repo, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo), *map(str, args)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result


def version_key(record):
    return tuple(map(int, record["version"].split(".")))


def expr(node):
    return ast.unparse(node) if node is not None else None


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def module_statements(body, conditional=False):
    """Yield module-level statements, including imports in guarded branches."""
    for item in body:
        yield item, conditional
        branches = []
        if isinstance(item, (ast.If, ast.Try)):
            branches = [item.body, item.orelse]
            if isinstance(item, ast.Try):
                branches.extend(handler.body for handler in item.handlers)
                branches.append(item.finalbody)
        elif isinstance(item, (ast.With, ast.AsyncWith)):
            branches = [item.body]
        for branch in branches:
            yield from module_statements(branch, True)


def literal_exports(tree):
    if tree is None:
        return None
    statements = list(module_statements(tree.body))
    if any(
        isinstance(item, ast.AugAssign) and isinstance(item.target, ast.Name)
        and item.target.id == "__all__"
        or isinstance(item, ast.Expr) and isinstance(item.value, ast.Call)
        and dotted(item.value.func) in {"__all__.append", "__all__.extend", "__all__.remove"}
        for item, _ in statements
    ):
        return None
    for item, _ in statements:
        if isinstance(item, (ast.Assign, ast.AnnAssign)):
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            if any(isinstance(target, ast.Name) and target.id == "__all__"
                   for target in targets):
                try:
                    value = ast.literal_eval(item.value)
                except (ValueError, TypeError, SyntaxError):
                    return None
                if isinstance(value, (list, tuple, set)) and all(
                    isinstance(name, str) for name in value
                ):
                    return set(value)
    return None


def signature(node):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    args = node.args
    positional = args.posonlyargs + args.args
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    result = []
    for index, (arg, default) in enumerate(zip(positional, defaults)):
        result.append({
            "name": arg.arg,
            "kind": "positional_only" if index < len(args.posonlyargs) else "positional_or_keyword",
            "default": expr(default),
            "annotation": expr(arg.annotation),
        })
    if args.vararg:
        result.append({"name": args.vararg.arg, "kind": "var_positional",
                       "default": None, "annotation": expr(args.vararg.annotation)})
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        result.append({"name": arg.arg, "kind": "keyword_only",
                       "default": expr(default), "annotation": expr(arg.annotation)})
    if args.kwarg:
        result.append({"name": args.kwarg.arg, "kind": "var_keyword",
                       "default": None, "annotation": expr(args.kwarg.annotation)})
    return result


def body_hash(node):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    value = ast.dump(ast.Module(body=body, type_ignores=[]), include_attributes=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def warning_calls(node):
    return any(
        isinstance(child, ast.Call) and dotted(child.func) in {"warn", "warnings.warn"}
        and (any("DeprecationWarning" in (expr(arg) or "") for arg in child.args[1:])
             or any(keyword.arg == "category" and "DeprecationWarning" in
                    (expr(keyword.value) or "") for keyword in child.keywords))
        for child in ast.walk(node)
    )


def delegated_call(node):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    for statement in node.body:
        if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Call):
            return dotted(statement.value.func)
    return None


@dataclass
class Resolution:
    status: str
    reason: str | None = None
    path: str | None = None
    node: ast.AST | None = None
    tree: ast.AST | None = None
    module: str | None = None
    alias_target: str | None = None


class SourceReader:
    def __init__(self, repo, library):
        self.repo = repo
        self.library = library
        self.root_module = ROOT_MODULE.get(library, library.replace("-", "_"))
        self.trees = OrderedDict()
        self.parsed = OrderedDict()
        self.states = {}

    def paths(self, sha):
        if sha not in self.trees:
            output = git(self.repo, "ls-tree", "-r", "--name-only", sha).stdout
            self.trees[sha] = set(output.splitlines())
            if len(self.trees) > 8:
                self.trees.popitem(last=False)
        self.trees.move_to_end(sha)
        return self.trees[sha]

    def module_file(self, sha, module):
        stem = module.replace(".", "/")
        files = self.paths(sha)
        for candidate in (f"{stem}/__init__.py", f"{stem}.py"):
            if candidate in files:
                return candidate
        # Some distributions place their import package under src/python/.
        matches = [path for path in files if path.endswith(f"/{stem}/__init__.py")
                   or path.endswith(f"/{stem}.py")]
        return matches[0] if len(matches) == 1 else None

    def parse(self, sha, path):
        key = (sha, path)
        if key not in self.parsed:
            raw = git(self.repo, "show", f"{sha}:{path}").stdout
            try:
                self.parsed[key] = ast.parse(raw, filename=path)
            except SyntaxError:
                self.parsed[key] = None
            if len(self.parsed) > 128:
                self.parsed.popitem(last=False)
        self.parsed.move_to_end(key)
        return self.parsed[key]

    def relative_module(self, current, path, statement):
        if statement.level == 0:
            return statement.module or ""
        package = current if path.endswith("/__init__.py") else current.rpartition(".")[0]
        parts = package.split(".") if package else []
        if statement.level > len(parts):
            return None
        parts = parts[:len(parts) - statement.level + 1]
        if statement.module:
            parts.extend(statement.module.split("."))
        return ".".join(parts)

    def resolve_name(self, sha, module, name, seen=None):
        seen = seen or set()
        key = (module, name)
        if key in seen or len(seen) >= MAX_IMPORT_DEPTH:
            return Resolution("unknown", "cyclic_or_deep_import")
        seen = seen | {key}
        path = self.module_file(sha, module)
        if path is None:
            return Resolution("unknown", "module_source_missing")
        tree = self.parse(sha, path)
        if tree is None:
            return Resolution("unknown", "source_syntax_not_supported", path=path)

        statements = list(module_statements(tree.body))
        for item, conditional in statements:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and item.name == name:
                return Resolution("present", "conditional_definition" if conditional else None,
                                  path=path, node=item, tree=tree, module=module)
            if isinstance(item, ast.Assign):
                if any(isinstance(target, ast.Name) and target.id == name for target in item.targets):
                    return Resolution("present", "conditional_definition" if conditional else None,
                                      path=path, node=item, tree=tree,
                                      module=module, alias_target=dotted(item.value))
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                if item.target.id == name:
                    return Resolution("present", "conditional_definition" if conditional else None,
                                      path=path, node=item, tree=tree,
                                      module=module, alias_target=dotted(item.value))

        uncertain = False
        for item, conditional in statements:
            if isinstance(item, ast.ImportFrom):
                imported = self.relative_module(module, path, item)
                if imported is None:
                    uncertain = True
                    continue
                for alias in item.names:
                    if (alias.asname or alias.name) == name:
                        result = self.resolve_name(sha, imported, alias.name, seen)
                        if result.status == "unknown":
                            submodule = f"{imported}.{alias.name}"
                            if self.module_file(sha, submodule):
                                return Resolution("present", "conditional_import" if conditional else None,
                                                  path=path, node=item,
                                                  tree=tree, module=submodule)
                        if conditional and result.status == "present" and result.reason is None:
                            result.reason = "conditional_import"
                        return result
                    if alias.name == "*" and not name.startswith("_"):
                        imported_path = self.module_file(sha, imported)
                        exports = literal_exports(self.parse(sha, imported_path)) if imported_path else None
                        if exports is not None and name not in exports:
                            continue
                        result = self.resolve_name(sha, imported, name, seen)
                        if result.status == "present":
                            if conditional and result.reason is None:
                                result.reason = "conditional_import"
                            return result
                        uncertain |= result.status == "unknown"
            elif isinstance(item, ast.Import):
                for alias in item.names:
                    if (alias.asname or alias.name.split(".")[0]) == name:
                        return Resolution("present", "conditional_import" if conditional else None,
                                          path=path, node=item,
                                          tree=tree, module=alias.name)

        if self.module_file(sha, f"{module}.{name}"):
            return Resolution("present", path=path, tree=tree, module=f"{module}.{name}")
        if any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
               and item.name == "__getattr__" for item, _ in statements):
            uncertain = True
        return Resolution("unknown" if uncertain else "absent",
                          "dynamic_or_unresolved_export" if uncertain else None, path=path)

    def resolve_api(self, sha, api):
        parts = api.split(".")
        if len(parts) < 2 or parts[0] != self.root_module:
            return Resolution("unknown", "api_root_does_not_match_library")
        module = parts[0]
        result = None
        for index, name in enumerate(parts[1:], start=1):
            result = self.resolve_name(sha, module, name)
            if result.status != "present" or index == len(parts) - 1:
                return result
            if isinstance(result.node, ast.ClassDef):
                for child_name in parts[index + 1:]:
                    result = self.resolve_class_member(sha, result, child_name)
                    if result.status != "present":
                        return result
                return result
            if result.module and result.module != module and self.module_file(sha, result.module):
                module = result.module
            elif self.module_file(sha, f"{module}.{name}"):
                module = f"{module}.{name}"
            else:
                return Resolution("unknown", "nested_attribute_unresolved", path=result.path)
        return result or Resolution("unknown", "empty_api")

    def resolve_class_member(self, sha, parent, name, seen=None):
        seen = seen or set()
        node = parent.node
        key = (parent.module, getattr(node, "name", None), name)
        if key in seen or len(seen) >= MAX_IMPORT_DEPTH:
            return Resolution("unknown", "cyclic_or_deep_inheritance", path=parent.path)
        seen = seen | {key}
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                    and item.name == name:
                return Resolution("present", path=parent.path, node=item,
                                  tree=parent.tree, module=parent.module)
            if isinstance(item, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in item.targets
            ):
                return Resolution("present", path=parent.path, node=item,
                                  tree=parent.tree, module=parent.module,
                                  alias_target=dotted(item.value))
        uncertain = False
        for base in node.bases:
            base_name = dotted(base)
            if not base_name:
                uncertain = True
                continue
            if "." in base_name:
                prefix, _, class_name = base_name.rpartition(".")
                namespace = self.resolve_name(sha, parent.module, prefix)
                inherited = (self.resolve_name(sha, namespace.module, class_name)
                             if namespace.status == "present" and namespace.module
                             else Resolution("unknown", "qualified_base_unresolved"))
            else:
                inherited = self.resolve_name(sha, parent.module, base_name)
            if inherited.status != "present" or not isinstance(inherited.node, ast.ClassDef):
                uncertain = True
                continue
            found = self.resolve_class_member(sha, inherited, name, seen)
            if found.status == "present":
                return found
            uncertain |= found.status == "unknown"
        return Resolution("unknown" if uncertain else "absent",
                          "class_member_or_inheritance_unresolved" if uncertain else None,
                          path=parent.path)

    def state(self, sha, api):
        key = (sha, api)
        if key in self.states:
            return self.states[key]
        resolved = self.resolve_api(sha, api)
        node = resolved.node
        warning = warning_calls(node) if node is not None else None
        warning_line = getattr(node, "lineno", None) if warning else None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and resolved.tree:
            # NumPy-style dispatch decorators can emit the warning in a helper.
            helper_names = {child.id for dec in node.decorator_list
                            for child in ast.walk(dec) if isinstance(child, ast.Name)}
            for item in resolved.tree.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and item.name in helper_names and warning_calls(item):
                    warning = True
                    warning_line = item.lineno
        state = {
            "api": api, "public_status": resolved.status,
            "reason": resolved.reason, "source_path": resolved.path,
            "symbol": getattr(node, "name", None),
            "line_start": getattr(node, "lineno", None),
            "line_end": getattr(node, "end_lineno", None),
            "declared_parameters": signature(node),
            "alias_target": resolved.alias_target,
            "delegates_to": delegated_call(node),
            "deprecation_warning_path": warning,
            "warning_line_start": warning_line,
            "implementation_hash": body_hash(node),
        }
        self.states[key] = state
        return state


def compare_parameters(before, after):
    if before is None or after is None:
        return []
    old = {item["name"]: item for item in before}
    new = {item["name"]: item for item in after}
    changes = []
    for name in old.keys() - new.keys():
        changes.append({"kind": "parameter_removed", "parameter": name,
                        "before": old[name], "after": None})
    for name in new.keys() - old.keys():
        changes.append({"kind": "parameter_added", "parameter": name,
                        "before": None, "after": new[name]})
    for name in old.keys() & new.keys():
        for field, kind in (("default", "parameter_default_changed"),
                            ("kind", "parameter_kind_changed"),
                            ("annotation", "annotation_changed")):
            if old[name][field] != new[name][field]:
                changes.append({"kind": kind, "parameter": name,
                                "before": old[name][field], "after": new[name][field]})
    if [item["name"] for item in before] != [item["name"] for item in after] and not changes:
        changes.append({"kind": "parameter_order_changed",
                        "before": before, "after": after})
    return sorted(changes, key=lambda item: (item["kind"], item.get("parameter", "")))


def compare_states(before, after):
    api = after["api"]
    old_status, new_status = before["public_status"], after["public_status"]
    if "unknown" in (old_status, new_status):
        return [], "public_api_unresolved"
    if old_status != new_status:
        kind = "public_api_added" if new_status == "present" else "public_api_removed"
        verification = "requires_review" if any(
            state["reason"] in {"conditional_import", "conditional_definition"}
            for state in (before, after)
        ) else "verified"
        return [{"api": api, "kind": kind, "before": old_status,
                 "after": new_status, "verification_status": verification}], None
    if old_status == "absent":
        return [], None
    changes = compare_parameters(before["declared_parameters"], after["declared_parameters"])
    for field, kind in (("alias_target", "alias_target_changed"),
                        ("delegates_to", "delegation_changed"),
                        ("deprecation_warning_path", "warning_path_changed")):
        if before[field] != after[field]:
            if field == "deprecation_warning_path":
                kind = "warning_path_added" if after[field] else "warning_path_removed"
            changes.append({"kind": kind, "before": before[field], "after": after[field]})
    if before["implementation_hash"] and after["implementation_hash"]:
        if before["implementation_hash"] != after["implementation_hash"] and not changes:
            changes.append({"kind": "implementation_changed", "before": None,
                            "after": None, "verification_status": "requires_review"})
    for change in changes:
        if before["reason"] in {"conditional_import", "conditional_definition"} or \
                after["reason"] in {"conditional_import", "conditional_definition"}:
            change.setdefault("verification_status", "requires_review")
        else:
            change.setdefault("verification_status", "verified")
        change["api"] = api
    return changes, None


def evidence(library, sha, state):
    path = state["source_path"]
    if not path:
        return None
    line = state["line_start"]
    url = f"https://github.com/{REPOSITORIES[library]}/blob/{sha}/{path}"
    if line:
        url += f"#L{line}"
    return {"kind": "source", "commit_sha": sha, "source_path": path,
            "line_start": line, "line_end": state["line_end"],
            "warning_line_start": state["warning_line_start"], "url": url}


def diff_evidence(repo, old_sha, new_sha, paths):
    if not paths:
        return None
    result = git(repo, "diff", "--no-ext-diff", "--unified=0", old_sha, new_sha,
                 "--", *sorted(paths))
    if not result.stdout:
        return None
    return {"kind": "git_diff", "from_commit_sha": old_sha,
            "to_commit_sha": new_sha, "source_paths": sorted(paths),
            "diff_sha256": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
            "hunk_headers": [line for line in result.stdout.splitlines()
                             if line.startswith("@@ ")][:40]}


def validate_version(repo, library, record):
    sha = record.get("commit_sha")
    if not isinstance(sha, str) or not FULL_SHA.fullmatch(sha):
        return "invalid_commit_sha"
    expected = f"https://github.com/{REPOSITORIES[library]}/tree/{sha}"
    if record.get("source_url") != expected:
        return "source_url_mismatch"
    if git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode:
        return "commit_missing"
    tag = record.get("tag")
    if not isinstance(tag, str) or not tag:
        return "tag_missing"
    result = git(repo, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}", check=False)
    if result.returncode:
        return "tag_not_fetched"
    return "tag_matches_commit" if result.stdout.strip().lower() == sha.lower() else "tag_mismatch"


def scan_pair(pair, versions, reader, version_status):
    candidates, unresolved = [], []
    for old, new in zip(versions, versions[1:]):
        old_sha, new_sha = old["commit_sha"], new["commit_sha"]
        base = {"mapping_id": pair["id"], "library": pair["library"],
                "deprecated_api": pair["deprecated_api"],
                "replacement_api": pair["replacement_api"],
                "from_version": old["version"], "to_version": new["version"],
                "from_commit_sha": old_sha, "to_commit_sha": new_sha}
        statuses = (version_status[old["version"]], version_status[new["version"]])
        if any(status in {"invalid_commit_sha", "source_url_mismatch", "commit_missing",
                              "tag_mismatch"} for status in statuses):
            unresolved.append({**base, "reason": "version_mapping_invalid",
                               "mapping_status": list(statuses)})
            continue
        mapping_unverified = any(status in {"tag_missing", "tag_not_fetched"}
                                 for status in statuses)
        if mapping_unverified:
            unresolved.append({**base, "reason": "version_mapping_unverified",
                               "mapping_status": list(statuses)})
        try:
            states = {api: (reader.state(old_sha, api), reader.state(new_sha, api))
                      for api in (pair["deprecated_api"], pair["replacement_api"])}
            changes = []
            reasons = []
            for before, after in states.values():
                found, reason = compare_states(before, after)
                changes.extend(found)
                if reason:
                    reasons.append({"api": after["api"], "reason": reason,
                                    "before": before["reason"], "after": after["reason"]})
            if reasons:
                unresolved.append({**base, "reason": "source_resolution_incomplete",
                                   "details": reasons})
            if not changes:
                continue
            source_evidence = []
            for api, (before, after) in states.items():
                if not any(change["api"] == api for change in changes):
                    continue
                for sha, state in ((old_sha, before), (new_sha, after)):
                    item = evidence(pair["library"], sha, state)
                    if item and item not in source_evidence:
                        source_evidence.append(item)
            for index, item in enumerate(source_evidence, start=1):
                item["id"] = f"src-{pair['id']}-{old['version']}-{new['version']}-{index}"
            changed_paths = {item["source_path"] for item in source_evidence}
            diff_item = diff_evidence(reader.repo, old_sha, new_sha, changed_paths)
            if diff_item:
                diff_item["id"] = f"diff-{pair['id']}-{old['version']}-{new['version']}"
                source_evidence.append(diff_item)
            for change in changes:
                change["observation_kind"] = "source_observed"
                change["evidence_refs"] = [item["id"] for item in source_evidence
                                           if item.get("kind") == "git_diff" or item["source_path"] in {
                                               states[change["api"]][0]["source_path"],
                                               states[change["api"]][1]["source_path"]}]
            candidates.append({**base, "artifact_source_match_status": "not_verified",
                               "tag_status": list(statuses),
                               "api_states_before": {api: state[0] for api, state in states.items()},
                               "api_states_after": {api: state[1] for api, state in states.items()},
                               "changes": changes, "evidence": source_evidence,
                               "comparison_status": "partial" if reasons or mapping_unverified
                               else "rule_checked"})
        except (OSError, RuntimeError) as exc:
            unresolved.append({**base, "reason": "source_read_failed", "detail": str(exc)})
    return candidates, unresolved


def load_inputs():
    pairs = json.loads(PAIRS_PATH.read_text(encoding="utf-8"))
    versions = json.loads(VERSIONS_PATH.read_text(encoding="utf-8"))
    if not isinstance(pairs, list) or not isinstance(versions, list):
        raise ValueError("Both input files must contain JSON arrays")
    grouped = defaultdict(list)
    for item in versions:
        grouped[item["library"]].append(item)
    for records in grouped.values():
        records.sort(key=version_key)
    return pairs, grouped


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", choices=sorted(REPOSITORIES))
    parser.add_argument("--mapping-id", type=int)
    parser.add_argument("--from-version", help="First version to scan (inclusive)")
    parser.add_argument("--to-version", help="Last version to scan (inclusive)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--unresolved-output", type=Path, default=DEFAULT_UNRESOLVED)
    args = parser.parse_args(argv)
    pairs, grouped = load_inputs()
    if args.mapping_id is not None:
        pairs = [pair for pair in pairs if pair["id"] == args.mapping_id]
        if not pairs:
            parser.error(f"Unknown mapping id: {args.mapping_id}")
    if args.library:
        pairs = [pair for pair in pairs if pair["library"] == args.library]
    if not pairs:
        parser.error("No migration pairs match the selected filters")
    candidates, unresolved = [], []
    for library in sorted({pair["library"] for pair in pairs}):
        selected = [pair for pair in pairs if pair["library"] == library]
        versions = grouped[library]
        names = [item["version"] for item in versions]
        if args.from_version:
            if args.from_version not in names:
                parser.error(f"{library}: unknown from-version {args.from_version}")
            versions = versions[names.index(args.from_version):]
        if args.to_version:
            names = [item["version"] for item in versions]
            if args.to_version not in names:
                parser.error(f"{library}: unknown or earlier to-version {args.to_version}")
            versions = versions[:names.index(args.to_version) + 1]
        repo = ROOT / "cache" / library / "source"
        if not repo.is_dir():
            for pair in selected:
                unresolved.append({"mapping_id": pair["id"], "library": library,
                                   "reason": "source_repository_missing"})
            continue
        try:
            validate_source(repo, f"https://github.com/{REPOSITORIES[library]}.git")
        except (OSError, RuntimeError) as exc:
            for pair in selected:
                unresolved.append({"mapping_id": pair["id"], "library": library,
                                   "reason": "source_repository_invalid", "detail": str(exc)})
            continue
        reader = SourceReader(repo, library)
        version_status = {item["version"]: validate_version(repo, library, item)
                          for item in versions}
        for pair in selected:
            found, missing = scan_pair(pair, versions, reader, version_status)
            candidates.extend(found)
            unresolved.extend(missing)
            print(f"{library} #{pair['id']}: {len(found)} changes, "
                  f"{len(missing)} unresolved intervals", file=sys.stderr)
    write_json(args.output, candidates)
    write_json(args.unresolved_output, unresolved)
    print(f"Saved {len(candidates)} change records to {args.output}")
    print(f"Saved {len(unresolved)} unresolved intervals to {args.unresolved_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
