"""Source-only evidence for SciPy special ufuncs generated before packaging."""

import ast
import json
import re

from source_evolution_rules import literal_exports, module_statements, signature


def _betainc_syntax(source, generated_signatures=None):
    """Read ufunc input/output arity from its source declaration."""
    if generated_signatures is None:
        match = re.search(
            r"betainc\s*=\s*np\.PyUFunc_FromFuncAndData\(.*?,\s*\d+\s*,"
            r"\s*(\d+)\s*,\s*(\d+)\s*,\s*\d+\s*,\s*['\"]betainc['\"]",
            source, re.S)
        arities = {(int(match[1]), int(match[2]))} if match else set()
    else:
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for child in value.values():
                    yield from strings(child)
            elif isinstance(value, list):
                for child in value:
                    yield from strings(child)

        arities = set()
        for value in strings(generated_signatures):
            match = re.fullmatch(r"([A-Za-z]+)->([A-Za-z]+)", value)
            if match:
                arities.add((len(match[1]), len(match[2])))
    if len(arities) != 1:
        return None
    inputs, outputs = arities.pop()
    return {"kind": "ufunc", "required_inputs": inputs,
            "inputs_positional_only": True, "outputs": outputs,
            "form": "scipy.special.betainc(" + ", ".join(
                f"input{i}" for i in range(1, inputs + 1)) + ")"}


def _legacy_betainc_ufunc(reader, sha, export):
    for path, declaration, output in (
        ("scipy/special/generate_ufuncs.py", "UFUNCS", 'generate("_ufuncs.pyx"'),
        ("scipy/special/_generate_pyx.py", "FUNCS", 'generate_ufuncs("_ufuncs"'),
    ):
        if path not in reader.paths(sha):
            continue
        source = reader.source_text(sha, path)
        if not re.search(r"(?m)^" + declaration + r'\s*=\s*"""', source) \
                or output not in source or "f.write(toplevel)" not in source:
            continue
        match = re.search(r"(?m)^betainc\s+--[^\n]*?\b([A-Za-z]+)->([A-Za-z]+)",
                          source)
        if not match:
            continue
        syntax = _betainc_syntax("", match[1] + "->" + match[2])
        if syntax is None:
            continue
        line = source.count("\n", 0, match.start()) + 1
        declaration_location = {"path": path, "line_start": line, "line_end": line}
        return {"kind": "generated_ufunc", "target": "scipy.special._ufuncs.betainc",
                "declaration": declaration_location, "export": export,
                "generator": declaration_location, "parameters": None,
                "call_syntax": syntax, "generated_signatures": match.group(0)}
    return None


def lookup_scipy_legacy_python_export(reader, sha, api):
    if api == "scipy.stats.chi2.sf":
        public_path = reader.module_file(sha, "scipy.stats")
        source_path = reader.module_file(sha, "scipy.stats.distributions")
        if not public_path or not source_path:
            return None
        public = reader.source_text(sha, public_path)
        source = reader.source_text(sha, source_path)
        exported = re.search(r"(?m)^from\s+\.?distributions\s+import\s+\*\s*$",
                             public)
        names = re.search(r"(?ms)^__all__\s*=\s*(\[[^\]]*\])", source)
        base = re.search(r"(?m)^class\s+rv_continuous\s*\([^\n]*\)\s*:", source)
        child = re.search(r"(?m)^class\s+chi2_gen\s*\(\s*rv_continuous\s*\)\s*:",
                          source)
        instance = re.search(r"(?m)^chi2\s*=\s*chi2_gen\s*\(", source)
        if not all((exported, names, base, child, instance)):
            return None
        body = source[base.end():child.start()]
        method = re.search(r"(?m)^    def\s+sf\s*\(([^\n]*)\)\s*:", body)
        if not method:
            return None
        try:
            if "chi2" not in ast.literal_eval(names[1]):
                return None
            parameters = signature(ast.parse(f"def sf({method[1]}): pass").body[0])
        except (ValueError, SyntaxError):
            return None
        line = source.count("\n", 0, base.end() + method.start()) + 1
        export_line = public.count("\n", 0, exported.start()) + 1
        return {"kind": "instance_member",
                "target": "scipy.stats.distributions.rv_continuous.sf",
                "declaration": {"path": source_path, "line_start": line,
                                "line_end": line},
                "export": {"path": public_path, "line_start": export_line,
                           "line_end": export_line},
                "generator": None, "parameters": parameters, "call_syntax": None}
    if api != "scipy.stats.chisqprob":
        return None
    public_path = reader.module_file(sha, "scipy.stats")
    source_path = reader.module_file(sha, "scipy.stats.stats")
    if not public_path or not source_path:
        return None
    public = reader.source_text(sha, public_path)
    source = reader.source_text(sha, source_path)
    exported = re.search(r"(?m)^from\s+\.?stats\s+import\s+\*\s*$", public)
    names = re.search(r"(?ms)^__all__\s*=\s*(\[[^\]]*\])", source)
    definition = re.search(r"(?m)^def\s+chisqprob\s*\(([^\n]*)\)\s*:", source)
    if not exported or not names or not definition:
        return None
    try:
        if "chisqprob" not in ast.literal_eval(names[1]):
            return None
        parameters = signature(ast.parse(
            f"def chisqprob({definition[1]}): pass").body[0])
    except (ValueError, SyntaxError):
        return None
    line = source.count("\n", 0, definition.start()) + 1
    export_line = public.count("\n", 0, exported.start()) + 1
    return {"kind": "wildcard_import", "target": "scipy.stats.stats.chisqprob",
            "declaration": {"path": source_path, "line_start": line, "line_end": line},
            "export": {"path": public_path, "line_start": export_line,
                       "line_end": export_line},
            "generator": None, "parameters": parameters, "call_syntax": None}


