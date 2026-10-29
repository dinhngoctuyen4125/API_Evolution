"""Gán các mốc migration phù hợp cho mẫu gọi API trong tập O/U.

Xem thử: python build_callsite_versions.py --library numpy --file O --dry-run
Chạy:     python build_callsite_versions.py --library numpy --file O
"""

import argparse
import hashlib
import io
import json
import os
import socket
import sys
import time
import tokenize
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path

from dotenv import load_dotenv

from build_migration_evolution_v2 import (
    DATA, MODEL, OPENROUTER_BASE_URL, ROOT, provider_model, version_key,
    write_json,
)
from enrich_pypi_versions import REPOSITORIES


PROMPT = ROOT / "prompts" / "callsite_version.txt"
PACKAGE_ROOT = {"pytorch": "torch", "scikit-learn": "sklearn"}
CALLABLE_STATUSES = {"active", "deprecated"}


# 1. Đọc mẫu và ghép timeline theo cặp API.
def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def package_root(library):
    return PACKAGE_ROOT.get(library, library)


def select_samples(records, library):
    prefix = package_root(library) + "."
    return [(index, sample) for index, sample in enumerate(records)
            if isinstance(sample.get("replacement api"), str)
            and sample["replacement api"].startswith(prefix)]


def load_timelines(path, library):
    if not path.is_file():
        raise ValueError(f"Missing migration timeline: {path}")
    by_pair = {}
    for timeline in read_json(path):
        if timeline.get("library") != library:
            raise ValueError(f"Timeline {timeline.get('mapping_id')} has wrong library")
        seed = timeline["seed"]
        key = (seed["deprecated_api"], seed["replacement_api"])
        if key in by_pair:
            raise ValueError(f"Duplicate migration timeline for {key}")
        by_pair[key] = timeline
    if not by_pair:
        raise ValueError(f"No migration timelines for {library}")
    return by_pair


def matching_timelines(sample, by_pair):
    replacement = sample["replacement api"]
    deprecated = sample.get("deprecated api", [])
    if not isinstance(deprecated, list):
        raise ValueError("Sample field 'deprecated api' must be an array")
    return [by_pair[(old, replacement)] for old in dict.fromkeys(deprecated)
            if (old, replacement) in by_pair]


# 2. Trích lời gọi API sau probing input.
def extract_api_call(sample, file_kind):
    function = sample["function"]
    probe = sample["probing input"].rstrip(" \n")
    if not function.startswith(probe):
        return None
    source = function[len(probe):].lstrip(" \n")
    target = target_api(sample, file_kind)
    aliases = sample.get("alias dict", {})
    names = {target, *(alias for alias, canonical in aliases.items()
                       if canonical == target)}
    name_tokens = {tuple(name.split(".")) for name in names}
    tokens = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            tokens.append(token)
    except (tokenize.TokenError, IndentationError):
        pass
    significant = [token for token in tokens
                   if token.type in (tokenize.NAME, tokenize.OP)]
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def offset(position):
        line, column = position
        return offsets[line - 1] + column

    def complete_call(start, opening):
        depth = 0
        for closing in significant[opening:]:
            if closing.string == "(":
                depth += 1
            elif closing.string == ")":
                depth -= 1
                if depth == 0:
                    begin, end = offset(start.start), offset(closing.end)
                    cursor = begin
                    pieces = []
                    for token in tokens:
                        if token.type != tokenize.COMMENT:
                            continue
                        comment_start = offset(token.start)
                        if begin <= comment_start < end:
                            pieces.append(source[cursor:comment_start])
                            cursor = offset(token.end)
                    pieces.append(source[cursor:end])
                    return "".join(pieces)
        return None

    for index, token in enumerate(significant):
        if token.type != tokenize.NAME:
            continue
        for name in name_tokens:
            end = index + 2 * len(name) - 1
            if end >= len(significant) or significant[end].string != "(":
                continue
            if any(significant[index + 2 * part].string != component
                   for part, component in enumerate(name)) or any(
                    significant[index + 2 * part + 1].string != "."
                    for part in range(len(name) - 1)):
                continue
            return complete_call(token, end)

    # Instance methods appear as obj.method(...) rather than Class.method(...).
    parts = target.split(".")
    if len(parts) >= 2 and parts[-2][:1].isupper():
        method = parts[-1]
        for index in range(2, len(significant) - 1):
            if (significant[index].string != method
                    or significant[index - 1].string != "."
                    or significant[index + 1].string != "("
                    or significant[index - 2].type != tokenize.NAME):
                continue
            start = index - 2
            while (start >= 2 and significant[start - 1].string == "."
                   and significant[start - 2].type == tokenize.NAME):
                start -= 2
            return complete_call(significant[start], index + 1)
    return None


