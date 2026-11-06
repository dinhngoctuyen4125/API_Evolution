"""Resolve TensorFlow public names declared by literal tf_export decorators."""

import ast
import os
import re
import subprocess
from collections import defaultdict
from functools import lru_cache

from source_evolution_rules import Resolution, dotted, module_statements


_prepared_candidate_paths = {}


def export_query(api, version):
    name = api.removeprefix("tensorflow.")
    if name.startswith("compat.v1."):
        return name.removeprefix("compat.v1."), 1
    return name, 1 if int(version.split(".", 1)[0]) < 2 else 2


def compat_v1_generated(reader, sha):
    paths = reader.paths(sha)
    build_path = "tensorflow/BUILD"
    if build_path not in paths:
        return None
    build = reader.source_text(sha, build_path)
    versions = re.findall(r"compat_api_versions\s*=\s*\[([^\]]*)\]", build,
                          re.S)
    if any(re.search(r"\b1\b", values) for values in versions):
        return True
    generator = "tensorflow/python/tools/api/generator/create_python_api.py"
    if generator not in paths or "compat_api_versions" not in reader.source_text(
            sha, generator):
        return False
    return None


def golden_export_status(reader, sha, api, version):
    """Read checked-in API golden files when they describe this public namespace."""
    if api.startswith("tensorflow.compat.v1."):
        generated = compat_v1_generated(reader, sha)
        if generated is False:
            return False
        if generated is not True:
            return None
    name, generation = export_query(api, version)
    parent, _, member = name.rpartition(".")
    module = "tensorflow" + (f".{parent}" if parent else "")
    bases = [f"tensorflow/tools/api/golden/v{generation}"]
    if generation == 1:
        bases.append("tensorflow/tools/api/golden")
    path = next((f"{base}/{module}.pbtxt" for base in bases
                 if f"{base}/{module}.pbtxt" in reader.paths(sha)), None)
    if path is None:
        if not parent:
            return None
        parent_module, _, child = module.rpartition(".")
        parent_path = next((f"{base}/{parent_module}.pbtxt" for base in bases
                            if f"{base}/{parent_module}.pbtxt" in reader.paths(sha)),
                           None)
        if parent_path is None:
            return None
        parent_source = reader.source_text(sha, parent_path)
        if re.search(r'(?m)^  member \{\n    name: "' + re.escape(child) +
                     r'"\s*$', parent_source) is None:
            return False
        return None
    source = reader.source_text(sha, path)
    return re.search(r'(?m)^  member(?:_method)? \{\n    name: "' + re.escape(member)
                     + r'"\s*$', source) is not None


def lookup_tensorflow_generated_substr(reader, sha, api, version):
    """Trace the old generated Substr binding to its C++ OpDef."""
    if api not in {"tensorflow.substr", "tensorflow.strings.substr"} \
            or golden_export_status(reader, sha, api, version) is not True:
        return None
    op_path = "tensorflow/core/ops/string_ops.cc"
    import_path = "tensorflow/python/ops/string_ops.py"
    name, generation = export_query(api, version)
    parent, _, member = name.rpartition(".")
    module = "tensorflow" + (f".{parent}" if parent else "")
    golden_path = f"tensorflow/tools/api/golden/v{generation}/{module}.pbtxt"
    paths = reader.paths(sha)
    if golden_path not in paths:
        golden_path = f"tensorflow/tools/api/golden/{module}.pbtxt"
    if any(path not in paths for path in (op_path, import_path, golden_path)):
        return None
    source = reader.source_text(sha, op_path)
    op = re.search(r'REGISTER_OP\("Substr"\)((?:\s*\.[A-Za-z]+\([^;]*?\))+)',
                   source)
    if op is None:
        return None
    inputs = re.findall(r'\.Input\("([A-Za-z_][A-Za-z_0-9]*):', op.group(1))
    golden = reader.source_text(sha, golden_path)
    member_match = re.search(r'(?m)^  member_method \{\n    name: "' +
                             re.escape(member) + r'"\n    argspec: "([^"\n]*)"',
                             golden)
    if member_match is None:
        return None
    argspec = member_match.group(1).replace("\\'", "'")
    args = re.search(r"args=(\[[^]]*\])", argspec)
    defaults = re.search(r"defaults=(\[[^]]*\])", argspec)
    if args is None or defaults is None:
        return None
    try:
        names = ast.literal_eval(args.group(1))
        default_values = ast.literal_eval(defaults.group(1))
    except (ValueError, SyntaxError):
        return None
    if names != inputs + ["name"] or default_values != ["None"]:
        return None
    imports = reader.source_text(sha, import_path)
    if "from tensorflow.python.ops.gen_string_ops import *" not in imports:
        return None
    line_start = source.count("\n", 0, op.start()) + 1
    golden_line = golden.count("\n", 0, member_match.start()) + 1
    return {
        "declaration": {"path": op_path, "line_start": line_start,
                        "line_end": line_start + 4},
        "export": {"path": golden_path, "line_start": golden_line,
                   "line_end": golden_line + 2},
        "target": "tensorflow.python.ops.gen_string_ops.substr",
        "parameters": [{"name": item, "kind": "positional_or_keyword",
                        "default": "None" if item == "name" else None,
                        "annotation": None} for item in names],
    }


