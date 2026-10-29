"""Prove selected torch.* Python exports from their binding generator.

This reads registration metadata and Python export code, never operator bodies.
It only returns positive evidence; an unrecognized binding stays unresolved.
"""

import ast
import re

from source_evolution_rules import dotted


SCHEMA = "aten/src/ATen/native/native_functions.yaml"
GENERATOR = "tools/autograd/gen_python_functions.py"
TEMPLATE = "tools/autograd/templates/python_torch_functions.cpp"


def _literal(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, TypeError):
                return None
    return None


def _condition(node, variable, name, private_ops):
    """Evaluate only the export guards understood by this resolver."""
    if isinstance(node, ast.BoolOp):
        values = [_condition(value, variable, name, private_ops)
                  for value in node.values]
        if any(value is None for value in values):
            return None
        return any(values) if isinstance(node.op, ast.Or) else all(values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = _condition(node.operand, variable, name, private_ops)
        return None if value is None else not value
    if isinstance(node, ast.Call) and dotted(node.func) == f"{variable}.startswith" \
            and len(node.args) == 1 and isinstance(node.args[0], ast.Constant):
        return name.startswith(node.args[0].value)
    if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1 \
            and isinstance(node.left, ast.Name) and node.left.id == variable:
        right = node.comparators[0]
        if isinstance(right, ast.Name) and right.id == "PRIVATE_OPS" \
                and private_ops is not None and isinstance(node.ops[0], ast.In):
            return name in private_ops
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            if isinstance(node.ops[0], ast.Eq):
                return name == right.value
            if isinstance(node.ops[0], ast.NotEq):
                return name != right.value
    return None


def _python_export(reader, sha, name):
    path = reader.module_file(sha, "torch")
    tree = reader.parse(sha, path) if path else None
    if tree is None:
        return None
    private_ops = _literal(tree, "PRIVATE_OPS")
    for loop in tree.body:
        if not isinstance(loop, ast.For) or not isinstance(loop.target, ast.Name):
            continue
        variable = loop.target.id
        call = loop.iter
        if not isinstance(call, ast.Call) or dotted(call.func) != "dir" \
                or len(call.args) != 1 \
                or dotted(call.args[0]) != "_C._VariableFunctions":
            continue
        writes_global = any(
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Call)
            and dotted(target.value.func) == "globals"
            and isinstance(target.slice, ast.Name)
            and target.slice.id == variable
            for item in loop.body if isinstance(item, ast.Assign)
            for target in item.targets
        )
        reads_binding = any(
            isinstance(item, ast.Call) and dotted(item.func) == "getattr"
            and len(item.args) >= 2
            and dotted(item.args[0]) == "_C._VariableFunctions"
            and isinstance(item.args[1], ast.Name)
            and item.args[1].id == variable
            for item in ast.walk(loop)
        )
        if not (writes_global and reads_binding):
            continue
        guards = [item for item in ast.walk(loop) if isinstance(item, ast.If)
                  and any(isinstance(child, ast.Continue) for child in item.body)]
        if any(_condition(guard.test, variable, name, private_ops) is not False
               for guard in guards):
            continue
        return {"path": path, "line_start": loop.lineno,
                "line_end": loop.end_lineno}
    return None


def _generator(reader, sha, name):
    if GENERATOR not in reader.paths(sha) or TEMPLATE not in reader.paths(sha):
        return None
    tree = reader.parse(sha, GENERATOR)
    if tree is None:
        return None
    functions = {node.name: node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    selector = functions.get("is_py_torch_function")
    filter_function = functions.get("should_generate_py_binding")
    if not selector or not filter_function or len(selector.body) != 1 \
            or not isinstance(selector.body[0], ast.Return) \
            or ast.unparse(selector.body[0].value) != (
                "f.python_module is None and Variant.function in f.variants"):
        return None
    filter_source = ast.get_source_segment(reader.source_text(sha, GENERATOR),
                                           filter_function) or ""
    if "SKIP_PYTHON_BINDINGS" not in filter_source \
            or "cpp.name(f.func)" not in filter_source:
        return None
    patterns = _literal(tree, "_SKIP_PYTHON_BINDINGS")
    if patterns is None:
        patterns = _literal(tree, "SKIP_PYTHON_BINDINGS")
    signatures = _literal(tree, "SKIP_PYTHON_BINDINGS_SIGNATURES")
    if not isinstance(patterns, list) or not isinstance(signatures, list):
        return None
    if any(re.fullmatch(pattern, name) for pattern in patterns) \
            or any(item.startswith(f"{name}(") for item in signatures):
        return None
    binding_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and dotted(node.func) in {
                         "create_python_bindings", "create_python_bindings_sharded"}
                     and any(isinstance(arg, ast.Name)
                             and arg.id == "is_py_torch_function" for arg in node.args)
                     and any(isinstance(arg, ast.Constant) and arg.value == "torch"
                             for arg in node.args)
                     and any(isinstance(arg, ast.Constant)
                             and arg.value == "python_torch_functions.cpp"
                             for arg in node.args)]
    source = reader.source_text(sha, GENERATOR)
    template = reader.source_text(sha, TEMPLATE)
    if not binding_calls or "parse_native_yaml" not in source \
            or "_VariableFunctions" not in template:
        return None
    return {"path": GENERATOR, "line_start": binding_calls[0].lineno,
            "line_end": binding_calls[0].end_lineno}