# 3. Chọn API, mốc ứng viên và dữ kiện gửi model.
def target_api(sample, file_kind):
    if file_kind == "U":
        return sample["replacement api"]
    deprecated = sample["deprecated api"]
    if len(deprecated) != 1:
        raise ValueError("An O sample must have exactly one deprecated API")
    return deprecated[0]


def compact_timeline(timeline, target):
    milestones = []
    for milestone in timeline["milestones"]:
        state = next((item for item in milestone["api_states"]
                      if item["name"] == target), None)
        if state is None:
            raise ValueError(f"Timeline {timeline['mapping_id']} lacks state for {target}")
        milestones.append({
            "version": milestone["version"],
            "api_states": milestone["api_states"],
            "changes": [
                {key: change.get(key) for key in
                 ("api", "type", "parameter", "old_value", "new_value",
                  "delegates_to")}
                for change in milestone["changes"]],
        })
    return {"mapping_id": timeline["mapping_id"], "seed": timeline["seed"],
            "deprecated_version": timeline.get("deprecated_version"),
            "milestones": milestones}


def candidate_versions(timelines, target):
    """Chỉ lấy các mốc mà API được xác nhận còn gọi được."""
    versions = set()
    for timeline in timelines:
        for milestone in timeline["milestones"]:
            state = next((item for item in milestone["api_states"]
                          if item["name"] == target), None)
            if state is None:
                raise ValueError(f"Timeline {timeline['mapping_id']} lacks state for {target}")
            if state["status"] in CALLABLE_STATUSES:
                versions.add(milestone["version"])
    return sorted(versions, key=version_key)


def relevant_aliases(sample, target, timelines):
    aliases = sample.get("alias dict", {})
    if not isinstance(aliases, dict):
        raise ValueError("Sample field 'alias dict' must be an object")
    function = sample["function"]
    api_names = {target}
    for timeline in timelines:
        api_names.update(timeline["seed"].values())
    return {alias: canonical for alias, canonical in aliases.items()
            if canonical in api_names or alias in function}


def model_input(sample, source_index, file_kind, timelines, candidates, api_call):
    target = target_api(sample, file_kind)
    return {
        "source_file": file_kind,
        "source_index": source_index,
        "target_api": target,
        "function": sample["function"],
        "probing_input": sample["probing input"],
        "api_call": api_call,
        "reference_dict": sample.get("reference dict", {}),
        "alias_dict": relevant_aliases(sample, target, timelines),
        "candidate_versions": candidates,
        "migration_timelines": [compact_timeline(item, target) for item in timelines],
    }


# 4. Gọi model và kiểm tra các version trả về.
def response_schema(candidates):
    return {"type": "object", "additionalProperties": False,
            "properties": {"versions": {"type": "array", "items": {
                "type": "string", "enum": candidates}}},
            "required": ["versions"]}


def openrouter_completion(api_key, payload):
    """Gọi OpenRouter trực tiếp qua HTTP."""
    request = Request(
        OPENROUTER_BASE_URL + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=180) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {408, 429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"OpenRouter connection failed: {exc}") from exc
    raise RuntimeError("OpenRouter request failed after retries")


def call_model(client, prompt, facts, model, provider):
    messages = [{"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(
                    facts, ensure_ascii=False, separators=(",", ":"))}]
    format_spec = {"type": "json_schema", "name": "callsite_versions",
                   "strict": True,
                   "schema": response_schema(facts["candidate_versions"])}
    if provider == "openrouter":
        response = openrouter_completion(client, {
            "model": model, "messages": messages,
            "response_format": {"type": "json_schema", "json_schema": {
                key: format_spec[key] for key in ("name", "strict", "schema")}},
            "provider": {"require_parameters": True},
        })
        if not isinstance(response, dict):
            raise RuntimeError("OpenRouter returned an invalid response")
        if response.get("error"):
            raise RuntimeError(f"OpenRouter error: {str(response['error'])[:500]}")
        choices = response.get("choices", [])
        if not choices or choices[0].get("finish_reason") != "stop":
            raise RuntimeError("Model response incomplete")
        content = choices[0].get("message", {}).get("content")
    else:
        response = client.responses.create(
            model=model, reasoning={"effort": "low"}, input=messages,
            text={"format": format_spec}, store=False,
        )
        if response.status != "completed":
            raise RuntimeError(f"Model response incomplete: {response.status}")
        content = response.output_text
    if not content:
        raise RuntimeError("Model response has no JSON content")
    return json.loads(content)


