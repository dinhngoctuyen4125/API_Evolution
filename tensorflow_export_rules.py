"""Resolve TensorFlow public names declared by literal tf_export decorators."""

import ast
import os
import re
import subprocess
from collections import defaultdict
from functools import lru_cache

from source_evolution_rules import Resolution, dotted, module_statements


_prepared_candidate_paths = {}


def prepare_candidate_paths(repo, export_names):
    """Scan the checkout once for all requested name hints.

    The checkout only supplies candidate paths; lookup_tensorflow_export still
    verifies every decorator in the selected Git commit.
    """
    names = set(export_names)
    pending = {name for name in names if (repo, name) not in _prepared_candidate_paths}
    if not pending:
        return
    paths = {name: set() for name in pending}
    root = repo / "tensorflow"
    if root.is_dir():
        choices = "|".join(re.escape(name) for name in sorted(pending, key=len, reverse=True))
        quoted = re.compile(r"(?P<quote>['\"])(?P<name>" + choices + r")(?P=quote)")
        for path in root.rglob("*.py"):
            if path.stem.endswith("_test") or "tests" in path.parts:
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if "tf_export" not in source:
                continue
            relative = path.relative_to(repo).as_posix()
            for match in quoted.finditer(source):
                paths[match.group("name")].add(relative)
    for name, found in paths.items():
        _prepared_candidate_paths[repo, name] = tuple(sorted(found))


@lru_cache(maxsize=4096)
def candidate_paths(repo, export_name):
    """Use the local checkout only to discover paths; verify every Git commit below."""
    prepared = _prepared_candidate_paths.get((repo, export_name))
    if prepared is not None:
        return prepared
    root = repo / "tensorflow"
    if not root.is_dir():
        return ()
    quoted_name = re.compile(r"(['\"])" + re.escape(export_name) + r"\1")
    paths = []
    for path in root.rglob("*.py"):
        if path.stem.endswith("_test") or "tests" in path.parts:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if "tf_export" in source and quoted_name.search(source):
            paths.append(path.relative_to(repo).as_posix())
    return tuple(sorted(paths))


def literal_names(value):
    try:
        names = ast.literal_eval(value)
    except (ValueError, TypeError, SyntaxError, MemoryError):
        return None
    if isinstance(names, str):
        return (names,)
    if isinstance(names, (list, tuple)) and all(isinstance(item, str) for item in names):
        return tuple(names)
    return None


def export_imports(tree):
    """Recognize tf_export even when the import uses an alias."""
    functions = set()
    modules = set()
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) \
                and statement.module == "tensorflow.python.util.tf_export":
            functions.update((alias.asname or alias.name) for alias in statement.names
                             if alias.name == "tf_export")
        elif isinstance(statement, ast.ImportFrom) \
                and statement.module == "tensorflow.python.util":
            modules.update((alias.asname or alias.name) for alias in statement.names
                           if alias.name == "tf_export")
        elif isinstance(statement, ast.Import):
            modules.update((alias.asname or alias.name) for alias in statement.names
                           if alias.name == "tensorflow.python.util.tf_export")
    return functions, modules


def is_export_call(call, functions, modules):
    function = dotted(call.func)
    return function in functions or any(
        function == f"{module}.tf_export" for module in modules)


def names_from_call(decorator):
    """Return the V1 and V2 export names, or None for a dynamic declaration."""
    v2 = []
    for argument in decorator.args:
        value = literal_names(argument)
        if value is None or len(value) != 1:
            return None
        v2.extend(value)
    v1 = v2
    for keyword in decorator.keywords:
        if keyword.arg == "v1":
            v1 = literal_names(keyword.value)
            if v1 is None:
                return None
        elif keyword.arg == "api_name":
            try:
                if ast.literal_eval(keyword.value) != "tensorflow":
                    return {"v1": [], "v2": []}
            except (ValueError, TypeError, SyntaxError):
                return None
    return {"v1": list(v1), "v2": list(v2)}


def export_decorator(node, tree, name, api_generation):
    """Find a literal tf_export and whether it serves this API generation."""
    functions, modules = export_imports(tree)
    inactive = None
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not is_export_call(
                decorator, functions, modules):
            continue
        export_names = names_from_call(decorator)
        if export_names is None:
            return None
        if name in export_names[f"v{api_generation}"]:
            return decorator, True, export_names
        if name in export_names[f"v{2 if api_generation == 1 else 1}"]:
            inactive = decorator, False, export_names
    return inactive