def lookup_tensorflow_generated_div(reader, sha, api):
    """Resolve the old tf.div through the generated math op export chain."""
    if api != "tensorflow.div":
        return None
    op_path = "tensorflow/core/ops/math_ops.cc"
    spec_path = "tensorflow/core/ops/ops.pbtxt"
    math_path = "tensorflow/python/ops/math_ops.py"
    standard_path = "tensorflow/python/ops/standard_ops.py"
    root_path = "tensorflow/python/__init__.py"
    paths = reader.paths(sha)
    if any(path not in paths for path in (
            op_path, spec_path, math_path, standard_path, root_path)):
        return None
    op_source = reader.source_text(sha, op_path)
    op = re.search(r'REGISTER_OP\("Div"\)', op_source)
    spec = reader.source_text(sha, spec_path)
    definition = re.search(r'(?m)^op \{\n  name: "Div"\n(.*?)^\}', spec, re.S | re.M)
    if op is None or definition is None:
        return None
    inputs = re.findall(r'(?m)^  input_arg \{\n    name: "([A-Za-z_]+)"',
                        definition.group(1))
    if inputs != ["x", "y"]:
        return None
    math_source = reader.source_text(sha, math_path)
    standard_source = reader.source_text(sha, standard_path)
    root_source = reader.source_text(sha, root_path)
    chain = (
        "from tensorflow.python.ops.gen_math_ops import *" in math_source
        and "from tensorflow.python.ops.math_ops import *" in standard_source
        and "from tensorflow.python.ops.standard_ops import *" in root_source
        and "remove_undocumented(__name__, _allowed_symbols" in root_source
        and "math_ops" in root_source)
    if not chain:
        return None
    line_start = op_source.count("\n", 0, op.start()) + 1
    spec_line = spec.count("\n", 0, definition.start()) + 1
    export_line = math_source.splitlines().index(
        next(line for line in math_source.splitlines()
             if "from tensorflow.python.ops.gen_math_ops import *" in line)) + 1
    return {
        "declaration": {"path": op_path, "line_start": line_start,
                        "line_end": line_start + 2},
        "export": {"path": math_path, "line_start": export_line,
                   "line_end": export_line},
        "target": "tensorflow.python.ops.gen_math_ops.div",
        "parameters": [{"name": item, "kind": "positional_or_keyword",
                        "default": None, "annotation": None} for item in inputs]
                      + [{"name": "name", "kind": "positional_or_keyword",
                          "default": "None", "annotation": None}],
        "parameter_source": {"path": spec_path, "line_start": spec_line,
                             "line_end": spec_line + 9},
    }


_KERAS_SOURCE_HINTS = {
    "keras.metrics.AUC": ("tensorflow/python/keras/metrics.py",),
    "keras.mixed_precision.LossScaleOptimizer": (
        "tensorflow/python/keras/mixed_precision/loss_scale_optimizer.py",
        "tensorflow/python/keras/mixed_precision/experimental/loss_scale_optimizer.py",
    ),
}