def validate_versions(answer, candidates):
    if not isinstance(answer, dict) or set(answer) != {"versions"} \
            or not isinstance(answer["versions"], list):
        raise ValueError("Model response must contain only a versions array")
    values = answer["versions"]
    if any(not isinstance(value, str) for value in values) \
            or len(values) != len(set(values)) or not set(values).issubset(candidates):
        raise ValueError("Model returned an invalid or repeated version")
    return [version for version in candidates if version in values]


# 5. Lưu tiến độ và dựng output.
def input_digest(dataset_path, timeline_path, prompt, provider, model, library,
                 file_kind):
    digest = hashlib.sha256()
    for value in (library, file_kind, provider, model, prompt):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    for path in (dataset_path, timeline_path):
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def read_progress(path, digest, count):
    if not path.is_file():
        raise ValueError(f"No progress journal to resume: {path}")
    with path.open(encoding="utf-8") as stream:
        header = json.loads(stream.readline())
        if header != {"digest": digest, "count": count}:
            raise ValueError("Progress journal does not match current inputs or model")
        versions = []
        for line in stream:
            entry = json.loads(line)
            values = entry.get("versions", entry.get("version"))
            if entry.get("index") != len(versions) or not isinstance(
                    values, list):
                raise ValueError("Progress journal is incomplete or out of order")
            versions.append(values)
    if len(versions) > count:
        raise ValueError("Progress journal exceeds selected sample count")
    return versions


def append_progress(path, index, source_index, versions, reason=None):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"index": index, "source_index": source_index,
                                 "versions": versions,
                                 "reason": reason},
                                ensure_ascii=False) + "\n")


def render_output(selected, completed, file_kind):
    if len(completed) > len(selected):
        raise ValueError("More completed results than selected samples")
    return [{**sample, "api_call": extract_api_call(sample, file_kind),
             "versions": versions}
            for (_, sample), versions in zip(selected, completed)]


