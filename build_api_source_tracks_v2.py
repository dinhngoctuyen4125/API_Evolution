"""Dựng lịch sử source của API công khai theo từng release.

Chạy: python build_api_source_tracks_v2.py --library seaborn
"""

import argparse
import ast
import copy
import hashlib
import json
import re
import sys
import time
from pathlib import Path

from crawl_source import validate_source
from enrich_pypi_versions import REPOSITORIES
from export_rules.dynamic_export_rules import lookup_dynamic_export
from export_rules.python_export_rules import find_public_binding
from export_rules.scipy_export_rules import lookup_scipy_generated_export
from export_rules.tensorflow_export_rules import (
    export_index, lookup_tensorflow_export, prepare_candidate_paths,
)
from export_rules.torch_binding_rules import lookup_torch_python_binding
from export_rules.transformers_export_rules import lookup_transformers_export
from source_evolution_rules import (
    MAX_IMPORT_DEPTH, Resolution, SourceReader, body_hash, docstring_spans, dotted,
    error_message_changes, error_message_profile, expr,
    forwarding_wrapper_target, git, signature, source_structure,
)

ROOT = Path(__file__).resolve().parent
PAIRS_FILE = ROOT / "data" / "api_mapping_pairs.json"
VERSIONS_FILE = ROOT / "data" / "pypi_versions_with_github_diy.json"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z", re.I)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


# 1. Đọc release và tạo chữ ký để xuất JSON.
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


def format_signature(api, parameters):
    if parameters is None:
        return None
    pieces = []
    positional_only = [i for i, p in enumerate(parameters)
                       if p["kind"] == "positional_only"]
    has_star = False
    for index, item in enumerate(parameters):
        kind = item["kind"]
        name = item["name"]
        if kind == "keyword_only" and not has_star:
            pieces.append("*")
            has_star = True
        if kind == "var_positional":
            name = "*" + name
            has_star = True
        elif kind == "var_keyword":
            name = "**" + name
        if item.get("annotation") is not None:
            name += ": " + item["annotation"]
        if kind not in {"var_positional", "var_keyword"} \
                and item.get("default") is not None:
            name += "=" + item["default"]
        pieces.append(name)
        if positional_only and index == positional_only[-1]:
            pieces.append("/")
    return f"{api}({', '.join(pieces)})"


def location(path, node):
    return {"path": path, "line_start": getattr(node, "lineno", None),
            "line_end": getattr(node, "end_lineno", None)}


# 2. Tái dùng vị trí source và export đã xác minh.
class TrackedSourceReader(SourceReader):
    def __init__(self, repo, library):
        super().__init__(repo, library)
        self.read_paths = None
        self._path_fingerprints = {}
        self._blobs = {}

    def parse(self, sha, path):
        if self.read_paths is not None:
            self.read_paths.add(path)
        return super().parse(sha, path)

    def path_fingerprint(self, sha):
        if sha not in self._path_fingerprints:
            paths = "\n".join(sorted(self.paths(sha)))
            self._path_fingerprints[sha] = hashlib.sha256(
                paths.encode("utf-8")).hexdigest()
        return self._path_fingerprints[sha]

    def blob(self, sha, path):
        key = sha, path
        if key not in self._blobs:
            result = git(self.repo, "rev-parse", f"{sha}:{path}", check=False)
            self._blobs[key] = result.stdout.strip() if result.returncode == 0 else None
        return self._blobs[key]

    def capture(self, action):
        self.read_paths = set()
        try:
            value = action()
            return value, set(self.read_paths)
        finally:
            self.read_paths = None


