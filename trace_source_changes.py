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
DEFAULT_COVERAGE = ROOT / "data" / "source_trace_coverage.json"
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
ROOT_MODULE = {"pytorch": "torch", "scikit-learn": "sklearn"}
GENERATED_PUBLIC_ROOTS = {"tensorflow"}
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
    exports = None
    for item, conditional in module_statements(tree.body):
        if isinstance(item, (ast.Assign, ast.AnnAssign)):
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            if any(isinstance(target, ast.Name) and target.id == "__all__"
                   for target in targets):
                if conditional:
                    return None
                try:
                    value = ast.literal_eval(item.value)
                except (ValueError, TypeError, SyntaxError):
                    return None
                if isinstance(value, (list, tuple, set)) and all(
                    isinstance(name, str) for name in value
                ):
                    exports = set(value)
                else:
                    return None
        elif isinstance(item, ast.AugAssign) and isinstance(item.target, ast.Name) \
                and item.target.id == "__all__":
            if conditional or exports is None or not isinstance(item.op, ast.Add):
                return None
            try:
                values = ast.literal_eval(item.value)
            except (ValueError, TypeError, SyntaxError):
                return None
            if not isinstance(values, (list, tuple, set)) or not all(isinstance(v, str) for v in values):
                return None
            exports.update(values)
        elif isinstance(item, ast.Expr) and isinstance(item.value, ast.Call) \
                and dotted(item.value.func) in {"__all__.append", "__all__.extend", "__all__.remove"}:
            if conditional or exports is None or len(item.value.args) != 1 or item.value.keywords:
                return None
            try:
                value = ast.literal_eval(item.value.args[0])
            except (ValueError, TypeError, SyntaxError):
                return None
            method = dotted(item.value.func)
            if method == "__all__.extend" and isinstance(value, (list, tuple, set)) \
                    and all(isinstance(v, str) for v in value):
                exports.update(value)
            elif method == "__all__.append" and isinstance(value, str):
                exports.add(value)
            elif method == "__all__.remove" and isinstance(value, str) and value in exports:
                exports.remove(value)
            else:
                return None
    return exports


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


