"""Áp dụng ba CSV đã rà soát để tạo data/{library}/O|U_modified.json.

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
    omitted_rows = read_csv("possible_omitted_versions_diy.csv")
    empty_rows = read_csv("empty_samples_reassessed.csv")
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
        key = sample_key(row)
        if key in reassessed or not row["versions"] or not row["api_call"]:
            raise ValueError(f"Invalid reassessed sample: {key}")
        reassessed[key] = row
        evidence[key].append(row)

    keys = set(evidence)
    if set(reassessed) & (set(invalid) | set(omitted)):
        raise ValueError("A reassessed empty sample also appears in another CSV")
    for key in set(invalid) & set(omitted):
        if invalid[key] & omitted[key]:
            raise ValueError(f"Version both invalid and omitted: {key}")

    # 2. Xóa version sai, thêm version thiếu/rỗng; chỉ điền api_call nếu gốc là null.
    pending = set(keys)
    changed = recovered_calls = 0
    paths = sorted(DATA.glob("*/*_with_versions.json"))
    if not paths:
        raise ValueError("No input JSON files found")
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
                    original_call is not None and row["api_call"] != original_call
                ):
                    raise ValueError(f"CSV does not match source sample: {key}")
            original = set(sample["versions"])
            if not invalid[key] <= original or omitted[key] & original:
                raise ValueError(f"CSV versions conflict with source sample: {key}")
            additions = set(omitted[key])
            if key in reassessed:
                if original:
                    raise ValueError(f"Reassessed sample is not empty: {key}")
                additions.update(reassessed[key]["versions"].split(";"))
                if original_call is None:
                    sample["api_call"] = reassessed[key]["api_call"]
                    recovered_calls += 1
            if "" in additions:
                raise ValueError(f"Empty version in CSV: {key}")
            sample["versions"] = sorted((original - invalid[key]) | additions,
                                        key=version_key)
            changed += 1
            pending.remove(key)

        # 3. Ghi file mới; không sửa O/U_with_versions.json.
        destination = source.with_name(f"{kind}_modified.json")
        temporary = destination.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(samples, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(destination)

    if pending:
        raise ValueError(f"CSV refers to missing input samples: {sorted(pending)}")
    print(f"Created {len(paths)} files; modified {changed} samples; "
          f"recovered {recovered_calls} api_call values")


if __name__ == "__main__":
    main()