# 6. Điều phối pipeline cho một thư viện và tập O/U.
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, choices=sorted(REPOSITORIES))
    parser.add_argument("--file", required=True, type=str.upper, choices=("O", "U"))
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--provider", choices=("openrouter", "openai"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-samples", type=int,
                        help="Process at most this many library samples in this run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print one model input without calling the API")
    parser.add_argument("--resume", action="store_true",
                        help="Continue from the progress journal")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace an existing output and progress journal")
    args = parser.parse_args(argv)
    try:
        if args.max_samples is not None and args.max_samples < 1:
            raise ValueError("--max-samples must be positive")
        if args.resume and args.overwrite:
            raise ValueError("Choose either --resume or --overwrite")
        # Nạp mẫu, timeline và cấu hình model.
        load_dotenv(ROOT / ".env")
        dataset_path = DATA / "icsme_data" / f"{args.file}.json"
        timeline_path = DATA / args.library / "migration_evolution_v2.json"
        selected = select_samples(read_json(dataset_path), args.library)
        if not selected:
            raise ValueError(f"No {args.file} samples for {args.library}")
        timelines = load_timelines(timeline_path, args.library)
        matched_count = sum(bool(matching_timelines(sample, timelines))
                            for _, sample in selected)
        if not matched_count:
            raise ValueError(f"No {args.file} samples match {args.library} timelines")
        prompt = PROMPT.read_text(encoding="utf-8")
        provider = args.provider or ("openrouter" if os.getenv("OPENROUTER_API_KEY")
                                     else "openai")
        model = provider_model(args.model, provider)
        output = args.output or DATA / args.library / f"{args.file}_with_versions.json"
        journal = output.with_name(output.stem + ".progress.jsonl")
        if output.resolve() in {dataset_path.resolve(), timeline_path.resolve()}:
            raise ValueError("Output must differ from the source dataset and timeline")
        # In thử prompt cho một mẫu hợp lệ.
        if args.dry_run:
            for source_index, sample in selected:
                matches = matching_timelines(sample, timelines)
                api_call = extract_api_call(sample, args.file)
                if matches and api_call:
                    candidates = candidate_versions(
                        matches, target_api(sample, args.file))
                    if candidates:
                        print(json.dumps({"provider": provider, "model": model,
                                          "instructions": prompt,
                                          "input": model_input(sample, source_index,
                                                               args.file, matches,
                                                               candidates, api_call)},
                                         ensure_ascii=False, indent=2))
                        return
            raise ValueError("No sample with an API call and matching timeline")

        print(f"{args.library} {args.file}: {len(selected)} samples, "
              f"{matched_count} with migration timelines", file=sys.stderr)

        # Khôi phục tiến độ hoặc chuẩn bị lần chạy mới.
        digest = input_digest(dataset_path, timeline_path, prompt, provider,
                              model, args.library, args.file)
        if args.resume:
            completed = read_progress(journal, digest, len(selected))
        else:
            if (output.exists() or journal.exists()) and not args.overwrite:
                raise ValueError("Output or progress exists; use --resume or --overwrite")
            completed = []

        stop = min(len(selected), len(completed) + args.max_samples) \
            if args.max_samples else len(selected)
        work = selected[len(completed):stop]
        # Chỉ khởi tạo model khi có mẫu cần đánh giá.
        needs_model = any(
            (matches := matching_timelines(sample, timelines))
            and extract_api_call(sample, args.file)
            and candidate_versions(matches, target_api(sample, args.file))
            for _, sample in work)
        client = None
        if needs_model:
            key_name = ("OPENROUTER_API_KEY" if provider == "openrouter"
                        else "OPENAI_API_KEY")
            api_key = os.getenv(key_name)
            if not api_key:
                raise ValueError(f"Set {key_name} before calling the model")
            if provider == "openrouter":
                client = api_key
            else:
                try:
                    from openai import OpenAI
                except ImportError as exc:
                    raise ValueError("Install requirements.txt to use the OpenAI API") from exc
                client = OpenAI(api_key=api_key, timeout=180, max_retries=2)

        output.parent.mkdir(parents=True, exist_ok=True)
        if not args.resume:
            journal.write_text(json.dumps({"digest": digest, "count": len(selected)})
                               + "\n", encoding="utf-8")
        # Đánh giá từng mẫu và ghi tiến độ ngay sau mỗi kết quả.
        no_timeline = missing_call = 0
        for index in range(len(completed), stop):
            source_index, sample = selected[index]
            matches = matching_timelines(sample, timelines)
            api_call = extract_api_call(sample, args.file)
            reason = None
            if not matches:
                versions = []
                reason = "migration_timeline_missing"
                no_timeline += 1
            elif not api_call:
                versions = []
                reason = "api_call_not_found_after_probe"
                missing_call += 1
            else:
                candidates = candidate_versions(matches, target_api(sample, args.file))
                if candidates:
                    facts = model_input(sample, source_index, args.file,
                                        matches, candidates, api_call)
                    try:
                        versions = validate_versions(call_model(
                            client, prompt, facts, model, provider), candidates)
                    except (ValueError, RuntimeError) as exc:
                        raise ValueError(f"Sample {source_index}: {exc}") from exc
                else:
                    versions = []
                    reason = "no_callable_milestone"
            if not versions and reason is None:
                reason = "model_found_no_compatible_milestone"
            append_progress(journal, index, source_index, versions, reason)
            completed.append(versions)
            if (index + 1) % 25 == 0 or index + 1 == stop:
                print(f"{args.library} {args.file}: {index + 1}/{len(selected)} samples",
                      file=sys.stderr, flush=True)

        # Gộp bản ghi gốc với danh sách version đã chọn.
        result = render_output(selected, completed, args.file)
        write_json(output, result)
        stage = "partial" if len(result) < len(selected) else "complete"
        print(f"Wrote {len(result)}/{len(selected)} samples ({stage}) to {output}; "
              "this run skipped "
              f"{no_timeline} without a timeline and {missing_call} without an "
              "API call", file=sys.stderr)
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
