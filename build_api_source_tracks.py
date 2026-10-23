"""Build independent source histories for the two APIs in each mapping pair.

Run: python build_api_source_tracks.py --library numpy
This is source analysis only. Release notes and migration synthesis are later phases.
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

from crawl_source import validate_source
from enrich_pypi_versions import REPOSITORIES
from source_evolution_rules import (
    SourceReader, body_hash, docstring_spans, dotted, error_message_changes,
    error_message_profile, expr, forwarding_wrapper_target, git, module_statements,
    signature, source_structure,
)
from tensorflow_export_rules import lookup_tensorflow_export
from torch_binding_rules import lookup_torch_python_binding


ROOT = Path(__file__).resolve().parent
PAIRS_FILE = ROOT / "data" / "api_mapping_pairs.json"
VERSIONS_FILE = ROOT / "data" / "pypi_versions_with_github_diy.json"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z", re.I)


# 1. Read the mappings and the curated Git releases.
def load_inputs(library, mapping_id=None):
    pairs = json.loads(PAIRS_FILE.read_text(encoding="utf-8"))
    records = json.loads(VERSIONS_FILE.read_text(encoding="utf-8"))
    selected = [pair for pair in pairs if pair["library"] == library
                and (mapping_id is None or pair["id"] == mapping_id)]
    versions = [record for record in records if record["library"] == library]
    versions.sort(key=lambda record: tuple(int(x) for x in record["version"].split(".")))
    if not selected or not versions:
        raise ValueError(f"No mapping pairs or curated versions for {library}")
    if len({item["version"] for item in versions}) != len(versions):
        raise ValueError(f"Duplicate curated versions for {library}")
    return selected, versions


def select_range(versions, first, last):
    names = [item["version"] for item in versions]
    start = names.index(first) if first else 0
    stop = names.index(last) + 1 if last else len(names)
    if start >= stop:
        raise ValueError("The selected version range is empty or reversed")
    return versions[start:stop]


# 2. Check that each release points to the intended source commit and tag.
def validate_releases(repo, library, versions):
    validate_source(repo, f"https://github.com/{REPOSITORIES[library]}.git")
    results = {}
    for index, record in enumerate(versions, 1):
        version, sha, tag = (record.get(key) for key in ("version", "commit_sha", "tag"))
        reason = None
        if not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha):
            reason = "invalid_commit_sha"
        elif record.get("source_url") != (
            f"https://github.com/{REPOSITORIES[library]}/tree/{sha}"
        ):
            reason = "source_url_mismatch"
        elif git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode:
            reason = "commit_missing"
        elif not isinstance(tag, str) or not tag:
            reason = "tag_missing"
        else:
            resolved = git(repo, "rev-parse", "--verify",
                           f"refs/tags/{tag}^{{commit}}", check=False)
            if resolved.returncode:
                reason = "tag_not_fetched"
            elif resolved.stdout.strip().lower() != sha.lower():
                reason = "tag_mismatch"
        results[version] = reason
        if index == 1 or index % 25 == 0 or index == len(versions):
            print(f"  validated {index}/{len(versions)} releases", file=sys.stderr,
                  flush=True)
    return results


# 3. Resolve a public binding and read its source declaration at one release.
def find_binding(reader, sha, api, resolved):
    def location(path, node):
        return {"path": path, "line_start": node.lineno,
                "line_end": node.end_lineno}

    parts = api.split(".")
    parent_api = ".".join(parts[:-1])
    if len(parts) > 2:
        parent = reader.resolve_api(sha, parent_api)
        if isinstance(parent.node, ast.ClassDef):
            return {"kind": "class_member", "path": resolved.path,
                    "target": f"{resolved.module}.{parent.node.name}.{parts[-1]}"}, []

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
            ("public_binding", location(path, binding_node))]
        return binding, evidence
    target = f"{resolved.module}.{getattr(resolved.node, 'name', name)}"
    evidence = []
    if wildcard_node:
        evidence.append(("public_import", location(path, wildcard_node)))
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
                evidence.append(("wildcard_export", location(resolved.path, item)))
                break
    return {"kind": "wildcard_import" if wildcard else "submodule",
            "path": path, "target": target}, evidence


def resolve_effective_parameters(reader, sha, resolved):
    node = resolved.node
    target = forwarding_wrapper_target(node)
    if target:
        if not resolved.module:
            return None, None
        forwarded = reader.resolve_name(sha, resolved.module, target)
        if forwarded.status == "present":
            return signature(forwarded.node), forwarded
        return None, None
    if resolved.alias_target and resolved.module:
        alias = resolved.alias_target
        if "." not in alias:
            target_resolution = reader.resolve_name(sha, resolved.module, alias)
        elif alias.startswith(f"{reader.root_module}."):
            target_resolution = reader.resolve_api(sha, alias)
        else:
            return None, None
        if target_resolution.status == "present":
            return signature(target_resolution.node), target_resolution
        return None, None
    # A direct declaration already is the effective signature.
    return signature(node), None


def read_api_state(reader, record, api, validation_reason):
    if validation_reason:
        return "unknown", None, validation_reason
    sha = record["commit_sha"]
    try:
        resolved = reader.resolve_api(sha, api)
        tf_export_binding = None
        if resolved.status != "present" and reader.library == "tensorflow":
            tf_export_binding, export_reason = lookup_tensorflow_export(
                reader, sha, api, record["version"])
            if (export_reason in {"tf_export_other_generation",
                                  "tf_export_name_absent"}
                    and resolved.reason == "generated_public_namespace_unresolved"):
                return "absent", None, export_reason
            if export_reason:
                return "unknown", None, export_reason
            if tf_export_binding:
                resolved = tf_export_binding["resolution"]
        if resolved.status != "present":
            binding = lookup_torch_python_binding(reader, sha, api)
            if binding:
                state = {
                    "version": record["version"], "commit_sha": sha,
                    "source_location": binding["declaration"],
                    "public_binding": {
                        "kind": "generated_python_binding",
                        "path": binding["export"]["path"],
                        "target": f"torch._C._VariableFunctions.{api.split('.')[-1]}",
                    },
                    "declared_parameters": binding["declared_parameters"],
                    "effective_parameters": binding["effective_parameters"],
                    "return_annotation": None,
                    "evidence_refs": [],
                }
                private = {"body_hash": None, "binding_declaration": True,
                           "binding_locations": [
                    ("binding_generator", binding["generator"]),
                    ("python_export", binding["export"]),
                    *binding["parameter_evidence"],
                ]}
                return "present", (state, private), None
            return resolved.status, None, resolved.reason
        node = resolved.node
        declared = signature(node)
        effective, effective_resolution = resolve_effective_parameters(
            reader, sha, resolved)
        if tf_export_binding:
            public_binding = {"kind": "tf_export",
                              "path": tf_export_binding["decorator"]["path"],
                              "target": tf_export_binding["target"],
                              "export_names": tf_export_binding["export_names"]}
            binding_evidence = [("tf_export_decorator",
                                 tf_export_binding["decorator"])]
        else:
            public_binding, binding_evidence = find_binding(reader, sha, api, resolved)
        state = {
            "version": record["version"],
            "commit_sha": sha,
            "source_location": {"path": resolved.path,
                                "line_start": getattr(node, "lineno", None),
                                "line_end": getattr(node, "end_lineno", None)},
            "public_binding": public_binding,
            "declared_parameters": declared,
            "effective_parameters": effective,
            "return_annotation": expr(node.returns)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None,
            "evidence_refs": [],
        }
        private = {"body_hash": body_hash(node),
                   "source_structure": source_structure(node),
                   "error_message_profile": error_message_profile(node),
                   "docstring_spans": docstring_spans(node),
                   "binding_locations": binding_evidence}
        if effective_resolution and effective_resolution.path:
            target_node = effective_resolution.node
            private["effective_location"] = {
                "path": effective_resolution.path,
                "line_start": getattr(target_node, "lineno", None),
                "line_end": getattr(target_node, "end_lineno", None),
            }
            private["effective_docstring_spans"] = docstring_spans(target_node)
        return "present", (state, private), None
    except (OSError, RuntimeError) as exc:
        return "unknown", None, f"source_read_failed: {exc}"


# 4. Compare only adjacent curated releases where this API exists in both.
def compare_parameters(before, after, scope):
    if before is None or after is None:
        return []
    changes = []
    old = {item["name"]: item for item in before}
    new = {item["name"]: item for item in after}
    removed = list(old.keys() - new.keys())
    added = list(new.keys() - old.keys())
    if len(removed) == len(added) == 1 and len(before) == len(after):
        old_item, new_item = old[removed[0]], new[added[0]]
        old_position = next(i for i, item in enumerate(before) if item["name"] == removed[0])
        if (new[added[0]] == after[old_position]
                and all(old_item[key] == new_item[key]
                        for key in ("kind", "default", "annotation"))):
            changes.append({"kind": "parameter_renamed", "scope": scope,
                            "before": removed[0], "after": added[0]})
            removed.clear()
            added.clear()
    for name in sorted(removed):
        changes.append({"kind": "parameter_removed", "scope": scope,
                        "parameter": name, "before": old[name], "after": None})
    for name in sorted(added):
        changes.append({"kind": "parameter_added", "scope": scope,
                        "parameter": name, "before": None, "after": new[name]})
    for name in sorted(old.keys() & new.keys()):
        for field, kind in (("kind", "parameter_kind_changed"),
                            ("default", "parameter_default_changed"),
                            ("annotation", "parameter_annotation_changed")):
            if old[name][field] != new[name][field]:
                changes.append({"kind": kind, "scope": scope, "parameter": name,
                                "before": old[name][field], "after": new[name][field]})
    common = old.keys() & new.keys()
    before_order = [item["name"] for item in before if item["name"] in common]
    after_order = [item["name"] for item in after if item["name"] in common]
    if before_order != after_order:
        changes.append({"kind": "parameter_order_changed", "scope": scope,
                        "before": before_order, "after": after_order})
    return changes


def compare_source_structure(old, new, old_private, new_private):
    before = old_private.get("source_structure")
    after = new_private.get("source_structure")
    if not before or not after:
        return []
    changes = []
    if before["kind"] != after["kind"]:
        changes.append({"kind": "definition_kind_changed",
                        "before": before["kind"], "after": after["kind"]})

    def changed(field, kind, applicable):
        if applicable and before.get(field) != after.get(field):
            changes.append({"kind": kind, "before": before.get(field),
                            "after": after.get(field)})

    class_involved = "ClassDef" in (before["kind"], after["kind"])
    assignment_involved = any(kind in ("Assign", "AnnAssign")
                              for kind in (before["kind"], after["kind"]))
    import_involved = any(kind in ("Import", "ImportFrom")
                          for kind in (before["kind"], after["kind"]))
    changed("bases", "class_bases_changed", class_involved)
    changed("expression", "assignment_expression_changed", assignment_involved)
    changed("annotation", "assignment_annotation_changed", assignment_involved)
    if assignment_involved:
        changed("targets", "assignment_targets_changed", True)
    elif import_involved:
        changed("targets", "import_targets_changed", True)
        changed("module", "import_module_changed", True)
        changed("level", "import_level_changed", True)

    if not class_involved:
        return changes
    old_methods = before.get("methods", [])
    new_methods = after.get("methods", [])
    old_names = [method["name"] for method in old_methods]
    new_names = [method["name"] for method in new_methods]
    if len(set(old_names)) != len(old_names) or len(set(new_names)) != len(new_names):
        # Repeated overload declarations cannot be paired safely by name.
        def normalized(methods):
            return [{key: method[key] for key in
                     ("name", "kind", "parameters", "return_annotation", "body_hash")}
                    for method in methods]
        if normalized(old_methods) != normalized(new_methods):
            changes.append({"kind": "declared_methods_changed",
                            "scope": "direct_class_declaration",
                            "before": old_names, "after": new_names})
        return changes
    old_by_name = {method["name"]: method for method in old_methods}
    new_by_name = {method["name"]: method for method in new_methods}

    def method_evidence(method, state):
        return {"name": method["name"], "kind": method["kind"],
                "parameters": method["parameters"],
                "return_annotation": method["return_annotation"],
                "source_location": {"path": state["source_location"]["path"],
                                    "line_start": method["line_start"],
                                    "line_end": method["line_end"]}}

    for name in sorted(old_by_name.keys() - new_by_name.keys()):
        changes.append({"kind": "declared_method_removed",
                        "scope": "direct_class_declaration", "method": name,
                        "before": method_evidence(old_by_name[name], old), "after": None})
    for name in sorted(new_by_name.keys() - old_by_name.keys()):
        changes.append({"kind": "declared_method_added",
                        "scope": "direct_class_declaration", "method": name,
                        "before": None, "after": method_evidence(new_by_name[name], new)})
    for name in sorted(old_by_name.keys() & new_by_name.keys()):
        old_method, new_method = old_by_name[name], new_by_name[name]
        if old_method["kind"] != new_method["kind"]:
            changes.append({"kind": "declared_method_kind_changed",
                            "scope": "direct_class_declaration", "method": name,
                            "before": old_method["kind"], "after": new_method["kind"]})
        changes.extend(compare_parameters(old_method["parameters"],
                                          new_method["parameters"],
                                          f"declared_method:{name}"))
        if old_method["return_annotation"] != new_method["return_annotation"]:
            changes.append({"kind": "declared_method_return_annotation_changed",
                            "method": name,
                            "before": old_method["return_annotation"],
                            "after": new_method["return_annotation"]})
        if old_method["body_hash"] != new_method["body_hash"]:
            changes.append({"kind": "declared_method_implementation_changed",
                            "method": name,
                            "before": method_evidence(old_method, old),
                            "after": method_evidence(new_method, new)})
    return changes


def compare_states(old, new, old_private, new_private):
    changes = compare_source_structure(old, new, old_private, new_private)
    old_effective = old["effective_parameters"]
    new_effective = new["effective_parameters"]
    if old_effective is not None and new_effective is not None:
        changes.extend(compare_parameters(old_effective, new_effective, "effective"))
        # Retain public wrapper declarations when they differ from the resolved target.
        if (old["declared_parameters"] != old_effective
                or new["declared_parameters"] != new_effective):
            changes.extend(compare_parameters(old["declared_parameters"],
                                              new["declared_parameters"], "declared"))
    else:
        # A missing effective signature must never be replaced by a guess.
        changes.extend(compare_parameters(old["declared_parameters"],
                                          new["declared_parameters"], "declared"))
    if ((old["public_binding"]["kind"] == "generated_python_binding") !=
            (new["public_binding"]["kind"] == "generated_python_binding")):
        # ATen schema types and Python annotations are different source types.
        changes = [change for change in changes
                   if change["kind"] != "parameter_annotation_changed"]
    for field, kind in (("return_annotation", "return_annotation_changed"),
                        ("public_binding", "public_binding_changed")):
        if old[field] != new[field]:
            changes.append({"kind": kind, "before": old[field], "after": new[field]})
    old_path = old["source_location"]["path"]
    new_path = new["source_location"]["path"]
    if old_path != new_path:
        changes.append({"kind": "source_path_changed", "before": old_path,
                        "after": new_path})
    old_hash, new_hash = old_private["body_hash"], new_private["body_hash"]
    if old_hash and new_hash and old_hash != new_hash:
        message_only = error_message_changes(
            old_private.get("error_message_profile"),
            new_private.get("error_message_profile"))
        changes.append({"kind": "error_message_changed" if message_only
                        else "implementation_changed",
                        "before": {"version": old["version"], **old["source_location"]},
                        "after": {"version": new["version"], **new["source_location"]}})
    return changes


# 5. Attach a source location to each state and a focused Git diff to changes.
def add_source_evidence(evidences, state, private):
    identifier = f"E{len(evidences) + 1:05d}"
    source_type = ("binding_declaration" if private.get("binding_declaration")
                   else "source")
    evidences.append({"id": identifier, "type": source_type,
                      "version": state["version"], "commit_sha": state["commit_sha"],
                      **state["source_location"]})
    state["evidence_refs"].append(identifier)
    for evidence_type, location in private.get("binding_locations", []):
        identifier = f"E{len(evidences) + 1:05d}"
        evidences.append({"id": identifier, "type": evidence_type,
                          "version": state["version"],
                          "commit_sha": state["commit_sha"], **location})
        state["evidence_refs"].append(identifier)
    effective = private.get("effective_location")
    if effective and effective != state["source_location"]:
        identifier = f"E{len(evidences) + 1:05d}"
        evidences.append({"id": identifier, "type": "effective_source",
                          "version": state["version"],
                          "commit_sha": state["commit_sha"], **effective})
        state["evidence_refs"].append(identifier)


def relevant_git_diff(reader, old, new, old_private, new_private):
    message_spans = error_message_changes(
        old_private.get("error_message_profile"),
        new_private.get("error_message_profile"))

    def locations(state, private):
        return [(state["source_location"], private.get("docstring_spans", [])),
                (private.get("effective_location"),
                 private.get("effective_docstring_spans", [])),
                *((location, []) for evidence_type, location in
                  private.get("binding_locations", [])
                  if evidence_type == "tf_export_decorator")]

    def classify_line(state, private, path, line, focus):
        matches = [spans for location, spans in locations(state, private)
                   if location and path == location["path"]
                   and location["line_start"] is not None
                   and location["line_start"] <= line <=
                   (location["line_end"] or location["line_start"])]
        if not matches:
            return False, False, False
        code = any(not any(start <= line <= end for start, end in spans)
                   for spans in matches)
        focused = bool(code and message_spans
                       and path == state["source_location"]["path"]
                       and any(start <= line <= end
                               for start, end in message_spans[focus]))
        return True, code, focused

    paths = sorted({location["path"] for state, private in (
        (old, old_private), (new, new_private))
        for location, _spans in locations(state, private)
        if location and location["path"]})
    if not paths:
        return None
    result = git(reader.repo, "diff", "--no-ext-diff", "--find-renames",
                 "--unified=0", old["commit_sha"], new["commit_sha"], "--", *paths)
    if not result.stdout:
        return None
    headers = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    selected = []
    old_path = new_path = None
    current = None

    def finish():
        if current and current["relevant"] and current["code"]:
            excerpt = current["text"]
            if len(excerpt) > 3000:
                excerpt = (excerpt[:1450] + "\n... [diff excerpt truncated] ...\n"
                           + excerpt[-1450:])
            selected.append((current["focused"], excerpt))

    for line in result.stdout.splitlines():
        if line.startswith("diff --git "):
            finish()
            current = None
        elif line.startswith("--- "):
            old_path = line[6:] if line.startswith("--- a/") else None
        elif line.startswith("+++ "):
            new_path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@ "):
            finish()
            match = headers.match(line)
            current = {"relevant": False, "code": False, "focused": False,
                       "text": line,
                       "old_line": int(match[1]) if match else None,
                       "new_line": int(match[3]) if match else None}
        elif current:
            current["text"] += "\n" + line
            if line.startswith("-") and current["old_line"] is not None:
                relevant, code, focused = classify_line(
                    old, old_private, old_path, current["old_line"], 0)
                current["relevant"] |= relevant
                current["code"] |= code
                current["focused"] |= focused
                current["old_line"] += 1
            elif line.startswith("+") and current["new_line"] is not None:
                relevant, code, focused = classify_line(
                    new, new_private, new_path, current["new_line"], 1)
                current["relevant"] |= relevant
                current["code"] |= code
                current["focused"] |= focused
                current["new_line"] += 1
            elif line.startswith(" "):
                if current["old_line"] is not None:
                    current["old_line"] += 1
                    current["new_line"] += 1
    finish()
    if message_spans and any(focused for focused, _ in selected):
        selected = [item for item in selected if item[0]]
    return {"type": "git_diff", "from_version": old["version"],
            "to_version": new["version"], "from_commit_sha": old["commit_sha"],
            "to_commit_sha": new["commit_sha"], "paths": paths,
            "diff_excerpt": "\n".join(excerpt for _, excerpt in selected[:8])} if selected else None


# 6. Assemble two independent histories and coverage for every mapping pair.
def build_history(reader, api, versions, validation):
    states = []
    private_states = []
    presence = []
    absent = []
    unresolved = []
    evidences = []
    for index, record in enumerate(versions, 1):
        status, result, reason = read_api_state(
            reader, record, api, validation[record["version"]])
        presence.append(status)
        if status == "present":
            state, private = result
            add_source_evidence(evidences, state, private)
            states.append(state)
            private_states.append(private)
        elif status == "absent":
            absent.append(record["version"])
        else:
            unresolved.append({"version": record["version"],
                               "reason": reason or "source_resolution_unresolved"})
        if index == 1 or index % 25 == 0 or index == len(versions):
            print(f"  {api}: {index}/{len(versions)} versions", file=sys.stderr,
                  flush=True)

    by_version = {state["version"]: (state, private)
                  for state, private in zip(states, private_states)}
    transitions = []
    unresolved_intervals = []
    skipped_intervals = 0
    for index, (old_record, new_record) in enumerate(zip(versions, versions[1:])):
        old_status, new_status = presence[index:index + 2]
        if "unknown" in (old_status, new_status):
            unresolved_intervals.append({"from_version": old_record["version"],
                                         "to_version": new_record["version"],
                                         "reason": "api_presence_unresolved"})
            continue
        if "absent" in (old_status, new_status):
            skipped_intervals += 1
            continue
        old, old_private = by_version[old_record["version"]]
        new, new_private = by_version[new_record["version"]]
        changes = compare_states(old, new, old_private, new_private)
        refs = [*old["evidence_refs"], *new["evidence_refs"]]
        status = "compared"
        if changes:
            try:
                diff = relevant_git_diff(reader, old, new, old_private, new_private)
                if diff:
                    diff["id"] = f"E{len(evidences) + 1:05d}"
                    evidences.append(diff)
                    refs.append(diff["id"])
            except (OSError, RuntimeError) as exc:
                status = "partial"
                unresolved_intervals.append({"from_version": old["version"],
                                             "to_version": new["version"],
                                             "reason": f"git_diff_failed: {exc}"})
        elif old_private["body_hash"] and old_private["body_hash"] == new_private["body_hash"]:
            identifier = f"E{len(evidences) + 1:05d}"
            evidences.append({"id": identifier, "type": "ast_comparison",
                              "from_version": old["version"],
                              "to_version": new["version"],
                              "from_commit_sha": old["commit_sha"],
                              "to_commit_sha": new["commit_sha"],
                              "hash_scope": "function_body_ast_excluding_docstring",
                              "before_hash": old_private["body_hash"],
                              "after_hash": new_private["body_hash"]})
            refs.append(identifier)
        for change in changes:
            change["evidence_refs"] = refs.copy()
        transitions.append({"from_version": old["version"],
                            "to_version": new["version"], "status": status,
                            "changes": changes, "evidence_refs": refs})

    coverage = {"versions_considered": len(versions),
                "versions_present": len(states), "versions_absent": len(absent),
                "versions_unresolved": len(unresolved),
                "absent_versions": absent, "unresolved_versions": unresolved,
                "intervals_considered": max(len(versions) - 1, 0),
                "intervals_compared": len(transitions) - len([
                    item for item in transitions if item["status"] != "compared"]),
                "intervals_skipped_absent": skipped_intervals,
                "unresolved_intervals": unresolved_intervals}
    return {"versions": states, "transitions": transitions,
            "coverage": coverage, "evidence": evidences}


def write_results(path, pairs, histories):
    result = []
    for pair in pairs:
        api_states = []
        for role in ("deprecated_api", "replacement_api"):
            api = pair[role]
            api_states.append({"role": role, "api": api, **histories[api]})
        result.append({"mapping_id": pair["id"], "library": pair["library"],
                       "api_states": api_states})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, choices=sorted(REPOSITORIES))
    parser.add_argument("--mapping-id", type=int, help="Only this mapping pair")
    parser.add_argument("--from-version", help="First curated version (for inspection)")
    parser.add_argument("--to-version", help="Last curated version (for inspection)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        pairs, versions = load_inputs(args.library, args.mapping_id)
        versions = select_range(versions, args.from_version, args.to_version)
        repo = ROOT / "cache" / args.library / "source"
        if not repo.is_dir():
            raise RuntimeError(f"Source repository missing: {repo}; run crawl_source.py first")
        print(f"{args.library}: validating {len(versions)} releases", file=sys.stderr)
        validation = validate_releases(repo, args.library, versions)
        reader = SourceReader(repo, args.library)
        apis = sorted({pair[role] for pair in pairs
                       for role in ("deprecated_api", "replacement_api")})
        histories = {}
        for api in apis:
            print(f"{args.library}: tracing {api}", file=sys.stderr, flush=True)
            histories[api] = build_history(reader, api, versions, validation)
        output = args.output or ROOT / "data" / args.library / "api_source_tracks.json"
        result = write_results(output, pairs, histories)
        print(f"Wrote {len(result)} mapping pairs to {output}", file=sys.stderr)
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
