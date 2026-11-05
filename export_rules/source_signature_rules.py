"""Trace source declarations behind copied functions and generated wrappers."""

import ast
import json
from functools import lru_cache
from pathlib import Path

from source_evolution_rules import SourceReader, dotted, git, signature


ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / "data" / "pypi_versions_with_github_diy.json"


def _location(path, node):
    return {"path": path, "line_start": node.lineno,
            "line_end": node.end_lineno}


def _numpy_records(first_minor, last_minor):
    data = json.loads(RECORDS.read_text(encoding="utf-8"))
    return [item for item in data if item["library"] == "numpy"
            and item["version"].startswith("1.")
            and first_minor <= int(item["version"].split(".")[1]) <= last_minor]


@lru_cache(maxsize=1)
def numpy_trapz_declaration():
    """Require one source signature throughout the NumPy releases SciPy may use."""
    reader = SourceReader(ROOT / "cache" / "numpy" / "source", "numpy")
    shared, examples = None, []
    for record in _numpy_records(13, 26):
        try:
            found = reader.resolve_api(record["commit_sha"], "numpy.trapz")
        except (OSError, RuntimeError):
            return None, []
        if found.status != "present" or not isinstance(found.node, ast.FunctionDef):
            return None, []
        parameters = signature(found.node)
        if shared is not None and parameters != shared:
            return None, []
        shared = parameters
        examples.append({"type": "dependency_signature", "library": "numpy",
                         "dependency_version": record["version"],
                         "commit_sha": record["commit_sha"],
                         **_location(found.path, found.node)})
    return shared, [examples[0], examples[-1]] if examples else []


@lru_cache(maxsize=1)
def numpy_deprecate_wrapper():
    """Read the literal def newfunc(*args, **kwds), not the wrapped target."""
    repo = ROOT / "cache" / "numpy" / "source"
    shared, examples = None, []
    for record in _numpy_records(8, 18):
        path = "numpy/lib/utils.py"
        source = git(repo, "show", f"{record['commit_sha']}:{path}",
                     check=False)
        if source.returncode:
            return None, []
        try:
            tree = ast.parse(source.stdout)
        except SyntaxError:
            return None, []
        decorator = next((item for item in tree.body if isinstance(
            item, ast.ClassDef) and item.name == "_Deprecate"), None)
        wrapper = next((item for item in ast.walk(decorator)
                        if isinstance(item, ast.FunctionDef)
                        and item.name == "newfunc"), None) if decorator else None
        if wrapper is None:
            return None, []
        forwarded = any(isinstance(item, ast.Return)
                        and isinstance(item.value, ast.Call)
                        and isinstance(item.value.func, ast.Name)
                        and item.value.func.id == "func"
                        and any(isinstance(arg, ast.Starred)
                                for arg in item.value.args)
                        and any(kw.arg is None for kw in item.value.keywords)
                        for item in ast.walk(wrapper))
        parameters = signature(wrapper)
        if not forwarded or (shared is not None and parameters != shared):
            return None, []
        shared = parameters
        examples.append({"type": "dependency_wrapper_signature",
                         "library": "numpy",
                         "dependency_version": record["version"],
                         "commit_sha": record["commit_sha"],
                         **_location(path, wrapper)})
    return shared, [examples[0], examples[-1]] if examples else []


def scipy_assignment_signature(reader, sha, api, resolved):
    if reader.library != "scipy" or not isinstance(resolved.node, ast.Assign):
        return None, [], []
    value = resolved.node.value
    if api in {"scipy.integrate.trapz", "scipy.integrate.trapezoid"}:
        if not isinstance(value, ast.Call) or dotted(value.func) != "_copy_func":
            return None, [], []
        name = api.rsplit(".", 1)[1]
        imported = any(isinstance(item, ast.ImportFrom)
                       and item.module == "numpy"
                       and any(alias.name == "trapz" and
                               (alias.asname or alias.name) == name
                               for alias in item.names)
                       for item in resolved.tree.body)
        copier = next((item for item in resolved.tree.body if isinstance(
            item, ast.FunctionDef) and item.name == "_copy_func"), None)
        if not imported or copier is None or not all(
                token in ast.unparse(copier)
                for token in ("f.__code__", "f.__defaults__", "f.__kwdefaults__")):
            return None, [], []
        parameters, external = numpy_trapz_declaration()
        return parameters, [("function_copy_source", _location(
            resolved.path, copier))] if parameters is not None else [], external

    if not api.startswith("scipy.misc.") or not isinstance(value, ast.Call) \
            or dotted(value.func) != "np.deprecate":
        return None, [], []
    name = api.rsplit(".", 1)[1]
    aliases = {"comb": "_comb", "factorial": "_fact",
               "factorial2": "_fact2", "logsumexp": "_lsm"}
    target = aliases.get(name)
    if not target or not value.args or dotted(value.args[0]) != target:
        return None, [], []
    imported = any(isinstance(item, ast.ImportFrom)
                   and item.module == "scipy.special"
                   and any(alias.name == name and alias.asname == target
                           for alias in item.names)
                   for item in resolved.tree.body)
    if not imported:
        return None, [], []
    parameters, external = numpy_deprecate_wrapper()
    return parameters, [], external