class PublicResolutionCache:
    def __init__(self, reader):
        self.reader = reader
        self.resolutions = {}
        self.bindings = {}

    def _same_paths(self, old, sha):
        return old["path_fingerprint"] == self.reader.path_fingerprint(sha)

    def _same_blob(self, old_sha, sha, path):
        blob = self.reader.blob(old_sha, path)
        return blob is not None and blob == self.reader.blob(sha, path)

    def resolve(self, sha, api):
        previous = self.resolutions.get(api)
        if previous and self._same_paths(previous, sha) and all(
            self._same_blob(previous["sha"], sha, path)
            for path in previous["read_paths"]
        ):
            # Các file đã đọc và danh sách đường dẫn đều không đổi.
            return previous["value"]
        resolved, paths = self.reader.capture(
            lambda: self.reader.resolve_api(sha, api))
        if resolved.status == "present" and resolved.reason is None and paths:
            self.resolutions[api] = {
                "sha": sha, "path_fingerprint": self.reader.path_fingerprint(sha),
                "read_paths": paths, "value": resolved,
            }
        return resolved

    def binding(self, sha, api, resolved):
        previous = self.bindings.get(api)
        if previous and self._same_paths(previous, sha) and self._same_blob(
            previous["sha"], sha, previous["path"]
        ):
            return copy.deepcopy(previous["value"])
        value = find_public_binding(self.reader, sha, api, resolved)
        binding = value[0]
        if binding["kind"] in {"definition", "import", "alias"} and binding["path"]:
            self.bindings[api] = {
                "sha": sha, "path_fingerprint": self.reader.path_fingerprint(sha),
                "path": binding["path"], "value": copy.deepcopy(value),
            }
        return value


# 3. Tìm export Python, lazy export và binding sinh mã.
def find_public_api(reader, cache, record, api):
    sha = record["commit_sha"]
    special = lookup_transformers_export(reader, sha, api)
    if special:
        resolved, binding, evidence = special
        return resolved, binding, evidence

    resolved = cache.resolve(sha, api)
    if resolved.status == "present":
        binding, evidence = cache.binding(sha, api, resolved)
        return resolved, binding, evidence

    dynamic = lookup_dynamic_export(reader, sha, api)
    if dynamic:
        return dynamic

    if reader.library == "tensorflow":
        tf_binding, reason = lookup_tensorflow_export(
            reader, sha, api, record["version"])
        if reason in {"tf_export_other_generation", "tf_export_name_absent"} \
                and resolved.reason == "generated_public_namespace_unresolved":
            resolved.status, resolved.reason = "absent", reason
        elif reason:
            resolved.status, resolved.reason = "unknown", reason
        elif tf_binding:
            resolved = tf_binding["resolution"]
            return resolved, {
                "kind": "tf_export", "path": tf_binding["decorator"]["path"],
                "target": tf_binding["target"],
                "export_names": tf_binding["export_names"],
            }, [("tf_export_decorator", tf_binding["decorator"])]
    return resolved, None, []


def generated_state(reader, record, api):
    """Đọc API được sinh từ Cython hoặc Python binding."""
    sha = record["commit_sha"]
    generated = lookup_scipy_generated_export(reader, sha, api)
    if generated:
        state = {
            "version": record["version"], "commit_sha": sha,
            "source_location": generated["declaration"],
            "public_binding": {
                "kind": generated["kind"],
                "path": generated["export"]["path"],
                "target": generated["target"],
            },
            "declared_parameters": generated["parameters"],
            "effective_parameters": generated["parameters"],
            "return_annotation": None, "evidence_refs": [],
        }
        if generated["generated_signatures"] is not None:
            state["generated_signatures"] = generated["generated_signatures"]
        private = {"body_hash": None, "binding_declaration": True,
                   "binding_locations": [("public_import", generated["export"])]}
        if generated["generator"]:
            private["binding_locations"].append(
                ("binding_generator", generated["generator"]))
        return state, private

    binding = lookup_torch_python_binding(reader, sha, api)
    if not binding:
        return None
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
        "return_annotation": None, "evidence_refs": [],
    }
    private = {"body_hash": None, "binding_declaration": True,
               "binding_locations": [
                   ("binding_generator", binding["generator"]),
                   ("python_export", binding["export"]),
                   *binding["parameter_evidence"],
               ]}
    return state, private


# 4. Truy wrapper và alias đến chữ ký cuối.
def declared_signature(reader, sha, resolved):
    node = resolved.node
    if isinstance(node, FUNCTIONS):
        return signature(node)
    if not isinstance(node, ast.ClassDef):
        return None
    constructor = reader.resolve_class_member(sha, resolved, "__init__")
    if constructor.status != "present" or not isinstance(constructor.node, FUNCTIONS):
        return None
    parameters = signature(constructor.node)
    if parameters and parameters[0]["name"] in {"self", "cls"}:
        return parameters[1:]
    return None


