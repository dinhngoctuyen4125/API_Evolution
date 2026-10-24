"""Combine two source histories and a deprecation version with GPT-6 Luna.

Dry run: python build_migration_evolution.py --library numpy --mapping-id 3 --dry-run
Live:    python build_migration_evolution.py --library numpy --mapping-id 3
"""

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PROMPT = ROOT / "prompts" / "migration_evolution.txt"
MODEL = "gpt-6-luna"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def version_key(version):
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError as exc:
        raise ValueError(f"Non-numeric version: {version}") from exc


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def load_inputs(library, mapping_id):
    pairs = [pair for pair in read_json(DATA / "api_mapping_pairs.json")
             if pair["library"] == library
             and (mapping_id is None or pair["id"] == mapping_id)]
    if not pairs:
        raise ValueError(f"No mapping pairs selected for {library}")
    announcements = {item["id"]: item for item in
                     read_json(DATA / "api_mapping_version.json")}
    track_path = DATA / library / "api_source_tracks.json"
    if not track_path.is_file():
        raise ValueError(f"Missing source tracks: {track_path}")
    tracks = {item["mapping_id"]: item for item in read_json(track_path)}
    ready, missing = [], []
    for pair in pairs:
        announcement = announcements.get(pair["id"])
        track = tracks.get(pair["id"])
        if announcement is None or track is None:
            missing.append((pair["id"], "deprecated_version" if announcement is None
                            else "api_source_tracks"))
            continue
        for key in ("library", "deprecated_api", "replacement_api"):
            if pair[key] != announcement[key]:
                raise ValueError(f"Mapping {pair['id']}: {key} differs in deprecation input")
        if track["library"] != library or {state["api"] for state in track["api_states"]} != {
                pair["deprecated_api"], pair["replacement_api"]}:
            raise ValueError(f"Mapping {pair['id']}: source-track APIs differ from seed")
        ready.append((pair, announcement, track))
    if mapping_id is not None and missing:
        raise ValueError(f"Mapping {mapping_id} is missing {missing[0][1]}")
    return ready, missing


def param_signature(api, parameters):
    if parameters is None:
        return None
    parts = []
    positional_only = [i for i, p in enumerate(parameters)
                       if p["kind"] == "positional_only"]
    added_star = False
    for index, parameter in enumerate(parameters):
        kind = parameter["kind"]
        name = parameter["name"]
        if kind == "keyword_only" and not added_star:
            parts.append("*")
            added_star = True
        if kind == "var_positional":
            name = "*" + name
            added_star = True
        elif kind == "var_keyword":
            name = "**" + name
        if parameter.get("annotation") is not None:
            name += ": " + parameter["annotation"]
        if kind not in {"var_positional", "var_keyword"} and \
                parameter.get("default") is not None:
            name += "=" + parameter["default"]
        parts.append(name)
        if positional_only and index == positional_only[-1]:
            parts.append("/")
    return f"{api}({', '.join(parts)})"


def short_value(value):
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def normalize_change(api, transition, change, evidence):
    kind = change["kind"]
    types = {"parameter_renamed": "parameter_rename",
             "parameter_default_changed": "default_changed"}
    before, after = change.get("before"), change.get("after")
    parameter = change.get("parameter")
    if kind == "parameter_renamed":
        parameter = before
    elif kind in {"parameter_added", "parameter_removed"}:
        before = before.get("default") if isinstance(before, dict) else before
        after = after.get("default") if isinstance(after, dict) else after
    elif kind == "parameter_default_changed":
        pass
    elif kind == "parameter_annotation_changed":
        before = before.get("annotation") if isinstance(before, dict) else before
        after = after.get("annotation") if isinstance(after, dict) else after
    elif kind == "implementation_changed":
        before = after = None
    refs = []
    for ref in change.get("evidence_refs", []):
        item = evidence.get(ref)
        if item:
            refs.append({key: item[key] for key in
                         ("type", "version", "from_version", "to_version", "path",
                          "line_start", "line_end", "from_commit_sha", "to_commit_sha")
                         if key in item})
    return {"api": api, "type": types.get(kind, kind), "parameter": parameter,
            "old_value": short_value(before), "new_value": short_value(after),
            "from_version": transition["from_version"],
            "source": None, "delegates_to": None, "source_evidence": refs}


