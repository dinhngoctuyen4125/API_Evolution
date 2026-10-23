"""Source-only evidence for SciPy special ufuncs generated before packaging."""

import ast
import json
import re

from source_evolution_rules import module_statements, signature


def lookup_scipy_generated_export(reader, sha, api):
    if reader.library != "scipy" or not api.startswith("scipy.special.") \
            or api.count(".") != 2:
        return None
    name = api.rpartition(".")[2]
    public_path = reader.module_file(sha, "scipy.special")
    generator_path = "scipy/special/_generate_pyx.py"
    if not public_path:
        return None
    public_tree = reader.parse(sha, public_path)
    if public_tree is None:
        return None
    wildcard = next((node for node, _ in module_statements(public_tree.body)
                     if isinstance(node, ast.ImportFrom) and node.level == 1
                     and node.module == "_ufuncs"
                     and any(alias.name == "*" for alias in node.names)), None)
    if wildcard is None:
        return None
    export = {"path": public_path, "line_start": wildcard.lineno,
              "line_end": wildcard.end_lineno}
    cython_path = "scipy/special/_ufuncs.pyx"
    if cython_path in reader.paths(sha):
        cython = reader.source_text(sha, cython_path)
        match = re.search(r"^" + re.escape(name) +
                          r"\s*=\s*np\.PyUFunc_FromFuncAndData\s*\(", cython, re.M)
        if match and not re.search(r"^__all__\s*=", cython, re.M):
            line = cython.count("\n", 0, match.start()) + 1
            return {"kind": "cython_ufunc", "target": f"scipy.special._ufuncs.{name}",
                    "declaration": {"path": cython_path, "line_start": line,
                                    "line_end": line},
                    "export": export, "generator": None, "parameters": None,
                    "generated_signatures": None}
    if generator_path not in reader.paths(sha):
        return None
    generator = reader.source_text(sha, generator_path)
    if not all(token in generator for token in
               ("functions.json", "Ufunc(f, sig)", "ufunc.generate(all_loops)")):
        return None
    explicit_all = "module_all" in generator and "ufunc.name" in generator
    implicit_all = "f.write(toplevel)" in generator and "self.name" in generator
    if not (explicit_all or implicit_all) or name.startswith("_"):
        return None
    generator_line = next((i for i, line in enumerate(generator.splitlines(), 1)
                           if ("module_all" in line and "__all__" in line)
                           or (implicit_all and "f.write(toplevel)" in line)), None)
    if generator_line is None:
        return None
    generator_location = {"path": generator_path, "line_start": generator_line,
                          "line_end": generator_line}

    functions_path = "scipy/special/functions.json"
    if functions_path in reader.paths(sha):
        functions_text = reader.source_text(sha, functions_path)
        try:
            functions = json.loads(functions_text)
        except ValueError:
            return None
        if isinstance(functions, dict) and name in functions:
            match = re.search(r'^\s*"' + re.escape(name) + r'"\s*:',
                              functions_text, re.M)
            if match is None:
                return None
            line = functions_text.count("\n", 0, match.start()) + 1
            return {"kind": "generated_ufunc", "target": f"scipy.special._ufuncs.{name}",
                    "declaration": {"path": functions_path, "line_start": line,
                                    "line_end": line},
                    "export": export, "generator": generator_location,
                    "parameters": None, "generated_signatures": functions[name]}

    extra_path = "scipy/special/_ufuncs_extra_code.pxi"
    if extra_path not in reader.paths(sha):
        return None
    extra_text = reader.source_text(sha, extra_path)
    match = re.search(r"^def\s+" + re.escape(name) + r"\s*\(([^\n]*)\)\s*:",
                      extra_text, re.M)
    if match is None:
        return None
    if explicit_all:
        # Newer generators write __all__; older ones rely on Python's default
        # wildcard behavior for public names in the generated module.
        export_names = re.search(r"all_ufuncs\s*=\s*\((.*?)module_all\s*=",
                                 generator, re.S)
        if export_names is None or not re.search(
                r"['\"]" + re.escape(name) + r"['\"]", export_names.group(1)):
            return None
    line = extra_text.count("\n", 0, match.start()) + 1
    try:
        stub = ast.parse(f"def {name}({match.group(1)}): pass").body[0]
        parameters = signature(stub)
    except SyntaxError:
        parameters = None
    return {"kind": "generated_extra_code", "target": f"scipy.special._ufuncs.{name}",
            "declaration": {"path": extra_path, "line_start": line, "line_end": line},
            "export": export, "generator": generator_location,
            "parameters": parameters, "generated_signatures": None}