def forwarding_call_target(node):
    """Nhận wrapper chuyển nguyên *args/**kwargs đến hàm đích."""
    if not isinstance(node, FUNCTIONS):
        return None
    args = node.args
    if args.posonlyargs or args.args or args.kwonlyargs \
            or not args.vararg or not args.kwarg:
        return None
    body = node.body[1:] if ast.get_docstring(node) else node.body
    if not body or not all(
        isinstance(item, ast.Expr) and isinstance(item.value, ast.Call)
        and dotted(item.value.func) in {"warn", "warnings.warn"}
        for item in body[:-1]
    ):
        return None
    last = body[-1]
    if not isinstance(last, ast.Return) or not isinstance(last.value, ast.Call):
        return None
    call = last.value
    target = dotted(call.func)
    if not target or target == node.name:
        return None
    if len(call.args) != 1 or not isinstance(call.args[0], ast.Starred) \
            or not isinstance(call.args[0].value, ast.Name) \
            or call.args[0].value.id != args.vararg.arg:
        return None
    if len(call.keywords) != 1 or call.keywords[0].arg is not None \
            or not isinstance(call.keywords[0].value, ast.Name) \
            or call.keywords[0].value.id != args.kwarg.arg:
        return None
    return target


def possible_forwarder(node):
    if not isinstance(node, FUNCTIONS):
        return False
    if not node.args.vararg and not node.args.kwarg:
        return False
    body = node.body[1:] if ast.get_docstring(node) else node.body
    return bool(body and isinstance(body[-1], ast.Return)
                and isinstance(body[-1].value, ast.Call)
                and all(isinstance(item, ast.Expr)
                        and isinstance(item.value, ast.Call)
                        and dotted(item.value.func) in {"warn", "warnings.warn"}
                        for item in body[:-1]))


def forwards_same_parameters(call, parameters):
    """Kiểm tra lời gọi chuyển nguyên tham số, kể cả **kwargs."""
    expected = {item["name"]: item["kind"] for item in parameters[1:]}
    positional = [item["name"] for item in parameters[1:]
                  if item["kind"] in {"positional_only", "positional_or_keyword"}]
    used = set()
    positional_index = 0
    for argument in call.args:
        if isinstance(argument, ast.Starred):
            name = argument.value.id if isinstance(argument.value, ast.Name) else None
            if expected.get(name) != "var_positional" or name in used:
                return False
        elif isinstance(argument, ast.Name):
            name = argument.id
            if positional_index >= len(positional) or name != positional[positional_index]:
                return False
            positional_index += 1
        else:
            return False
        used.add(name)
    for argument in call.keywords:
        name = argument.value.id if isinstance(argument.value, ast.Name) else None
        if argument.arg is None:
            if expected.get(name) != "var_keyword":
                return False
        elif argument.arg != name or expected.get(name) not in {
            "positional_or_keyword", "keyword_only"
        }:
            return False
        if name in used:
            return False
        used.add(name)
    return used == set(expected)


def forwarded_self_method(reader, sha, resolved):
    """Theo self.method khi lời gọi giữ nguyên hình dạng tham số."""
    node = resolved.node
    if not possible_forwarder(node) or resolved.tree is None:
        return None
    body = node.body[1:] if ast.get_docstring(node) else node.body
    call = body[-1].value
    if not isinstance(call.func, ast.Attribute) or not isinstance(
        call.func.value, ast.Name
    ) or call.func.value.id != "self":
        return None
    parameters = signature(node)
    if not parameters or parameters[0]["name"] != "self" or not forwards_same_parameters(
        call, parameters
    ):
        return None
    owner = next((item for item in ast.walk(resolved.tree)
                  if isinstance(item, ast.ClassDef) and node in item.body), None)
    if owner is None:
        return None
    parent = Resolution("present", path=resolved.path, node=owner,
                        tree=resolved.tree, module=resolved.module)
    target = reader.resolve_class_member(sha, parent, call.func.attr)
    if target.status != "present" or not isinstance(target.node, FUNCTIONS):
        return None
    target_parameters = signature(target.node)
    if target_parameters is None or len(parameters) != len(target_parameters):
        return None
    # Khác annotation thì vẫn theo; khác tên, kiểu hoặc default thì chưa đủ chắc.
    for source, destination in zip(parameters, target_parameters):
        if any(source[field] != destination[field]
               for field in ("name", "kind", "default")):
            return None
    return target


