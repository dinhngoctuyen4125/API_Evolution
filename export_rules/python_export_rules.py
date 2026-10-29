"""Tìm điểm export Python dùng chung cho các thư viện."""

import ast

from source_evolution_rules import dotted, module_statements


def _location(path, node):
    return {"path": path, "line_start": node.lineno,
            "line_end": node.end_lineno}


def find_public_binding(reader, sha, api, resolved):
    parts = api.split(".")
    parent_api = ".".join(parts[:-1])
    if len(parts) > 2:
        parent = reader.resolve_api(sha, parent_api)
        if isinstance(parent.node, ast.ClassDef):
            return {"kind": "class_member", "path": resolved.path,
                    "target": f"{resolved.module}.{parent.node.name}.{parts[-1]}"}, []
        if isinstance(parent.node, (ast.Assign, ast.AnnAssign)):
            value = parent.node.value
            constructor = dotted(value.func) if isinstance(value, ast.Call) else None
            if constructor and "." not in constructor and parent.module:
                instance_class = reader.resolve_name(sha, parent.module, constructor)
                if instance_class.status == "present" and isinstance(
                    instance_class.node, ast.ClassDef
                ):
                    owner = next((item.name for item in (resolved.tree.body if resolved.tree else [])
                                  if isinstance(item, ast.ClassDef)
                                  and resolved.node in item.body), None)
                    target = f"{resolved.module}.{owner}.{parts[-1]}" if owner else \
                        f"{resolved.module}.{parts[-1]}"
                    return {"kind": "instance_member", "path": parent.path,
                            "target": target}, [
                        ("instance_constructor", _location(parent.path, parent.node))]

    module = parent_api if reader.module_file(sha, parent_api) else parts[0]
    name = parts[-1] if module == parent_api else parts[1]
    path = reader.module_file(sha, module)
    if not path:
        return {"kind": "resolved_export", "path": resolved.path,
                "target": f"{resolved.module}.{getattr(resolved.node, 'name', name)}"}, []
    tree = reader.parse(sha, path)
    if tree is None:
        return {"kind": "resolved_export", "path": resolved.path,
                "target": f"{resolved.module}.{getattr(resolved.node, 'name', name)}"}, []

    binding = None
    binding_node = None
    wildcard = False
    wildcard_node = None
    for node, _conditional in module_statements(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == name:
            binding = {"kind": "definition", "path": path,
                       "target": f"{module}.{name}"}
            binding_node = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name
                   for target in targets):
                binding = {"kind": "alias", "path": path,
                           "target": dotted(node.value) or f"{module}.{name}"}
                binding_node = node
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    wildcard = True
                    imported = reader.relative_module(sha, module, path, node)
                    if imported == resolved.module:
                        wildcard_node = node
                elif (alias.asname or alias.name) == name:
                    imported = reader.relative_module(sha, module, path, node)
                    binding = {"kind": "import", "path": path,
                               "target": f"{imported}.{alias.name}" if imported else alias.name}
                    binding_node = node
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    binding = {"kind": "import", "path": path,
                               "target": alias.name}
                    binding_node = node
    if binding:
        evidence = [] if binding["kind"] == "definition" else [
            ("public_binding", _location(path, binding_node))]
        return binding, evidence

    target = f"{resolved.module}.{getattr(resolved.node, 'name', name)}"
    evidence = []
    if wildcard_node:
        evidence.append(("public_import", _location(path, wildcard_node)))
        source_tree = reader.parse(sha, resolved.path)
        for item, _conditional in module_statements(source_tree.body):
            if not isinstance(item, (ast.Assign, ast.AnnAssign)):
                continue
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            if not any(isinstance(value, ast.Name) and value.id == "__all__"
                       for value in targets):
                continue
            try:
                exports = ast.literal_eval(item.value)
            except (ValueError, TypeError, SyntaxError):
                continue
            if isinstance(exports, (list, tuple, set)) and name in exports:
                evidence.append(("wildcard_export", _location(resolved.path, item)))
                break
    return {"kind": "wildcard_import" if wildcard else "submodule",
            "path": path, "target": target}, evidence
