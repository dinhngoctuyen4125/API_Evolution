# API Evolution

## Cấu trúc thư mục

```text
API_Evolution/
├── crawl_pypi_versions.py       # Bước 1: lấy version
├── enrich_pypi_versions.py      # Bước 2: ghép Git tag và commit SHA
├── crawl_release_notes.py       # Bước 3: tải release notes
├── crawl_source.py              # Bước 4: tải và chuyển version source
├── trace_source_changes.py      # Bước 5: tìm thay đổi Python API trong source
├── data/                        # JSON kết quả; *_diy.json là bản chỉnh tay
├── cache/{library}/
│   ├── docs/{version}/release_notes.html
│   └── source/                  # Một Git repository cho mỗi thư viện
└── requirements.txt
```

## Chuẩn bị môi trường

```powershell
conda create -n api_crawl python=3.10 -y
conda activate api_crawl
pip install -r requirements.txt
git --version
```

Bước 4 cần chương trình Git (`git.exe`), cài trên Windows hoặc trong môi trường Conda. Sau khi kích hoạt môi trường, PowerShell phải chạy được lệnh `git --version`; Git không được cài qua `requirements.txt`.

## Bước 1 — Lấy version từ PyPI

**Mô tả:** đọc lịch sử phát hành của 8 thư viện trên PyPI. Script lấy các tên version chỉ gồm số và dấu chấm, như `X.Y`, `X.Y.Z`, rồi sắp xếp tăng dần theo từng thư viện.

**Chạy toàn bộ:**

```powershell
python crawl_pypi_versions.py
```

**Output:** `data/pypi_versions.json`, mỗi bản ghi có `library` và `version`. File kết quả này được Git ignore.

## Bước 2 — Ghép Git tag và commit SHA

**Mô tả:** đọc output bước 1, tìm Git tag tương ứng trong repository của từng thư viện và lấy commit SHA. Script cũng xét tag khác cách viết nhưng tương đương về số version, chẳng hạn `0.3` và `v0.3.0`.

**Chạy toàn bộ:**

```powershell
python enrich_pypi_versions.py
```

**Output:** `data/pypi_versions_with_github.json`, giữ `library`, `version` và thêm `tag`, `commit_sha`, `source_url`. `source_url` trỏ tới cây mã nguồn theo commit SHA. File kết quả này được Git ignore.

