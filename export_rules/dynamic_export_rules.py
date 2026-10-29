"""Phân giải bảng lazy export dạng literal mà không import thư viện đích."""

import ast

from source_evolution_rules import Resolution, dotted, module_statements


def _location(path, node):
    return {"path": path, "line_start": node.lineno,
            "line_end": node.end_lineno}


def _literal_map(value):
    try:
        mapping = ast.literal_eval(value)
    except (ValueError, TypeError, SyntaxError):
        return None
    if not isinstance(mapping, dict):
        return None
    if not all(isinstance(key, str) and isinstance(names, (list, tuple, set))
               and all(isinstance(name, str) for name in names)
               for key, names in mapping.items()):
        return None
    return mapping


def _table_assignments(tree, name):
    for node, conditional in module_statements(tree.body):
        if conditional or not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name
               for target in targets):
            yield node, _literal_map(node.value)


def _lazy_loader_names(tree):
    modules = set()
    functions = set()
    for node, _ in module_statements(tree.body):
        if isinstance(node, ast.Import):
            modules.update(alias.asname or "lazy_loader" for alias in node.names
                           if alias.name == "lazy_loader")
        elif isinstance(node, ast.ImportFrom) and node.module == "lazy_loader":
            functions.update(alias.asname or alias.name for alias in node.names
                             if alias.name == "attach")
    return {f"{name}.attach" for name in modules} | functions


def _installs_lazy_module(targets):
    return any(isinstance(target, ast.Subscript)
               and dotted(target.value) == "sys.modules"
               and isinstance(target.slice, ast.Name)
               and target.slice.id == "__name__"
               for target in targets)


def lookup_dynamic_export(reader, sha, api):
    """Trả binding đã xác minh, hoặc để bước tìm tiếp theo xử lý."""
    module, _, name = api.rpartition(".")
    path = reader.module_file(sha, module)
    if not path or not path.endswith("/__init__.py"):
        return None
    tree = reader.parse(sha, path)
    if tree is None:
        return None
    lazy_names = _lazy_loader_names(tree)
    candidates = []
    for node, conditional in module_statements(tree.body):
        if conditional or not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        calls = [child for child in ast.walk(node.value)
                 if isinstance(child, ast.Call)]
        if any(isinstance(child, ast.Name) and child.id == "__getattr__"
               for target in targets for child in ast.walk(target)):
            for call in calls:
                if dotted(call.func) not in lazy_names:
                    continue
                keyword = next((kw for kw in call.keywords
                                if kw.arg == "submod_attrs"), None)
                if keyword is None:
                    continue
                if isinstance(keyword.value, ast.Name) \
                        and keyword.value.id == "submod_attrs":
                    table = list(_table_assignments(tree, "submod_attrs"))
                else:
                    table = [(node, _literal_map(keyword.value))]
                candidates.extend(("lazy_loader", assignment, call, mapping)
                                  for assignment, mapping in table if mapping)
        for call in calls:
            if dotted(call.func) != "_LazyModule":
                continue
            if not _installs_lazy_module(targets):
                continue
            if not any(isinstance(arg, ast.Name) and arg.id == "_import_structure"
                       for arg in call.args):
                continue
            candidates.extend(("import_structure", assignment, call, mapping)
                              for assignment, mapping in
                              _table_assignments(tree, "_import_structure")
                              if mapping)

    matches = []
    for kind, assignment, loader, mapping in candidates:
        for submodule, names in mapping.items():
            if name not in names or not submodule.strip("."):
                continue
            target_module = f"{module}.{submodule.lstrip('.')}"
            resolved = reader.resolve_name(sha, target_module, name)
            if resolved.status == "present" and resolved.reason is None \
                    and resolved.node is not None:
                binding = {"kind": kind, "path": path,
                           "target": f"{resolved.module}.{getattr(resolved.node, 'name', name)}"}
                evidence = [("export_table", _location(path, assignment)),
                            ("lazy_loader", _location(path, loader))]
                matches.append((resolved, binding, evidence))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return Resolution("unknown", "ambiguous_lazy_export", path=path), None, []
    return None