@lru_cache(maxsize=32)
def export_source_paths(repo, sha):
    """Find export-bearing files; report missing blobs in partial Git clones."""
    env = os.environ.copy()
    env["GIT_NO_LAZY_FETCH"] = "1"
    result = subprocess.run(
        ["git", "-C", str(repo), "grep", "-l", "-F", "tf_export",
         sha, "--", "tensorflow/*.py"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, check=False,
    )
    paths = tuple(line.split(":", 1)[1] for line in result.stdout.splitlines()
                  if ":" in line)
    complete = result.returncode in (0, 1) and not result.stderr.strip()
    return paths, complete


@lru_cache(maxsize=32)
def export_index(reader, sha):
    """Build a version-specific name -> Python declaration index."""
    paths, complete = export_source_paths(reader.repo, sha)
    names = {"v1": defaultdict(list), "v2": defaultdict(list)}
    uncertain_names = {"v1": set(), "v2": set()}
    for path in paths:
        if (not path.startswith("tensorflow/") or path.endswith("_test.py")
                or "/tests/" in path or "/test/" in path):
            continue
        try:
            tree = reader.parse(sha, path)
        except (OSError, RuntimeError):
            complete = False
            continue
        if tree is None:
            complete = False
            continue
        functions, modules = export_imports(tree)
        declarations = {}
        for node, conditional in module_statements(tree.body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call) and is_export_call(
                            decorator, functions, modules):
                        declarations[id(decorator)] = (node, conditional)
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            if not is_export_call(call, functions, modules):
                function = dotted(call.func) or ""
                if function == "tf_export" or function.endswith(".tf_export"):
                    complete = False
                continue
            export_names = names_from_call(call)
            if export_names is None:
                complete = False
                continue
            declaration = declarations.get(id(call))
            for generation, api_names in export_names.items():
                for name in api_names:
                    if declaration is None or declaration[1]:
                        uncertain_names[generation].add(name)
                    else:
                        names[generation][name].append((
                            path, tree, declaration[0], call, export_names))
    return names, uncertain_names, complete


def lookup_tensorflow_export(reader, sha, api, version):
    """Resolve a generated TensorFlow API to its decorated Python definition."""
    if reader.library != "tensorflow" or not api.startswith("tensorflow."):
        return None, None
    name = api.removeprefix("tensorflow.")
    generation = 1 if int(version.split(".", 1)[0]) < 2 else 2
    matches = []
    inactive = False
    unparsed = False
    paths_at_commit = reader.paths(sha)
    for path in candidate_paths(reader.repo, name):
        if path not in paths_at_commit:
            continue
        tree = reader.parse(sha, path)
        if tree is None:
            unparsed = True
            continue
        for node, conditional in module_statements(tree.body):
            if conditional or not isinstance(node, (
                    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            export = export_decorator(node, tree, name, generation)
            if export is None:
                continue
            decorator, active, export_names = export
            if not active:
                inactive = True
                continue
            module = path[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[:-9]
            matches.append({
                "resolution": Resolution("present", path=path, node=node,
                                         tree=tree, module=module),
                "target": f"{module}.{node.name}",
                "export_names": export_names,
                "decorator": {"path": path, "line_start": decorator.lineno,
                              "line_end": decorator.end_lineno},
            })
    if len(matches) > 1:
        return None, "ambiguous_tf_export"
    if matches:
        return matches[0], None
    if inactive and not unparsed:
        return None, "tf_export_other_generation"
    names, uncertain_names, complete = export_index(reader, sha)
    generation_key = f"v{generation}"
    for path, tree, node, decorator, export_names in names[generation_key][name]:
        module = path[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[:-9]
        matches.append({
            "resolution": Resolution("present", path=path, node=node,
                                     tree=tree, module=module),
            "target": f"{module}.{node.name}",
            "export_names": export_names,
            "decorator": {"path": path, "line_start": decorator.lineno,
                          "line_end": decorator.end_lineno},
        })
    if len(matches) > 1:
        return None, "ambiguous_tf_export"
    if matches and name not in uncertain_names[generation_key]:
        return matches[0], None
    if matches or name in uncertain_names[generation_key]:
        return None, "tf_export_binding_unresolved"
    if complete:
        return None, "tf_export_name_absent"
    return None, "tf_export_index_incomplete"