def next_target(reader, sha, resolved):
    method = forwarded_self_method(reader, sha, resolved)
    if method is not None:
        return method
    alias = (forwarding_wrapper_target(resolved.node)
             or forwarding_call_target(resolved.node)
             or resolved.alias_target)
    if not alias:
        return None
    if not resolved.module:
        return None
    if "." not in alias:
        return reader.resolve_name(sha, resolved.module, alias)
    if alias.startswith(reader.root_module + "."):
        return reader.resolve_api(sha, alias)
    prefix, _, name = alias.rpartition(".")
    namespace = reader.resolve_name(sha, resolved.module, prefix)
    if namespace.status == "present" and namespace.module:
        return reader.resolve_name(sha, namespace.module, name)
    return namespace


def final_signature(reader, sha, resolved):
    current = resolved
    seen = set()
    chain = []
    for _ in range(MAX_IMPORT_DEPTH):
        key = (current.module, current.path,
               getattr(current.node, "lineno", None),
               getattr(current.node, "name", None))
        if key in seen:
            return None, None, chain, "cyclic_forwarding"
        seen.add(key)
        target = next_target(reader, sha, current)
        if target is None:
            if current.alias_target or possible_forwarder(current.node):
                return None, None, chain, "forwarding_pattern_unresolved"
            parameters = declared_signature(reader, sha, current)
            if parameters is None and isinstance(current.node, ast.ClassDef):
                return None, None, chain, "class_constructor_unresolved"
            return parameters, current, chain, None
        if target.status != "present" or target.node is None:
            return None, None, chain, target.reason or "forwarding_target_unresolved"
        chain.append(location(target.path, target.node))
        current = target
    return None, None, chain, "forwarding_depth_exceeded"


def read_state(reader, cache, record, api, validation_reason):
    if validation_reason:
        return "unknown", None, validation_reason
    sha = record["commit_sha"]
    try:
        resolved, binding, binding_evidence = find_public_api(
            reader, cache, record, api)
        if resolved.status != "present":
            generated = generated_state(reader, record, api)
            if generated:
                state, private = generated
                return "present", (state, private), None
            return resolved.status, None, resolved.reason

        node = resolved.node
        declared = declared_signature(reader, sha, resolved)
        effective, terminal, chain, reason = final_signature(reader, sha, resolved)
        state = {
            "version": record["version"], "commit_sha": sha,
            "source_location": location(resolved.path, node),
            "public_binding": binding,
            "declared_parameters": declared,
            "effective_parameters": effective,
            "return_annotation": expr(node.returns) if isinstance(node, FUNCTIONS) else None,
            "evidence_refs": [],
        }
        private = {
            "body_hash": body_hash(node),
            "source_structure": source_structure(node) if node else None,
            "error_message_profile": error_message_profile(node),
            "docstring_spans": docstring_spans(node),
            "binding_locations": binding_evidence,
            "effective_unresolved_reason": reason,
            "effective_target": (
                f"{terminal.module}.{getattr(terminal.node, 'name', '')}"
                if terminal and terminal.module else None),
        }
        if terminal and terminal.path:
            private["effective_location"] = location(terminal.path, terminal.node)
            private["effective_docstring_spans"] = docstring_spans(terminal.node)
            private["effective_body_hash"] = body_hash(terminal.node)
            private["effective_error_profile"] = error_message_profile(terminal.node)
        if chain:
            private["binding_locations"].extend(
                ("forwarding_target", step) for step in chain[:-1])
        return "present", (state, private), None
    except (OSError, RuntimeError) as exc:
        return "unknown", None, f"source_read_failed: {exc}"


# 5. So sánh release liền kề, bỏ qua tham số chưa phân giải.
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
        # Overload trùng tên không đủ căn cứ để ghép từng method.
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