**Ngoại lệ / xử lý tay:** Bản hiện tại đã loại `pandas` `0.1` và `0.2` vì không có tag tương ứng trong repository hiện tại; loại `transformers` `0.1` vì là package trùng tên, không thuộc Hugging Face; loại `transformers` `4.25.0` vì không tìm thấy tag và source code tương ứng trên GitHub. Với `transformers` `5.10.4`, PyPI ghi `5.10.4` nhưng [GitHub dùng tag `v5.10.3`](https://github.com/huggingface/transformers/releases/tag/v5.10.3), nên bản chỉnh tay dùng tag, SHA và URL mã nguồn của `v5.10.3`.

## Bước 3 — Lấy trang release notes

**Mô tả:** đọc bản chỉnh tay ở bước 2, tìm trang tài liệu hoặc trang GitHub theo từng version và lưu toàn bộ HTML vào cache. Mặc định script xử lý cả 8 thư viện.

**Chạy toàn bộ:**

```powershell
python crawl_release_notes.py
```

**Chạy riêng:**

```powershell
# Một thư viện hoặc một version.
python crawl_release_notes.py --library transformers
python crawl_release_notes.py --library numpy --version 1.25.0

# Tải lại HTML của những bản được chọn.
python crawl_release_notes.py --library transformers --refresh

# Chạy 7 thư viện ngoài NumPy.
python crawl_release_notes.py --library pandas --library pytorch --library scipy --library scikit-learn --library seaborn --library tensorflow --library transformers
```

**Output:** `data/release_notes.json` giữ các trường đầu vào và thêm `release_url` (URL trên web), `release_path` (đường dẫn tương đối tới HTML trên máy).

## Bước 4 — Tải source code

**Mô tả:** dùng Git tải source code từ GitHub. Mỗi thư viện chỉ có một bản source tại `cache/{library}/source`; lần đầu lấy commit của nhánh mặc định. Dùng `--version` để chuyển sang commit SHA của version trong `data/pypi_versions_with_github_diy.json`.

**Chạy toàn bộ:**

```powershell
python crawl_source.py --all
```

**Chạy riêng:**

```powershell
# Một thư viện.
python crawl_source.py --library numpy

# Chuyển source sang commit của một version.
python crawl_source.py --library numpy --version 1.4.1
```

**Output:** `cache/{library}/source`, một Git repository cho mỗi thư viện.

## Bước 5 — Phát hiện thay đổi Python API từ source code

**Mô tả:** So sánh các version liền kề của từng cặp API trên Git source để tạo candidate thay đổi và bằng chứng đi kèm. Bước này không phân tích docstring.

**Input:** `data/api_mapping_pairs.json`, `data/pypi_versions_with_github_diy.json` và repository `cache/{library}/source` từ Bước 4.

**Chạy riêng từng thư viện:**

```powershell
$library = "numpy"
python trace_source_changes.py --library $library `
  --output "data/$library/source_change_candidates.json" `
  --unresolved-output "data/$library/source_trace_unresolved.json" `
  --coverage-output "data/$library/source_trace_coverage.json"
```

Đổi giá trị `$library` để chạy thư viện khác, ví dụ `pandas` hoặc `scikit-learn`.

**Output:**

- `source_change_candidates.json`: các khoảng version có candidate thay đổi và bằng chứng source.
- `source_trace_unresolved.json`: các khoảng chưa phân tích đầy đủ và nguyên nhân.
- `source_trace_coverage.json`: trạng thái kiểm tra của từng khoảng version đã thu thập.

Trong `source_change_candidates.json`, trường `effective_*` vắng mặt lấy giá trị trường gốc, trạng thái runtime vắng mặt là chưa kiểm tra; mốc quan hệ đầu tiên nằm ở `source_trace_coverage.json`.

**Các rule kiểm tra:**

- **Version và Git:** Đối chiếu tag, SHA và source URL trong input với Git; ví dụ tag `v1.25.0` phải trỏ đúng commit đã ghi.
- **Public API:** Đọc import, `__all__` và `__getattr__` để xét tên có được xuất ra không; ví dụ `numpy.alltrue` vắng mặt ở NumPy 2.0.
- **Signature:** Đọc AST của hàm hoặc đích wrapper để so sánh tham số; ví dụ `x` đổi tên thành `a`, hoặc `keepdims=False` đổi thành `keepdims=np._NoValue`.
- **Delegation:** Lần theo lời gọi `return` và biến cục bộ trong AST để xác định đích; ví dụ `all = a.all; return all(...)` gọi method `a.all`.
- **Forwarding:** So sánh argument của lời gọi và `**kwargs`; ví dụ `keepdims` từ luôn được truyền sang chỉ truyền khi được cung cấp.
- **Implementation:** So sánh AST thân hàm sau khi bỏ docstring; ví dụ `arr.all(...)` đổi sang `_wrapreduction(...)`, chưa kết luận hành vi đổi.
- **Helper:** Theo lời gọi tới helper và tối đa một wrapper phía trên; ví dụ `_wrapreduction` đổi được ghi cho cả `numpy.all` và `numpy.alltrue`, với tác động hành vi chưa xác định.
- **Di chuyển source:** So đường dẫn symbol và AST trước/sau; ví dụ `numpy/core/fromnumeric.py` chuyển sang `numpy/_core/fromnumeric.py`.
- **Deprecation:** Tìm `warnings.warn` trong hàm hoặc decorator; ví dụ dispatcher của `alltrue` thêm cảnh báo deprecated.
- **Migration:** Đối chiếu cặp API trong input với alias/wrapper giải được từ AST; ví dụ `product` gọi `prod`, còn khi `product` bị xóa thì quan hệ không còn kiểm tra được.
- **Evidence:** Ghép Git diff với vị trí AST của đúng hàm; ví dụ diff của `alltrue` không lấy code của `sum`.
- **Coverage:** Kiểm tra từng cặp version liền kề đã thu thập; ví dụ khoảng không có event được ghi `unchanged`, khoảng thiếu source là `unresolved`.
- **Runtime tùy chọn:** Dùng interpreter được cấp để kiểm tra tên và signature; ví dụ kiểm tra `np.alltrue` còn truy cập được ở NumPy 2.0 không.
