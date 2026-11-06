"""Dựng lịch sử source của API công khai, chỉ dùng chữ ký khai báo.

Chạy: python build_api_source_tracks.py --library seaborn
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
from export_rules.scipy_export_rules import (
    lookup_scipy_generated_export, lookup_scipy_legacy_python_export,
    missing_scipy_public_api,
)
from export_rules.source_signature_rules import (
    pandas_loc_syntax, scipy_assignment_signature,
    tensorflow_deprecated_signature,
)
from export_rules.tensorflow_export_rules import (
    export_index, export_query, export_source_paths, legacy_public_root_absent,
    lookup_tensorflow_export,
    lookup_tensorflow_generated_div, lookup_tensorflow_generated_substr,
    prepare_candidate_paths,
)
from export_rules.torch_binding_rules import (
    lookup_torch_linalg_signature, lookup_torch_python_binding,
    missing_functional_lu_alias,
)
from export_rules.transformers_export_rules import lookup_transformers_export
from source_evolution_rules import (
    Resolution, SourceReader, body_hash, docstring_spans, dotted,
    error_message_changes, error_message_profile, expr, git, signature,
    source_structure,
)

ROOT = Path(__file__).resolve().parent
PAIRS_FILE = ROOT / "data" / "api_mapping_pairs.json"
VERSIONS_FILE = ROOT / "data" / "pypi_versions_with_github_diy.json"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z", re.I)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


# 1. Đọc release và chuẩn bị dữ liệu để xuất JSON.
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


def signature_location(path, node):
    first_body_line = node.body[0].lineno if node.body else node.lineno
    return {"path": path, "line_start": node.lineno,
            "line_end": max(node.lineno, first_body_line - 1)}


# 2. Tái dùng kết quả phân giải khi đường dẫn và file đã đọc không đổi.
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

    def source_text(self, sha, path):
        if self.read_paths is not None:
            self.read_paths.add(path)
        return super().source_text(sha, path)

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
    """Reuse a confirmed API state when its source dependencies are unchanged."""

    def __init__(self, reader):
        self.reader = reader
        self.states = {}

    def _same_blob(self, old_sha, sha, path):
        blob = self.reader.blob(old_sha, path)
        return blob is not None and blob == self.reader.blob(sha, path)

    @staticmethod
    def context(record, library):
        # Cùng source nhưng TensorFlow 1.x/2.x có tên export khác nhau.
        return int(record["version"].split(".", 1)[0]) < 2 if library == "tensorflow" else None

    def reuse(self, record, api):
        previous = self.states.get(api)
        if previous is None or previous["context"] != self.context(record, self.reader.library):
            return None
        sha = record["commit_sha"]
        if previous["path_fingerprint"] != self.reader.path_fingerprint(sha):
            return None
        paths = previous["read_paths"]
        if not self._same_blob(previous["sha"], sha, paths[0]):
            return None
        if previous["export_paths"] is not None:
            export_paths, complete = export_source_paths(self.reader.repo, sha)
            if not complete or export_paths != previous["export_paths"]:
                return None
        if not all(self._same_blob(previous["sha"], sha, path)
                   for path in paths[1:]):
            return None
        state, private = copy.deepcopy(previous["value"])
        state["version"], state["commit_sha"] = record["version"], sha
        return state, private

    def remember(self, record, api, value, paths):
        state, private = value
        paths = set(paths)
        if state["source_location"].get("path"):
            paths.add(state["source_location"]["path"])
        binding = state.get("public_binding")
        if binding and binding.get("path"):
            paths.add(binding["path"])
        paths.update(location["path"] for _, location in
                     private.get("binding_locations", []) if location.get("path"))
        sha = record["commit_sha"]
        export_paths = None
        if binding and binding["kind"] == "tf_export":
            export_paths, complete = export_source_paths(self.reader.repo, sha)
            if not complete:
                return
            paths.update(export_paths)
        # File export thường đổi giữa các release; kiểm tra nó trước để bỏ cache sớm.
        priority = [binding["path"]] if binding and binding.get("path") else []
        priority.append(state["source_location"].get("path"))
        ordered_paths = tuple(dict.fromkeys(
            path for path in [*priority, *sorted(path for path in paths if path)]
            if path))
        if not ordered_paths:
            return
        self.states[api] = {
            "sha": sha, "path_fingerprint": self.reader.path_fingerprint(sha),
            "read_paths": ordered_paths,
            "context": self.context(record, self.reader.library),
            "export_paths": export_paths,
            "value": copy.deepcopy(value),
        }


# 3. Sau cache, tìm export Python, lazy export và binding sinh mã.
def find_public_api(reader, record, api):
    sha = record["commit_sha"]
    special = lookup_transformers_export(reader, sha, api)
    if special:
        resolved, binding, evidence = special
        return resolved, binding, evidence

    resolved = reader.resolve_api(sha, api)
    if resolved.status == "present":
        binding, evidence = find_public_binding(reader, sha, api, resolved)
        return resolved, binding, evidence

    dynamic = lookup_dynamic_export(reader, sha, api)
    if dynamic:
        return dynamic

    if reader.library == "tensorflow":
        if legacy_public_root_absent(reader, sha, api):
            resolved.status, resolved.reason = "absent", "public_name_filtered_from_source"
            return resolved, None, []
        tf_binding, reason = lookup_tensorflow_export(
            reader, sha, api, record["version"])
        if (reason in {"tf_compat_namespace_not_generated",
                       "tf_api_golden_name_absent"}
                or (reason in {"tf_export_other_generation", "tf_export_name_absent"}
                    and resolved.reason == "generated_public_namespace_unresolved")):
            resolved.status, resolved.reason = "absent", reason
        elif reason and (resolved.status != "absent"
                         or reason != "tf_export_index_incomplete"):
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
    if reader.library == "tensorflow":
        generated = (lookup_tensorflow_generated_substr(
            reader, sha, api, record["version"])
            or lookup_tensorflow_generated_div(reader, sha, api))
        if generated:
            locations = [("public_export", generated["export"])]
            if generated.get("parameter_source"):
                locations.append(("parameter_source", generated["parameter_source"]))
            return ({
                "version": record["version"], "commit_sha": sha,
                "source_location": generated["declaration"],
                "public_binding": {
                    "kind": "generated_op", "path": generated["export"]["path"],
                    "target": generated["target"],
                },
                "declared_parameters": generated["parameters"],
                "return_annotation": None, "evidence_refs": [],
            }, {
                "body_hash": None, "binding_declaration": True,
                "binding_locations": locations,
            })
    generated = (lookup_scipy_generated_export(reader, sha, api)
                 or lookup_scipy_legacy_python_export(reader, sha, api))
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
            "return_annotation": None, "evidence_refs": [],
        }
        if generated.get("call_syntax") is not None:
            state["call_syntax"] = generated["call_syntax"]
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
        "return_annotation": None, "evidence_refs": [],
    }
    private = {"body_hash": None, "binding_declaration": True,
               "binding_locations": [
                   ("binding_generator", binding["generator"]),
                   ("python_export", binding["export"]),
                   *binding["parameter_evidence"],
               ]}
    return state, private


def pandas_loc_public_binding(resolved):
    """Identify the declaration that supplies the public DataFrame.loc accessor."""
    node = resolved.node
    if resolved.reason == "generated_class_member" and isinstance(node, ast.For):
        installer = next((dotted(item.value.func) for item in node.body
                          if isinstance(item, ast.Expr)
                          and isinstance(item.value, ast.Call)
                          and dotted(item.value.func).endswith("._create_indexer")), None)
        owner = installer.rpartition(".")[0] if installer else None
        if owner:
            return {"kind": "generated_class_member", "path": resolved.path,
                    "target": f"{resolved.module}.{owner}.loc"}
    if isinstance(node, ast.FunctionDef) and any(
            dotted(decorator) == "property" for decorator in node.decorator_list):
        owner = next((item.name for item in resolved.tree.body
                      if isinstance(item, ast.ClassDef) and node in item.body), None)
        if owner:
            return {"kind": "property", "path": resolved.path,
                    "target": f"{resolved.module}.{owner}.loc"}
    raise RuntimeError("DataFrame.loc binding owner could not be confirmed")


# 4. Chỉ đọc chữ ký khai báo tại định nghĩa của API.
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


def trace_declared_signature(reader, sha, resolved, seen=None):
    """Chỉ lần theo alias khi binding công khai chưa có chữ ký def."""
    if resolved.status != "present" or resolved.node is None:
        return None, []
    seen = seen or set()
    key = (resolved.path, getattr(resolved.node, "lineno", None))
    if key in seen or len(seen) >= 24:
        return None, []
    seen = seen | {key}

    parameters = declared_signature(reader, sha, resolved)
    if parameters is not None:
        origin = resolved
        if isinstance(resolved.node, ast.ClassDef):
            origin = reader.resolve_class_member(sha, resolved, "__init__")
        return parameters, [("signature_source", signature_location(
            origin.path, origin.node))]

    node = resolved.node
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        return None, []
    value = node.value
    native = lookup_torch_linalg_signature(reader, sha, resolved)
    if native:
        return native
    target = dotted(value)
    if target:
        # Ưu tiên thành viên cùng class, ví dụ DataFrame.items = iteritems.
        owner = next((item for item in ast.walk(resolved.tree)
                      if isinstance(item, ast.ClassDef) and node in item.body), None) \
            if resolved.tree is not None else None
        if owner and "." not in target:
            parent = Resolution("present", path=resolved.path, node=owner,
                                tree=resolved.tree, module=resolved.module)
            member = reader.resolve_class_member(sha, parent, target)
            if member.status == "present":
                return trace_declared_signature(reader, sha, member, seen)
        if "." not in target:
            found = reader.resolve_name(sha, resolved.module, target)
        elif target.startswith(reader.root_module + "."):
            found = reader.resolve_api(sha, target)
        else:
            prefix, _, name = target.rpartition(".")
            namespace = reader.resolve_name(sha, resolved.module, prefix)
            if namespace.status != "present":
                return None, []
            if isinstance(namespace.node, ast.ClassDef):
                found = reader.resolve_class_member(sha, namespace, name)
            elif namespace.module:
                found = reader.resolve_name(sha, namespace.module, name)
            else:
                return None, []
        return trace_declared_signature(reader, sha, found, seen)

    # Chỉ lấy chữ ký boolean_dispatch khi hai nhánh khai báo cùng tham số.
    if isinstance(value, ast.Call) and dotted(value.func) == "boolean_dispatch":
        branches = {}
        for keyword in value.keywords:
            if keyword.arg in {"if_true", "if_false"}:
                name = dotted(keyword.value)
                if name and "." not in name:
                    found = reader.resolve_name(sha, resolved.module, name)
                    branches[keyword.arg] = trace_declared_signature(
                        reader, sha, found, seen)
        if len(branches) == 2 and branches["if_true"][0] is not None \
                and branches["if_true"][0] == branches["if_false"][0]:
            return branches["if_true"][0], (
                branches["if_true"][1] + branches["if_false"][1])
    return None, []


def read_state_uncached(reader, record, api):
    sha = record["commit_sha"]
    resolved, binding, binding_evidence = find_public_api(reader, record, api)
    if resolved.status != "present":
        generated = generated_state(reader, record, api)
        if generated:
            return "present", generated, None
        if missing_scipy_public_api(reader, sha, api):
            return "absent", None, "public_export_not_in_source"
        if missing_functional_lu_alias(reader, sha, api):
            return "absent", None, "functional_alias_not_exported"
        return resolved.status, None, resolved.reason

    node = resolved.node
    parameters, signature_evidence = trace_declared_signature(reader, sha, resolved)
    call_syntax, syntax_evidence = pandas_loc_syntax(reader, sha, api, resolved)
    if call_syntax is not None:
        # loc is an indexer property; its getter's def is not the public call.
        parameters, signature_evidence = None, []
        binding = pandas_loc_public_binding(resolved)
    extra_evidence, external_evidence = [], []
    if parameters is None:
        if reader.library == "scipy":
            parameters, extra_evidence, external_evidence = scipy_assignment_signature(
                reader, sha, api, resolved)
        elif reader.library == "tensorflow":
            parameters, extra_evidence = tensorflow_deprecated_signature(
                reader, sha, api, resolved)
    state = {
        "version": record["version"], "commit_sha": sha,
        "source_location": location(resolved.path, node),
        "public_binding": binding,
        "declared_parameters": parameters,
        "return_annotation": expr(node.returns) if isinstance(node, FUNCTIONS) else None,
        "evidence_refs": [],
    }
    if call_syntax is not None:
        state["call_syntax"] = call_syntax
    private = {
        "body_hash": body_hash(node),
        "source_structure": source_structure(node) if node else None,
        "error_message_profile": error_message_profile(node),
        "docstring_spans": docstring_spans(node),
        "binding_locations": binding_evidence + syntax_evidence + (
            signature_evidence if not isinstance(node, FUNCTIONS + (ast.ClassDef,)) else []
        ) + extra_evidence,
        "external_evidence": external_evidence,
    }
    return "present", (state, private), None


def read_state(reader, cache, record, api, validation_reason):
    if validation_reason:
        return "unknown", None, validation_reason
    try:
        reused = cache.reuse(record, api)
        if reused is not None:
            return "present", reused, None
        result, paths = reader.capture(lambda: read_state_uncached(reader, record, api))
        if result[0] == "present":
            cache.remember(record, api, result[1], paths)
        return result
    except (OSError, RuntimeError) as exc:
        return "unknown", None, f"source_read_failed: {exc}"


# 5. So sánh source và chữ ký khai báo giữa hai release liền kề.
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
    if (old.get("call_syntax", {}).get("kind") == "indexer"
            and new.get("call_syntax", {}).get("kind") == "indexer"
            and old["public_binding"]["kind"] == "generated_class_member"
            and new["public_binding"]["kind"] == "property"):
        # These nodes are an installer loop and a property getter, not two API definitions.
        changes = [change for change in changes
                   if change["kind"] != "definition_kind_changed"]
    # Chữ ký của wrapper là chữ ký khai báo; không lần theo hàm đích.
    changes.extend(compare_parameters(old["declared_parameters"],
                                      new["declared_parameters"], "declared"))
    if old.get("call_syntax") != new.get("call_syntax"):
        changes.append({"kind": "call_syntax_changed",
                        "before": old.get("call_syntax"),
                        "after": new.get("call_syntax")})
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


def output_state(api, state):
    result = {key: copy.deepcopy(value) for key, value in state.items()
              if key != "declared_parameters"}
    result["signature"] = format_signature(api, state["declared_parameters"])
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
    for item in private.get("external_evidence", []):
        identifier = f"E{len(evidences) + 1:05d}"
        evidences.append({"id": identifier, **item})
        state["evidence_refs"].append(identifier)
def relevant_git_diff(reader, old, new, old_private, new_private):
    message_spans = error_message_changes(
        old_private.get("error_message_profile"),
        new_private.get("error_message_profile"))

    def locations(state, private):
        return [(state["source_location"], private.get("docstring_spans", [])),
                *((location, []) for evidence_type, location in
                  private.get("binding_locations", [])
                  if evidence_type in {"tf_export_decorator", "signature_source",
                                       "function_copy_source"})]

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
    transitions, unresolved_intervals = [], []
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
        changes = compare_base_states(old, new, old_private, new_private)
        refs = [*old["evidence_refs"], *new["evidence_refs"]]
        status = "compared"
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
        "intervals_considered": max(len(versions) - 1, 0),
        "intervals_compared": sum(t["status"] == "compared" for t in transitions),
        "intervals_skipped_absent": skipped_absent,
        "unresolved_intervals": unresolved_intervals,
    }
    return {"versions": [output_state(api, state) for state in states],
            "transitions": transitions, "coverage": coverage,
            "evidence": evidence}


# 7. Chạy một thư viện và ghi JSON.
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
                repo, (export_query(api, versions[0]["version"])[0]
                       for api in apis))
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

        output = args.output or ROOT / "data" / args.library / "api_source_tracks.json"
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
