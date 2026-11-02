"""Đối chiếu version bước 7 với chữ ký API trong migration path bước 6.

Kiểm tra tĩnh, không gọi thư viện hay LLM. Chạy: python audit_step7.py
"""

import ast
from collections import Counter, defaultdict
import csv
from functools import lru_cache
import inspect
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports" / "step7" / "output"
CALLABLE = {"active", "deprecated"}
SENTINEL = object()


def read_json(path):
    """Đọc một file JSON đầu vào."""
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(name, rows, fields):
    """Ghi một bảng báo cáo với thứ tự cột cố định."""
    with (REPORTS / name).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@lru_cache(maxsize=None)
def parse_signature(value):
    """Đổi 'tsplot(x, data, ci=68)' thành chữ ký để kiểm tra đối số."""
    if not value or "(" not in value:
        return None
    try:
        definition = ast.parse("def f" + value[value.index("("):] + ":\n pass")
        args = definition.body[0].args
        parameters = []
        positional = args.posonlyargs + args.args
        first_default = len(positional) - len(args.defaults)
        for i, arg in enumerate(args.posonlyargs):
            parameters.append(inspect.Parameter(arg.arg, inspect.Parameter.POSITIONAL_ONLY,
                                                default=SENTINEL if i >= first_default else inspect.Parameter.empty))
        for i, arg in enumerate(args.args, start=len(args.posonlyargs)):
            parameters.append(inspect.Parameter(arg.arg, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                                default=SENTINEL if i >= first_default else inspect.Parameter.empty))
        if args.vararg:
            parameters.append(inspect.Parameter(args.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
        for arg, default in zip(args.kwonlyargs, args.kw_defaults):
            parameters.append(inspect.Parameter(arg.arg, inspect.Parameter.KEYWORD_ONLY,
                                                default=SENTINEL if default is not None else inspect.Parameter.empty))
        if args.kwarg:
            parameters.append(inspect.Parameter(args.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
        return inspect.Signature(parameters)
    except (SyntaxError, ValueError, TypeError):
        return None


@lru_cache(maxsize=None)
def parse_call(value):
    """Đếm positional/keyword; ví dụ f(x, axis=0) có 1 positional, 1 keyword."""
    if not value:
        return None
    try:
        node = ast.parse(value, mode="eval").body
        if not isinstance(node, ast.Call):
            return None
        positional = sum(not isinstance(arg, ast.Starred) for arg in node.args)
        keywords = [kw.arg for kw in node.keywords if kw.arg is not None]
        uncertain = any(isinstance(arg, ast.Starred) for arg in node.args) or any(
            kw.arg is None for kw in node.keywords)
        return positional, keywords, uncertain, ast.unparse(node.func)
    except (SyntaxError, ValueError):
        return None


def binding_modes(signature, callee, target):
    """Xử lý self/cls: obj.method(x) đã được Python truyền self ngầm."""
    params = list(signature.parameters.values())
    if not params or params[0].name not in {"self", "cls"}:
        return [signature]
    pieces = target.split(".")
    if len(pieces) >= 2 and pieces[-2][:1].isupper():
        class_name = pieces[-2]
        # Class.method(obj, ...) truyền self tường minh; obj.method(...) thì không.
        if callee.split(".")[-2:-1] == [class_name]:
            return [signature]
        return [signature.replace(parameters=params[1:])]
    if target == "scipy.stats.chi2.sf":
        # chi2 là object đã bind; chi2.sf(x) nhận self ngầm.
        return [signature.replace(parameters=params[1:])]
    # Hàm module như torch.svd có thể thật sự đặt tên tham số đầu là self.
    # Chỉ constructor của class mới bind tham số đó ngầm.
    if pieces[-1][:1].isupper():
        return [signature.replace(parameters=params[1:]), signature]
    return [signature]


def incompatibility(call, signature_text, target, aliases=None):
    """Thử bind: '' = khớp, chuỗi lỗi = sai cú pháp, None = chưa đủ dữ kiện."""
    parsed = parse_call(call)
    signature = parse_signature(signature_text)
    if parsed is None or signature is None:
        return None
    positional, keywords, uncertain, callee = parsed
    # Chỉ so chữ ký API mà alias dict thực sự trỏ tới (nếu có ánh xạ).
    if aliases and aliases.get(callee, target) != target:
        return None
    if uncertain:
        # f(*args) hoặc f(**kwargs): không biết số/tên đối số lúc chạy.
        return None
    args = [SENTINEL] * positional
    kwargs = {name: SENTINEL for name in keywords}
    errors = []
    for mode in binding_modes(signature, callee, target):
        try:
            mode.bind(*args, **kwargs)
            return ""
        except TypeError as exc:
            errors.append(str(exc))
    return errors[0] if errors else None


def classify_empty(row, reason, kind, target, states_by_version, call):
    """Phân loại versions=[]; ví dụ có chữ ký khớp thì cần xem model bỏ sót."""
    candidate, compatible, uncertain, incompatible = [], [], [], []
    for version, states in states_by_version.items():
        usable = [state for state in states if state["status"] in CALLABLE]
        if not usable:
            continue
        candidate.append(version)
        results = [incompatibility(call, state.get("effective_signature"), target,
                                   row.get("alias dict"))
                   for state in usable]
        if target == "pandas.DataFrame.loc":
            uncertain.append(version)
        elif "" in results:
            compatible.append(version)
        elif not results or None in results:
            uncertain.append(version)
        else:
            incompatible.append(version)

    if reason == "migration_timeline_missing":
        classification = "missing_timeline"
    elif reason == "no_callable_milestone":
        classification = "no_callable_milestone"
    elif reason == "api_call_not_found_after_probe":
        names = {target, *(name for name, canonical in row.get("alias dict", {}).items()
                           if canonical == target)}
        function = row["function"]
        probe = row["probing input"]
        suffix = function[len(probe):] if function.startswith(probe) else ""
        reference = row.get("reference", "")
        # Lời gọi trong f-string vẫn có ở reference/function dù api_call=null.
        found = any(re.search(r"(?<![\w.])" + re.escape(name) + r"\s*\(", reference)
                    and re.search(r"(?<![\w.])" + re.escape(name) + r"\s*\(", suffix)
                    for name in names)
        classification = "api_call_extraction_miss" if found else "api_call_missing"
    elif reason == "model_found_no_compatible_milestone":
        parsed = parse_call(call)
        aliases = row.get("alias dict", {})
        if parsed and parsed[3] in aliases and aliases[parsed[3]] != target:
            classification = "alias_target_conflict"
        elif compatible:
            classification = "possible_false_negative"
        elif uncertain:
            classification = "insufficient_signature_evidence"
        else:
            classification = "no_compatible_signature"
    else:
        raise ValueError(f"Unexpected empty-result reason: {reason}")

    order = lambda value: tuple(int(part) for part in value.split("."))
    return {"reason": reason or "", "classification": classification,
            "reference": row.get("reference", ""),
            "candidate_versions": ";".join(sorted(candidate, key=order)),
            "compatible_versions": ";".join(sorted(compatible, key=order)),
            "uncertain_versions": ";".join(sorted(uncertain, key=order)),
            "incompatible_versions": ";".join(sorted(incompatible, key=order))}


def main():
    """Rà mọi thư viện và hai tập O/U, rồi ghi các CSV báo cáo."""
    # 1. Chuẩn bị thư mục báo cáo; O/U_with_versions.json là dữ liệu đầu vào đầy đủ.
    REPORTS.mkdir(parents=True, exist_ok=True)
    issues = []
    manual_review = []
    possible_omissions = []
    empty_audit = []
    stats = []
    # 2. Ghép từng mẫu O/U với timeline theo cặp API.
    for library_dir in sorted(DATA.iterdir()):
        # Ghép timeline bằng cặp API; một mẫu U có thể khớp nhiều API cũ.
        timeline_path = library_dir / "migration_evolution_v2.json"
        if not timeline_path.exists():
            continue
        library = library_dir.name
        timelines = read_json(timeline_path)
        by_pair = {(t["seed"]["deprecated_api"], t["seed"]["replacement_api"]): t
                   for t in timelines}
        for kind in ("O", "U"):
            rows = read_json(library_dir / f"{kind}_with_versions.json")
            counts = Counter()
            for index, row in enumerate(rows):
                # index là vị trí mẫu trong file O/U_with_versions.json của thư viện.
                target = row["replacement api"] if kind == "U" else row["deprecated api"][0]
                matching = [by_pair[(old, row["replacement api"])]
                            for old in row["deprecated api"]
                            if (old, row["replacement api"]) in by_pair]
                call = row.get("api_call")
                counts["samples"] += 1
                counts["selected_versions"] += len(row["versions"])
                states_by_version = defaultdict(list)
                for timeline in matching:
                    for milestone in timeline["milestones"]:
                        state = next((s for s in milestone["api_states"] if s["name"] == target), None)
                        if state:
                            states_by_version[milestone["version"]].append(state)
                if not row["versions"]:
                    # 3. Mẫu rỗng: suy ra nguyên nhân từ dữ liệu cuối.
                    if not matching:
                        reason = "migration_timeline_missing"
                    elif not call:
                        reason = "api_call_not_found_after_probe"
                    elif not any(state["status"] in CALLABLE
                                 for states in states_by_version.values() for state in states):
                        reason = "no_callable_milestone"
                    else:
                        reason = "model_found_no_compatible_milestone"
                    counts["no_result"] += 1
                    empty_audit.append(dict(
                        library=library, set=kind, index=index,
                        target_api=target,
                        api_call=call or "",
                        **classify_empty(row, reason, kind, target,
                                         states_by_version, call)))
                # 4. Thử lời gọi với chữ ký hiệu lực của mọi mốc callable chưa chọn.
                for version, states in states_by_version.items():
                    if not row["versions"] or version in row["versions"] or not call:
                        continue
                    for state in states:
                        if state["status"] not in CALLABLE:
                            continue
                        effective_signature = state.get("effective_signature")
                        if not effective_signature:
                            continue
                        if incompatibility(call, effective_signature, target,
                                           row.get("alias dict")) == "":
                            possible_omissions.append(dict(
                                library=library, set=kind, index=index,
                                target_api=target,
                                api_call=call, version=version,
                                basis="effective_signature_accepts_call",
                                effective_signature=effective_signature))
                            break
                # 5. Mốc đã chọn: kiểm tra trạng thái và cách truyền đối số.
                for version in row["versions"]:
                    states = states_by_version.get(version, [])
                    if not states or not any(s["status"] in CALLABLE for s in states):
                        issue = dict(library=library, set=kind, index=index,
                                     target_api=target,
                                     api_call=call or "", version=version, category="unavailable",
                                     detail="No matched timeline confirms active/deprecated at this milestone",
                                     effective_signatures="")
                        issues.append(issue)
                        counts["unavailable"] += 1
                        continue
                    usable = [state for state in states if state["status"] in CALLABLE]
                    evidence = []
                    compatible = False
                    for state in usable:
                        effective_signature = state.get("effective_signature")
                        if not effective_signature:
                            compatible = True  # Thiếu chữ ký thì chưa thể chứng minh version sai.
                            continue
                        result = incompatibility(call, effective_signature, target,
                                                 row.get("alias dict"))
                        if result in (None, ""):
                            compatible = True
                        evidence.append((effective_signature, result))
                    if compatible or not evidence:
                        continue
                    # Ví dụ tsplot(x, data) không nhận tsplot(x): thiếu data.
                    category = "signature_mismatch"
                    detail = " | ".join(dict.fromkeys(error for _, error in evidence))
                    issue = dict(library=library, set=kind, index=index,
                                 target_api=target,
                                 api_call=call or "", version=version,
                                 category=category, detail=detail,
                                 effective_signatures=" | ".join(dict.fromkeys(sig for sig, _ in evidence)))
                    if target == "pandas.DataFrame.loc":
                        # loc là accessor; chữ ký getter không mô tả df.loc(axis=0).
                        manual_review.append(issue)
                        counts["accessor_needs_review"] += 1
                        continue
                    issues.append(issue)
                    counts[category] += 1
            stats.append((library, kind, counts))
    # 6. Ghi bốn bảng không trùng nhau và in thống kê ngắn.
    fields = ["library", "set", "index", "target_api", "api_call",
              "effective_signatures", "version", "category", "detail"]
    write_csv("invalid_selected_versions.csv", issues, fields)
    write_csv("accessor_needs_review.csv", manual_review, fields)
    write_csv("possible_omitted_versions.csv", possible_omissions, [
        "library", "set", "index", "target_api", "api_call",
        "effective_signature", "version", "basis"])
    write_csv("empty_samples_audit.csv", empty_audit, [
        "library", "set", "index", "target_api", "api_call",
        "reference", "candidate_versions", "compatible_versions",
        "uncertain_versions", "incompatible_versions", "reason", "classification"])
    for library, kind, counts in stats:
        print(library, kind, dict(counts))
    print("TOTAL", len(issues), "invalid selected versions;", len(empty_audit), "unresolved samples;",
          len(manual_review), "accessor review;", len(possible_omissions), "possible omissions")
    print("EMPTY AUDIT", dict(Counter(item["classification"] for item in empty_audit)))


if __name__ == "__main__":
    main()
