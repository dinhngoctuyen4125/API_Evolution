"""Áp dụng ba CSV audit để tạo O|U_modified.json.

Chạy: python build_step7_modified.py
Không gọi LLM và không suy ra version ngoài các CSV đầu vào.
"""

import csv
import json
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUTPUT = ROOT / "reports" / "step7" / "output"


def read_csv(name):
    with (OUTPUT / name).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def sample_key(row):
    index = int(row["index"])
    if row["set"] not in {"O", "U"} or index < 0:
        raise ValueError(f"Invalid sample key: {row}")
    return row["library"], row["set"], index


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def main():
    # 1. Đọc đúng ba CSV; mỗi dòng giữ nguyên library/set/index để tra mẫu gốc.
    invalid_rows = read_csv("invalid_selected_versions.csv")
    omitted_rows = read_csv("possible_omitted_versions.csv")
    empty_rows = read_csv("empty_samples_audit.csv")
    invalid, omitted, reassessed = defaultdict(set), defaultdict(set), {}
    evidence = defaultdict(list)
    for rows, destination in ((invalid_rows, invalid), (omitted_rows, omitted)):
        for row in rows:
            key = sample_key(row)
            version = row["version"]
            if not version or version in destination[key]:
                raise ValueError(f"Empty or duplicate version: {key}, {version!r}")
            destination[key].add(version)
            evidence[key].append(row)
    for row in empty_rows:
        if row["classification"] != "possible_false_negative":
            continue
        key = sample_key(row)
        if key in reassessed or not row["compatible_versions"] or not row["api_call"]:
            raise ValueError(f"Invalid empty audit sample: {key}")
        reassessed[key] = row
        evidence[key].append(row)

    keys = set(evidence)
    if set(reassessed) & (set(invalid) | set(omitted)):
        raise ValueError("A reassessed empty sample also appears in another CSV")
    for key in set(invalid) & set(omitted):
        if invalid[key] & omitted[key]:
            raise ValueError(f"Version both invalid and omitted: {key}")

    # 2. Chỉ xóa version sai và thêm version thiếu/rỗng.
    pending = set(keys)
    changed = 0
    paths = sorted(DATA.glob("*/*_with_versions.json"))
    if not paths:
        raise ValueError("No input JSON files found")
    outputs = []
    for source in paths:
        library = source.parent.name
        kind = source.name.split("_", 1)[0]
        if kind not in {"O", "U"}:
            continue
        samples = json.loads(source.read_text(encoding="utf-8"))
        for key in sorted(k for k in pending if k[:2] == (library, kind)):
            index = key[2]
            if index >= len(samples):
                raise ValueError(f"Index outside {source}: {key}")
            sample = samples[index]
            target = (sample["replacement api"] if kind == "U"
                      else sample["deprecated api"][0])
            original_call = sample.get("api_call")
            for row in evidence[key]:
                if row["target_api"] != target or (
                    row["api_call"] != (original_call or "")
                ):
                    raise ValueError(f"CSV does not match source sample: {key}")
            original = set(sample["versions"])
            if not invalid[key] <= original or omitted[key] & original:
                raise ValueError(f"CSV versions conflict with source sample: {key}")
            additions = set(omitted[key])
            if key in reassessed:
                if original:
                    raise ValueError(f"Reassessed sample is not empty: {key}")
                additions.update(reassessed[key]["compatible_versions"].split(";"))
            if "" in additions:
                raise ValueError(f"Empty version in CSV: {key}")
            sample["versions"] = sorted((original - invalid[key]) | additions,
                                        key=version_key)
            changed += 1
            pending.remove(key)

        # 3. Ghi file mới; không sửa O/U_with_versions.json.
        destination = source.with_name(f"{kind}_modified.json")
        outputs.append((destination, samples))

    if pending:
        raise ValueError(f"CSV refers to missing input samples: {sorted(pending)}")
    for destination, samples in outputs:
        temporary = destination.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(samples, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(destination)
    print(f"Created {len(paths)} files; modified {changed} samples")


if __name__ == "__main__":
    main()