def state_statuses(state):
    statuses = {item["version"]: "present" for item in state["versions"]}
    statuses.update({version: "absent" for version in
                     state["coverage"].get("absent_versions", [])})
    statuses.update({item["version"]: "unknown" for item in
                     state["coverage"].get("unresolved_versions", [])})
    return statuses


def wrapper_event(old_api, replacement, from_state, to_state, replacement_state,
                  evidence):
    if not from_state or not to_state or not replacement_state:
        return None
    declared = to_state.get("declared_parameters")
    if (not declared or len(declared) != 2 or
            [p["kind"] for p in declared] != ["var_positional", "var_keyword"] or
            from_state.get("declared_parameters") == declared or
            to_state.get("effective_parameters") is None or
            to_state["effective_parameters"] != replacement_state.get("effective_parameters")):
        return None
    target = replacement_state["source_location"]
    forwarded = any(
        evidence.get(ref, {}).get("type") == "effective_source"
        and evidence[ref].get("path") == target["path"]
        and evidence[ref].get("line_start") == target["line_start"]
        for ref in to_state.get("evidence_refs", [])
    )
    if not forwarded:
        return None
    return {"api": old_api, "type": "wrapper_changed", "parameter": None,
            "old_value": param_signature(old_api, from_state["declared_parameters"]),
            "new_value": param_signature(old_api, declared),
            "from_version": from_state["version"], "source": None,
            "delegates_to": replacement, "source_evidence": [target]}


def build_facts(pair, announcement, track):
    histories = {item["api"]: item for item in track["api_states"]}
    version_states = {api: {item["version"]: item for item in history["versions"]}
                      for api, history in histories.items()}
    statuses = {api: state_statuses(history) for api, history in histories.items()}
    all_versions = sorted(set().union(*(set(items) for items in statuses.values())),
                          key=version_key)
    present_versions = [version for version in all_versions if any(
        statuses[api].get(version) == "present" for api in histories)]
    baseline = present_versions[0] if present_versions else announcement["deprecated_version"]
    pending = [{"version": baseline, "category": "baseline", "api": None,
                "mandatory": True, "change": None}]
    old_api, new_api = pair["deprecated_api"], pair["replacement_api"]

    for api, history in histories.items():
        evidence = {item["id"]: item for item in history.get("evidence", [])}
        states = version_states[api]
        for transition in history["transitions"]:
            if transition["status"] != "compared" or not transition["changes"]:
                continue
            version = transition["to_version"]
            wrapper = (wrapper_event(api, new_api, states.get(transition["from_version"]),
                                     states.get(version), version_states[new_api].get(version),
                                     evidence) if api == old_api else None)
            if wrapper:
                pending.append({"version": version, "category": "wrapper",
                                "api": api, "mandatory": True, "change": wrapper})
            for change in transition["changes"]:
                if wrapper and change.get("scope") == "declared" and change["kind"].startswith(
                        "parameter_"):
                    continue
                normalized = normalize_change(api, transition, change, evidence)
                mandatory = change["kind"].startswith("parameter_") or change["kind"] in {
                    "return_annotation_changed", "generated_ufunc_signatures_changed"}
                pending.append({"version": version, "category": "source_change",
                                "api": api, "mandatory": mandatory,
                                "change": normalized})

    for api in histories:
        previous = None
        for version in sorted(statuses[api], key=version_key):
            current = statuses[api][version]
            if previous and previous[1] == "present" and current == "absent":
                change = {"api": api, "type": "removed", "parameter": None,
                          "old_value": "present", "new_value": "absent",
                          "from_version": previous[0],
                          "source": f"data/{pair['library']}/api_source_tracks.json",
                          "delegates_to": new_api if api == old_api else None,
                          "source_evidence": []}
                pending.append({"version": version, "category": "removal",
                                "api": api, "mandatory": True, "change": change})
            elif previous and previous[1] == "absent" and current == "present":
                change = {"api": api, "type": "introduced", "parameter": None,
                          "old_value": "absent", "new_value": "present",
                          "from_version": previous[0],
                          "source": f"data/{pair['library']}/api_source_tracks.json",
                          "delegates_to": None, "source_evidence": []}
                pending.append({"version": version, "category": "introduction",
                                "api": api, "mandatory": True, "change": change})
            previous = (version, current)

    pending.append({"version": announcement["deprecated_version"],
                    "category": "deprecation", "api": old_api, "mandatory": True,
                    "change": {"api": old_api, "type": "deprecated", "parameter": None,
                               "old_value": None, "new_value": new_api,
                               "from_version": None,
                               "source": f"data/api_mapping_version.json#id={pair['id']}",
                               "delegates_to": None, "source_evidence": []}})
    pending.sort(key=lambda item: (version_key(item["version"]),
                                   item["category"], item["api"] or ""))
    events = []
    for index, item in enumerate(pending, 1):
        events.append({"id": f"EV{index:04d}", **item})

    histories_summary = []
    for api, history in histories.items():
        states = version_states[api]
        signature_runs = []
        for version in sorted(states, key=version_key):
            state = states[version]
            current = (param_signature(api, state.get("declared_parameters")),
                       param_signature(api, state.get("effective_parameters")))
            if signature_runs and (signature_runs[-1]["signature"],
                                   signature_runs[-1]["effective_signature"]) == current:
                signature_runs[-1]["to_version"] = version
            else:
                signature_runs.append({"from_version": version, "to_version": version,
                                       "signature": current[0],
                                       "effective_signature": current[1]})
        histories_summary.append({"api": api,
                                  "present_versions": sorted(states, key=version_key),
                                  "absent_versions": history["coverage"].get(
                                      "absent_versions", []),
                                  "unresolved_versions": history["coverage"].get(
                                      "unresolved_versions", []),
                                  "signature_runs": signature_runs})
    facts = {"mapping_id": pair["id"], "library": pair["library"],
             "seed": {"deprecated_api": old_api, "replacement_api": new_api},
             "deprecated_version": announcement["deprecated_version"],
             "api_histories": histories_summary, "candidate_events": events}
    return facts, version_states, statuses