def warning_observations(node, api_symbol):
    """Classify static deprecation calls without treating parameter warnings as API warnings."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    parameters = {item["name"] for item in signature(node) or []}
    literal_messages = {}
    for statement in node.body:
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Constant) \
                and isinstance(statement.value.value, str):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    literal_messages[target.id] = (statement.value.value, statement.lineno)
    observations = []

    def visit(item, guards=()):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        if isinstance(item, ast.If):
            tested = {child.id for child in ast.walk(item.test) if isinstance(child, ast.Name)}
            for child in item.body + item.orelse:
                visit(child, guards + (tested,))
            return
        if isinstance(item, ast.Call) and dotted(item.func) in {"warn", "warnings.warn"} \
                and (any("DeprecationWarning" in (expr(arg) or "") for arg in item.args[1:])
                     or any(keyword.arg == "category" and "DeprecationWarning" in
                            (expr(keyword.value) or "") for keyword in item.keywords)):
            affected = sorted(set().union(*guards) & parameters) if guards else []
            message = ""
            if item.args:
                first = item.args[0]
                if isinstance(first, ast.Name):
                    known = literal_messages.get(first.id)
                    message = known[0] if known and known[1] < item.lineno else ""
                else:
                    message = expr(first) or ""
            if affected:
                scope = "parameter"
            elif not guards and re.search(rf"\b{re.escape(api_symbol)}\b", message):
                scope = "api"
            else:
                scope = "unknown"
            observations.append({"scope": scope, "line_start": item.lineno,
                                 "affected_parameters": affected})
        for child in ast.iter_child_nodes(item):
            visit(child, guards)

    for statement in node.body:
        visit(statement)
    return observations


def delegated_call(node):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    for statement in node.body:
        if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Call):
            return dotted(statement.value.func)
    return None


def forwarding_wrapper_target(node):
    """Recognize an unchanged *args/**kwargs pass-through to a local function."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    args = node.args
    if args.posonlyargs or args.args or args.kwonlyargs or not args.vararg or not args.kwarg:
        return None
    body = node.body[1:] if node.body and isinstance(node.body[0], ast.Expr) \
        and isinstance(node.body[0].value, ast.Constant) \
        and isinstance(node.body[0].value.value, str) else node.body
    if not body or not all(
        isinstance(item, ast.Expr) and isinstance(item.value, ast.Call)
        and dotted(item.value.func) in {"warn", "warnings.warn"}
        for item in body[:-1]
    ):
        return None
    last = body[-1]
    if not isinstance(last, ast.Return) or not isinstance(last.value, ast.Call):
        return None
    call = last.value
    if not isinstance(call.func, ast.Name) or call.func.id == node.name:
        return None
    if len(call.args) != 1 or not isinstance(call.args[0], ast.Starred) \
            or not isinstance(call.args[0].value, ast.Name) \
            or call.args[0].value.id != args.vararg.arg:
        return None
    if len(call.keywords) != 1 or call.keywords[0].arg is not None \
            or not isinstance(call.keywords[0].value, ast.Name) \
            or call.keywords[0].value.id != args.kwarg.arg:
        return None
    return call.func.id


def excludes_name(test, parameter, name):
    """Return True only when an if-condition is provably false for name."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    left, right = test.left, test.comparators[0]
    if isinstance(left, ast.Name) and left.id == parameter and isinstance(right, ast.Constant) \
            and isinstance(right.value, str):
        if isinstance(test.ops[0], ast.Eq):
            return name != right.value
        if isinstance(test.ops[0], ast.NotEq):
            return name == right.value
    return False


def getattr_excludes_name(node, name):
    """Prove a module __getattr__ cannot return this name; otherwise stay unknown."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.args.args:
        return False
    final = node.body[-1] if node.body else None
    if not isinstance(final, ast.Raise) or not isinstance(final.exc, ast.Call) \
            or dotted(final.exc.func) != "AttributeError":
        return False
    parameter = node.args.args[0].arg

    def can_return(nodes):
        for item in nodes:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(item, (ast.Return, ast.Yield, ast.YieldFrom)):
                return True
            if isinstance(item, ast.If):
                if not excludes_name(item.test, parameter, name) and can_return(item.body):
                    return True
                if can_return(item.orelse):
                    return True
            elif can_return(ast.iter_child_nodes(item)):
                return True
        return False

    return not can_return(node.body)


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

    def exact_module_file(self, sha, module):
        stem = module.replace(".", "/")
        files = self.paths(sha)
        return next((candidate for candidate in (f"{stem}/__init__.py", f"{stem}.py")
                     if candidate in files), None)

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

    def relative_module(self, sha, current, path, statement):
        if statement.level == 0:
            imported = statement.module or ""
            if self.exact_module_file(sha, imported):
                return imported
            # Python 2 packages commonly used implicit intra-package imports.
            package = current if path.endswith("/__init__.py") else current.rpartition(".")[0]
            local = f"{package}.{imported}" if package and imported else imported
            return local if self.exact_module_file(sha, local) else imported
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
                imported = self.relative_module(sha, module, path, item)
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
                        imported = alias.name
                        if not self.exact_module_file(sha, imported):
                            package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
                            local = f"{package}.{imported}" if package else imported
                            if self.exact_module_file(sha, local):
                                imported = local
                        return Resolution("present", "conditional_import" if conditional else None,
                                          path=path, node=item,
                                          tree=tree, module=imported)

        if self.module_file(sha, f"{module}.{name}"):
            return Resolution("present", path=path, tree=tree, module=f"{module}.{name}")
        getters = [item for item, _ in statements
                   if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and item.name == "__getattr__"]
        dynamic_loader = any(
            isinstance(item, ast.Expr) and isinstance(item.value, ast.Call)
            and dotted(item.value.func) == "pkgload"
            for item, _ in statements
        )
        if getters and not all(getattr_excludes_name(item, name) for item in getters):
            uncertain = True
        uncertain |= dynamic_loader
        generated_root = module == self.root_module and module in GENERATED_PUBLIC_ROOTS
        uncertain |= generated_root
        return Resolution("unknown" if uncertain else "absent",
                          "dynamic_package_loader" if dynamic_loader else
                          "generated_public_namespace_unresolved" if generated_root else
                          "dynamic_or_unresolved_export" if uncertain else
                          "getattr_excludes_name" if getters else None, path=path)

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
        wrapper_target = forwarding_wrapper_target(node)
        effective = signature(node)
        effective_status = "direct" if effective is not None else "unavailable"
        effective_resolution = resolved
        if wrapper_target and resolved.module:
            target = self.resolve_name(sha, resolved.module, wrapper_target)
            effective = signature(target.node) if target.status == "present" else None
            effective_status = "forwarded" if effective is not None else "unresolved_forwarding"
            effective_resolution = target if effective is not None else resolved
        warnings = warning_observations(node, getattr(node, "name", ""))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and resolved.tree:
            # NumPy-style dispatch decorators can emit the warning in a helper.
            helper_names = {child.id for dec in node.decorator_list
                            for child in ast.walk(dec) if isinstance(child, ast.Name)}
            for item in resolved.tree.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and item.name in helper_names:
                    warnings.extend(warning_observations(item, node.name))
        warning = bool(warnings) if node is not None else None
        warning_scopes = sorted({item["scope"] for item in warnings})
        warning_line = warnings[0]["line_start"] if warnings else None
        state = {
            "api": api, "public_status": resolved.status,
            "reason": resolved.reason, "source_path": resolved.path,
            "symbol": getattr(node, "name", None),
            "line_start": getattr(node, "lineno", None),
            "line_end": getattr(node, "end_lineno", None),
            "declared_parameters": signature(node),
            "effective_parameters": effective,
            "effective_signature_status": effective_status,
            "signature_inference_status": (
                "resolved_static_wrapper" if effective_status == "forwarded" else
                "resolved_static_declaration" if effective_status == "direct" else "unknown"
            ),
            "runtime_signature_status": "not_checked",
            "effective_source_path": effective_resolution.path,
            "effective_line_start": getattr(effective_resolution.node, "lineno", None),
            "effective_line_end": getattr(effective_resolution.node, "end_lineno", None),
            "wrapper_target": wrapper_target,
            "alias_target": resolved.alias_target,
            "delegates_to": delegated_call(node),
            "deprecation_warning_path": ("api" in warning_scopes) if node is not None else None,
            "warning_observed": warning,
            "deprecation_warning_scopes": warning_scopes,
            "warning_observations": warnings,
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
    changes = compare_parameters(before["effective_parameters"], after["effective_parameters"])
    for field, kind in (("alias_target", "alias_target_changed"),
                        ("delegates_to", "delegation_changed"),
                        ("wrapper_target", "wrapper_forwarding_changed")):
        if before[field] != after[field]:
            if field == "delegates_to" and None in (before[field], after[field]):
                continue
            changes.append({"kind": kind, "before": before[field], "after": after[field]})
    for scope in sorted(set(before["deprecation_warning_scopes"])
                        | set(after["deprecation_warning_scopes"])):
        before_has = scope in before["deprecation_warning_scopes"]
        after_has = scope in after["deprecation_warning_scopes"]
        if before_has != after_has:
            kind = {"api": "warning_path", "parameter": "parameter_warning_path",
                    "unknown": "unclassified_warning_path"}[scope]
            change = {"kind": f"{kind}_{'added' if after_has else 'removed'}",
                      "warning_scope": scope, "before": before_has, "after": after_has}
            observed_state = after if after_has else before
            affected = sorted({parameter for item in observed_state["warning_observations"]
                               if item["scope"] == scope
                               for parameter in item["affected_parameters"]})
            if affected:
                change["affected_parameters"] = affected
                if len(affected) == 1:
                    change["parameter"] = affected[0]
            changes.append(change)
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
    reason = "effective_signature_unresolved" if "unresolved_forwarding" in (
        before["effective_signature_status"], after["effective_signature_status"]
    ) else None
    return changes, reason


PARAMETER_CHANGES = {"parameter_added", "parameter_removed", "parameter_default_changed",
                     "parameter_kind_changed", "annotation_changed", "parameter_order_changed"}


def classify_change_origin(change, before, after, states):
    """Distinguish a changed declaration from a changed forwarded target."""
    if change["kind"] not in PARAMETER_CHANGES:
        change["change_origin"] = "source_observation"
        return
    if before["effective_signature_status"] == after["effective_signature_status"] == "forwarded" \
            and before["wrapper_target"] == after["wrapper_target"]:
        change["change_origin"] = "forwarded_via_wrapper"
        for api, (_, target_after) in states.items():
            if api != change["api"] and target_after["source_path"] == after["effective_source_path"] \
                    and target_after["line_start"] == after["effective_line_start"]:
                change["origin_api"] = api
                break
    elif before["effective_signature_status"] == after["effective_signature_status"] == "direct":
        change["change_origin"] = "direct_declaration"
    else:
        change["change_origin"] = "mixed_or_unresolved"


def verify_source_change(change, before, after, mapping_status, evidence_items):
    """Verify narrow source claims, without claiming artifact or runtime equivalence."""
    properties = []
    source_refs = [item for item in evidence_items if item["kind"] == "source"
                   and item.get("api") == change["api"]]
    if any(item["commit_sha"] == mapping_status[0][0] and item["line_start"]
           for item in source_refs):
        properties.append("source_symbol_before_located")
    if any(item["commit_sha"] == mapping_status[1][0] and item["line_start"]
           for item in source_refs):
        properties.append("source_symbol_after_located")
    tags_match = all(status == "tag_matches_commit" for _, status in mapping_status)
    if tags_match:
        properties.append("git_tags_match_commit_shas")
    diff = next((item for item in evidence_items if item["kind"] == "git_diff"), None)
    hunks = [hunk for hunk in diff["relevant_hunks"]
             if change["api"] in hunk["related_apis"]] if diff else []
    parameter = change.get("parameter")
    hunk_support = bool(hunks) and (parameter is None or any(
        re.search(rf"\b{re.escape(parameter)}\b", hunk["excerpt"]) for hunk in hunks
    ))
    if hunk_support:
        properties.append("relevant_git_diff_hunk_located")
    old = {item["name"]: item for item in before["effective_parameters"] or []}
    new = {item["name"]: item for item in after["effective_parameters"] or []}
    structural = False
    if change["kind"] == "parameter_added" and parameter not in old and parameter in new:
        properties.extend(("parameter_absent_before", "parameter_present_after"))
        structural = True
    elif change["kind"] == "parameter_removed" and parameter in old and parameter not in new:
        properties.extend(("parameter_present_before", "parameter_absent_after"))
        structural = True
    elif change["kind"] in {"parameter_default_changed", "parameter_kind_changed",
                             "annotation_changed"} and parameter in old and parameter in new:
        field = {"parameter_default_changed": "default", "parameter_kind_changed": "kind",
                 "annotation_changed": "annotation"}[change["kind"]]
        structural = old[parameter][field] != new[parameter][field]
        if structural:
            properties.append(f"parameter_{field}_differs_between_snapshots")
    elif change["kind"] == "parameter_order_changed" and old and new:
        structural = [p["name"] for p in before["effective_parameters"]] != [
            p["name"] for p in after["effective_parameters"]]
        if structural:
            properties.append("parameter_order_differs_between_snapshots")
    if structural and change["change_origin"] == "forwarded_via_wrapper":
        properties.append("static_wrapper_forwarding_resolved")
    conditional = any(state["reason"] in {"conditional_import", "conditional_definition"}
                      for state in (before, after))
    source_located = all(key in properties for key in
                         ("source_symbol_before_located", "source_symbol_after_located"))
    if change["kind"] in PARAMETER_CHANGES and structural and source_located \
            and hunk_support and tags_match and not conditional \
            and change["change_origin"] != "mixed_or_unresolved":
        change["verification_status"] = "verified"
    else:
        change["verification_status"] = "requires_review"
    change["verified_properties"] = properties
    change["verification_scope"] = "git_source_only"


def evidence(library, sha, state, forwarded=False):
    path = state["effective_source_path"] if forwarded else state["source_path"]
    if not path:
        return None
    line = state["effective_line_start"] if forwarded else state["line_start"]
    url = f"https://github.com/{REPOSITORIES[library]}/blob/{sha}/{path}"
    if line:
        url += f"#L{line}"
    return {"kind": "source", "api": state["api"],
            "commit_sha": sha, "source_path": path,
            "role": "forwarded_target" if forwarded else "public_symbol",
            "line_start": line, "line_end": state["effective_line_end"] if forwarded else state["line_end"],
            "warning_line_start": state["warning_line_start"], "url": url}


def diff_evidence(repo, old_sha, new_sha, states, changes):
    regions = defaultdict(lambda: {"old": [], "new": []})
    changed_apis = {change["api"] for change in changes}
    for api in changed_apis:
        for side, state in zip(("old", "new"), states[api]):
            for prefix in ("", "effective_"):
                path = state.get(f"{prefix}source_path")
                start = state.get(f"{prefix}line_start")
                end = state.get(f"{prefix}line_end")
                if path and start:
                    regions[path][side].append((start, end or start, api))
            if state["source_path"] and state["warning_line_start"]:
                line = state["warning_line_start"]
                regions[state["source_path"]][side].append((line, line, api))
    paths = set(regions)
    if not paths:
        return None
    result = git(repo, "diff", "--no-ext-diff", "--find-renames", "--unified=0", old_sha, new_sha,
                 "--", *sorted(paths))
    if not result.stdout:
        return None
    hunk_pattern = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    old_path = new_path = None
    selected = []
    current = None

    def matching_apis(path, side, start, count):
        matches = set()
        for low, high, api in regions.get(path, {}).get(side, []):
            if count == 0 and low - 1 <= start <= high:
                matches.add(api)
            if count and start <= high and start + count - 1 >= low:
                matches.add(api)
        return matches

    def finish():
        if current and current["relevant"]:
            excerpt = "\n".join(current["lines"])
            selected.append({"old_path": old_path, "new_path": new_path,
                             "header": current["header"], "excerpt": excerpt[:2000],
                             "excerpt_truncated": len(excerpt) > 2000,
                             "related_apis": sorted(current["related_apis"])})

    for line in result.stdout.splitlines():
        if line.startswith("diff --git "):
            finish()
            current = None
            old_path = new_path = None
        elif line.startswith("--- "):
            old_path = line[6:] if line.startswith("--- a/") else None
        elif line.startswith("+++ "):
            new_path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@ "):
            finish()
            match = hunk_pattern.match(line)
            related = set()
            if match:
                related.update(matching_apis(old_path, "old", int(match.group(1)),
                                             int(match.group(2) or 1)))
                related.update(matching_apis(new_path, "new", int(match.group(3)),
                                             int(match.group(4) or 1)))
            current = {"header": line, "relevant": bool(related),
                       "related_apis": related, "lines": [line]}
        elif current:
            current["lines"].append(line)
    finish()
    if not selected:
        return None
    return {"kind": "git_diff", "from_commit_sha": old_sha,
            "to_commit_sha": new_sha, "source_paths": sorted(paths),
            "diff_sha256": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
            "hunk_headers": [item["header"] for item in selected],
            "relevant_hunks": selected}


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


def scan_pair(pair, versions, reader, version_status, progress=None, progress_every=10):
    candidates, unresolved = [], []
    total = max(0, len(versions) - 1)
    for index, (old, new) in enumerate(zip(versions, versions[1:]), start=1):
        if progress and progress_every and (
            index == 1 or index % progress_every == 0 or index == total
        ):
            progress(f"  interval {index}/{total}: {old['version']} -> {new['version']}")
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
            for change in changes:
                before, after = states[change["api"]]
                classify_change_origin(change, before, after, states)
            source_evidence = []
            for api, (before, after) in states.items():
                if not any(change["api"] == api for change in changes):
                    continue
                for sha, state in ((old_sha, before), (new_sha, after)):
                    for forwarded in (False, True) if state["wrapper_target"] else (False,):
                        item = evidence(pair["library"], sha, state, forwarded)
                        if item and item not in source_evidence:
                            source_evidence.append(item)
            for index, item in enumerate(source_evidence, start=1):
                item["id"] = f"src-{pair['id']}-{old['version']}-{new['version']}-{index}"
            diff_item = diff_evidence(reader.repo, old_sha, new_sha, states, changes)
            if diff_item:
                diff_item["id"] = f"diff-{pair['id']}-{old['version']}-{new['version']}"
                source_evidence.append(diff_item)
            for change in changes:
                change["observation_kind"] = "source_observed"
                change["evidence_refs"] = [item["id"] for item in source_evidence
                                           if (item["kind"] == "source" and item["api"] == change["api"])
                                           or (item["kind"] == "git_diff" and any(
                                               change["api"] in hunk["related_apis"]
                                               for hunk in item["relevant_hunks"]))]
                before, after = states[change["api"]]
                verify_source_change(change, before, after,
                                     ((old_sha, statuses[0]), (new_sha, statuses[1])),
                                     source_evidence)
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


def summarize_coverage(pair, versions, candidates, unresolved, blocked_reason=None):
    candidate_intervals = {(item["from_version"], item["to_version"])
                           for item in candidates}
    unresolved_intervals = {(item.get("from_version"), item.get("to_version"))
                            for item in unresolved if item.get("from_version")}
    planned = max(0, len(versions) - 1)
    scanned = 0 if blocked_reason else planned
    return {"mapping_id": pair["id"], "library": pair["library"],
            "deprecated_api": pair["deprecated_api"],
            "replacement_api": pair["replacement_api"],
            "version_scope": "collected_versions_only",
            "first_version": versions[0]["version"] if versions else None,
            "last_version": versions[-1]["version"] if versions else None,
            "versions_considered": len(versions), "intervals_planned": planned,
            "versions_scanned": 0 if blocked_reason else len(versions),
            "intervals_scanned": scanned,
            "candidate_intervals": len(candidate_intervals),
            "unresolved_intervals": len(unresolved_intervals),
            "unresolved_interval_refs": [
                {"from_version": old, "to_version": new}
                for old, new in sorted(unresolved_intervals)
            ],
            "rule_checked_without_candidate": max(
                0, scanned - len(candidate_intervals | unresolved_intervals)
            ),
            "coverage_status": "blocked" if blocked_reason else
            "partial" if unresolved or scanned == 0 else "rule_checked",
            "blocking_reason": blocked_reason,
            "historical_completeness": "not_claimed"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", choices=sorted(REPOSITORIES))
    parser.add_argument("--mapping-id", type=int)
    parser.add_argument("--from-version", help="First version to scan (inclusive)")
    parser.add_argument("--to-version", help="Last version to scan (inclusive)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--unresolved-output", type=Path, default=DEFAULT_UNRESOLVED)
    parser.add_argument("--coverage-output", type=Path, default=DEFAULT_COVERAGE)
    parser.add_argument("--progress-every", type=int, default=10,
                        help="Show progress every N version intervals (0 disables interval logs)")
    args = parser.parse_args(argv)
    if args.progress_every < 0:
        parser.error("--progress-every must be nonnegative")
    pairs, grouped = load_inputs()
    if args.mapping_id is not None:
        pairs = [pair for pair in pairs if pair["id"] == args.mapping_id]
        if not pairs:
            parser.error(f"Unknown mapping id: {args.mapping_id}")
    if args.library:
        pairs = [pair for pair in pairs if pair["library"] == args.library]
    if not pairs:
        parser.error("No migration pairs match the selected filters")
    candidates, unresolved, coverage = [], [], []
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
        def report(message):
            print(message, file=sys.stderr, flush=True)

        report(f"{library}: {len(selected)} pairs, {len(versions)} versions "
               f"({max(0, len(versions) - 1)} intervals per pair)")
        repo = ROOT / "cache" / library / "source"
        if not repo.is_dir():
            for pair in selected:
                missing = [{"mapping_id": pair["id"], "library": library,
                            "reason": "source_repository_missing"}]
                unresolved.extend(missing)
                coverage.append(summarize_coverage(pair, versions, [], missing,
                                                   "source_repository_missing"))
            continue
        try:
            validate_source(repo, f"https://github.com/{REPOSITORIES[library]}.git")
        except (OSError, RuntimeError) as exc:
            for pair in selected:
                missing = [{"mapping_id": pair["id"], "library": library,
                            "reason": "source_repository_invalid", "detail": str(exc)}]
                unresolved.extend(missing)
                coverage.append(summarize_coverage(pair, versions, [], missing,
                                                   "source_repository_invalid"))
            continue
        reader = SourceReader(repo, library)
        version_status = {}
        report(f"{library}: validating version metadata and Git tags...")
        for index, item in enumerate(versions, start=1):
            if index == 1 or index % 20 == 0 or index == len(versions):
                report(f"  validation {index}/{len(versions)}: {item['version']}")
            version_status[item["version"]] = validate_version(repo, library, item)
        for pair in selected:
            report(f"{library} #{pair['id']}: tracing "
                   f"{pair['deprecated_api']} -> {pair['replacement_api']}...")
            found, missing = scan_pair(pair, versions, reader, version_status,
                                       progress=report, progress_every=args.progress_every)
            candidates.extend(found)
            unresolved.extend(missing)
            coverage.append(summarize_coverage(pair, versions, found, missing))
            report(f"{library} #{pair['id']}: {len(found)} changes, "
                   f"{len(missing)} unresolved intervals")
    write_json(args.output, candidates)
    write_json(args.unresolved_output, unresolved)
    write_json(args.coverage_output, coverage)
    print(f"Saved {len(candidates)} change records to {args.output}")
    print(f"Saved {len(unresolved)} unresolved intervals to {args.unresolved_output}")
    print(f"Saved {len(coverage)} coverage records to {args.coverage_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
