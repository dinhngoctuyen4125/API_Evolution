"""Các luật phân giải Python/Git cho bước 5 v2.

Adapted from trace_source_changes.py; this module does not import or run it.
It resolves source bindings and signatures without importing target libraries.
"""

import ast
import copy
import hashlib
import re
import subprocess
import warnings
from collections import OrderedDict
from dataclasses import dataclass

ROOT_MODULE = {"pytorch": "torch", "scikit-learn": "sklearn"}
GENERATED_PUBLIC_ROOTS = {"tensorflow"}
MAX_IMPORT_DEPTH = 24


# Source parsing and signatures

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
    match = re.fullmatch(r"([ \t]*)except[ \t]+(?:\(([\w.]+)\)|([\w.]+))[ \t]*,[ \t]*(\w+)[ \t]*:([ \t]*(?:#.*)?)", body)
    if match:
        return f"{match[1]}except {match[2] or match[3]} as {match[4]}:{match[5]}{ending}", "except_binding"
    match = re.fullmatch(r"([ \t]*)print[ \t]*>>[ \t]*([^,#]+),[ \t]*([^#]+)", body)
    if match:
        values = match[3].rstrip()
        no_newline = values.endswith(",")
        if no_newline:
            values = values[:-1].rstrip()
        end = ", end=' '" if no_newline else ""
        return f"{match[1]}print({values}{end}, file={match[2]}){ending}", "print_redirect"
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
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="invalid escape sequence",
                                        category=SyntaxWarning)
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


def docstring_spans(node):
    """Line spans of docstrings in a function and its nested definitions."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return []
    spans = []
    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and child.body and isinstance(child.body[0], ast.Expr) \
                and isinstance(child.body[0].value, ast.Constant) \
                and isinstance(child.body[0].value.value, str):
            doc = child.body[0]
            spans.append((doc.lineno, doc.end_lineno))
    return spans


def executable_body(node):
    """Copy a function body without docstrings, including nested definitions."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None

    class WithoutDocstrings(ast.NodeTransformer):
        def visit_FunctionDef(self, child):
            self.generic_visit(child)
            child.body = executable_statements(child)
            return child

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef

    cleaned = WithoutDocstrings().visit(copy.deepcopy(node))
    return ast.Module(body=cleaned.body, type_ignores=[])


