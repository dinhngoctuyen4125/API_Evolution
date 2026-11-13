"""Đối chiếu version bước 7 với chữ ký API trong migration path bước 6.

Kiểm tra tĩnh, không gọi thư viện hay LLM. Chạy: python audit_step7.py
"""

import ast
from collections import Counter, defaultdict
import csv
from functools import lru_cache
import inspect
from itertools import product
import json
from pathlib import Path
import re
import warnings


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
        declaration = "def f" + value[value.index("("):] + ":\n pass"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            try:
                definition = ast.parse(declaration)
            except SyntaxError:
                # PyTorch dùng ký hiệu kiểu ngoài cú pháp Python: str?, Tensor[].
                declaration = re.sub(r"\b([\w.]+)\[\](?=\s*[,)=])",
                                     r"list[\1]", declaration)
                declaration = re.sub(r"\b([\w.]+(?:\[[^\]]+\])?)\?(?=\s*[,)=])",
                                     r"Optional[\1]", declaration)
                definition = ast.parse(declaration)
        args = definition.body[0].args
        parameters = []
        positional = args.posonlyargs + args.args
        first_default = len(positional) - len(args.defaults)
        for i, arg in enumerate(args.posonlyargs):
            parameters.append(inspect.Parameter(arg.arg, inspect.Parameter.POSITIONAL_ONLY,
                                                default=SENTINEL if i >= first_default else inspect.Parameter.empty,
                                                annotation=ast.unparse(arg.annotation) if arg.annotation else inspect.Parameter.empty))
        for i, arg in enumerate(args.args, start=len(args.posonlyargs)):
            parameters.append(inspect.Parameter(arg.arg, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                                default=SENTINEL if i >= first_default else inspect.Parameter.empty,
                                                annotation=ast.unparse(arg.annotation) if arg.annotation else inspect.Parameter.empty))
        if args.vararg:
            parameters.append(inspect.Parameter(args.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
        for arg, default in zip(args.kwonlyargs, args.kw_defaults):
            parameters.append(inspect.Parameter(arg.arg, inspect.Parameter.KEYWORD_ONLY,
                                                default=SENTINEL if default is not None else inspect.Parameter.empty,
                                                annotation=ast.unparse(arg.annotation) if arg.annotation else inspect.Parameter.empty))
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
        node = parse_expression(value)
        if not isinstance(node, ast.Call):
            return None
        positional = sum(not isinstance(arg, ast.Starred) for arg in node.args)
        keywords = [kw.arg for kw in node.keywords if kw.arg is not None]
        uncertain = any(isinstance(arg, ast.Starred) for arg in node.args) or any(
            kw.arg is None for kw in node.keywords)
        return positional, keywords, uncertain, ast.unparse(node.func), node
    except (SyntaxError, ValueError):
        return None


def parse_expression(value):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(value, mode="eval").body


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
    """Thử bind: '' = có cách gán hợp lệ, chuỗi = mâu thuẫn, None = không phân tích được."""
    parsed = parse_call(call)
    signature = parse_signature(signature_text)
    if parsed is None or signature is None:
        return None
    positional, keywords, uncertain, callee, node = parsed
    # Chỉ so chữ ký API mà alias dict thực sự trỏ tới (nếu có ánh xạ).
    if aliases and aliases.get(callee, target) != target:
        return "alias points to another API"
    if len(keywords) != len(set(keywords)):
        return "duplicate keyword argument"
    kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
    errors = []
    for mode in binding_modes(signature, callee, target):
        if uncertain:
            result = bind_with_expansions(mode, node, kwargs)
            if result == "":
                return ""
            errors.append(result)
            continue
        try:
            mode.bind(*node.args, **kwargs)
            return ""
        except TypeError as exc:
            errors.append(str(exc))
    return errors[0] if errors else None


def bind_with_expansions(signature, node, explicit_kwargs):
    """Tìm một cách mở rộng *args/**kwargs có thể khớp chữ ký."""
    starred = sum(isinstance(arg, ast.Starred) for arg in node.args)
    has_kwargs = any(kw.arg is None for kw in node.keywords)
    fixed_slots = sum(p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                 inspect.Parameter.POSITIONAL_OR_KEYWORD)
                      for p in signature.parameters.values())
    known_positional = len(node.args) - starred
    max_extra = max(0, fixed_slots - known_positional)
    for lengths in product(range(max_extra + 1), repeat=starred):
        if sum(lengths) > max_extra:
            continue
        sizes = iter(lengths)
        args = []
        for arg in node.args:
            args.extend([SENTINEL] * (next(sizes) if isinstance(arg, ast.Starred) else 1))
        try:
            partial = signature.bind_partial(*args, **explicit_kwargs)
        except TypeError:
            continue
        kwargs = dict(explicit_kwargs)
        missing = False
        for param in signature.parameters.values():
            if param.name in partial.arguments or param.default is not inspect.Parameter.empty \
                    or param.kind in (inspect.Parameter.VAR_POSITIONAL,
                                      inspect.Parameter.VAR_KEYWORD):
                continue
            if has_kwargs and param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                             inspect.Parameter.KEYWORD_ONLY):
                kwargs[param.name] = SENTINEL
            else:
                missing = True
                break
        if missing:
            continue
        try:
            signature.bind(*args, **kwargs)
            return ""
        except TypeError:
            continue
    return "no expansion of *args/**kwargs can satisfy the signature"