def _argument_text(lines, start):
    """Read a possibly multiline func schema through its outer argument list."""
    text = ""
    depth = 0
    opened = False
    for index in range(start, len(lines)):
        fragment = lines[index].split("func:", 1)[1] if index == start \
            else lines[index].strip()
        text += " " + fragment.strip()
        for position, char in enumerate(fragment):
            if char == "(":
                depth += 1
                opened = True
            elif char == ")":
                depth -= 1
                if opened and depth == 0:
                    return text[:len(text) - len(fragment) + position + 1].strip(), index
        if index > start and re.match(r"^\s*[\w]+:\s*", lines[index]):
            break
    return None, None


def _split_arguments(text):
    pieces, start, depth = [], 0, 0
    for index, char in enumerate(text):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            pieces.append(text[start:index].strip())
            start = index + 1
    pieces.append(text[start:].strip())
    return pieces


def _parameters(schema):
    opening = schema.find("(")
    if opening < 0 or not schema.endswith(")"):
        return None
    parameters = []
    keyword_only = False
    for item in _split_arguments(schema[opening + 1:-1]):
        if item == "*":
            keyword_only = True
            continue
        declaration, separator, default = item.partition("=")
        match = re.fullmatch(r"(.+?)\s+([A-Za-z_]\w*)", declaration.strip())
        if not match:
            return None
        annotation, name = match.groups()
        parameters.append({"name": name,
                           "kind": "keyword_only" if keyword_only else
                           "positional_or_keyword",
                           "default": default.strip() if separator else None,
                           "annotation": annotation})
    return parameters


def _schema_entry(lines, name):
    start = next((index for index, line in enumerate(lines)
                  if re.match(r"^\s*-\s*func:\s*" + re.escape(name) + r"\(", line)),
                 None)
    if start is None:
        return None
    end = next((index for index in range(start + 1, len(lines))
                if re.match(r"^\s*-\s*func:\s*", lines[index])), len(lines))
    block = lines[start:end]
    variant = next((line.split(":", 1)[1].strip() for line in block
                    if re.match(r"^\s*variants:\s*", line)), None)
    if variant is None or "function" not in variant.split(", ") \
            or any(re.match(r"^\s*python_module:\s*\S", line) for line in block) \
            or any(re.match(r"^\s*tags:\s*.*\bgenerated\b", line) for line in block):
        return None
    schema, last = _argument_text(lines, start)
    return {"location": {"path": SCHEMA, "line_start": start + 1,
                         "line_end": (last if last is not None else start) + 1},
            "parameters": _parameters(schema) if schema else None}


def _out_merge_rule(reader, sha):
    """Locate codegen's rule that merges an out overload into Python's signature."""
    markers = ("signature=outplace.signature if outplace is not None else base.signature",
               'signature_kwargs = out.signature.__dict__.copy()')
    for path in (GENERATOR, "tools/codegen/api/python.py", "torchgen/api/python.py"):
        if path not in reader.paths(sha):
            continue
        lines = reader.source_text(sha, path).splitlines()
        line = next((index + 1 for index, text in enumerate(lines)
                     if any(marker in text for marker in markers)
                     or ("signature=outplace.signature" in text
                         and "if outplace is not None" in " ".join(
                             lines[index:index + 3])
                         and "else base.signature" in " ".join(
                             lines[index:index + 4]))), None)
        if line:
            return {"path": path, "line_start": line, "line_end": line}
    return None


def _schema(reader, sha, name):
    if SCHEMA not in reader.paths(sha):
        return None
    lines = reader.source_text(sha, SCHEMA).splitlines()
    base = _schema_entry(lines, name)
    if base is None:
        return None
    declared = base["parameters"]
    effective = [item.copy() for item in declared] if declared is not None else None
    extra_evidence = []
    out = _schema_entry(lines, name + ".out")
    if out and effective is not None:
        out_parameters = out["parameters"]
        rule = _out_merge_rule(reader, sha)
        matching_inputs = (out_parameters is not None
                           and out_parameters[:len(declared)] == declared)
        outputs = out_parameters[len(declared):] if matching_inputs else []
        if rule and outputs and all(item["kind"] == "keyword_only"
                                    and item["annotation"].startswith("Tensor")
                                    for item in outputs):
            effective.append({"name": "out", "kind": "keyword_only",
                              "default": "None", "annotation": None})
            extra_evidence = [("binding_overload", out["location"]),
                              ("python_signature_rule", rule)]
        else:
            effective = None
    return {"location": base["location"],
            "declared_parameters": declared,
            "effective_parameters": effective,
            "parameter_evidence": extra_evidence}


def lookup_torch_python_binding(reader, sha, api):
    """Confirm a generated top-level Python API, or leave it unresolved."""
    if reader.library != "pytorch" or not api.startswith("torch.") \
            or api.count(".") != 1:
        return None
    name = api.split(".", 1)[1]
    export = _python_export(reader, sha, name)
    generator = _generator(reader, sha, name) if export else None
    declaration = _schema(reader, sha, name) if generator else None
    if not (export and generator and declaration):
        return None
    return {"declaration": declaration["location"],
            "declared_parameters": declaration["declared_parameters"],
            "effective_parameters": declaration["effective_parameters"],
            "parameter_evidence": declaration["parameter_evidence"],
            "generator": generator, "export": export}
