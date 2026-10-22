# API Evolution

## Cấu trúc thư mục

```text
API_Evolution/
├── crawl_pypi_versions.py       # Bước 1: lấy version
├── enrich_pypi_versions.py      # Bước 2: ghép Git tag và commit SHA
├── crawl_release_notes.py       # Bước 3: tải release notes
├── crawl_source.py              # Bước 4: tải và chuyển version source
├── build_api_source_tracks.py    # Bước 5: dựng đường tiến hóa source riêng cho từng API
├── source_evolution_rules.py     # Rule phân giải public API và AST trong Git source
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

## Bước 5 — Dựng đường tiến hóa source cho từng API

**Mô tả:** Với mỗi cặp trong `data/api_mapping_pairs.json`, quét Git source tại các version trong `data/pypi_versions_with_github_diy.json` và dựng riêng đường tiến hóa của từng API. Chỉ liệt kê version đã xác nhận API hiện diện; không phân tích docstring hoặc release notes.

**Chạy từng thư viện:**
```powershell
python build_api_source_tracks.py --library numpy
```

**Schema** cho `data/{library}/api_source_tracks.json` (mỗi phần tử là một cặp):

```text
mapping_id, library
└── api_states[]                         # 2 lịch sử API độc lập
    ├── role, api                       # deprecated_api | replacement_api
    ├── versions[]                      # chỉ version xác nhận có API
    │   ├── version, commit_sha
    │   ├── source_location             # path, line_start, line_end
    │   ├── public_binding              # kind, path, target
    │   ├── declared_parameters[]       # name, kind, default, annotation
    │   ├── effective_parameters        # trực tiếp = declared; wrapper/alias đã giải = đích; chưa rõ = null
    │   └── return_annotation, evidence_refs[]
    ├── transitions[]                   # chỉ giữa hai release liền kề có API
    │   └── from_version, to_version, status, changes[], evidence_refs[]
    ├── coverage                        # version có/vắng/chưa rõ; khoảng đã/chưa so sánh
    └── evidence[]                      # source location và Git diff theo ID
```

`changes: []` nghĩa là không phát hiện thay đổi theo các rule đã chạy; không nối qua version vắng mặt hoặc chưa phân giải. Mốc công bố deprecation thuộc pha đọc release notes, còn đường tiến hóa chung của cặp thuộc pha tổng hợp sau đó.
