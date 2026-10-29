"""Tổng hợp hai lịch sử API v2 thành đường tiến hóa của cặp migration.

Dry run: python build_migration_evolution_v2.py --library numpy --mapping-id 3 --dry-run
Live:    python build_migration_evolution_v2.py --library numpy
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib import error, request

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


# 1. Ghép mapping với hai lịch sử API v2.
def load_inputs(library, mapping_id):
    mappings = [item for item in read_json(DATA / "api_mapping_versions.json")
                if item["library"] == library
                and (mapping_id is None or item["id"] == mapping_id)]
    if not mappings:
        raise ValueError(f"No mapping pairs selected for {library}")
    if len({item["id"] for item in mappings}) != len(mappings):
        raise ValueError(f"Duplicate mapping IDs for {library}")
    track_path = DATA / library / "api_source_tracks_v2.json"
    if not track_path.is_file():
        raise ValueError(f"Missing source tracks: {track_path}")
    track_rows = read_json(track_path)
    tracks = {item["mapping_id"]: item for item in track_rows}
    if len(tracks) != len(track_rows):
        raise ValueError(f"Duplicate source-track mapping IDs for {library}")
    ready, skipped = [], []
    for mapping in mappings:
        row_id = mapping["id"]
        deprecated_version = mapping.get("deprecated_version")
        if deprecated_version is None:
            skipped.append((row_id, "deprecated_version is null"))
            continue
        if not isinstance(deprecated_version, str) or not deprecated_version:
            raise ValueError(f"Mapping {row_id}: invalid deprecated_version")
        version_key(deprecated_version)
        track = tracks.get(row_id)
        if track is None:
            raise ValueError(f"Mapping {row_id}: api_source_tracks_v2 is missing")
        actual = {state["role"]: state["api"] for state in track["api_states"]}
        expected = {role: mapping[role] for role in
                    ("deprecated_api", "replacement_api")}
        if (track["library"] != library or len(track["api_states"]) != 2
                or actual != expected):
            raise ValueError(f"Mapping {row_id}: source-track APIs differ from mapping")
        ready.append((mapping, track))
    return ready, skipped


# 2. Dùng chữ ký đã có, không phân tích lại tham số.
def signature_shape(api, value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith(f"{api}("):
        raise ValueError(f"Invalid signature for {api}: {value!r}")
    return value[len(api):]


def short_value(value):
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# 3. Chuẩn hóa thay đổi theo schema bước 6 cũ.
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
    declared = to_state.get("signature")
    declared_shape = signature_shape(old_api, declared)
    effective_shape = signature_shape(old_api, to_state.get("effective_signature"))
    replacement_shape = signature_shape(
        replacement, replacement_state.get("effective_signature"))
    if (declared_shape is None or not re.fullmatch(
        r"\(\*[A-Za-z_]\w*,\s*\*\*[A-Za-z_]\w*\)", declared_shape
    ) or from_state.get("signature") == declared
            or effective_shape is None or effective_shape != replacement_shape):
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
            "old_value": from_state.get("signature"),
            "new_value": declared,
            "from_version": from_state["version"], "source": None,
            "delegates_to": replacement, "source_evidence": [target]}


# 4. Tạo sự kiện ứng viên cho một cặp migration.
def build_facts(pair, track):
    histories = {item["api"]: item for item in track["api_states"]}
    version_states = {api: {item["version"]: item for item in history["versions"]}
                      for api, history in histories.items()}
    statuses = {api: state_statuses(history) for api, history in histories.items()}
    all_versions = sorted(set().union(*(set(items) for items in statuses.values())),
                          key=version_key)
    present_versions = [version for version in all_versions if any(
        statuses[api].get(version) == "present" for api in histories)]
    baseline = present_versions[0] if present_versions else pair["deprecated_version"]
    pending = [{"version": baseline, "category": "baseline", "api": None,
                "mandatory": True, "change": None}]
    old_api, new_api = pair["deprecated_api"], pair["replacement_api"]

    for api, history in histories.items():
        evidence = {item["id"]: item for item in history.get("evidence", [])}
        states = version_states[api]
        for transition in history["transitions"]:
            status = transition["status"]
            if status not in {"compared", "partial"} or not transition["changes"]:
                continue
            version = transition["to_version"]
            wrapper = (wrapper_event(api, new_api, states.get(transition["from_version"]),
                                     states.get(version), version_states[new_api].get(version),
                                     evidence) if api == old_api and status == "compared"
                       else None)
            if wrapper:
                pending.append({"version": version, "category": "wrapper",
                                "api": api, "mandatory": True, "change": wrapper})
            target_changed = any(
                change["kind"] == "effective_target_changed"
                and change.get("before") is not None
                and change.get("after") is not None
                for change in transition["changes"])
            for change in transition["changes"]:
                kind = change["kind"]
                # Chữ ký chưa rõ: chỉ giữ thay đổi source đã có bằng chứng.
                if status == "partial" and (kind.startswith("parameter_") or
                                            change.get("scope") == "effective_target"):
                    continue
                if (kind == "effective_target_changed" and
                        (change.get("before") is None or change.get("after") is None)):
                    continue
                if (target_changed and kind == "implementation_changed" and
                        change.get("scope") == "effective_target"):
                    continue
                if wrapper and change.get("scope") == "declared" and kind.startswith(
                        "parameter_"):
                    continue
                normalized = normalize_change(api, transition, change, evidence)
                pending.append({"version": version, "category": "source_change",
                                "api": api, "mandatory": True,
                                "comparison_status": status,
                                "change": normalized})

    for api in histories:
        previous = None
        for version in sorted(statuses[api], key=version_key):
            current = statuses[api][version]
            if previous and previous[1] == "present" and current == "absent":
                change = {"api": api, "type": "removed", "parameter": None,
                          "old_value": "present", "new_value": "absent",
                          "from_version": previous[0],
                          "source": f"data/{pair['library']}/api_source_tracks_v2.json",
                          "delegates_to": new_api if api == old_api else None,
                          "source_evidence": []}
                pending.append({"version": version, "category": "removal",
                                "api": api, "mandatory": True, "change": change})
            elif previous and previous[1] == "absent" and current == "present":
                change = {"api": api, "type": "introduced", "parameter": None,
                          "old_value": "absent", "new_value": "present",
                          "from_version": previous[0],
                          "source": f"data/{pair['library']}/api_source_tracks_v2.json",
                          "delegates_to": None, "source_evidence": []}
                pending.append({"version": version, "category": "introduction",
                                "api": api, "mandatory": True, "change": change})
            previous = (version, current)

    pending.append({"version": pair["deprecated_version"],
                    "category": "deprecation", "api": old_api, "mandatory": True,
                    "change": {"api": old_api, "type": "deprecated", "parameter": None,
                               "old_value": None, "new_value": new_api,
                               "from_version": None,
                               "source": f"data/api_mapping_versions.json#id={pair['id']}",
                               "delegates_to": None, "source_evidence": []}})
    pending.sort(key=lambda item: (version_key(item["version"]),
                                   item["category"], item["api"] or ""))
    # Gộp thay đổi giống nhau sau khi bỏ scope, giữ mọi bằng chứng.
    unique, seen = [], {}
    for item in pending:
        if item["category"] != "source_change":
            unique.append(item)
            continue
        change = item["change"]
        key = (item["version"], json.dumps(
            {name: value for name, value in change.items()
             if name != "source_evidence"}, sort_keys=True, ensure_ascii=False))
        previous = seen.get(key)
        if previous is None:
            seen[key] = item
            unique.append(item)
            continue
        previous["mandatory"] |= item["mandatory"]
        previous["comparison_status"] = (
            "compared" if item["comparison_status"] == "compared"
            else previous["comparison_status"])
        refs = previous["change"]["source_evidence"]
        refs.extend(ref for ref in change["source_evidence"] if ref not in refs)
    events = []
    for index, item in enumerate(unique, 1):
        events.append({"id": f"EV{index:04d}", **item})

    histories_summary = []
    for api, history in histories.items():
        states = version_states[api]
        signature_runs = []
        for version in sorted(states, key=version_key):
            state = states[version]
            current = (state.get("signature"), state.get("effective_signature"))
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
             "deprecated_version": pair["deprecated_version"],
             "api_histories": histories_summary, "candidate_events": events}
    return facts, version_states, statuses


# 5. Giới hạn LLM chỉ chọn ID sự kiện.
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


# 6. Ghép các mốc được chọn thành output cố định.
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
                               "signature": state.get("signature") if state else None,
                               "effective_signature": (
                                   state.get("effective_signature") if state else None)})
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
        # Gọi OpenRouter trực tiếp để tránh DLL jiter của OpenAI SDK.
        payload = {"model": model, "messages": messages,
                   "response_format": {"type": "json_schema", "json_schema": {
                       "name": "migration_milestones", "strict": True,
                       "schema": RESPONSE_SCHEMA}}}
        api_request = request.Request(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {client}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(api_request, timeout=180) as stream:
                response = json.load(stream)
        except error.HTTPError as exc:
            raise RuntimeError(f"OpenRouter HTTP {exc.code}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"OpenRouter connection failed: {exc.reason}") from exc
        choices = response.get("choices") or []
        if not choices or choices[0].get("finish_reason") != "stop":
            raise RuntimeError("Model response incomplete")
        content = choices[0].get("message", {}).get("content")
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


# 7. Chạy từng thư viện và ghi JSON v2.
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build pairwise migration evolution from Step 5 v2 tracks")
    parser.add_argument("--library", required=True)
    parser.add_argument("--mapping-id", type=int)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--provider", choices=("openrouter", "openai"),
                        default="openrouter", help="Defaults to OpenRouter")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print one model input without calling the API")
    parser.add_argument("--resume", action="store_true",
                        help="Reuse output entries with the same input digest")
    args = parser.parse_args(argv)
    try:
        load_dotenv(ROOT / ".env")
        ready, skipped = load_inputs(args.library, args.mapping_id)
        for mapping_id, reason in skipped:
            print(f"Skipping mapping {mapping_id}: {reason}", file=sys.stderr)
        if args.dry_run and len(ready) != 1:
            raise ValueError("--dry-run requires one selected mapping with complete inputs")
        if not ready:
            raise ValueError("No mappings have both inputs")
        prompt = PROMPT.read_text(encoding="utf-8")
        provider = args.provider
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
        if provider == "openrouter":
            client = api_key
        else:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ValueError("Install requirements.txt to use the model API") from exc
            client = OpenAI(api_key=api_key, timeout=180, max_retries=2)
        default_name = (f"migration_evolution_v2_mapping_{args.mapping_id}.json"
                        if args.mapping_id is not None else "migration_evolution_v2.json")
        output = args.output or DATA / args.library / default_name
        existing = read_json(output) if args.resume and output.is_file() else []
        selected_ids = {pair["id"] for pair, _track in ready}
        results = {item["mapping_id"]: item for item in existing
                   if item["mapping_id"] in selected_ids}
        for index, (pair, track) in enumerate(ready, 1):
            facts, version_states, statuses = build_facts(pair, track)
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
        write_json(output, [results[key] for key in sorted(results)])
        print(f"Wrote {len(results)} migration histories to {output}", file=sys.stderr)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
