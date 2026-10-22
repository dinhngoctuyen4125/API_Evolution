"""Find source-observed Python API changes between curated releases.

Only intervals with a relevant candidate go to the main output. Intervals that
cannot be checked reliably go to a separate unresolved output. This script does
not read release notes or claim that an implementation diff changes behavior.
"""

import argparse
import ast
import copy
import difflib
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


def legacy_line_rewrite(line):
    body, ending = line.rstrip("\r\n"), line[len(line.rstrip("\r\n")):]
    match = re.fullmatch(r"([ \t]*)except[ \t]+([\w.]+)[ \t]*,[ \t]*(\w+)[ \t]*:", body)
    if match:
        return f"{match[1]}except {match[2]} as {match[3]}:{ending}", "except_binding"
    match = re.fullmatch(r"([ \t]*)print[ \t]*>>[ \t]*([^,#]+),[ \t]*([^#]+)", body)
    if match:
        return f"{match[1]}print({match[3]}, file={match[2]}){ending}", "print_redirect"
    match = re.fullmatch(r"([ \t]*)print[ \t]+([^#]+)", body)
    if match:
        return f"{match[1]}print({match[2]}){ending}", "print_statement"
    return None, None


def parse_with_legacy_syntax(source, path):
    """Parse a few Python 2 statements without changing source line positions.

    Rewrite only the line reported by SyntaxError. Unsupported syntax remains
    unresolved instead of guessing at a whole-module translation.
    """
    lines = source.splitlines(keepends=True)
    rewrites = []
    for _ in range(32):
        try:
            tree = ast.parse("".join(lines), filename=path)
            redirect_lines = sorted({item.lineno for item in ast.walk(tree)
                                     if isinstance(item, ast.BinOp)
                                     and isinstance(item.left, ast.Name)
                                     and item.left.id == "print"
                                     and isinstance(item.op, ast.RShift)})
            rewrote_redirect = False
            for number in redirect_lines:
                replacement, kind = legacy_line_rewrite(lines[number - 1])
                if kind == "print_redirect":
                    lines[number - 1] = replacement
                    rewrites.append({"line_start": number, "kind": kind})
                    rewrote_redirect = True
            if rewrote_redirect:
                continue
            return tree, rewrites
        except SyntaxError as exc:
            if not exc.lineno or exc.lineno > len(lines):
                break
            index = exc.lineno - 1
            replacement, kind = legacy_line_rewrite(lines[index])
            if replacement is None or replacement == lines[index]:
                break
            lines[index] = replacement
            rewrites.append({"line_start": exc.lineno, "kind": kind})
    return None, []


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


def executable_statements(node):
    """Return function statements without the leading docstring."""
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def body_hash(node):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    body = executable_statements(node)
    value = ast.dump(ast.Module(body=body, type_ignores=[]), include_attributes=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalized_body_hash(node):
    """Normalize only identity-test negation, whose Python semantics are identical."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    body = executable_statements(node)

    class IdentityNegationNormalizer(ast.NodeTransformer):
        def visit_UnaryOp(self, item):
            self.generic_visit(item)
            comparison = item.operand
            if isinstance(item.op, ast.Not) and isinstance(comparison, ast.Compare) \
                    and len(comparison.ops) == 1:
                if isinstance(comparison.ops[0], ast.Is):
                    comparison.ops[0] = ast.IsNot()
                    return comparison
                if isinstance(comparison.ops[0], ast.IsNot):
                    comparison.ops[0] = ast.Is()
                    return comparison
            return item

    module = ast.Module(body=copy.deepcopy(body), type_ignores=[])
    normalized = IdentityNegationNormalizer().visit(module)
    return hashlib.sha256(ast.dump(normalized, include_attributes=False).encode("utf-8")).hexdigest()


def body_equal_after_parameter_rename(before, after, renames):
    """Check whether a body diff consists only of unambiguous parameter names."""
    if not renames or not all(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                              for node in (before, after)):
        return False
    nested_scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                     ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    for node in (before, after):
        if any(isinstance(item, nested_scopes + (ast.Global, ast.Nonlocal))
               for statement in executable_statements(node) for item in ast.walk(statement)):
            return False

    class RenameParameters(ast.NodeTransformer):
        def visit_Name(self, item):
            if item.id in renames:
                item.id = renames[item.id]
            return item

    old_body = ast.Module(body=copy.deepcopy(executable_statements(before)), type_ignores=[])
    new_body = ast.Module(body=executable_statements(after), type_ignores=[])
    old_body = RenameParameters().visit(old_body)
    return ast.dump(old_body, include_attributes=False) == \
        ast.dump(new_body, include_attributes=False)


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


def returned_calls(node):
    """Collect returned calls in this function, including guarded branches."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [], 0
    calls = []
    unresolved_returns = 0

    def visit(item):
        nonlocal unresolved_returns
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        if isinstance(item, ast.Return):
            if isinstance(item.value, ast.Call) and dotted(item.value.func):
                calls.append(item.value)
            else:
                unresolved_returns += 1
            return
        for child in ast.iter_child_nodes(item):
            visit(child)

    for statement in node.body:
        visit(statement)
    return calls, unresolved_returns


def delegation_observation(node):
    calls, unresolved = returned_calls(node)
    parameters = {item["name"] for item in signature(node) or []}
    assignments = defaultdict(set)

    def collect_assignments(item):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        if isinstance(item, (ast.Assign, ast.AnnAssign)):
            for target in item.targets if isinstance(item, ast.Assign) else [item.target]:
                if isinstance(target, ast.Name):
                    assignments[target.id].add(dotted(item.value))
        for child in ast.iter_child_nodes(item):
            collect_assignments(child)

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for statement in node.body:
            collect_assignments(statement)
    targets = set()
    details = []
    for call in calls:
        raw_target = dotted(call.func)
        target = raw_target
        via_local = None
        if isinstance(call.func, ast.Name) and raw_target not in parameters:
            bindings = assignments.get(raw_target, set())
            if len(bindings) == 1 and next(iter(bindings)):
                target = next(iter(bindings))
                via_local = raw_target
            elif bindings:
                unresolved += 1
        targets.add(target)
        root = target.split(".", 1)[0]
        kind = "object_method" if "." in target and root in parameters else "call_target"
        detail = {"target": target, "kind": kind}
        if via_local:
            detail["via_local"] = via_local
        if detail not in details:
            details.append(detail)
    targets = sorted(targets)
    if unresolved:
        status = "partial_return_resolution"
    elif not targets:
        status = "no_return_call_resolved"
    elif len(targets) == 1:
        status = "single_return_target"
    else:
        status = "multiple_return_targets"
    return {"targets": targets, "details": details, "status": status,
            "single_target": targets[0] if len(targets) == 1 and not unresolved else None}


def keyword_dict_updates(node):
    """Find simple assignments such as kwargs['keepdims'] = keepdims."""
    updates = defaultdict(list)
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return updates

    def visit(item, guards=()):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        if isinstance(item, ast.If):
            condition = expr(item.test)
            for child in item.body:
                visit(child, guards + (condition,))
            for child in item.orelse:
                visit(child, guards + (f"not ({condition})",))
            return
        if isinstance(item, (ast.Assign, ast.AnnAssign)):
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            for target in targets:
                if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) \
                        and isinstance(target.slice, ast.Constant) \
                        and isinstance(target.slice.value, str):
                    updates[target.value.id].append({
                        "keyword": target.slice.value, "value": expr(item.value),
                        "condition": " and ".join(guards) if guards else None,
                    })
        for child in ast.iter_child_nodes(item):
            visit(child, guards)

    for statement in node.body:
        visit(statement)
    return updates