_SPECIAL_PYTHON_DEFINITIONS = {
    "comb": ("scipy/special/basic.py", "scipy/special/_basic.py"),
    "factorial": ("scipy/special/basic.py", "scipy/special/_basic.py"),
    "factorial2": ("scipy/special/basic.py", "scipy/special/_basic.py"),
    "logsumexp": ("scipy/special/_logsumexp.py",),
    "sph_jn": ("scipy/special/basic.py",),
    "sph_yn": ("scipy/special/basic.py",),
    "spherical_jn": ("scipy/special/_spherical_bessel.py",),
    "spherical_yn": ("scipy/special/_spherical_bessel.py",),
    "seterr": ("scipy/special/_ufuncs_extra_code.pxi",),
}


def missing_scipy_public_api(reader, sha, api):
    """Prove absence for known SciPy exports hidden by wildcard/lazy imports."""
    if reader.library != "scipy":
        return False
    module, _, name = api.rpartition(".")
    if api == "scipy.datasets.face":
        return reader.module_file(sha, "scipy.datasets") is None
    if api == "scipy.stats.betai":
        public_path = reader.module_file(sha, "scipy.stats")
        source_path = reader.module_file(sha, "scipy.stats.stats")
        if not public_path or not source_path:
            return False
        public = reader.source_text(sha, public_path)
        exports = literal_exports(reader.parse(sha, source_path))
        return (exports is not None and "betai" not in exports
                and re.search(r"(?m)^from\s+\.stats\s+import\s+\*\s*$",
                              public) is not None)
    if module == "scipy.misc" and name in {
            "comb", "factorial", "factorial2", "logsumexp"}:
        public_path = reader.module_file(sha, module)
        common_path = reader.module_file(sha, "scipy.misc._common")
        if not public_path or not common_path:
            return False
        public = reader.source_text(sha, public_path)
        exports = literal_exports(reader.parse(sha, common_path))
        return (exports is not None and name not in exports
                and "from ._common import *" in public
                and "__all__ = _common.__all__" in public
                and "if name not in __all__:" in public)
    paths = (_SPECIAL_PYTHON_DEFINITIONS.get(name)
             if module == "scipy.special" else
             ("scipy/linalg/basic.py", "scipy/linalg/_basic.py")
             if api == "scipy.linalg.pinv2" else None)
    if paths is None:
        return False
    public_path = reader.module_file(sha, module)
    if not public_path:
        return False
    tree = reader.parse(sha, public_path)
    if tree is None:
        return False
    for node, _ in module_statements(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name:
            return False
        if isinstance(node, ast.ImportFrom) and any(
                alias.name != "*" and (alias.asname or alias.name) == name
                for alias in node.names):
            return False
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets):
            return False
    for path in paths:
        if path not in reader.paths(sha):
            continue
        source = reader.source_text(sha, path)
        if re.search(r"(?m)^(?:def|cpdef|cdef)\s+" + re.escape(name)
                     + r"\s*\(", source):
            return False
    if module == "scipy.special":
        for path in ("scipy/special/generate_ufuncs.py",
                     "scipy/special/_generate_pyx.py"):
            if path in reader.paths(sha) and re.search(
                    r"(?m)^" + re.escape(name) + r"\s+--",
                    reader.source_text(sha, path)):
                return False
    return True


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
                    "call_syntax": _betainc_syntax(cython) if name == "betainc" else None,
                    "generated_signatures": None}
        extra_path = "scipy/special/_ufuncs_extra_code.pxi"
        if ('include "_ufuncs_extra_code.pxi"' in cython
                and extra_path in reader.paths(sha)
                and not re.search(r"(?m)^__all__\s*=", cython)):
            extra = reader.source_text(sha, extra_path)
            definition = re.search(r"(?m)^def\s+" + re.escape(name)
                                   + r"\s*\(([^\n]*)\)\s*:", extra)
            if definition:
                try:
                    parameters = signature(ast.parse(
                        f"def {name}({definition[1]}): pass").body[0])
                except SyntaxError:
                    parameters = None
                if parameters is not None:
                    line = extra.count("\n", 0, definition.start()) + 1
                    return {"kind": "generated_extra_code",
                            "target": f"scipy.special._ufuncs.{name}",
                            "declaration": {"path": extra_path, "line_start": line,
                                            "line_end": line},
                            "export": export, "generator": None,
                            "parameters": parameters, "generated_signatures": None}
    if name == "betainc":
        legacy = _legacy_betainc_ufunc(reader, sha, export)
        if legacy:
            return legacy
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
                    "call_syntax": _betainc_syntax("", functions[name])
                    if name == "betainc" else None,
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
