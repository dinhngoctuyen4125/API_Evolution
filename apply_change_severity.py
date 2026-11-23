"""Thêm `change_severity` vào mỗi sự kiện trong `milestones[].changes[]`.

Đọc data/{library}/migration_evolution.json và ghi data/{library}/migration_evolution_modified.json.
File gốc không bị sửa. Không gọi LLM; mức độ suy ra hoàn toàn từ `type`,
`old_value` và `new_value` của từng sự kiện.

Chạy:
    python apply_change_severity.py                  # cả 8 thư viện
    python apply_change_severity.py numpy pandas     # một số thư viện

Ba mức, theo phần nào của API thay đổi:
    major     tập tên hoặc đường dẫn truy cập API thay đổi (xuất hiện, biến mất,
              đổi tên, đổi alias, thêm hoặc bớt method).
    moderate  tên và đường dẫn giữ nguyên, nhưng chữ ký gọi đổi: số lượng, tên,
              thứ tự, loại tham số; bắt buộc <-> tùy chọn; kiểu dữ liệu đổi thật;
              cú pháp gọi.
    low       tên, đường dẫn và cấu trúc tham số giữ nguyên; chỉ đổi giá trị
              default hoặc thay đổi không nhìn thấy ở interface.
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
INPUT_NAME = "migration_evolution.json"
OUTPUT_NAME = "migration_evolution_modified.json"
LEVELS = ("major", "moderate", "low")

# Loại sự kiện làm thay đổi tập tên/đường dẫn truy cập API.
MAJOR_TYPES = {"deprecated", "removed", "introduced",
               "declared_method_added", "declared_method_removed"}
# Loại sự kiện làm thay đổi chữ ký gọi, bất kể giá trị cụ thể.
MODERATE_TYPES = {"parameter_added", "parameter_removed", "parameter_rename",
                  "parameter_order_changed", "parameter_kind_changed",
                  "call_syntax_changed"}
# Loại sự kiện chỉ so annotation hai phía (cả hai phải có mới tính là đổi kiểu).
ANNOTATION_TYPES = {"parameter_annotation_changed", "return_annotation_changed",
                    "declared_method_return_annotation_changed"}
# Loại sự kiện không chạm vào tên, đường dẫn hay cấu trúc tham số.
LOW_TYPES = {"implementation_changed", "source_path_changed", "error_message_changed",
             "class_bases_changed", "definition_kind_changed",
             "assignment_expression_changed", "assignment_targets_changed"}


def normalize_annotation(annotation):
    """Đưa annotation về tập các thành phần để bỏ qua khác biệt chỉ về cú pháp.

    Optional[X], Union[X, Y], X | Y, Tuple/tuple, dấu nháy và typing. đều quy về một dạng.
    """
    text = str(annotation).strip().strip("'\"").replace("typing.", "")
    text = re.sub(r"\bOptional\[(.+)\]", r"\1|None", text)
    text = re.sub(r"\b(Tuple|List|Dict|Set|FrozenSet|Type)\b",
                  lambda match: match.group(1).lower(), text)
    text = re.sub(r"\s+", "", text)
    union = re.fullmatch(r"Union\[(.+)\]", text)
    if union:
        text = union.group(1).replace(",", "|")
    return frozenset(text.replace("'", "").replace('"', "").split("|"))


def annotation_really_changed(old, new):
    """Kiểu đổi thật: cả hai phía có annotation và khác nhau sau chuẩn hóa.

    Thêm mới hoặc bỏ annotation không đổi kiểu khi chạy nên không tính.
    """
    return old is not None and new is not None \
        and normalize_annotation(old) != normalize_annotation(new)


def load_json(text, change, field):
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{change['type']} của {change['api']}: {field} không phải JSON hợp lệ") from exc


def export_names(text, change, field):
    """Tập (nhánh, tên export) của một public binding kiểu tf_export; None nếu không có."""
    names = load_json(text, change, field).get("export_names")
    return None if names is None else {(branch, name) for branch in names
                                      for name in names[branch]}


def method_severity(change):
    """declared_method_implementation_changed: so chữ ký hai phiên bản của method."""
    old = load_json(change["old_value"], change, "old_value")
    new = load_json(change["new_value"], change, "new_value")
    old_params, new_params = old["parameters"], new["parameters"]

    def structure(param):  # tên, loại tham số, có hay không có default
        return param["name"], param["kind"], param["default"] is not None

    if [structure(p) for p in old_params] != [structure(p) for p in new_params]:
        return "moderate"
    if any(annotation_really_changed(a["annotation"], b["annotation"])
           for a, b in zip(old_params, new_params)):
        return "moderate"
    if annotation_really_changed(old.get("return_annotation"), new.get("return_annotation")):
        return "moderate"
    return "low"  # chỉ đổi giá trị default hoặc thân hàm


def classify(change):
    """Trả về major | moderate | low cho một sự kiện; ném KeyError nếu `type` lạ."""
    kind, old, new = change["type"], change["old_value"], change["new_value"]
    if kind in MAJOR_TYPES:
        return "major"
    if kind in MODERATE_TYPES:
        return "moderate"
    if kind in LOW_TYPES:
        return "low"
    if kind == "default_changed":
        # `null` nghĩa là tham số không có default: đổi giữa bắt buộc và tùy chọn.
        return "moderate" if old is None or new is None else "low"
    if kind in ANNOTATION_TYPES:
        return "moderate" if annotation_really_changed(old, new) else "low"
    if kind == "declared_method_implementation_changed":
        return method_severity(change)
    if kind == "declared_methods_changed":
        old_methods = set(load_json(old, change, "old_value"))
        new_methods = set(load_json(new, change, "new_value"))
        return "major" if old_methods != new_methods else "low"
    if kind == "public_binding_changed":
        old_names = export_names(old, change, "old_value")
        new_names = export_names(new, change, "new_value")
        changed = old_names is not None and new_names is not None and old_names != new_names
        return "major" if changed else "low"
    raise KeyError(f"Loại sự kiện chưa có quy tắc: {kind!r}")


def with_severity(change):
    """Trả về bản sao của sự kiện với `change_severity` đặt ngay sau `type`."""
    severity = classify(change)
    result = {}
    for key, value in change.items():
        if key == "change_severity":  # chạy lại trên file đã có trường này thì ghi đè
            continue
        result[key] = value
        if key == "type":
            result["change_severity"] = severity
    if "change_severity" not in result:
        raise KeyError("Sự kiện không có trường `type`")
    return result


def convert(source, destination, counts):
    mappings = json.loads(source.read_text(encoding="utf-8"))
    for mapping in mappings:
        for milestone in mapping["milestones"]:
            milestone["changes"] = [with_severity(change) for change in milestone["changes"]]
            for change in milestone["changes"]:
                counts[change["change_severity"]] += 1
    temporary = destination.with_name(destination.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        # Giữ CRLF như file gốc để diff chỉ hiện dòng `change_severity` mới.
        stream.write(json.dumps(mappings, ensure_ascii=False, indent=2).replace("\n", "\r\n")
                     + "\r\n")
    temporary.replace(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("libraries", nargs="*",
                        help="tên thư viện; bỏ trống để xử lý mọi thư mục có "
                             f"{INPUT_NAME}")
    args = parser.parse_args()
    libraries = args.libraries or sorted(
        path.parent.name for path in DATA.glob(f"*/{INPUT_NAME}"))
    if not libraries:
        parser.exit(1, f"Error: không tìm thấy {INPUT_NAME} trong {DATA}\n")

    totals, per_library = Counter(), defaultdict(Counter)
    try:
        for library in libraries:
            source = DATA / library / INPUT_NAME
            if not source.is_file():
                raise FileNotFoundError(f"Không có file {source}")
            convert(source, source.with_name(OUTPUT_NAME), per_library[library])
            totals.update(per_library[library])
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")

    print(f"{'library':14s}" + "".join(f"{level:>10s}" for level in LEVELS) + f"{'total':>10s}")
    for library in libraries:
        counts = per_library[library]
        print(f"{library:14s}" + "".join(f"{counts[level]:10d}" for level in LEVELS)
              + f"{sum(counts.values()):10d}")
    print(f"{'ALL':14s}" + "".join(f"{totals[level]:10d}" for level in LEVELS)
          + f"{sum(totals.values()):10d}", file=sys.stdout)


if __name__ == "__main__":
    main()