RESPONSE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"milestones": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"version": {"type": "string"},
                       "event_ids": {"type": "array", "items": {"type": "string"}}},
        "required": ["version", "event_ids"]}}},
    "required": ["milestones"],
}


def validate_selection(selection, events):
    by_id = {event["id"]: event for event in events}
    found, versions = set(), []
    if not isinstance(selection, dict) or not isinstance(selection.get("milestones"), list):
        raise ValueError("LLM response has no milestones array")
    for milestone in selection["milestones"]:
        if not isinstance(milestone, dict):
            raise ValueError("LLM milestone must be an object")
        version = milestone.get("version")
        ids = milestone.get("event_ids")
        if not isinstance(version, str) or not isinstance(ids, list) or not ids:
            raise ValueError("LLM milestone needs a version and event IDs")
        versions.append(version)
        for event_id in ids:
            if event_id not in by_id or event_id in found or by_id[event_id]["version"] != version:
                raise ValueError(f"Invalid, repeated, or misplaced event ID: {event_id}")
            found.add(event_id)
    if versions != sorted(set(versions), key=version_key):
        raise ValueError("LLM milestones are not unique and chronologically ordered")
    required = {event["id"] for event in events if event["mandatory"]}
    if required - found:
        raise ValueError(f"LLM omitted mandatory events: {sorted(required - found)}")
    return selection


def confirmed_removed_at(statuses, version):
    removed, previous = False, None
    for current_version in sorted(statuses, key=version_key):
        if version_key(current_version) > version_key(version):
            break
        current = statuses[current_version]
        if current in {"present", "unknown"}:
            removed = False
        elif previous == "present":
            removed = True
        previous = current
    return removed


def render_milestones(facts, selection, version_states, statuses):
    by_id = {item["id"]: item for item in facts["candidate_events"]}
    old_api = facts["seed"]["deprecated_api"]
    result, previous = [], None
    for selected in selection["milestones"]:
        version = selected["version"]
        api_states = []
        for api in (facts["seed"]["deprecated_api"],
                    facts["seed"]["replacement_api"]):
            state = version_states[api].get(version)
            status = statuses[api].get(version, "unknown")
            if status == "present":
                status = ("deprecated" if api == old_api and version_key(version) >=
                          version_key(facts["deprecated_version"]) else "active")
            elif status == "absent" and confirmed_removed_at(statuses[api], version):
                status = "removed"
            api_states.append({"name": api, "status": status,
                               "signature": param_signature(
                                   api, state.get("declared_parameters")) if state else None,
                               "effective_signature": param_signature(
                                   api, state.get("effective_parameters")) if state else None})
        changes = [by_id[event_id]["change"] for event_id in selected["event_ids"]
                   if by_id[event_id]["change"] is not None]
        result.append({"version": version, "compared_to_version": previous,
                       "api_states": api_states, "changes": changes})
        previous = version
    return result