def syntax_incompatibility(call, syntax, target, aliases=None):
    """Kiểm tra hai dạng call_syntax của bước 6; None là chưa kết luận được."""
    if not call or not isinstance(syntax, dict):
        return None
    try:
        node = parse_expression(call)
    except (SyntaxError, TypeError, ValueError):
        return None
    if syntax.get("kind") == "indexer":
        if not isinstance(node, ast.Subscript):
            return "not an indexer"
        value = node.value
        if isinstance(value, ast.Attribute) and value.attr == "loc":
            form, callee = "obj.loc[key]", ast.unparse(value)
        elif isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) \
                and value.func.attr == "loc":
            form, callee = "obj.loc(axis=axis)[key]", ast.unparse(value.func)
            named = [kw.arg for kw in value.keywords if kw.arg is not None]
            if any(not isinstance(arg, ast.Starred) for arg in value.args) \
                    or any(name != "axis" for name in named) \
                    or len(named) != len(set(named)) \
                    or ("axis" not in named and not any(
                        kw.arg is None for kw in value.keywords)):
                return "unsupported indexer arguments"
        else:
            return "unsupported indexer form"
        if aliases and aliases.get(callee, target) != target:
            return "alias points to another API"
        return "" if form in syntax.get("forms", []) else "indexer form not listed"
    if syntax.get("kind") == "ufunc":
        if not isinstance(node, ast.Call):
            return "not a call"
        callee = ast.unparse(node.func)
        if aliases and aliases.get(callee, target) != target:
            return "alias points to another API"
        required = syntax.get("required_inputs")
        if not isinstance(required, int):
            return None
        if node.keywords and not syntax.get("inputs_positional_only"):
            return None
        known = sum(not isinstance(arg, ast.Starred) for arg in node.args)
        starred = any(isinstance(arg, ast.Starred) for arg in node.args)
        if known < required and not starred:
            return f"expected at least {required} positional inputs"
        if node.keywords:
            if (starred or known != required
                    or [kw.arg for kw in node.keywords] != ["dtype"]):
                return None
        return "" if known <= required else None
    return None


def state_incompatibility(call, state, target, aliases=None):
    signatures = state.get("signatures")
    if signatures:
        results = [incompatibility(call, value, target, aliases)
                   for value in signatures]
        if "" in results:
            return ""
        return None if None in results else results[0]
    return syntax_incompatibility(call, state.get("call_syntax"), target, aliases)


def state_evidence(state):
    return json.dumps(state["signatures"], ensure_ascii=False) if state.get(
        "signatures") else json.dumps(state.get("call_syntax"), ensure_ascii=False)


