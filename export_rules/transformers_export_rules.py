"""Resolve Transformers lazy exports from source at a specific Git commit.

The model directory inferred from a class name is only a search hint. A hit
requires the release's lazy import declarations, a literal module ``__all__``,
and a real definition or import of the requested name in that module.
"""

import ast
import re

from source_evolution_rules import Resolution, dotted, literal_exports, module_statements


def location(path, node):
    return {"path": path, "line_start": node.lineno,
            "line_end": node.end_lineno}


def lazy_declaration(tree):
    """Locate a call that creates an import structure in this package."""
    if tree is None:
        return None
    structure = None
    lazy_module = False
    for statement, _ in module_statements(tree.body):
        if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr)):
            for node in ast.walk(statement):
                if isinstance(node, ast.Call) and dotted(node.func) == "define_import_structure":
                    structure = node
                elif isinstance(node, ast.Call) and dotted(node.func) == "_LazyModule":
                    lazy_module = True
    return structure if lazy_module else None


def model_directories(paths):
    prefix = "src/transformers/models/"
    return {path[len(prefix):].split("/", 1)[0]
            for path in paths if path.startswith(prefix)
            and path.endswith("/__init__.py")
            and path.count("/") == 4}


def candidate_models(paths, name):
    """Narrow the search by spelling; never treat this hint as export proof."""
    spelling = re.sub(r"[^a-z0-9]", "", name.lower())
    spellings = (spelling, spelling[2:]) if spelling.startswith("tf") else (spelling,)
    matches = [model for model in model_directories(paths)
               if any(value.startswith(model.replace("_", "")) for value in spellings)]
    return sorted(matches, key=lambda model: (-len(model.replace("_", "")), model))


def export_declaration(tree, name):
    """Find the literal __all__ statement that names the public object."""
    for statement, conditional in module_statements(tree.body):
        if conditional:
            continue
        targets = (statement.targets if isinstance(statement, ast.Assign)
                   else [statement.target] if isinstance(statement, (ast.AnnAssign, ast.AugAssign))
                   else [])
        touches_all = any(isinstance(target, ast.Name) and target.id == "__all__"
                          for target in targets)
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            touches_all = dotted(statement.value.func) in {
                "__all__.append", "__all__.extend"}
        if touches_all and any(isinstance(node, ast.Constant) and node.value == name
                               for node in ast.walk(statement)):
            return statement
    return None


def lookup_transformers_export(reader, sha, api):
    """Return (resolution, public binding, evidence), or None for generic fallback."""
    if reader.library != "transformers" or not api.startswith("transformers."):
        return None
    names = api.split(".")
    if len(names) < 2:
        return None
    public_name = names[1]
    files = reader.paths(sha)
    root_module = "transformers"
    models_module = "transformers.models"
    root_path = reader.exact_module_file(sha, root_module)
    models_path = reader.exact_module_file(sha, models_module)
    if not root_path or not models_path:
        return None
    root_tree = reader.parse(sha, root_path)
    models_tree = reader.parse(sha, models_path)
    root_lazy = lazy_declaration(root_tree)
    models_lazy = lazy_declaration(models_tree)
    if root_lazy is None or models_lazy is None:
        return None
    if not any(isinstance(node, ast.Constant) and node.value == "models"
               for node in ast.walk(root_lazy)):
        return None

    # The longest model prefix is usually the target. Keep shorter prefixes as
    # candidates too; only source declarations can confirm a match.
    candidates = []
    inspected_models = []
    for model in candidate_models(files, public_name):
        model_module = f"transformers.models.{model}"
        model_path = reader.exact_module_file(sha, model_module)
        if model_path is None:
            continue
        model_lazy = lazy_declaration(reader.parse(sha, model_path))
        if model_lazy is None:
            continue
        inspected_models.append(model_path)
        prefix = model_path.removesuffix("__init__.py")
        matches = []
        adjacent = sorted(path for path in files if path.startswith(prefix)
                          and path != model_path and "/" not in path[len(prefix):]
                          and path.endswith(".py"))
        # Match create_import_structure_from_path: generated modeling files
        # supersede their modular source when both exist in one package.
        if any(path[len(prefix):].startswith("modeling_") for path in adjacent) \
                and any(path[len(prefix):].startswith("modular_") for path in adjacent):
            adjacent = [path for path in adjacent
                        if not path[len(prefix):].startswith("modular_")]
        for path in adjacent:
            tree = reader.parse(sha, path)
            if tree is None or public_name not in (literal_exports(tree) or ()):
                continue
            module = f"{model_module}.{path[len(prefix):-3]}"
            result = reader.resolve_name(sha, module, public_name)
            if result.status == "present" and result.reason is None \
                    and result.node is not None:
                matches.append((result, path, export_declaration(tree, public_name)))
        if len(matches) == 1:
            candidates.append((model_module, model_path, model_lazy, *matches[0]))
        elif len(matches) > 1:
            return (Resolution("unknown", "ambiguous_lazy_export", path=model_path),
                    None, [])
    if len(candidates) != 1:
        if not inspected_models or any(
                isinstance(node, ast.Constant) and node.value == public_name
                for tree in (root_tree, models_tree) for node in ast.walk(tree)):
            return None
        # Source verifies the name is not exported by its candidate model, but
        # another model might export the same spelling. Preserve uncertainty
        # without traversing hundreds of unrelated wildcard imports.
        return (Resolution("unknown", "lazy_export_not_confirmed",
                           path=inspected_models[0]), None, [])
    model_module, model_path, model_lazy, resolved, export_path, export_node = candidates[0]
    class_module = resolved.module
    if len(names) > 2:
        if not isinstance(resolved.node, ast.ClassDef):
            return None
        for member in names[2:]:
            resolved = reader.resolve_class_member(sha, resolved, member)
            if resolved.status != "present":
                break
    target = (f"{class_module}.{names[1]}.{'.'.join(names[2:])}"
              if len(names) > 2 else
              f"{resolved.module}.{getattr(resolved.node, 'name', public_name)}")
    binding = {"kind": "lazy_export", "path": root_path,
               "target": target, "model": model_module,
               "export_module": export_path}
    evidence = [("lazy_import_structure", location(root_path, root_lazy)),
                ("lazy_import_structure", location(models_path, models_lazy)),
                ("lazy_import_structure", location(model_path, model_lazy))]
    if export_node is not None:
        evidence.append(("lazy_module_export", location(export_path, export_node)))
    return resolved, binding, evidence
