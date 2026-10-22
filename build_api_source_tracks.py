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
    SourceReader, body_hash, dotted, expr, forwarding_wrapper_target, git,
    module_statements, signature,
)
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
    parts = api.split(".")
    parent_api = ".".join(parts[:-1])
    if len(parts) > 2:
        parent = reader.resolve_api(sha, parent_api)
        if isinstance(parent.node, ast.ClassDef):
            return {"kind": "class_member", "path": resolved.path,
                    "target": f"{resolved.module}.{parent.node.name}.{parts[-1]}"}

    module = parent_api if reader.module_file(sha, parent_api) else parts[0]
    name = parts[-1] if module == parent_api else parts[1]
    path = reader.module_file(sha, module)
    if not path:
        return {"kind": "resolved_export", "path": resolved.path,
                "target": f"{resolved.module}.{getattr(resolved.node, 'name', name)}"}
    tree = reader.parse(sha, path)
    if tree is None:
        return {"kind": "resolved_export", "path": resolved.path,
                "target": f"{resolved.module}.{getattr(resolved.node, 'name', name)}"}

    binding = None
    wildcard = False
    for node, _conditional in module_statements(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == name:
            binding = {"kind": "definition", "path": path,
                       "target": f"{module}.{name}"}
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name
                   for target in targets):
                binding = {"kind": "alias", "path": path,
                           "target": dotted(node.value) or f"{module}.{name}"}
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    wildcard = True
                elif (alias.asname or alias.name) == name:
                    imported = reader.relative_module(sha, module, path, node)
                    binding = {"kind": "import", "path": path,
                               "target": f"{imported}.{alias.name}" if imported else alias.name}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    binding = {"kind": "import", "path": path,
                               "target": alias.name}
    if binding:
        return binding
    target = f"{resolved.module}.{getattr(resolved.node, 'name', name)}"
    return {"kind": "wildcard_import" if wildcard else "submodule",
            "path": path, "target": target}


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
                    # The binding is generated; the repository has no Python
                    # function declaration whose parameters can be read here.
                    "declared_parameters": None,
                    "effective_parameters": None,
                    "return_annotation": None,
                    "evidence_refs": [],
                }
                private = {"body_hash": None, "binding_locations": [
                    ("binding_generator", binding["generator"]),
                    ("python_export", binding["export"]),
                ]}
                return "present", (state, private), None
            return resolved.status, None, resolved.reason
        node = resolved.node
        declared = signature(node)
        effective, effective_resolution = resolve_effective_parameters(
            reader, sha, resolved)
        state = {
            "version": record["version"],
            "commit_sha": sha,
            "source_location": {"path": resolved.path,
                                "line_start": getattr(node, "lineno", None),
                                "line_end": getattr(node, "end_lineno", None)},
            "public_binding": find_binding(reader, sha, api, resolved),
            "declared_parameters": declared,
            "effective_parameters": effective,
            "return_annotation": expr(node.returns)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None,
            "evidence_refs": [],
        }
        private = {"body_hash": body_hash(node)}
        if effective_resolution and effective_resolution.path:
            target_node = effective_resolution.node
            private["effective_location"] = {
                "path": effective_resolution.path,
                "line_start": getattr(target_node, "lineno", None),
                "line_end": getattr(target_node, "end_lineno", None),
            }
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


def compare_states(old, new, old_private, new_private):
    changes = []
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
        changes.append({"kind": "implementation_changed",
                        "before": {"version": old["version"], **old["source_location"]},
                        "after": {"version": new["version"], **new["source_location"]}})
    return changes


# 5. Attach a source location to each state and a focused Git diff to changes.
def add_source_evidence(evidences, state, private):
    identifier = f"E{len(evidences) + 1:05d}"
    source_type = ("binding_declaration" if private.get("binding_locations")
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
    def locations(state, private):
        return [state["source_location"], private.get("effective_location")]

    paths = sorted({location["path"] for state, private in (
        (old, old_private), (new, new_private))
        for location in locations(state, private)
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
        if current and current["relevant"]:
            selected.append(current["text"][:3000])

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
            relevant = False
            if match:
                for state, private, path, start, count in (
                    (old, old_private, old_path, int(match[1]), int(match[2] or 1)),
                    (new, new_private, new_path, int(match[3]), int(match[4] or 1)),
                ):
                    for location in locations(state, private):
                        if not location:
                            continue
                        low, high = location["line_start"], location["line_end"]
                        if path == location["path"] and low is not None:
                            relevant |= start <= (high or low) and start + max(count, 1) >= low
            current = {"relevant": relevant, "text": line}
        elif current:
            current["text"] += "\n" + line
    finish()
    return {"type": "git_diff", "from_version": old["version"],
            "to_version": new["version"], "from_commit_sha": old["commit_sha"],
            "to_commit_sha": new["commit_sha"], "paths": paths,
            "diff_excerpt": "\n".join(selected[:4])} if selected else None


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
        refs = [*old["evidence_refs"], *new["evidence_refs"]] if changes else []
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