def body_hash(node):
    body = executable_body(node)
    if body is None:
        return None
    value = ast.dump(body, include_attributes=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_structure(node):
    """Record directly declared Python structure, excluding docstring content."""
    structure = {"kind": type(node).__name__}
    if isinstance(node, ast.ClassDef):
        structure["bases"] = [expr(base) for base in node.bases]
        structure["methods"] = [
            {"name": method.name, "kind": type(method).__name__,
             "parameters": signature(method),
             "return_annotation": expr(method.returns),
             "line_start": method.lineno, "line_end": method.end_lineno,
             "body_hash": body_hash(method)}
            for method in node.body
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        structure["targets"] = [expr(target) for target in targets]
        structure["expression"] = expr(node.value)
        if isinstance(node, ast.AnnAssign):
            structure["annotation"] = expr(node.annotation)
    elif isinstance(node, ast.ImportFrom):
        structure["module"] = node.module
        structure["level"] = node.level
        structure["targets"] = [
            {"name": alias.name, "asname": alias.asname} for alias in node.names]
    elif isinstance(node, ast.Import):
        structure["targets"] = [
            {"name": alias.name, "asname": alias.asname} for alias in node.names]
    return structure


def error_message_profile(node):
    """Mask only strings used solely as exception messages in a copied body."""
    body = executable_body(node)
    if body is None:
        return None

    def simple_value(value):
        if isinstance(value, (ast.Name, ast.Constant)):
            return True
        if isinstance(value, (ast.Tuple, ast.List)):
            return all(simple_value(item) for item in value.elts)
        return False

    def string_expression(value):
        if isinstance(value, ast.Constant):
            return isinstance(value.value, str)
        if isinstance(value, ast.JoinedStr):
            return all(isinstance(item, ast.Constant)
                       or (isinstance(item, ast.FormattedValue)
                           and simple_value(item.value)) for item in value.values)
        if isinstance(value, ast.BinOp) and isinstance(value.op, (ast.Add, ast.Mod)):
            return string_expression(value.left) and (
                string_expression(value.right) if isinstance(value.op, ast.Add)
                else simple_value(value.right))
        return (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                and value.func.attr == "format" and string_expression(value.func.value)
                and all(simple_value(arg) for arg in value.args)
                and all(simple_value(arg.value) for arg in value.keywords))

    direct = set()
    message_loads = {}
    for child in ast.walk(body):
        if not isinstance(child, ast.Raise) or not isinstance(child.exc, ast.Call) \
                or not child.exc.args:
            continue
        exception = dotted(child.exc.func)
        if not exception or not exception.rsplit(".", 1)[-1].endswith(
                ("Error", "Exception")):
            continue
        message = child.exc.args[0]
        if string_expression(message):
            direct.add(id(message))
        elif isinstance(message, ast.Name):
            message_loads.setdefault(message.id, set()).add(id(message))

    assignments = {}
    for child in ast.walk(body):
        if isinstance(child, ast.Assign) and len(child.targets) == 1 \
                and isinstance(child.targets[0], ast.Name):
            assignments.setdefault(child.targets[0].id, []).append(child)
    safe_names = set()
    for name, loads in message_loads.items():
        all_loads = {id(child) for child in ast.walk(body)
                     if isinstance(child, ast.Name) and child.id == name
                     and isinstance(child.ctx, ast.Load)}
        all_stores = {id(child) for child in ast.walk(body)
                      if isinstance(child, ast.Name) and child.id == name
                      and isinstance(child.ctx, ast.Store)}
        assigned = assignments.get(name, [])
        if (all_loads == loads
                and all_stores == {id(item.targets[0]) for item in assigned}
                and assigned and all(string_expression(item.value) for item in assigned)):
            safe_names.add(name)

    candidates = []

    class MaskMessages(ast.NodeTransformer):
        def mask(self, value):
            candidates.append((ast.dump(value, include_attributes=False),
                               (value.lineno, value.end_lineno)))
            return ast.copy_location(ast.Constant(value="<error message>"), value)

        def visit_Raise(self, child):
            self.generic_visit(child)
            if isinstance(child.exc, ast.Call) and child.exc.args \
                    and id(child.exc.args[0]) in direct:
                child.exc.args[0] = self.mask(child.exc.args[0])
            return child

        def visit_Assign(self, child):
            self.generic_visit(child)
            if len(child.targets) == 1 and isinstance(child.targets[0], ast.Name) \
                    and child.targets[0].id in safe_names:
                child.value = self.mask(child.value)
            return child

    MaskMessages().visit(body)
    return {"shape": ast.dump(body, include_attributes=False),
            "messages": candidates}


def error_message_changes(before, after):
    """Return source spans when the only body differences are error messages."""
    if not before or not after or before["shape"] != after["shape"]:
        return None
    old_messages, new_messages = before["messages"], after["messages"]
    if len(old_messages) != len(new_messages):
        return None
    changed = [(old[1], new[1]) for old, new in zip(old_messages, new_messages)
               if old[0] != new[0]]
    return ([old for old, _ in changed], [new for _, new in changed]) if changed else None


# Public API resolution

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


def getattr_excludes_name(node, name, module_tree=None):
    """Prove a module __getattr__ cannot return this name; otherwise stay unknown."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.args.args:
        return False
    final = node.body[-1] if node.body else None
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

    if isinstance(final, ast.Raise) and isinstance(final.exc, ast.Call) \
            and dotted(final.exc.func) == "AttributeError":
        return not can_return(node.body)

    # A guarded globals()[name] lookup can only return a binding already in the
    # module namespace. The resolver has checked those bindings above.
    guarded_lookup = (
        isinstance(final, ast.Try) and len(final.body) == 1
        and not final.orelse and not final.finalbody
        and isinstance(final.body[0], ast.Return)
        and isinstance(final.body[0].value, ast.Subscript)
        and isinstance(final.body[0].value.value, ast.Call)
        and dotted(final.body[0].value.value.func) == "globals"
        and isinstance(final.body[0].value.slice, ast.Name)
        and final.body[0].value.slice.id == parameter
        and len(final.handlers) == 1
        and dotted(final.handlers[0].type) == "KeyError"
        and len(final.handlers[0].body) == 1
        and isinstance(final.handlers[0].body[0], ast.Raise)
        and dotted(final.handlers[0].body[0].exc) == "AttributeError"
    )
    if not guarded_lookup or module_tree is None:
        return False
    # Do not make a negative claim when source can add globals dynamically.
    for item in ast.walk(module_tree):
        if isinstance(item, ast.Subscript) and isinstance(item.ctx, ast.Store) \
                and isinstance(item.value, ast.Call) \
                and dotted(item.value.func) == "globals":
            return False
        if isinstance(item, ast.Call) and (
            dotted(item.func) == "exec" or
            (isinstance(item.func, ast.Attribute)
             and item.func.attr in {"update", "setdefault"}
             and isinstance(item.func.value, ast.Call)
             and dotted(item.func.value.func) == "globals")
        ):
            return False
    return not can_return(node.body[:-1])


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
        for candidate in self._module_candidates(files, module):
            if candidate in files:
                return candidate
        # Some distributions place their import package under src/python/.
        matches = [path for path in files if path.endswith(f"/{stem}/__init__.py")
                   or path.endswith(f"/{stem}.py")]
        return matches[0] if len(matches) == 1 else None

    def _module_candidates(self, files, module):
        stem = module.replace(".", "/")
        candidates = (f"{stem}/__init__.py", f"{stem}.py")
        if (module == self.root_module or module.startswith(f"{self.root_module}.")) \
                and f"src/{self.root_module}/__init__.py" in files:
            candidates += (f"src/{stem}/__init__.py", f"src/{stem}.py")
        return candidates

    def exact_module_file(self, sha, module):
        files = self.paths(sha)
        return next((candidate for candidate in self._module_candidates(files, module)
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
        if getters and not all(getattr_excludes_name(item, name, tree)
                               for item in getters):
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
            # A public object may be constructed from a class in the same module,
            # with the requested method inherited from one of that class's bases.
            if isinstance(result.node, (ast.Assign, ast.AnnAssign)) and result.module:
                value = result.node.value
                constructor = dotted(value.func) if isinstance(value, ast.Call) else None
                if constructor and "." not in constructor:
                    instance_class = self.resolve_name(sha, result.module, constructor)
                    if instance_class.status == "present" and isinstance(
                        instance_class.node, ast.ClassDef
                    ):
                        for child_name in parts[index + 1:]:
                            if not isinstance(instance_class.node, ast.ClassDef):
                                return Resolution("unknown", "nested_attribute_unresolved",
                                                  path=instance_class.path)
                            result = self.resolve_class_member(sha, instance_class, child_name)
                            if result.status != "present":
                                return result if result.status == "unknown" else Resolution(
                                    "unknown", "instance_member_unresolved", path=instance_class.path
                                )
                            instance_class = result
                        return result
            if result.module and result.module != module and self.module_file(sha, result.module):
                module = result.module
            elif self.module_file(sha, f"{module}.{name}"):
                module = f"{module}.{name}"
            else:
                return Resolution("unknown", "nested_attribute_unresolved", path=result.path)
        return result or Resolution("unknown", "empty_api")

    def generated_class_member(self, sha, parent, name):
        """Resolve a literal member installed by a classmethod in a module-level loop."""
        node = parent.node
        if not isinstance(node, ast.ClassDef) or parent.tree is None:
            return None
        installers = set()
        for method in node.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or \
                    not any(dotted(dec) == "classmethod" for dec in method.decorator_list) or \
                    len(method.args.args) < 2:
                continue
            cls_arg, name_arg = method.args.args[:2]
            if any(isinstance(item, ast.Call) and dotted(item.func) == "setattr"
                   and len(item.args) >= 2 and isinstance(item.args[0], ast.Name)
                   and item.args[0].id == cls_arg.arg
                   and isinstance(item.args[1], ast.Name)
                   and item.args[1].id == name_arg.arg
                   for item in ast.walk(method)):
                installers.add(method.name)
        if not installers:
            return None
        for statement in parent.tree.body:
            if not isinstance(statement, ast.For) or not isinstance(statement.target, ast.Tuple) \
                    or not statement.target.elts or not isinstance(statement.target.elts[0], ast.Name) \
                    or not isinstance(statement.iter, ast.Call):
                continue
            loop_name = statement.target.elts[0].id
            installed = any(isinstance(item, ast.Expr) and isinstance(item.value, ast.Call)
                            and dotted(item.value.func) in
                            {f"{node.name}.{method}" for method in installers}
                            and item.value.args and isinstance(item.value.args[0], ast.Name)
                            and item.value.args[0].id == loop_name
                            for item in statement.body)
            if not installed:
                continue
            generator_name = dotted(statement.iter.func)
            if not generator_name or "." not in generator_name:
                continue
            prefix, _, function_name = generator_name.rpartition(".")
            namespace = self.resolve_name(sha, parent.module, prefix)
            if namespace.status != "present" or namespace.reason or not namespace.module:
                continue
            generator = self.resolve_name(sha, namespace.module, function_name)
            if generator.status != "present" or generator.reason or not isinstance(
                generator.node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            for returned in ast.walk(generator.node):
                if not isinstance(returned, ast.Return) or not isinstance(
                    returned.value, (ast.List, ast.Tuple)
                ):
                    continue
                for item in returned.value.elts:
                    if isinstance(item, (ast.Tuple, ast.List)) and item.elts and isinstance(
                        item.elts[0], ast.Constant
                    ) and item.elts[0].value == name:
                        return Resolution("present", "generated_class_member", path=parent.path,
                                          node=statement, tree=parent.tree, module=parent.module)
        return None

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
        generated = self.generated_class_member(sha, parent, name)
        if generated is not None:
            return generated
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
            if base_name == "object" and inherited.status == "absent":
                continue
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

