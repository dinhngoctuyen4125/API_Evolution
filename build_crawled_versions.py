"""Ghép mẫu crawl bước 9 thành O|U_crawled.json và gán thêm mốc bằng model (có cache).

Đầu vào:  data/{library}/{O,U}_crawled_dependency.json (mốc từ dependency) và
          data/{library}/{O,U}_crawled_not_dependency.json (versions rỗng).
Đầu ra:   data/{library}/{O,U}_crawled.json.
Cache:    data/{library}/{O,U}_crawled.cache.jsonl, theo nội dung mẫu; mẫu đã gán không gọi lại model.

Xem thử: python build_crawled_versions.py --library pandas --file O --dry-run
Chạy:     python build_crawled_versions.py --library pandas
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
import random
import re
import sys
import threading
import time

from dotenv import load_dotenv

from build_migration_evolution import DATA, MODEL, ROOT, version_key, write_json
from github_crawler import sample_key

# Cùng prompt với bước 7: chọn mọi mốc mà lời gọi khớp chữ ký.
PROMPT = ROOT / "prompts" / "callsite_version.txt"
CALLABLE_STATUSES = {"active", "deprecated"}
SHARED_FACT_KEYS = ("target_api", "candidate_versions", "migration_timelines")


# 1. Đọc dữ liệu và timeline (cùng logic bước 7).
def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_timelines(path, library):
    if not path.is_file():
        raise ValueError(f"Missing migration timeline: {path}")
    by_pair = {}
    for timeline in read_json(path):
        if timeline.get("library") != library:
            raise ValueError(f"Timeline {timeline.get('mapping_id')} has wrong library")
        seed = timeline["seed"]
        by_pair[(seed["deprecated_api"], seed["replacement_api"])] = timeline
    if not by_pair:
        raise ValueError(f"No migration timelines for {library}")
    return by_pair


def matching_timelines(sample, by_pair):
    replacement = sample["replacement api"]
    return [by_pair[(old, replacement)] for old in dict.fromkeys(sample.get("deprecated api", []))
            if (old, replacement) in by_pair]


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
        state = next((item for item in milestone["api_states"] if item["name"] == target), None)
        if state is None:
            raise ValueError(f"Timeline {timeline['mapping_id']} lacks state for {target}")
        if state["status"] not in CALLABLE_STATUSES:
            continue
        milestones.append({"version": milestone["version"],
                           "api_states": [{key: state.get(key) for key in
                                           ("name", "status", "signatures", "call_syntax")}]})
    return {"mapping_id": timeline["mapping_id"], "milestones": milestones}


def candidate_versions(timelines, target):
    """Chỉ lấy các mốc mà API được xác nhận còn gọi được."""
    versions = set()
    for timeline in timelines:
        for milestone in timeline["milestones"]:
            state = next((item for item in milestone["api_states"] if item["name"] == target), None)
            if state is not None and state["status"] in CALLABLE_STATUSES:
                versions.add(milestone["version"])
    return sorted(versions, key=version_key)


def relevant_aliases(sample, target, timelines):
    aliases = sample.get("alias dict", {})
    api_names = {target}
    for timeline in timelines:
        api_names.update(timeline["seed"].values())
    return {alias: canonical for alias, canonical in aliases.items()
            if canonical in api_names or alias in sample["function"]}


def model_input(sample, index, file_kind, timelines, candidates):
    target = target_api(sample, file_kind)
    return {
        "target_api": target,
        "candidate_versions": candidates,
        "migration_timelines": [compact_timeline(item, target) for item in timelines],
        "source_file": file_kind,
        "source_index": index,
        "function": sample["function"],
        "probing_input": sample["probing input"],
        # Mẫu crawl đã có lời gọi do AST tách ra ở bước 9.
        "api_call": sample["api_call"],
        "reference_dict": sample.get("reference dict", {}),
        "alias_dict": relevant_aliases(sample, target, timelines),
    }


def cache_key(facts):
    """Nội dung quyết định câu trả lời; không gồm model, prompt và vị trí mẫu."""
    content = {key: facts[key] for key in facts if key not in ("source_file", "source_index")}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True)
                          .encode("utf-8")).hexdigest()


# 2. Gọi model OpenAI và kiểm tra câu trả lời (cùng logic bước 7).
def response_schema(candidates):
    return {"type": "object", "additionalProperties": False,
            "properties": {"versions": {"type": "array", "items": {
                "type": "string", "enum": candidates}}},
            "required": ["versions"]}


def call_model(client, prompt, facts, model):
    dump = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    shared = {key: facts[key] for key in SHARED_FACT_KEYS}
    own = {key: value for key, value in facts.items() if key not in SHARED_FACT_KEYS}
    response = client.responses.create(
        model=model, reasoning={"effort": "low"},
        input=[{"role": "system", "content": prompt}, {"role": "user", "content": [
            {"type": "input_text", "text": dump(shared),
             "prompt_cache_breakpoint": {"mode": "explicit"}},
            {"type": "input_text", "text": dump(own)}]}],
        text={"format": {"type": "json_schema", "name": "callsite_versions", "strict": True,
                         "schema": response_schema(facts["candidate_versions"])}},
        store=False, extra_body={"prompt_cache_options": {"mode": "explicit"}},
    )
    if response.status != "completed":
        raise RuntimeError(f"Model response incomplete: {response.status}")
    if not response.output_text:
        raise RuntimeError("Model response has no JSON content")
    return json.loads(response.output_text)


def validate_versions(answer, candidates):
    if not isinstance(answer, dict) or set(answer) != {"versions"} \
            or not isinstance(answer["versions"], list):
        raise ValueError("Model response must contain only a versions array")
    values = answer["versions"]
    if any(not isinstance(value, str) for value in values) \
            or len(values) != len(set(values)) or not set(values).issubset(candidates):
        raise ValueError("Model returned an invalid or repeated version")
    return [version for version in candidates if version in values]


class RateLimitGate:
    """Giãn cách request OpenAI và tạm dừng mọi luồng sau một lỗi 429 tạm thời."""

    def __init__(self):
        self.condition = threading.Condition()
        self.next_start = self.cooldown = self.interval = self.last_limit = 0.0

    def wait(self):
        with self.condition:
            while True:
                now = time.monotonic()
                delay = max(self.next_start, self.cooldown) - now
                if delay <= 0:
                    self.next_start = now + self.interval
                    return
                self.condition.wait(timeout=delay)

    def limited(self, error, delay):
        match = re.search(r"\bTPM\)?:\s*Limit ([\d,]+),\s*Used [\d,]+,\s*Requested ([\d,]+)",
                          str(error), re.IGNORECASE)
        limit = int(match.group(1).replace(",", "")) if match else 0
        floor = 60 * int(match.group(2).replace(",", "")) / limit * 1.2 if limit else 0.5
        with self.condition:
            now = time.monotonic()
            previous = self.interval
            if now - self.last_limit >= 1:
                self.interval = max(floor, self.interval * 1.25)
                self.last_limit = now
            else:
                self.interval = max(floor, self.interval)
            self.cooldown = max(self.cooldown, now + delay + random.uniform(0.05, 0.2))
            self.condition.notify_all()
            if self.interval > previous:
                print(f"OpenAI 429: spacing requests by {self.interval:.2f}s; "
                      f"retrying after at least {delay:.2f}s", file=sys.stderr, flush=True)


def retry_after_seconds(error, attempt):
    headers = error.response.headers
    for name, divisor in (("retry-after-ms", 1000), ("retry-after", 1)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            delay = float(value) / divisor
        except ValueError:
            if name != "retry-after":
                continue
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                continue
        if math.isfinite(delay) and delay >= 0:
            return delay
    match = re.search(r"try again in ([\d.]+)\s*(ms|s|m)\b", str(error), re.IGNORECASE)
    if match:
        return float(match.group(1)) * {"ms": 0.001, "s": 1, "m": 60}[match.group(2).lower()]
    return min(2 ** attempt, 60)


def ask_model(facts, client, prompt, model, gate):
    from openai import APIConnectionError, InternalServerError, RateLimitError
    deadline = time.monotonic() + 300
    for attempt in range(12):
        gate.wait()
        try:
            answer = call_model(client, prompt, facts, model)
            break
        except RateLimitError as exc:
            if exc.code in {"insufficient_quota", "billing_hard_limit_reached",
                            "usage_limit_reached"} or exc.type == "insufficient_quota":
                raise
            delay = retry_after_seconds(exc, attempt)
            if attempt == 11 or delay > deadline - time.monotonic():
                raise RuntimeError("OpenAI rate limit persisted; run again later") from exc
            gate.limited(exc, delay)
        except (APIConnectionError, InternalServerError):
            if attempt == 11:
                raise
            time.sleep(min(2 ** attempt, 30) + random.uniform(0, 0.5))
    return validate_versions(answer, facts["candidate_versions"])


# 3. Cache theo nội dung mẫu.
def read_cache(path):
    """key -> versions; dòng sau ghi đè dòng trước (ví dụ khi chạy --refresh)."""
    cache = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                cache[entry["key"]] = entry["versions"]
    return cache


def append_cache(path, key, versions, model, prompt_digest):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"key": key, "versions": versions, "model": model,
                                 "prompt_sha256": prompt_digest}, ensure_ascii=False) + "\n")


# 4. Ghép một tập O hoặc U.
def build(library, file_kind, by_pair, client_factory, prompt, model, workers, refresh, dry_run):
    folder = DATA / library
    inputs = [folder / f"{file_kind}_crawled_{name}.json" for name in ("dependency", "not_dependency")]
    if not any(path.is_file() for path in inputs):
        raise ValueError(f"No crawled samples for {library} {file_kind}: {inputs[0]}")
    dependency, loose = (read_json(path) if path.is_file() else [] for path in inputs)
    cache_path = folder / f"{file_kind}_crawled.cache.jsonl"
    output = folder / f"{file_kind}_crawled.json"
    cache = {} if refresh else read_cache(cache_path)
    prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    stats = {"dependency": len(dependency), "not_dependency": len(loose), "cached": 0,
             "asked": 0, "no_timeline_or_call": 0, "duplicate": 0, "dropped_empty": 0,
             "model_missed_dependency_version": 0}

    # Mẫu dependency trước; mẫu không dependency trùng mã với mẫu dependency thì bỏ.
    rows, seen = [], set()
    for source, samples in (("dependency", dependency), ("not_dependency", loose)):
        for sample in samples:
            key = sample_key(sample)
            if key in seen:
                stats["duplicate"] += 1
                continue
            seen.add(key)
            rows.append((source, sample))

    facts_by_row, pending = {}, {}
    for index, (source, sample) in enumerate(rows):
        matches = matching_timelines(sample, by_pair)
        target = target_api(sample, file_kind)
        candidates = candidate_versions(matches, target) if matches else []
        if not candidates or not sample.get("api_call"):
            stats["no_timeline_or_call"] += 1
            continue
        facts = model_input(sample, index, file_kind, matches, candidates)
        key = cache_key(facts)
        facts_by_row[index] = (key, facts)
        if key in cache:
            stats["cached"] += 1
        else:
            pending.setdefault(key, facts)

    if dry_run:
        print(f"{library} {file_kind}: {len(rows)} samples, {len(pending)} need the model",
              file=sys.stderr)
        if pending:
            print(json.dumps({"model": model, "instructions": prompt,
                              "input": next(iter(pending.values()))},
                             ensure_ascii=False, indent=2))
        return stats

    if pending:
        client, gate = client_factory(), RateLimitGate()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(ask_model, facts, client, prompt, model, gate): key
                       for key, facts in pending.items()}
            for done, future in enumerate(as_completed(futures), 1):
                key = futures[future]
                versions = future.result()
                # Ghi ngay từng câu trả lời: chạy bị ngắt thì lần sau chỉ hỏi phần còn lại.
                append_cache(cache_path, key, versions, model, prompt_digest)
                cache[key] = versions
                stats["asked"] += 1
                if done % 25 == 0 or done == len(futures):
                    print(f"{library} {file_kind}: model {done}/{len(futures)}",
                          file=sys.stderr, flush=True)

    result = []
    for index, (source, sample) in enumerate(rows):
        own = list(sample.get("versions", [])) if source == "dependency" else []
        if index in facts_by_row:
            answer = cache[facts_by_row[index][0]]
            # Mốc từ dependency là bằng chứng: luôn giữ, model chỉ thêm mốc.
            if any(version not in answer for version in own):
                stats["model_missed_dependency_version"] += 1
            versions = sorted(set(own) | set(answer), key=version_key)
        else:
            versions = own
        if not versions:
            stats["dropped_empty"] += 1
            continue
        result.append({**sample, "versions": versions})
    write_json(output, result)
    stats["written"] = len(result)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True)
    parser.add_argument("--file", type=str.upper, choices=("O", "U"),
                        help="Chỉ ghép một tập; mặc định cả O và U")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--workers", type=int, default=8,
                        help="Maximum concurrent model requests (default: 8)")
    parser.add_argument("--refresh", action="store_true",
                        help="Bỏ qua cache và hỏi lại model cho mọi mẫu")
    parser.add_argument("--dry-run", action="store_true",
                        help="In dữ kiện gửi model của một mẫu chưa có trong cache, không gọi API")
    args = parser.parse_args(argv)
    try:
        if args.workers < 1:
            raise ValueError("--workers must be positive")
        load_dotenv(ROOT / ".env")
        by_pair = load_timelines(DATA / args.library / "migration_evolution.json", args.library)
        prompt = PROMPT.read_text(encoding="utf-8")
        model = args.model[len("openai/"):] if args.model.startswith("openai/") else args.model

        def client_factory():
            # Chỉ cần key khi có mẫu chưa nằm trong cache.
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("Set OPENAI_API_KEY before calling the model")
            from openai import OpenAI
            return OpenAI(api_key=api_key, timeout=180, max_retries=0)

        for file_kind in ([args.file] if args.file else ["O", "U"]):
            stats = build(args.library, file_kind, by_pair, client_factory, prompt, model,
                          args.workers, args.refresh, args.dry_run)
            print(f"{args.library} {file_kind}: " + ", ".join(f"{k}={v}" for k, v in stats.items()),
                  file=sys.stderr)
    except (ValueError, OSError, KeyError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