def compare_base_states(old, new, old_private, new_private):
    changes = compare_source_structure(old, new, old_private, new_private)
    if (old.get("generated_signatures") is not None
            and new.get("generated_signatures") is not None
            and old["generated_signatures"] != new["generated_signatures"]):
        changes.append({"kind": "generated_ufunc_signatures_changed",
                        "before": old["generated_signatures"],
                        "after": new["generated_signatures"]})
    old_effective = old["effective_parameters"]
    new_effective = new["effective_parameters"]
    if old_effective is not None and new_effective is not None:
        changes.extend(compare_parameters(old_effective, new_effective, "effective"))
        # Giữ chữ ký khai báo khi wrapper khác hàm đích.
        if (old["declared_parameters"] != old_effective
                or new["declared_parameters"] != new_effective):
            changes.extend(compare_parameters(old["declared_parameters"],
                                              new["declared_parameters"], "declared"))
    if ((old["public_binding"]["kind"] == "generated_python_binding") !=
            (new["public_binding"]["kind"] == "generated_python_binding")):
        # Kiểu ATen và annotation Python không cùng hệ kiểu.
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


def compare_states(old, new, old_private, new_private):
    changes = compare_base_states(old, new, old_private, new_private)

    if old_private.get("effective_target") != new_private.get("effective_target"):
        changes.append({"kind": "effective_target_changed",
                        "before": old_private.get("effective_target"),
                        "after": new_private.get("effective_target")})

    old_target = old_private.get("effective_location")
    new_target = new_private.get("effective_location")
    old_hash = old_private.get("effective_body_hash")
    new_hash = new_private.get("effective_body_hash")
    if (old_target and new_target and old_hash and new_hash
            and old_hash != new_hash
            and (old_target != old["source_location"]
                 or new_target != new["source_location"])):
        message_only = error_message_changes(
            old_private.get("effective_error_profile"),
            new_private.get("effective_error_profile"))
        changes.append({
            "kind": "error_message_changed" if message_only else "implementation_changed",
            "scope": "effective_target",
            "before": {"version": old["version"], **old_target},
            "after": {"version": new["version"], **new_target},
        })
    return changes


def output_state(api, state):
    result = {key: copy.deepcopy(value) for key, value in state.items()
              if key not in {"declared_parameters", "effective_parameters"}}
    result["signature"] = format_signature(api, state["declared_parameters"])
    result["effective_signature"] = format_signature(
        api, state["effective_parameters"])
    return result


# 6. Ghi bằng chứng, transition và coverage.
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


def build_history(reader, cache, api, versions, validation, observations=None,
                  trace_versions=False):
    states, privates, presence = [], [], []
    absent, unresolved, evidence = [], [], []
    for index, record in enumerate(versions):
        started = time.perf_counter()
        status, result, reason = (observations[index] if observations is not None
                                  else read_state(reader, cache, record, api,
                                                  validation[record["version"]]))
        presence.append(status)
        if status == "present":
            state, private = result
            add_source_evidence(evidence, state, private)
            states.append(state)
            privates.append(private)
        elif status == "absent":
            absent.append(record["version"])
        else:
            unresolved.append({"version": record["version"],
                               "reason": reason or "source_resolution_unresolved"})
        if trace_versions:
            print(f"  {api}: {record['version']} {status} "
                  f"({time.perf_counter() - started:.2f}s)", file=sys.stderr,
                  flush=True)

    by_version = {state["version"]: (state, private)
                  for state, private in zip(states, privates)}
    transitions, unresolved_intervals, signature_intervals = [], [], []
    skipped_absent = 0
    for index, (old_record, new_record) in enumerate(zip(versions, versions[1:])):
        old_version, new_version = old_record["version"], new_record["version"]
        old_status, new_status = presence[index:index + 2]
        if "unknown" in (old_status, new_status):
            unresolved_intervals.append({"from_version": old_version,
                                         "to_version": new_version,
                                         "reason": "api_presence_unresolved"})
            continue
        if "absent" in (old_status, new_status):
            skipped_absent += 1
            continue
        old, old_private = by_version[old_version]
        new, new_private = by_version[new_version]
        changes = compare_states(old, new, old_private, new_private)
        refs = [*old["evidence_refs"], *new["evidence_refs"]]
        partial = bool(old_private.get("effective_unresolved_reason")
                       or new_private.get("effective_unresolved_reason"))
        status = "partial" if partial else "compared"
        if partial:
            signature_intervals.append({
                "from_version": old_version, "to_version": new_version,
                "reason": "effective_signature_unresolved",
            })
        if changes:
            try:
                diff = relevant_git_diff(reader, old, new, old_private, new_private)
                if diff:
                    diff["id"] = f"E{len(evidence) + 1:05d}"
                    evidence.append(diff)
                    refs.append(diff["id"])
            except (OSError, RuntimeError) as exc:
                status = "partial"
                unresolved_intervals.append({"from_version": old_version,
                                             "to_version": new_version,
                                             "reason": f"git_diff_failed: {exc}"})
        elif old_private["body_hash"] and old_private["body_hash"] == new_private["body_hash"]:
            identifier = f"E{len(evidence) + 1:05d}"
            evidence.append({
                "id": identifier, "type": "ast_comparison",
                "from_version": old_version, "to_version": new_version,
                "from_commit_sha": old["commit_sha"],
                "to_commit_sha": new["commit_sha"],
                "hash_scope": "function_body_ast_excluding_docstring",
                "before_hash": old_private["body_hash"],
                "after_hash": new_private["body_hash"],
            })
            refs.append(identifier)
        for change in changes:
            change["evidence_refs"] = refs.copy()
        transitions.append({"from_version": old_version,
                            "to_version": new_version, "status": status,
                            "changes": changes, "evidence_refs": refs})

    coverage = {
        "versions_considered": len(versions), "versions_present": len(states),
        "versions_absent": len(absent), "versions_unresolved": len(unresolved),
        "absent_versions": absent, "unresolved_versions": unresolved,
        "signature_unresolved_versions": [
            {"version": state["version"],
             "reason": private["effective_unresolved_reason"]}
            for state, private in zip(states, privates)
            if private.get("effective_unresolved_reason")
        ],
        "intervals_considered": max(len(versions) - 1, 0),
        "intervals_compared": sum(t["status"] == "compared" for t in transitions),
        "intervals_skipped_absent": skipped_absent,
        "unresolved_intervals": unresolved_intervals,
        "signature_unresolved_intervals": signature_intervals,
    }
    return {"versions": [output_state(api, state) for state in states],
            "transitions": transitions, "coverage": coverage,
            "evidence": evidence}