_HISTORICAL_SOURCE_HINTS = {
    "config.experimental_run_functions_eagerly": (
        "tensorflow/python/eager/def_function.py",),
    "config.run_functions_eagerly": (
        "tensorflow/python/eager/def_function.py",),
}


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
            if "tf_export" not in source and "keras_export" not in source:
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
    """Recognize tf_export and keras_export even when imported with aliases."""
    functions = set()
    modules = set()
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) \
                and statement.module == "tensorflow.python.util.tf_export":
            functions.update((alias.asname or alias.name) for alias in statement.names
                             if alias.name in {"tf_export", "keras_export"})
        elif isinstance(statement, ast.ImportFrom) \
                and statement.module == "tensorflow.python.util":
            modules.update((alias.asname or alias.name) for alias in statement.names
                           if alias.name in {"tf_export", "keras_export"})
        elif isinstance(statement, ast.Import):
            modules.update((alias.asname or alias.name) for alias in statement.names
                           if alias.name == "tensorflow.python.util.tf_export")
    return functions, modules


def is_export_call(call, functions, modules):
    function = dotted(call.func)
    return function in functions or any(
        function in {f"{module}.tf_export", f"{module}.keras_export"}
        for module in modules)


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
                if function in {"tf_export", "keras_export"} or function.endswith(
                        (".tf_export", ".keras_export")):
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
    name, generation = export_query(api, version)
    if api.startswith("tensorflow.compat.v1.") \
            and compat_v1_generated(reader, sha) is False:
        return None, "tf_compat_namespace_not_generated"
    matches = []
    inactive = False
    unparsed = False
    paths_at_commit = reader.paths(sha)
    paths = set(candidate_paths(reader.repo, name))
    paths.update(_KERAS_SOURCE_HINTS.get(name, ()))
    paths.update(_HISTORICAL_SOURCE_HINTS.get(name, ()))
    for path in sorted(paths):
        if path not in paths_at_commit:
            continue
        tree = reader.parse(sha, path)
        if tree is None:
            unparsed = True
            continue
        module = path[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[:-9]
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
            matches.append({
                "resolution": Resolution("present", path=path, node=node,
                                         tree=tree, module=module),
                "target": f"{module}.{node.name}",
                "export_names": export_names,
                "decorator": {"path": path, "line_start": decorator.lineno,
                              "line_end": decorator.end_lineno},
            })
        if name in {"arg_max", "arg_min"} and path.endswith("/math_ops.py"):
            functions, modules = export_imports(tree)
            assignments = {node.targets[0].id: node for node in tree.body
                           if isinstance(node, ast.Assign) and len(node.targets) == 1
                           and isinstance(node.targets[0], ast.Name)}
            for statement in tree.body:
                if not isinstance(statement, ast.Expr) or not isinstance(
                        statement.value, ast.Call):
                    continue
                applied = statement.value
                if not isinstance(applied.func, ast.Call) or not is_export_call(
                        applied.func, functions, modules):
                    continue
                if len(applied.args) != 1:
                    continue
                exported = applied.args[0]
                if isinstance(exported, ast.Call) and dotted(
                        exported.func) == "dispatch.add_dispatch_support" \
                        and len(exported.args) == 1:
                    exported = exported.args[0]
                if not isinstance(exported, ast.Name) or exported.id != name:
                    continue
                export_names = names_from_call(applied.func)
                if export_names is None or name not in export_names[f"v{generation}"]:
                    continue
                assignment = assignments.get(name)
                if assignment is None or assignment.lineno >= statement.lineno:
                    continue
                matches.append({
                    "resolution": Resolution("present", path=path, node=assignment,
                                             tree=tree, module=module),
                    "target": f"{module}.{name}",
                    "export_names": export_names,
                    "decorator": {"path": path, "line_start": applied.func.lineno,
                                  "line_end": applied.func.end_lineno},
                })
    if len(matches) > 1:
        return None, "ambiguous_tf_export"
    if matches:
        return matches[0], None
    if golden_export_status(reader, sha, api, version) is False:
        return None, "tf_api_golden_name_absent"
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