def tensorflow_deprecated_signature(reader, sha, api, resolved):
    if reader.library != "tensorflow" or api not in {
            "tensorflow.arg_max", "tensorflow.arg_min"} \
            or not isinstance(resolved.node, ast.Assign):
        return None, []
    value = resolved.node.value
    if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Call) \
            or dotted(value.func.func) not in {
                "deprecated", "deprecation.deprecated"} \
            or not value.args or dotted(value.args[0]) != api.rsplit(".", 1)[1]:
        return None, []
    imports = resolved.tree.body
    if not any(isinstance(item, ast.ImportFrom)
               and item.module == "tensorflow.python.ops.gen_math_ops"
               and any(alias.name == "*" for alias in item.names)
               for item in imports):
        return None, []
    if dotted(value.func.func) == "deprecation.deprecated" and not any(
            isinstance(item, ast.ImportFrom)
            and item.module == "tensorflow.python.util"
            and any(alias.name == "deprecation" for alias in item.names)
            for item in imports):
        return None, []
    path = "tensorflow/python/util/deprecation.py"
    tree = reader.parse(sha, path)
    if tree is None:
        return None, []
    decorator = next((item for item in tree.body if isinstance(
        item, ast.FunctionDef) and item.name == "deprecated"), None)
    wrapper = next((item for item in ast.walk(decorator)
                    if isinstance(item, ast.FunctionDef)
                    and item.name == "new_func"), None) if decorator else None
    if wrapper is None:
        return None, []
    forwarded = any(isinstance(item, ast.Return)
                    and isinstance(item.value, ast.Call)
                    and dotted(item.value.func) == "func"
                    and any(isinstance(arg, ast.Starred)
                            for arg in item.value.args)
                    and any(kw.arg is None for kw in item.value.keywords)
                    for item in ast.walk(wrapper))
    if not forwarded:
        return None, []
    return signature(wrapper), [("signature_source", _location(path, wrapper))]


def pandas_loc_syntax(reader, sha, api, resolved):
    """Describe the indexer syntax without treating a property getter as a call."""
    if reader.library != "pandas" or api != "pandas.DataFrame.loc":
        return None, []
    node = resolved.node
    property_getter = isinstance(node, ast.FunctionDef) and any(
        dotted(decorator) == "property" for decorator in node.decorator_list)
    generated_property = isinstance(node, ast.For) and any(
        isinstance(item, ast.Call) and dotted(item.func).endswith("._create_indexer")
        for item in ast.walk(node))
    if not (property_getter or generated_property):
        return None, []

    path = "pandas/core/indexing.py"
    tree = reader.parse(sha, path)
    if tree is None:
        return None, []
    classes = {item.name: item for item in tree.body
               if isinstance(item, ast.ClassDef)}
    indexer = classes.get("_LocIndexer")
    if indexer is None:
        return None, []

    def member(owner, name, seen=None):
        if owner.name in (seen or set()):
            return None
        seen = (seen or set()) | {owner.name}
        found = next((item for item in owner.body
                      if isinstance(item, ast.FunctionDef) and item.name == name), None)
        if found is not None:
            return found
        for base in owner.bases:
            parent = classes.get(dotted(base))
            if parent is not None:
                found = member(parent, name, seen)
                if found is not None:
                    return found
        return None

    getitem = member(indexer, "__getitem__")
    if getitem is None:
        return None, []
    forms = ["obj.loc[key]"]
    evidence = [("indexer_syntax_source", _location(path, getitem))]
    call = member(indexer, "__call__")
    if call is not None and any(item["name"] == "axis"
                                for item in signature(call)):
        forms.append("obj.loc(axis=axis)[key]")
        evidence.append(("indexer_syntax_source", _location(path, call)))
    return {"kind": "indexer", "forms": forms}, evidence