# 7. Chạy một thư viện và ghi JSON v2.
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Trace public API source histories by release")
    parser.add_argument("--library", required=True, choices=sorted(REPOSITORIES))
    parser.add_argument("--mapping-id", type=int)
    parser.add_argument("--from-version")
    parser.add_argument("--to-version")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trace-versions", action="store_true")
    args = parser.parse_args(argv)
    if args.output is None and (args.mapping_id is not None
                               or args.from_version or args.to_version):
        parser.error("Use --output for a mapping or version subset")
    try:
        pairs, versions = load_inputs(args.library, args.mapping_id)
        versions = select_range(versions, args.from_version, args.to_version)
        repo = ROOT / "cache" / args.library / "source"
        if not repo.is_dir():
            raise RuntimeError(f"Source repository missing: {repo}")
        validation = validate_releases(repo, args.library, versions)
        reader = TrackedSourceReader(repo, args.library)
        cache = PublicResolutionCache(reader)
        apis = sorted({pair[role] for pair in pairs
                       for role in ("deprecated_api", "replacement_api")})
        histories = {}
        if args.library == "tensorflow":
            prepare_candidate_paths(
                repo, (api.removeprefix("tensorflow.") for api in apis))
            observations = {api: [] for api in apis}
            for index, record in enumerate(versions, 1):
                print(f"tensorflow: tracing {index}/{len(versions)} "
                      f"{record['version']}", file=sys.stderr, flush=True)
                for api in apis:
                    observations[api].append(read_state(
                        reader, cache, record, api, validation[record["version"]]))
                export_index.cache_clear()
            for api in apis:
                histories[api] = build_history(
                    reader, cache, api, versions, validation,
                    observations=observations[api],
                    trace_versions=args.trace_versions)
        else:
            for api in apis:
                print(f"{args.library}: tracing {api}", file=sys.stderr,
                      flush=True)
                histories[api] = build_history(
                    reader, cache, api, versions, validation,
                    trace_versions=args.trace_versions)

        output = args.output or ROOT / "data" / args.library / "api_source_tracks_v2.json"
        result = [{"mapping_id": pair["id"], "library": pair["library"],
                   "api_states": [
                       {"role": role, "api": pair[role], **histories[pair[role]]}
                       for role in ("deprecated_api", "replacement_api")
                   ]} for pair in pairs]
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.name + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        temporary.replace(output)
        print(f"Wrote {len(result)} mapping pairs to {output}", file=sys.stderr)
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