def provider_model(model, provider):
    if provider == "openrouter" and "/" not in model:
        return f"openai/{model}"
    if provider == "openai" and model.startswith("openai/"):
        return model.removeprefix("openai/")
    return model


def call_model(client, prompt, facts, model, provider):
    messages = [{"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(
                    facts, ensure_ascii=False, separators=(",", ":"))}]
    if provider == "openrouter":
        response = client.chat.completions.create(
            model=model, messages=messages,
            response_format={"type": "json_schema", "json_schema": {
                "name": "migration_milestones", "strict": True,
                "schema": RESPONSE_SCHEMA}},
        )
        if not response.choices or response.choices[0].finish_reason != "stop":
            raise RuntimeError("Model response incomplete")
        content = response.choices[0].message.content
    else:
        response = client.responses.create(
            model=model, reasoning={"effort": "low"}, input=messages,
            text={"format": {"type": "json_schema", "name": "migration_milestones",
                             "strict": True, "schema": RESPONSE_SCHEMA}},
            store=False,
        )
        if response.status != "completed":
            raise RuntimeError(f"Model response incomplete: {response.status}")
        content = response.output_text
    if not content:
        raise RuntimeError("Model response has no JSON content")
    return json.loads(content)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True)
    parser.add_argument("--mapping-id", type=int)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--provider", choices=("openrouter", "openai"),
                        help="Defaults to OpenRouter when OPENROUTER_API_KEY is set")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print one model input without calling the API")
    parser.add_argument("--resume", action="store_true",
                        help="Reuse output entries with the same input digest")
    args = parser.parse_args(argv)
    try:
        load_dotenv(ROOT / ".env")
        ready, missing = load_inputs(args.library, args.mapping_id)
        for mapping_id, reason in missing:
            print(f"Skipping mapping {mapping_id}: missing {reason}", file=sys.stderr)
        if args.dry_run and len(ready) != 1:
            raise ValueError("--dry-run requires one selected mapping with complete inputs")
        if not ready:
            raise ValueError("No mappings have both inputs")
        prompt = PROMPT.read_text(encoding="utf-8")
        provider = args.provider or ("openrouter" if os.getenv("OPENROUTER_API_KEY")
                                     else "openai")
        model = provider_model(args.model, provider)
        if args.dry_run:
            facts, _, _ = build_facts(*ready[0])
            print(json.dumps({"provider": provider, "model": model,
                              "instructions": prompt,
                              "input": facts}, ensure_ascii=False, indent=2))
            return
        key_name = "OPENROUTER_API_KEY" if provider == "openrouter" else "OPENAI_API_KEY"
        api_key = os.getenv(key_name)
        if not api_key:
            raise ValueError(f"Set {key_name} before calling the model")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ValueError("Install requirements.txt to use the model API") from exc
        client = OpenAI(api_key=api_key, timeout=180, max_retries=2,
                        **({"base_url": OPENROUTER_BASE_URL}
                           if provider == "openrouter" else {}))
        default_name = (f"migration_evolution_mapping_{args.mapping_id}.json"
                        if args.mapping_id is not None else "migration_evolution.json")
        output = args.output or DATA / args.library / default_name
        existing = read_json(output) if args.resume and output.is_file() else []
        results = {item["mapping_id"]: item for item in existing}
        for index, (pair, announcement, track) in enumerate(ready, 1):
            facts, version_states, statuses = build_facts(pair, announcement, track)
            digest = hashlib.sha256(json.dumps(
                [provider, model, prompt, facts], ensure_ascii=False,
                sort_keys=True).encode("utf-8")).hexdigest()
            if args.resume and results.get(pair["id"], {}).get("input_digest") == digest:
                print(f"{index}/{len(ready)} mapping {pair['id']}: reused", file=sys.stderr)
                continue
            print(f"{index}/{len(ready)} mapping {pair['id']}: {len(facts['candidate_events'])} "
                  "events", file=sys.stderr, flush=True)
            selection = validate_selection(call_model(client, prompt, facts, model, provider),
                                           facts["candidate_events"])
            results[pair["id"]] = {
                "mapping_id": pair["id"], "library": pair["library"],
                "seed": facts["seed"], "deprecated_version": facts["deprecated_version"],
                "milestones": render_milestones(facts, selection, version_states, statuses),
                "model": model, "input_digest": digest,
            }
            write_json(output, [results[key] for key in sorted(results)])
        print(f"Wrote {len(results)} migration histories to {output}", file=sys.stderr)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