def classify_empty(row, reason, kind, target, states_by_version, call):
    """Phân loại versions=[] theo signature hoặc call_syntax của từng mốc."""
    candidate, compatible, uncertain, incompatible = [], [], [], []
    for version, states in states_by_version.items():
        usable = [state for state in states if state["status"] in CALLABLE]
        if not usable:
            continue
        candidate.append(version)
        results = [state_incompatibility(call, state, target,
                                         row.get("alias dict"))
                   for state in usable]
        if "" in results:
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
            classification = "no_compatible_milestone"
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
    possible_omissions = []
    empty_audit = []
    stats = []
    # 2. Ghép từng mẫu O/U với timeline theo cặp API.
    source_paths = sorted(DATA.glob("*/*_with_versions.json"))
    if not source_paths:
        raise ValueError("No step-7 output files found")
    for library_dir in sorted({path.parent for path in source_paths}):
        # Ghép timeline bằng cặp API; một mẫu U có thể khớp nhiều API cũ.
        timeline_path = library_dir / "migration_evolution.json"
        if not timeline_path.is_file():
            raise ValueError(f"Missing step-6 timeline: {timeline_path}")
        library = library_dir.name
        timelines = read_json(timeline_path)
        by_pair = {(t["seed"]["deprecated_api"], t["seed"]["replacement_api"]): t
                   for t in timelines}
        for kind in ("O", "U"):
            source = library_dir / f"{kind}_with_versions.json"
            if not source.is_file():
                raise ValueError(f"Missing step-7 output: {source}")
            rows = read_json(source)
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
                # 4. Thử lời gọi với signature/call_syntax của mốc chưa chọn.
                for version, states in states_by_version.items():
                    if not row["versions"] or version in row["versions"] or not call:
                        continue
                    for state in states:
                        if state["status"] not in CALLABLE:
                            continue
                        if state_incompatibility(call, state, target,
                                                 row.get("alias dict")) == "":
                            possible_omissions.append(dict(
                                library=library, set=kind, index=index,
                                target_api=target,
                                api_call=call, version=version,
                                basis="signature_or_call_syntax_accepts_call",
                                evidence=state_evidence(state)))
                            break
                # 5. Mốc đã chọn: kiểm tra trạng thái và cách truyền đối số.
                for version in row["versions"]:
                    states = states_by_version.get(version, [])
                    if not states or not any(s["status"] in CALLABLE for s in states):
                        issue = dict(library=library, set=kind, index=index,
                                     target_api=target,
                                     api_call=call or "", version=version, category="unavailable",
                                     detail="No matched timeline confirms active/deprecated at this milestone",
                                     evidence="")
                        issues.append(issue)
                        counts["unavailable"] += 1
                        continue
                    usable = [state for state in states if state["status"] in CALLABLE]
                    evidence = []
                    compatible = False
                    uncertain = False
                    for state in usable:
                        result = state_incompatibility(call, state, target,
                                                       row.get("alias dict"))
                        compatible |= result == ""
                        uncertain |= result is None
                        evidence.append((state_evidence(state), result))
                    if compatible:
                        continue
                    if uncertain:
                        counts["insufficient_evidence_selected"] += 1
                        continue
                    # Ví dụ tsplot(x, data) không nhận tsplot(x): thiếu data.
                    category = "signature_mismatch"
                    detail = " | ".join(dict.fromkeys(error for _, error in evidence if error))
                    issue = dict(library=library, set=kind, index=index,
                                 target_api=target,
                                 api_call=call or "", version=version,
                                 category=category, detail=detail,
                                 evidence=" | ".join(dict.fromkeys(sig for sig, _ in evidence)))
                    issues.append(issue)
                    counts[category] += 1
            stats.append((library, kind, counts))
    # 6. Ghi ba bảng và in thống kê ngắn.
    fields = ["library", "set", "index", "target_api", "api_call",
              "version", "category", "detail", "evidence"]
    write_csv("invalid_selected_versions.csv", issues, fields)
    write_csv("possible_omitted_versions.csv", possible_omissions, [
        "library", "set", "index", "target_api", "api_call",
        "evidence", "version", "basis"])
    write_csv("empty_samples_audit.csv", empty_audit, [
        "library", "set", "index", "target_api", "api_call",
        "reference", "candidate_versions", "compatible_versions",
        "uncertain_versions", "incompatible_versions", "reason", "classification"])
    for library, kind, counts in stats:
        print(library, kind, dict(counts))
    print("TOTAL", len(issues), "invalid selected versions;", len(empty_audit), "empty samples;",
          len(possible_omissions), "possible omissions")
    print("EMPTY AUDIT", dict(Counter(item["classification"] for item in empty_audit)))


if __name__ == "__main__":
    main()
