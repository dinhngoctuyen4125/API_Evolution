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


def legacy_structure_entries(tree, name):
    """Return literal _import_structure entries that export a name."""
    entries = []
    for statement, _ in module_statements(tree.body):
        values = []
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id == "_import_structure" \
                        and isinstance(statement.value, ast.Dict):
                    values.extend(zip(statement.value.keys, statement.value.values))
                elif isinstance(target, ast.Subscript) \
                        and dotted(target.value) == "_import_structure":
                    values.append((target.slice, statement.value))
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            call = statement.value
            if isinstance(call.func, ast.Attribute) and call.func.attr in {"append", "extend"} \
                    and isinstance(call.func.value, ast.Subscript) \
                    and dotted(call.func.value.value) == "_import_structure" \
                    and len(call.args) == 1:
                values.append((call.func.value.slice, call.args[0]))
        for key, value in values:
            if isinstance(key, ast.Constant) and isinstance(key.value, str) \
                    and any(isinstance(item, ast.Constant) and item.value == name
                            for item in ast.walk(value)):
                entries.append((key.value, statement))
    return entries


def lookup_legacy_export(reader, sha, names, root_path, root_tree):
    """Use the old literal lazy-import map before generic wildcard resolution."""
    if root_tree is None or not any(
            isinstance(item, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "_import_structure"
                    for target in item.targets)
            and isinstance(item.value, ast.Dict) for item in root_tree.body):
        return None
    public_name = names[1]
    root_entries = legacy_structure_entries(root_tree, public_name)
    if not root_entries:
        if public_name.endswith(("ImageProcessor", "FeatureExtractor")) \
                and not any(isinstance(node, ast.Constant) and node.value == public_name
                            for node in ast.walk(root_tree)):
            return (Resolution("absent", "public_name_not_in_import_structure",
                               path=root_path), None, [])
        return None

    # TYPE_CHECKING imports may rebind an alias. Follow the runtime map instead.
    if len(names) != 2:
        return None
    if reader.resolve_api(sha, ".".join(names)).status == "present":
        return None
    matches = {}
    for key, root_node in root_entries:
        if not key.startswith("models."):
            continue
        model_module = f"transformers.{key}"
        model_path = reader.exact_module_file(sha, model_module)
        model_tree = reader.parse(sha, model_path) if model_path else None
        if model_tree is None:
            continue
        for source_key, model_node in legacy_structure_entries(model_tree, public_name):
            source_module = f"{model_module}.{source_key}"
            resolved = reader.resolve_name(sha, source_module, public_name)
            if resolved.status == "present" and resolved.reason is None \
                    and isinstance(resolved.node, (ast.ClassDef, ast.Assign, ast.AnnAssign)):
                matches[(resolved.path, resolved.node.lineno)] = (
                    resolved, model_path, root_node, model_node)
    if len(matches) != 1:
        return None
    resolved, model_path, root_node, model_node = next(iter(matches.values()))
    binding = {"kind": "lazy_export", "path": root_path,
               "target": f"{resolved.module}.{public_name}"}
    evidence = [("lazy_import_structure", location(root_path, root_node)),
                ("lazy_import_structure", location(model_path, model_node))]
    return resolved, binding, evidence


def missing_feature_encoder(reader, sha, api):
    """Check the model's own class chain for the renamed freeze method."""
    if reader.library != "transformers" or not api.endswith(".freeze_feature_encoder"):
        return None
    parent = reader.resolve_api(sha, api.rsplit(".", 1)[0])
    if parent.status != "present" or not isinstance(parent.node, ast.ClassDef):
        return None
    for _ in range(3):
        if any(getattr(item, "name", None) == "freeze_feature_encoder"
               or isinstance(item, ast.Assign) and any(
                   isinstance(target, ast.Name) and target.id == "freeze_feature_encoder"
                   for target in item.targets) for item in parent.node.body):
            return None
        if parent.node.name in {"PreTrainedModel", "TFPreTrainedModel"} \
                and parent.path in {"src/transformers/modeling_utils.py",
                                    "src/transformers/modeling_tf_utils.py"} \
                and "freeze_feature_encoder" not in reader.source_text(sha, parent.path):
            return Resolution("absent", "class_member_not_declared", path=parent.path)
        if len(parent.node.bases) != 1:
            return None
        base = dotted(parent.node.bases[0])
        if not base or "." in base:
            return None
        parent = reader.resolve_name(sha, parent.module, base)
        if parent.status != "present" or not isinstance(parent.node, ast.ClassDef):
            return None
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
        return lookup_legacy_export(reader, sha, names, root_path, root_tree)
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
        # A model-specific FeatureExtractor belongs to the matching model
        # package; its missing literal export proves this public name absent.
        spelling = re.sub(r"[^a-z0-9]", "", public_name.lower())
        if any(spelling == path.split("/models/", 1)[1].split("/", 1)[0]
               .replace("_", "") + "featureextractor"
               for path in inspected_models):
            return (Resolution("absent", "lazy_export_not_declared",
                               path=inspected_models[0]), None, [])
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