def forwarded_arguments(node):
    """Describe statically visible forwarding of declared parameters in returns."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {}
    parameters = {item["name"] for item in signature(node) or []}
    updates = keyword_dict_updates(node)
    observations = defaultdict(list)
    calls, _ = returned_calls(node)
    aliases = defaultdict(set)
    for detail in delegation_observation(node)["details"]:
        if detail.get("via_local"):
            aliases[detail["via_local"]].add(detail["target"])

    def add(parameter, target, mode, keyword=None, condition=None):
        if parameter not in parameters:
            return
        value = {"target": target, "mode": mode}
        if keyword is not None:
            value["keyword"] = keyword
        if condition is not None:
            value["condition"] = condition
        if value not in observations[parameter]:
            observations[parameter].append(value)

    for call in calls:
        target = dotted(call.func)
        if target in aliases and len(aliases[target]) == 1:
            target = next(iter(aliases[target]))
        for argument in call.args:
            if isinstance(argument, ast.Name):
                add(argument.id, target, "direct_positional")
            elif isinstance(argument, ast.Starred) and isinstance(argument.value, ast.Name):
                add(argument.value.id, target, "starred_positional")
        for keyword in call.keywords:
            if keyword.arg is not None and isinstance(keyword.value, ast.Name):
                add(keyword.value.id, target, "direct_keyword", keyword.arg)
            elif keyword.arg is None and isinstance(keyword.value, ast.Name):
                name = keyword.value.id
                if name in parameters:
                    add(name, target, "starred_keywords")
                for update in updates[name]:
                    if update["value"] in parameters:
                        add(update["value"], target,
                            "conditional_dict_keyword" if update["condition"] else
                            "dict_keyword", update["keyword"], update["condition"])
    return {parameter: sorted(values, key=lambda item: tuple(sorted(item.items())))
            for parameter, values in sorted(observations.items())}


def forwarding_wrapper_target(node):
    """Recognize an unchanged *args/**kwargs pass-through to a local function."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    args = node.args
    if args.posonlyargs or args.args or args.kwonlyargs or not args.vararg or not args.kwarg:
        return None
    body = executable_statements(node)
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
        self.sources = OrderedDict()
        self.parse_rewrites = {}
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
            raw = self.source_text(sha, path)
            self.parsed[key], rewrites = parse_with_legacy_syntax(raw, path)
            if rewrites:
                self.parse_rewrites[key] = rewrites
            if len(self.parsed) > 128:
                self.parsed.popitem(last=False)
        self.parsed.move_to_end(key)
        return self.parsed[key]

    def source_text(self, sha, path):
        key = (sha, path)
        if key not in self.sources:
            self.sources[key] = git(self.repo, "show", f"{sha}:{path}").stdout
            if len(self.sources) > 128:
                self.sources.popitem(last=False)
        self.sources.move_to_end(key)
        return self.sources[key]

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
        bindings = []
        for item, conditional in statements:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                    and item.name == name:
                bindings.append((item, conditional, "definition", None))
            elif isinstance(item, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in item.targets
            ):
                bindings.append((item, conditional, "definition", dotted(item.value)))
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) \
                    and item.target.id == name:
                bindings.append((item, conditional, "definition", dotted(item.value)))
            elif isinstance(item, ast.ImportFrom):
                for alias in item.names:
                    if alias.name != "*" and (alias.asname or alias.name) == name:
                        bindings.append((item, conditional, "from_import", alias.name))
            elif isinstance(item, ast.Import):
                for alias in item.names:
                    if (alias.asname or alias.name.split(".")[0]) == name:
                        bindings.append((item, conditional, "import", alias.name))
        if bindings:
            item, conditional, kind, target_name = bindings[-1]
            if conditional and len(bindings) > 1:
                return Resolution("unknown", "conditional_rebinding_unresolved", path=path)
            if kind == "definition":
                return Resolution("present", "conditional_definition" if conditional else None,
                                  path=path, node=item, tree=tree, module=module,
                                  alias_target=target_name)
            if kind == "from_import":
                imported = self.relative_module(sha, module, path, item)
                if imported is None:
                    return Resolution("unknown", "relative_import_unresolved", path=path)
                result = self.resolve_name(sha, imported, target_name, seen)
                if result.status == "unknown":
                    submodule = f"{imported}.{target_name}"
                    if self.module_file(sha, submodule):
                        return Resolution("present", "conditional_import" if conditional else None,
                                          path=path, node=item, tree=tree, module=submodule)
                if conditional and result.status == "present" and result.reason is None:
                    result.reason = "conditional_import"
                return result
            imported = target_name
            if not self.exact_module_file(sha, imported):
                package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
                local = f"{package}.{imported}" if package else imported
                if self.exact_module_file(sha, local):
                    imported = local
            return Resolution("present", "conditional_import" if conditional else None,
                              path=path, node=item, tree=tree, module=imported)

        uncertain = False
        for item, conditional in statements:
            if isinstance(item, ast.ImportFrom):
                imported = self.relative_module(sha, module, path, item)
                if imported is None:
                    uncertain = True
                    continue
                for alias in item.names:
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
        warning_trigger = "function_body" if warnings else None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and resolved.tree:
            # NumPy-style dispatch decorators can emit the warning in a helper.
            helper_names = {child.id for dec in node.decorator_list
                            for child in ast.walk(dec) if isinstance(child, ast.Name)}
            for item in resolved.tree.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and item.name in helper_names:
                    helper_warnings = warning_observations(item, node.name)
                    if helper_warnings:
                        warnings.extend(helper_warnings)
                        warning_trigger = "decorator_helper"
        warning = bool(warnings) if node is not None else None
        warning_scopes = sorted({item["scope"] for item in warnings})
        warning_line = warnings[0]["line_start"] if warnings else None
        delegation = delegation_observation(node)
        state = {
            "api": api, "public_status": resolved.status,
            "reason": resolved.reason, "source_path": resolved.path,
            "public_status_basis": "git_source_static_resolution",
            "namespace_resolution_status": "unresolved" if resolved.status == "unknown"
            else "rule_checked",
            "runtime_namespace_status": "not_checked",
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
            "delegates_to": delegation["single_target"],
            "delegation_targets": delegation["targets"],
            "delegation_target_details": delegation["details"],
            "delegation_resolution_status": delegation["status"],
            "forwarded_arguments": forwarded_arguments(node),
            "deprecation_warning_path": ("api" in warning_scopes) if node is not None else None,
            "warning_defined_in_source": warning,
            "warning_runtime_status": "not_checked",
            "warning_trigger": warning_trigger,
            "deprecation_warning_scopes": warning_scopes,
            "warning_observations": warnings,
            "warning_line_start": warning_line,
            "implementation_hash": body_hash(node),
            "implementation_hash_scope": "function_body_ast_excluding_docstring",
            "normalized_implementation_hash": normalized_body_hash(node),
        }
        self.states[key] = state
        return state


def parameter_renames(before, after):
    """Recognize one positional name substitution with unchanged parameter metadata."""
    if before is None or after is None or len(before) != len(after):
        return {}
    old_names = {item["name"] for item in before}
    new_names = {item["name"] for item in after}
    removed, added = old_names - new_names, new_names - old_names
    if len(removed) != 1 or len(added) != 1:
        return {}
    old_name, new_name = next(iter(removed)), next(iter(added))
    position = next(index for index, item in enumerate(before)
                    if item["name"] == old_name)
    old_item, new_item = before[position], after[position]
    if new_item["name"] != new_name or any(
        old_item[field] != new_item[field] for field in ("kind", "default", "annotation")
    ):
        return {}
    if any(before[index]["name"] != after[index]["name"] for index in range(len(before))
           if index != position):
        return {}
    return {old_name: new_name}


def compare_parameters(before, after):
    if before is None or after is None:
        return []
    old = {item["name"]: item for item in before}
    new = {item["name"]: item for item in after}
    renames = parameter_renames(before, after)
    changes = []
    for old_name, new_name in renames.items():
        changes.append({"kind": "parameter_renamed", "before": old_name,
                        "after": new_name,
                        "position": next(index for index, item in enumerate(before)
                                         if item["name"] == old_name)})
    for name in old.keys() - new.keys() - renames.keys():
        changes.append({"kind": "parameter_removed", "parameter": name,
                        "before": old[name], "after": None})
    for name in new.keys() - old.keys() - set(renames.values()):
        changes.append({"kind": "parameter_added", "parameter": name,
                        "before": None, "after": new[name]})
    for name in old.keys() & new.keys():
        for field, kind in (("default", "parameter_default_changed"),
                            ("kind", "parameter_kind_changed"),
                            ("annotation", "annotation_changed")):
            if old[name][field] != new[name][field]:
                changes.append({"kind": kind, "parameter": name,
                                "before": old[name][field], "after": new[name][field]})
    shared = old.keys() & new.keys()
    if ([item["name"] for item in before if item["name"] in shared] !=
            [item["name"] for item in after if item["name"] in shared]):
        changes.append({"kind": "parameter_order_changed",
                        "before": before, "after": after})
    return sorted(changes, key=lambda item: (item["kind"], item.get("parameter", "")))


def implementation_relationship(pair, reader, sha, states):
    """Confirm a direct alias/wrapper target against the mapped replacement."""
    old_api, new_api = pair["deprecated_api"], pair["replacement_api"]
    old_state, new_state = states[old_api], states[new_api]
    result = {"status": "not_observed", "kind": None, "target_api": new_api,
              "basis": "git_source_static_resolution"}
    if old_state["public_status"] == "absent":
        result["status"] = "unavailable_due_to_api_removal"
        return result
    if old_state["public_status"] != "present" or new_state["public_status"] != "present":
        result["status"] = "unresolved" if "unknown" in (
            old_state["public_status"], new_state["public_status"]) else "not_observed"
        return result
    target_name = old_state["wrapper_target"] or old_state["alias_target"]
    if not target_name or "." in target_name:
        return result
    source = reader.resolve_api(sha, old_api)
    replacement = reader.resolve_api(sha, new_api)
    if not source.module or replacement.status != "present":
        result["status"] = "unresolved"
        return result
    target = reader.resolve_name(sha, source.module, target_name)
    if target.status != "present" or target.node is None or replacement.node is None:
        result["status"] = "unresolved"
        return result
    same_symbol = (target.path == replacement.path and
                   type(target.node) is type(replacement.node) and
                   getattr(target.node, "lineno", None) ==
                   getattr(replacement.node, "lineno", None))
    if same_symbol:
        result["status"] = "observed"
        result["kind"] = "direct_wrapper" if old_state["wrapper_target"] else "direct_alias"
        result["source_path"] = source.path
        result["line_start"] = getattr(source.node, "lineno", None)
        result["target_source_path"] = target.path
        result["target_line_start"] = getattr(target.node, "lineno", None)
    return result


def direct_private_helpers(reader, sha, api):
    """Resolve direct return calls to private module helpers, one hop only."""
    resolved = reader.resolve_api(sha, api)
    if resolved.status != "present" or not resolved.module or not isinstance(
        resolved.node, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        return {}
    calls, unresolved = returned_calls(resolved.node)
    if unresolved:
        return {}
    parameters = {item["name"] for item in signature(resolved.node) or []}
    local_bindings = {item.id for item in ast.walk(resolved.node)
                      if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)}
    helpers = {}
    for call in calls:
        if not isinstance(call.func, ast.Name):
            continue
        name = call.func.id
        if not name.startswith("_") or name in parameters or name in local_bindings:
            continue
        target = reader.resolve_name(sha, resolved.module, name)
        if target.status == "present" and target.reason is None and isinstance(
            target.node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            helpers[name] = target
    return helpers


def indirect_implementation_changes(api, reader, old_sha, new_sha, before, after):
    """Report a changed private helper reached by an unchanged public body."""
    if before["public_status"] != "present" or after["public_status"] != "present" \
            or not before["implementation_hash"] or \
            before["implementation_hash"] != after["implementation_hash"]:
        return []
    old_helpers = direct_private_helpers(reader, old_sha, api)
    new_helpers = direct_private_helpers(reader, new_sha, api)
    changes = []
    for name in sorted(old_helpers.keys() & new_helpers.keys()):
        old_target, new_target = old_helpers[name], new_helpers[name]
        old_hash, new_hash = body_hash(old_target.node), body_hash(new_target.node)
        if old_hash != new_hash:
            changes.append({"api": api, "kind": "indirect_implementation_changed",
                            "before_dependency": f"{old_target.module}.{name}",
                            "changed_dependency": f"{new_target.module}.{name}",
                            "before_hash": old_hash, "after_hash": new_hash,
                            "reachability_status": "statically_resolved_direct_return_call",
                            "behavioral_impact": "unknown"})
    return changes


def transitive_dependency_changes(pair, states, relationships, direct_changes):
    """Propagate a proved helper diff through a stable direct wrapper/alias."""
    old_api, replacement = pair["deprecated_api"], pair["replacement_api"]
    if states[old_api][1]["public_status"] != "present" or not all(
        relation["status"] == "observed" and
        relation["kind"] in {"direct_wrapper", "direct_alias"}
        for relation in relationships
    ):
        return []
    return [{"api": old_api, "kind": "transitive_dependency_changed",
             "origin_api": replacement,
             "before_dependency": change["before_dependency"],
             "changed_dependency": change["changed_dependency"],
             "dependency_path": [old_api, replacement, change["changed_dependency"]],
             "before_hash": change["before_hash"], "after_hash": change["after_hash"],
             "reachability_status": "statically_resolved_two_hops",
             "behavioral_impact": "unknown"}
            for change in direct_changes
            if change["kind"] == "indirect_implementation_changed"
            and change["api"] == replacement]


def compare_states(before, after, before_node=None, after_node=None):
    api = after["api"]
    old_status, new_status = before["public_status"], after["public_status"]
    if "unknown" in (old_status, new_status):
        return [], "public_api_unresolved"
    if old_status != new_status:
        kind = "public_api_added" if new_status == "present" else "public_api_removed"
        return [{"api": api, "kind": kind, "before": old_status,
                 "after": new_status}], None
    if old_status == "absent":
        return [], None
    changes = compare_parameters(before["effective_parameters"], after["effective_parameters"])
    renames = parameter_renames(before["effective_parameters"], after["effective_parameters"])
    if before["source_path"] and after["source_path"] and \
            before["source_path"] != after["source_path"]:
        changes.append({"kind": "source_path_changed",
                        "before": before["source_path"], "after": after["source_path"],
                        "body_ast_relation": "unchanged" if before["implementation_hash"]
                        and before["implementation_hash"] == after["implementation_hash"]
                        else "changed_or_unavailable"})
    for field, kind in (("alias_target", "alias_target_changed"),
                        ("wrapper_target", "wrapper_forwarding_changed")):
        if before[field] != after[field]:
            changes.append({"kind": kind, "before": before[field], "after": after[field]})
    def renamed_target(target):
        head, separator, tail = target.partition(".")
        return renames.get(head, head) + separator + tail

    if before["delegation_targets"] and after["delegation_targets"] and \
            before["delegation_resolution_status"] != "partial_return_resolution" and \
            after["delegation_resolution_status"] != "partial_return_resolution" and \
            [renamed_target(item) for item in before["delegation_targets"]] != \
            after["delegation_targets"]:
        changes.append({"kind": "delegation_changed",
                        "before": before["delegates_to"], "after": after["delegates_to"],
                        "before_targets": before["delegation_targets"],
                        "after_targets": after["delegation_targets"]})
    common_targets = set(before["delegation_targets"]) & set(after["delegation_targets"])
    old_names = {p["name"] for p in before["declared_parameters"] or []}
    new_names = {p["name"] for p in after["declared_parameters"] or []}
    forwarding_before, forwarding_after = {}, {}
    for name in sorted(old_names & new_names):
        old_modes = [item for item in before["forwarded_arguments"].get(name, [])
                     if item["target"] in common_targets]
        new_modes = [item for item in after["forwarded_arguments"].get(name, [])
                     if item["target"] in common_targets]
        if old_modes != new_modes and old_modes and new_modes:
            forwarding_before[name] = old_modes
            forwarding_after[name] = new_modes
    if forwarding_before:
        changes.append({"kind": "argument_forwarding_changed",
                        "parameters": sorted(forwarding_before),
                        "before": forwarding_before, "after": forwarding_after})
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
        if before["implementation_hash"] != after["implementation_hash"]:
            rename_only = before["effective_signature_status"] == \
                after["effective_signature_status"] == "direct" and \
                body_equal_after_parameter_rename(before_node, after_node, renames)
            if rename_only:
                rename_change = next(item for item in changes
                                     if item["kind"] == "parameter_renamed")
                rename_change.update({
                    "implementation_change_class": "parameter_rename_only",
                    "before_implementation_hash": before["implementation_hash"],
                    "after_implementation_hash": after["implementation_hash"],
                    "semantic_equivalence_status": "not_established",
                })
            else:
                equivalent = before["normalized_implementation_hash"] == \
                    after["normalized_implementation_hash"]
                changes.append({"kind": "implementation_changed",
                                "before_hash": before["implementation_hash"],
                                "after_hash": after["implementation_hash"],
                                "source_path_changed": before["source_path"] != after["source_path"],
                                "cooccurring_change_kinds": [item["kind"] for item in changes],
                                "implementation_change_class": "syntax_equivalent_refactor" if equivalent
                                else "unknown_semantics",
                                "semantic_equivalence_status": "proved_for_supported_identity_rewrite"
                                if equivalent else "not_established"})
    for change in changes:
        change["api"] = api
    reason = "effective_signature_unresolved" if "unresolved_forwarding" in (
        before["effective_signature_status"], after["effective_signature_status"]
    ) else None
    return changes, reason


PARAMETER_CHANGES = {"parameter_added", "parameter_removed", "parameter_renamed",
                     "parameter_default_changed",
                     "parameter_kind_changed", "annotation_changed", "parameter_order_changed"}


def classify_change_origin(change, before, after, states):
    """Distinguish a changed declaration from a changed forwarded target."""
    if change["kind"] not in PARAMETER_CHANGES:
        change["change_origin"] = "source_observation"
        return
    if before["effective_signature_status"] == "direct" and \
            after["effective_signature_status"] == "forwarded":
        change["change_origin"] = "forwarded_via_new_wrapper"
    elif before["effective_signature_status"] == after["effective_signature_status"] == "forwarded" \
            and before["wrapper_target"] == after["wrapper_target"]:
        change["change_origin"] = "forwarded_via_wrapper"
    elif before["effective_signature_status"] == after["effective_signature_status"] == "direct":
        change["change_origin"] = "direct_declaration"
    else:
        change["change_origin"] = "mixed_or_unresolved"
        change["unresolved_reason"] = "effective_signature_transition_not_classified"
    if change["change_origin"] in {"forwarded_via_wrapper", "forwarded_via_new_wrapper"}:
        for api, (_, target_after) in states.items():
            if api != change["api"] and target_after["source_path"] == after["effective_source_path"] \
                    and target_after["line_start"] == after["effective_line_start"]:
                change["origin_api"] = api
                break
        if "origin_api" not in change:
            change["unresolved_reason"] = "wrapper_target_not_in_migration_pair"


def verify_source_change(change, before, after, mapping_status, evidence_items):
    """Verify narrow source claims, without claiming artifact or runtime equivalence."""
    properties = []
    source_refs = [item for item in evidence_items if item["kind"] == "source"
                   and item.get("api") == change["api"]
                   and item.get("role") == "public_symbol"]
    if before["public_status"] == "present" and any(
            item["commit_sha"] == mapping_status[0][0] and item["line_start"]
           for item in source_refs):
        properties.append("source_symbol_before_located")
    if after["public_status"] == "present" and any(
            item["commit_sha"] == mapping_status[1][0] and item["line_start"]
           for item in source_refs):
        properties.append("source_symbol_after_located")
    namespace_refs = [item for item in evidence_items
                      if item["kind"] == "namespace_resolution"
                      and item.get("api") == change["api"]
                      and item["commit_sha"] == mapping_status[1][0]]
    if after["public_status"] == "absent":
        if any(item["checks"]["module_getattr"]["result"] == "excludes_name"
               for item in namespace_refs):
            properties.append("module_getattr_exclusion_checked")
        if any(item["checks"]["explicit_imports"]["checked_count"]
               and not item["checks"]["explicit_imports"]["target_found"]
               for item in namespace_refs):
            properties.append("explicit_import_names_checked")
    tags_match = all(status == "tag_matches_commit" for _, status in mapping_status)
    if tags_match:
        properties.append("git_tags_match_commit_shas")
    diff = next((item for item in evidence_items if item["kind"] == "git_diff"), None)
    hunks = [hunk for hunk in diff["relevant_hunks"]
             if change["api"] in hunk["related_apis"]] if diff else []
    function_diff = next((item for item in evidence_items
                          if item["kind"] == "function_diff" and item["api"] == change["api"]), None)
    dependency_diff = next((item for item in evidence_items
                            if item["kind"] == "dependency_diff"
                            and item["api"] == change.get("origin_api", change["api"])
                            and item["changed_dependency"] == change.get("changed_dependency")), None)
    if dependency_diff and dependency_diff["diff"]["excerpt"]:
        properties.append("transitive_dependency_body_diff_located" if change["kind"] ==
                          "transitive_dependency_changed" else
                          "direct_dependency_body_diff_located")
    if change["kind"] in {"indirect_implementation_changed", "transitive_dependency_changed"}:
        hunks = []
    scoped_diff = None
    if change["kind"] == "implementation_changed":
        scope = "implementation_body"
        if function_diff and function_diff["diffs"][scope]["excerpt"]:
            properties.append("function_body_diff_located")
        hunks = []
    elif function_diff and change["kind"] not in {
        "warning_path_added", "warning_path_removed", "indirect_implementation_changed",
        "transitive_dependency_changed"
    }:
        scopes = ("declaration",) if change["kind"] in PARAMETER_CHANGES else \
            ("implementation_body", "declaration")
        if any(function_diff["diffs"][scope]["excerpt"] for scope in scopes):
            scoped_diff = function_diff
            properties.append("function_source_diff_located")
            hunks = []
    parameter = change.get("parameter")
    hunk_support = bool(hunks) and (parameter is None or any(
        re.search(rf"\b{re.escape(parameter)}\b", hunk["excerpt"]) for hunk in hunks
    ))
    if scoped_diff:
        hunk_support = parameter is None or any(
            re.search(rf"\b{re.escape(parameter)}\b", scoped_diff["diffs"][scope]["excerpt"])
            for scope in scopes
        )
    if hunk_support:
        if not scoped_diff:
            properties.append("related_source_diff_hunk_located" if change["kind"] in
                              {"public_api_added", "public_api_removed"}
                              else "relevant_git_diff_hunk_located")
    old = {item["name"]: item for item in before["effective_parameters"] or []}
    new = {item["name"]: item for item in after["effective_parameters"] or []}
    structural = False
    if change["kind"] == "parameter_added" and parameter not in old and parameter in new:
        properties.extend(("parameter_absent_before", "parameter_present_after"))
        structural = True
    elif change["kind"] == "parameter_removed" and parameter in old and parameter not in new:
        properties.extend(("parameter_present_before", "parameter_absent_after"))
        structural = True
    elif change["kind"] == "parameter_renamed" and \
            parameter_renames(before["effective_parameters"],
                              after["effective_parameters"]) == \
            {change["before"]: change["after"]}:
        properties.append("parameter_name_differs_at_same_position")
        structural = True
        if change.get("implementation_change_class") == "parameter_rename_only" and \
                function_diff and function_diff["diffs"]["implementation_body"]["excerpt"]:
            properties.append("body_diff_matches_parameter_rename")
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
    if structural and change["change_origin"] == "forwarded_via_new_wrapper":
        properties.append("static_new_wrapper_forwarding_resolved")
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


def evidence(reader, library, sha, state, forwarded=False):
    path = state["effective_source_path"] if forwarded else state["source_path"]
    if not path:
        return None
    if state["public_status"] == "absent" and state["reason"] == "getattr_excludes_name" \
            and not forwarded:
        name = state["api"].rsplit(".", 1)[-1]
        tree = reader.parse(sha, path)
        explicit = []
        wildcard = []
        matching_import = False
        getter = None
        if tree is not None:
            for item, conditional in module_statements(tree.body):
                if isinstance(item, ast.ImportFrom):
                    location = {"line_start": item.lineno, "line_end": item.end_lineno,
                                "conditional": conditional}
                    if any(alias.name == "*" for alias in item.names):
                        wildcard.append(location)
                    else:
                        explicit.append(location)
                        matching_import |= any((alias.asname or alias.name) == name
                                               for alias in item.names)
                elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and item.name == "__getattr__":
                    getter = {"result": "excludes_name" if getattr_excludes_name(item, name)
                              else "unresolved", "line_start": item.lineno,
                              "line_end": item.end_lineno, "conditional": conditional}
        if getter is None:
            getter = {"result": "not_defined", "line_start": None,
                      "line_end": None, "conditional": None}
        line = getter["line_start"]
        url = f"https://github.com/{REPOSITORIES[library]}/blob/{sha}/{path}"
        if line:
            url += f"#L{line}"
        return {"kind": "namespace_resolution", "api": state["api"],
                "commit_sha": sha, "source_path": path, "checked_name": name,
                "line_start": line, "line_end": getter["line_end"], "url": url,
                "checks": {
                    "explicit_imports": {"checked_count": len(explicit),
                                         "target_found": matching_import,
                                         "locations": explicit},
                    "wildcard_imports": {"count": len(wildcard), "locations": wildcard},
                    "module_getattr": getter,
                }}
    line = state["effective_line_start"] if forwarded else state["line_start"]
    url = f"https://github.com/{REPOSITORIES[library]}/blob/{sha}/{path}"
    if line:
        url += f"#L{line}"
    return {"kind": "source", "api": state["api"],
            "commit_sha": sha, "source_path": path,
            "role": "forwarded_target" if forwarded else "public_symbol",
            "line_start": line,
            "line_end": state["effective_line_end"] if forwarded else state["line_end"],
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


def function_parts(node, source):
    """Extract declaration and executable body from the resolved function."""
    statements = node.body
    lines = source.splitlines()
    header_end = statements[0].lineno - 1 if statements else node.end_lineno
    if header_end < node.lineno:
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        result = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        declaration = f"{prefix} {node.name}({ast.unparse(node.args)}){result}:"
    else:
        declaration = "\n".join(lines[node.lineno - 1:header_end])
    statements = executable_statements(node)
    body = "\n".join(ast.get_source_segment(source, statement) or ""
                     for statement in statements)
    return {"declaration": declaration, "implementation_body": body}


def scoped_text_diff(before, after):
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                      fromfile="before", tofile="after", lineterm=""))
    edits = lines[2:]
    removed = [line[1:].strip() for line in edits
               if line.startswith("-") and not line.startswith("---")]
    added = [line[1:].strip() for line in edits
             if line.startswith("+") and not line.startswith("+++")]
    excerpt = "\n".join(lines)
    return {"excerpt": excerpt[:4000], "excerpt_truncated": len(excerpt) > 4000,
            "removed_count": len(removed), "added_count": len(added),
            "removed_lines": removed[:8], "added_lines": added[:8],
            "line_samples_truncated": len(removed) > 8 or len(added) > 8}


def resolved_function_parts(reader, sha, resolution):
    if not resolution.path or not isinstance(
        resolution.node, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        return None
    return function_parts(resolution.node, reader.source_text(sha, resolution.path))


def function_diff_evidence(reader, old_sha, new_sha, api, before, after):
    """Compare the two resolved public functions, independent of file diff hunks."""
    if before["public_status"] != "present" or after["public_status"] != "present":
        return None
    old_resolution = reader.resolve_api(old_sha, api)
    new_resolution = reader.resolve_api(new_sha, api)
    old_parts = resolved_function_parts(reader, old_sha, old_resolution)
    new_parts = resolved_function_parts(reader, new_sha, new_resolution)
    if old_parts is None or new_parts is None:
        return None
    diffs = {scope: scoped_text_diff(old_parts[scope], new_parts[scope])
             for scope in old_parts}
    return {"kind": "function_diff", "api": api,
            "from_commit_sha": old_sha, "to_commit_sha": new_sha,
            "before_source_path": old_resolution.path,
            "after_source_path": new_resolution.path,
            "before_line_start": old_resolution.node.lineno,
            "before_line_end": old_resolution.node.end_lineno,
            "after_line_start": new_resolution.node.lineno,
            "after_line_end": new_resolution.node.end_lineno,
            "scope": "resolved_public_function_ast_spans",
            "diffs": diffs}


def dependency_diff_evidence(reader, old_sha, new_sha, change):
    old_module, _, old_name = change["before_dependency"].rpartition(".")
    new_module, _, new_name = change["changed_dependency"].rpartition(".")
    old = reader.resolve_name(old_sha, old_module, old_name)
    new = reader.resolve_name(new_sha, new_module, new_name)
    if old.status != "present" or new.status != "present":
        return None
    old_parts = resolved_function_parts(reader, old_sha, old)
    new_parts = resolved_function_parts(reader, new_sha, new)
    if old_parts is None or new_parts is None:
        return None
    return {"kind": "dependency_diff", "api": change["api"],
            "changed_dependency": change["changed_dependency"],
            "from_commit_sha": old_sha, "to_commit_sha": new_sha,
            "before_source_path": old.path, "after_source_path": new.path,
            "before_line_start": old.node.lineno, "after_line_start": new.node.lineno,
            "before_url": f"https://github.com/{REPOSITORIES[reader.library]}/blob/"
                          f"{old_sha}/{old.path}#L{old.node.lineno}",
            "after_url": f"https://github.com/{REPOSITORIES[reader.library]}/blob/"
                         f"{new_sha}/{new.path}#L{new.node.lineno}",
            "diff": scoped_text_diff(old_parts["implementation_body"],
                                     new_parts["implementation_body"])}


def describe_function_change(change, function_diff):
    diff = function_diff["diffs"]["implementation_body"] if function_diff else None
    if diff is None:
        change["change_summary"] = "Function-specific source diff unavailable."
        return
    removed, added = diff["removed_count"], diff["added_count"]
    change["change_summary"] = (
        f"Function body differs; {removed} removed and {added} added lines in the "
        "resolved function (semantic effect not determined)."
    )
    sample = {"before": diff["removed_lines"], "after": diff["added_lines"],
              "excerpt_truncated": diff["excerpt_truncated"] or
              diff["line_samples_truncated"]}
    change["changed_source_lines"] = sample


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
    candidates, unresolved, relationship_history = [], [], []
    observed_versions = set()
    source_compared_intervals = set()
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
            relationships = []
            for version, sha, position in ((old["version"], old_sha, 0),
                                           (new["version"], new_sha, 1)):
                relation = implementation_relationship(
                    pair, reader, sha, {api: pair_states[position]
                                        for api, pair_states in states.items()})
                relationships.append(relation)
                if version not in observed_versions:
                    relationship_history.append({"version": version, **relation})
                    observed_versions.add(version)
            changes = []
            reasons = []
            for before, after in states.values():
                renames = parameter_renames(before["effective_parameters"],
                                            after["effective_parameters"])
                check_body = renames and before["implementation_hash"] != \
                    after["implementation_hash"] and \
                    before["effective_signature_status"] == \
                    after["effective_signature_status"] == "direct"
                before_node = reader.resolve_api(old_sha, before["api"]).node \
                    if check_body else None
                after_node = reader.resolve_api(new_sha, after["api"]).node \
                    if check_body else None
                found, reason = compare_states(before, after, before_node, after_node)
                changes.extend(found)
                if reason:
                    reasons.append({"api": after["api"], "reason": reason,
                                    "before": before["reason"], "after": after["reason"]})
                changes.extend(indirect_implementation_changes(
                    after["api"], reader, old_sha, new_sha, before, after))
            changes.extend(transitive_dependency_changes(pair, states, relationships, changes))
            if all(state[side]["public_status"] == "present"
                   for state in states.values() for side in (0, 1)) and \
                    all(item["status"] != "unresolved" for item in relationships) and (
                relationships[0]["status"] != relationships[1]["status"] or
                relationships[0]["kind"] != relationships[1]["kind"]
            ):
                changes.append({"api": pair["deprecated_api"],
                                "kind": "implementation_relationship_changed",
                                "before": relationships[0]["kind"],
                                "after": relationships[1]["kind"],
                                "before_status": relationships[0]["status"],
                                "after_status": relationships[1]["status"],
                                "behavioral_impact": "unknown"})
            source_compared_intervals.add((old["version"], new["version"]))
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
                if not any(change["api"] == api for change in changes) and not any(
                    change["kind"] == "implementation_relationship_changed" for change in changes
                ):
                    continue
                for sha, state in ((old_sha, before), (new_sha, after)):
                    for forwarded in (False, True) if state["wrapper_target"] else (False,):
                        item = evidence(reader, pair["library"], sha, state, forwarded)
                        if item and item not in source_evidence:
                            source_evidence.append(item)
            for index, item in enumerate(source_evidence, start=1):
                item["id"] = f"src-{pair['id']}-{old['version']}-{new['version']}-{index}"
            diff_item = diff_evidence(reader.repo, old_sha, new_sha, states, changes)
            if diff_item:
                diff_item["id"] = f"diff-{pair['id']}-{old['version']}-{new['version']}"
                source_evidence.append(diff_item)
            for api in states:
                if not any(change["api"] == api for change in changes):
                    continue
                before, after = states[api]
                item = function_diff_evidence(reader, old_sha, new_sha, api, before, after)
                if item:
                    item["id"] = f"func-{pair['id']}-{old['version']}-{new['version']}-{api}"
                    source_evidence.append(item)
            for index, change in enumerate(changes, start=1):
                if change["kind"] != "indirect_implementation_changed":
                    continue
                item = dependency_diff_evidence(
                    reader, old_sha, new_sha, change)
                if item:
                    item["id"] = f"dep-{pair['id']}-{old['version']}-{new['version']}-{index}"
                    source_evidence.append(item)
            for change in changes:
                change["observation_kind"] = "source_observed"
                before, after = states[change["api"]]
                function_diff = next((item for item in source_evidence
                                      if item["kind"] == "function_diff"
                                      and item["api"] == change["api"]), None)
                if change["kind"] == "implementation_changed":
                    describe_function_change(change, function_diff)
                scoped_function_change = function_diff and change["kind"] not in \
                    {"warning_path_added", "warning_path_removed"} and any(
                        function_diff["diffs"][scope]["excerpt"] for scope in
                        (("declaration",) if change["kind"] in PARAMETER_CHANGES else
                         ("implementation_body", "declaration")))
                change["evidence_refs"] = [item["id"] for item in source_evidence
                                           if (item["kind"] in {"source", "namespace_resolution"}
                                               and item["api"] == change["api"])
                                           or (item["kind"] == "function_diff"
                                               and item["api"] == change["api"]
                                               and scoped_function_change)
                                           or (item["kind"] == "dependency_diff"
                                               and item["api"] == change["api"]
                                               and item["changed_dependency"] ==
                                               change.get("changed_dependency"))
                                           or (item["kind"] == "git_diff" and any(
                                               change["api"] in hunk["related_apis"]
                                               for hunk in item["relevant_hunks"])
                                               and not scoped_function_change)]
                if change["kind"] == "indirect_implementation_changed":
                    change["evidence_refs"] = [
                        item["id"] for item in source_evidence
                        if (item["kind"] in {"source", "namespace_resolution"}
                            and item["api"] == change["api"])
                        or (item["kind"] == "dependency_diff"
                            and item["api"] == change["api"]
                            and item["changed_dependency"] ==
                            change["changed_dependency"])]
                elif change["kind"] == "transitive_dependency_changed":
                    change["evidence_refs"] = [
                        item["id"] for item in source_evidence
                        if (item["kind"] in {"source", "namespace_resolution"}
                            and item["api"] in {change["api"], change["origin_api"]})
                        or (item["kind"] == "dependency_diff"
                            and item["api"] == change["origin_api"]
                            and item["changed_dependency"] == change["changed_dependency"])]
                verify_source_change(change, before, after,
                                     ((old_sha, statuses[0]), (new_sha, statuses[1])),
                                     source_evidence)
                if change["kind"] == "implementation_relationship_changed":
                    change["relationship_evidence_refs"] = [
                        item["id"] for item in source_evidence
                        if item["kind"] == "source" and item["api"] == pair["replacement_api"]]
                    change["evidence_refs"].extend(change["relationship_evidence_refs"])
            candidates.append({**base, "artifact_source_match_status": "not_verified",
                               "migration_mapping": {"source": "input_pair",
                                                     "deprecated_api": pair["deprecated_api"],
                                                     "replacement_api": pair["replacement_api"]},
                               "implementation_relationship_before": relationships[0],
                               "implementation_relationship_after": relationships[1],
                               "tag_status": list(statuses),
                               "api_states_before": {api: state[0] for api, state in states.items()},
                               "api_states_after": {api: state[1] for api, state in states.items()},
                               "changes": changes, "evidence": source_evidence,
                               "comparison_status": "partial" if reasons or mapping_unverified
                               else "rule_checked"})
        except (OSError, RuntimeError) as exc:
            unresolved.append({**base, "reason": "source_read_failed", "detail": str(exc)})
    first_observed = next((item["version"] for item in relationship_history
                           if item["status"] == "observed"), None)
    for candidate in candidates:
        candidate["relationship_first_observed_version"] = first_observed
    return candidates, unresolved, relationship_history, source_compared_intervals


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


def unresolved_reason_codes(item):
    codes = set()
    status_codes = {"tag_missing": "missing_tag", "tag_not_fetched": "missing_tag",
                    "commit_missing": "missing_commit", "invalid_commit_sha": "invalid_commit",
                    "tag_mismatch": "tag_mismatch",
                    "source_url_mismatch": "source_url_mismatch"}
    for status in item.get("mapping_status", []):
        if status in status_codes:
            codes.add(status_codes[status])
    source_codes = {"source_syntax_not_supported": "parse_error",
                    "dynamic_package_loader": "dynamic_binding_unresolved",
                    "generated_public_namespace_unresolved": "dynamic_binding_unresolved",
                    "dynamic_or_unresolved_export": "dynamic_binding_unresolved",
                    "conditional_rebinding_unresolved": "dynamic_binding_unresolved",
                    "module_source_missing": "source_resolution_failed"}
    for detail in item.get("details", []):
        for side in ("before", "after"):
            reason = detail.get(side)
            if reason in source_codes:
                codes.add(source_codes[reason])
            elif reason and reason not in {"conditional_import", "conditional_definition"}:
                codes.add("source_resolution_failed")
    if not codes:
        codes.add({"source_read_failed": "source_read_failed",
                   "source_repository_missing": "source_repository_missing",
                   "source_repository_invalid": "source_repository_invalid"}.get(
                       item["reason"], "source_resolution_failed"))
    return sorted(codes)


RUNTIME_PROBE = r'''
import importlib, inspect, json, sys, warnings
root = importlib.import_module(sys.argv[1])
result = {"module_version": getattr(root, "__version__", None), "apis": {}}
for api in json.loads(sys.argv[2]):
    obj = root
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            for name in api.split(".")[1:]:
                obj = getattr(obj, name)
            status = "present"
        except AttributeError:
            status = "absent"
        except Exception as exc:
            status = "error"
            result["apis"][api] = {"namespace_status": status,
                                   "error": type(exc).__name__}
            continue
    observation = {"namespace_status": status,
                   "access_warning_count": len(caught)}
    if status == "present":
        try:
            observation["signature"] = str(inspect.signature(obj))
            observation["signature_status"] = "observed"
        except (TypeError, ValueError):
            observation["signature_status"] = "unavailable"
    result["apis"][api] = observation
print(json.dumps(result))
'''


def runtime_probe(python_executable, library, version, apis):
    if not python_executable:
        return {"status": "runtime_unavailable", "reason": "interpreter_not_configured"}
    try:
        result = subprocess.run(
            [str(python_executable), "-c", RUNTIME_PROBE,
             ROOT_MODULE.get(library, library.replace("-", "_")), json.dumps(apis)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "runtime_unavailable", "reason": type(exc).__name__}
    if result.returncode:
        return {"status": "runtime_unavailable", "reason": "probe_failed",
                "error": result.stderr.strip()[-500:]}
    try:
        observation = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"status": "runtime_unavailable", "reason": "probe_output_invalid"}
    if observation["module_version"] != version:
        return {"status": "runtime_unavailable", "reason": "version_mismatch",
                "observed_version": observation["module_version"]}
    return {"status": "runtime_observed", "scope": "namespace_observed_signature_recorded",
            "observations": observation["apis"]}


def attach_runtime_verification(candidates, runtime_map):
    """Record namespace probes on candidates and their versioned API states."""
    runtime_cache = {}
    for candidate in candidates:
        checks = {}
        for side in ("from", "to"):
            version = candidate[f"{side}_version"]
            key = (candidate["library"], version,
                   candidate["deprecated_api"], candidate["replacement_api"])
            if key not in runtime_cache:
                executable = runtime_map.get(candidate["library"], {}).get(version)
                runtime_cache[key] = runtime_probe(
                    executable, candidate["library"], version,
                    [candidate["deprecated_api"], candidate["replacement_api"]])
            check = runtime_cache[key]
            checks[side] = check
            states = candidate[f"api_states_{'before' if side == 'from' else 'after'}"]
            for api, state in states.items():
                if check["status"] == "runtime_observed":
                    observed = check["observations"].get(api, {})
                    state["runtime_namespace_status"] = observed.get(
                        "namespace_status", "unavailable")
                    state["runtime_signature_status"] = observed.get(
                        "signature_status", "unavailable")
                else:
                    state["runtime_namespace_status"] = "unavailable"
                    state["runtime_signature_status"] = "unavailable"
        candidate["runtime_verification"] = checks
        candidate["runtime_verification_scope"] = "public_namespace_only"
        if all(check["status"] == "runtime_observed" for check in checks.values()):
            matches = all(
                candidate[f"api_states_{'before' if side == 'from' else 'after'}"]
                [api]["runtime_namespace_status"] ==
                candidate[f"api_states_{'before' if side == 'from' else 'after'}"]
                [api]["public_status"]
                for side in ("from", "to")
                for api in (candidate["deprecated_api"], candidate["replacement_api"])
            )
            candidate["runtime_verification_status"] = (
                "runtime_verified" if matches else "runtime_mismatch")
        else:
            candidate["runtime_verification_status"] = "runtime_unavailable"


def summarize_coverage(pair, versions, candidates, unresolved, blocked_reason=None,
                       relationship_history=None, source_compared_intervals=None):
    candidate_by_interval = {(item["from_version"], item["to_version"]): item
                             for item in candidates}
    unresolved_by_interval = defaultdict(list)
    for item in unresolved:
        if item.get("from_version") and item.get("to_version"):
            unresolved_by_interval[item["from_version"], item["to_version"]].append(item)
    candidate_intervals = set(candidate_by_interval)
    unresolved_intervals = set(unresolved_by_interval)
    planned = max(0, len(versions) - 1)
    compared = set() if blocked_reason else (
        source_compared_intervals if source_compared_intervals is not None else
        {(old["version"], new["version"]) for old, new in zip(versions, versions[1:])})
    scanned = len(compared)
    intervals = []
    for old, new in zip(versions, versions[1:]):
        key = (old["version"], new["version"])
        candidate = candidate_by_interval.get(key)
        issues = unresolved_by_interval.get(key, [])
        reasons = sorted({item["reason"] for item in issues} | (
            {blocked_reason} if blocked_reason else set()))
        reason_codes = sorted({code for item in issues
                               for code in unresolved_reason_codes(item)} | (
                                   {blocked_reason} if blocked_reason else set()))
        status = ("unresolved" if blocked_reason else
                  "partially_analyzed" if candidate and issues else
                  "unsupported" if "parse_error" in reason_codes else
                  "unresolved" if issues else
                  "analyzed" if candidate else "unchanged")
        intervals.append({"from_version": key[0], "to_version": key[1],
                          "status": status, "change_count": len(candidate["changes"])
                          if candidate else 0, "unresolved_reasons": reasons,
                          "unresolved_reason_codes": reason_codes})
    without_unresolved = sum(item["status"] in
                             {"analyzed", "unchanged"}
                             for item in intervals)
    return {"mapping_id": pair["id"], "library": pair["library"],
            "deprecated_api": pair["deprecated_api"],
            "replacement_api": pair["replacement_api"],
            "migration_mapping": {"source": "input_pair",
                                  "deprecated_api": pair["deprecated_api"],
                                  "replacement_api": pair["replacement_api"]},
            "implementation_relationship_history": relationship_history or [],
            "relationship_first_observed_version": next(
                (item["version"] for item in relationship_history or []
                 if item["status"] == "observed"), None),
            "version_scope": "collected_versions_only",
            "scan_strategy": "adjacent_collected_versions",
            "first_version": versions[0]["version"] if versions else None,
            "last_version": versions[-1]["version"] if versions else None,
            "versions_considered": len(versions), "intervals_planned": planned,
            "versions_scanned": len({version for interval in compared
                                     for version in interval}),
            "intervals_scanned": scanned,
            "intervals_attempted": 0 if blocked_reason else planned,
            "intervals_without_unresolved": without_unresolved,
            "candidate_intervals": len(candidate_intervals),
            "unresolved_intervals": len(unresolved_intervals),
            "unresolved_interval_refs": [
                {"from_version": old, "to_version": new}
                for old, new in sorted(unresolved_intervals)
            ],
            "rule_checked_without_candidate": sum(
                item["status"] == "unchanged" for item in intervals
            ),
            "interval_results": intervals,
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
    parser.add_argument("--runtime-python-map", type=Path,
                        help="Optional JSON {library: {version: python_executable}} for runtime probes")
    parser.add_argument("--progress-every", type=int, default=10,
                        help="Show progress every N version intervals (0 disables interval logs)")
    args = parser.parse_args(argv)
    if args.progress_every < 0:
        parser.error("--progress-every must be nonnegative")
    pairs, grouped = load_inputs()
    runtime_map = json.loads(args.runtime_python_map.read_text(encoding="utf-8")) \
        if args.runtime_python_map else None
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
            found, missing, relationships, compared = scan_pair(
                pair, versions, reader, version_status,
                progress=report, progress_every=args.progress_every)
            candidates.extend(found)
            unresolved.extend(missing)
            coverage.append(summarize_coverage(
                pair, versions, found, missing, relationship_history=relationships,
                source_compared_intervals=compared))
            report(f"{library} #{pair['id']}: {len(found)} candidate intervals "
                   f"({sum(len(item['changes']) for item in found)} changes), "
                   f"{len(missing)} unresolved intervals")
    coverage_path = args.coverage_output.resolve()
    try:
        coverage_path = coverage_path.relative_to(ROOT)
    except ValueError:
        pass
    for candidate in candidates:
        candidate["coverage_ref"] = {"path": coverage_path.as_posix(),
                                     "mapping_id": candidate["mapping_id"]}
        candidate["source_verification_status"] = (
            "source_verified" if candidate["comparison_status"] == "rule_checked"
            and all(change["verification_status"] == "verified"
                    for change in candidate["changes"]) else "source_requires_review")
    if runtime_map is not None:
        attach_runtime_verification(candidates, runtime_map)
    for item in unresolved:
        item["reason_codes"] = unresolved_reason_codes(item)
    write_json(args.output, candidates)
    write_json(args.unresolved_output, unresolved)
    write_json(args.coverage_output, coverage)
    print(f"Saved {len(candidates)} change records to {args.output}")
    print(f"Saved {len(unresolved)} unresolved intervals to {args.unresolved_output}")
    print(f"Saved {len(coverage)} coverage records to {args.coverage_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
