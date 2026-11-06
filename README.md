# API Evolution

## Cấu trúc thư mục

```text
API_Evolution/
├── crawl_pypi_versions.py                       # Bước 1: lấy version từ PyPI
├── enrich_pypi_versions.py                      # Bước 2: ghép Git tag và commit SHA
├── crawl_release_notes.py                       # Bước 3: tải release notes
├── crawl_source.py                              # Bước 4: tải và chuyển version source
├── build_api_source_tracks.py                   # Bước 5: truy source và chữ ký khai báo
├── build_migration_evolution.py                 # Bước 6: tổng hợp đường tiến hóa cặp API
├── build_callsite_versions.py                   # Bước 7: gán mốc version cho mẫu gọi API O/U
├── audit_step7.py                               # Bước 8: rà soát version đã gán
├── build_step7_modified.py                      # Bước 8: tạo bộ mẫu đã sửa
├── source_evolution_rules.py                    # Quy tắc phân giải public API và AST
├── export_rules/                                # Quy tắc export chung và theo thư viện
│   ├── __init__.py
│   ├── python_export_rules.py
│   ├── dynamic_export_rules.py
│   ├── scipy_export_rules.py
│   ├── tensorflow_export_rules.py
│   ├── torch_binding_rules.py
│   └── transformers_export_rules.py
├── prompts/
│   ├── migration_evolution.txt                  # Prompt bước 6
│   └── callsite_version.txt                     # Prompt bước 7
├── data/
│   ├── icsme_data/
│   │   ├── O.json                               # Mẫu gọi API cũ
│   │   └── U.json                               # Mẫu gọi API thay thế
│   ├── api_mapping_pairs.json
│   ├── api_mapping_versions.json
│   ├── pypi_versions.json                       # Kết quả bước 1
│   ├── pypi_versions_with_github.json           # Kết quả bước 2
│   ├── pypi_versions_with_github_diy.json       # Bản chỉnh tay bước 2
│   ├── release_notes.json                       # Kết quả bước 3
│   └── {library}/
│       ├── api_source_tracks.json               # Kết quả bước 5
│       ├── migration_evolution.json             # Kết quả bước 6
│       ├── O_with_versions.json                 # Kết quả bước 7 (tập O)
│       ├── U_with_versions.json                 # Kết quả bước 7 (tập U)
│       ├── O_modified.json                      # Kết quả bước 8 (tập O)
│       ├── U_modified.json                      # Kết quả bước 8 (tập U)
│       └── *.progress.jsonl                     # Tiến độ bước 7 khi chạy
├── reports/step7/                               # Báo cáo rà soát bước 8
├── cache/{library}/                             # Dữ liệu tải về, không đưa vào Git
│   ├── docs/{version}/release_notes.html
│   └── source/                                  # Một Git repository cho mỗi thư viện
├── .gitattributes
├── .gitignore
├── README.md
└── requirements.txt
```

Các file kết quả và file tiến độ chỉ xuất hiện sau khi chạy bước tương ứng; `{library}` là tên thư viện, ví dụ `numpy`.

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

**Output:** `data/pypi_versions.json`, mỗi bản ghi có `library` và `version`.

## Bước 2 — Ghép Git tag và commit SHA

**Mô tả:** đọc output bước 1, tìm Git tag tương ứng trong repository của từng thư viện và lấy commit SHA. Script cũng xét tag khác cách viết nhưng tương đương về số version, chẳng hạn `0.3` và `v0.3.0`.

**Chạy toàn bộ:**

```powershell
python enrich_pypi_versions.py
```

**Output:** `data/pypi_versions_with_github.json`, giữ `library`, `version` và thêm `tag`, `commit_sha`, `source_url`. `source_url` trỏ tới cây mã nguồn theo commit SHA.

**Ngoại lệ / xử lý tay:**

- Loại `pandas` `0.1` và `0.2` vì không có tag tương ứng trong repository hiện tại
- Loại `transformers` `0.1` vì là package trùng tên, không thuộc Hugging Face
- Loại `transformers` `4.25.0` vì không tìm thấy tag và source code tương ứng trên GitHub.
- Với `transformers` `5.10.4`, PyPI ghi `5.10.4` nhưng [GitHub dùng tag `v5.10.3`](https://github.com/huggingface/transformers/releases/tag/v5.10.3), nên bản chỉnh tay dùng tag, SHA và URL mã nguồn của `v5.10.3`.

## Bước 3 — Lấy release notes

**Mô tả:** đọc bản chỉnh tay ở bước 2, tìm trang tài liệu hoặc trang GitHub theo từng version và lưu toàn bộ HTML vào `cache/`.

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

**Output:** `data/release_notes.json` giữ các trường đầu vào và thêm:

- `release_url` là URL trên web
- `release_path` là đường dẫn tương đối tới HTML trên máy.

## Bước 4 — Tải source code

**Mô tả:** mỗi thư viện tải source code tại Github, lưu tại `cache/{library}/source`; lần đầu lấy commit của nhánh mặc định. Dùng `--version` để chuyển sang commit SHA của version trong `data/pypi_versions_with_github_diy.json`.

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

**Mô tả:** đọc các cặp trong `data/api_mapping_pairs.json`, truy source ở từng commit của `data/pypi_versions_with_github_diy.json` và ghi đường tiến hóa riêng cho deprecated API và replacement API.

**Chiến lược tìm kiếm:**

- Xác thực commit/tag của release; dùng lại kết quả nếu danh sách đường dẫn và các file liên quan không đổi.
- Thử quy tắc export riêng của Transformers, rồi phân giải định nghĩa, import, alias, wildcard import và thành viên class bằng AST.
- Nếu tìm thấy API, xác định public binding. Nếu chưa, thử dynamic/lazy export rồi decorator export của TensorFlow.
- Nếu vẫn chưa xác nhận được, kiểm tra ufunc sinh mã của SciPy hoặc Python binding của PyTorch.
- Lấy chữ ký `def` đã khai báo; với alias hoặc hàm sao chép, lần theo hàm nguồn. Wrapper giữ chữ ký `def` của chính nó.
- Với accessor `DataFrame.loc` và ufunc `special.betainc`, ghi `signature: null` và cú pháp được source xác nhận vào `call_syntax`.

**Chạy riêng:**

```powershell
python build_api_source_tracks.py --library seaborn
```

**Output:** `data/{library}/api_source_tracks.json`.

**Schema output:**

```text
[]
└── mapping_id, library
    └── api_states[]                        # lịch sử API cũ và API thay thế
        ├── role, api
        ├── versions[]                      # các release xác nhận có API
        │   ├── version, commit_sha
        │   ├── source_location             # path, line_start, line_end
        │   ├── public_binding              # kind, path, target
        │   ├── signature, return_annotation
        │   ├── call_syntax                # cú pháp accessor/ufunc (nếu có)
        │   └── evidence_refs[]
        ├── transitions[]                   # so sánh hai release liền kề
        │   ├── from_version, to_version, status, evidence_refs[]
        │   └── changes[]                   # kind, scope, parameter/method, before, after, evidence_refs[]
        ├── coverage                        # version/khoảng có, vắng, chưa rõ
        └── evidence[]                      # source, binding, Git diff, so sánh AST theo ID
```

## Bước 6 — Tổng hợp đường tiến hóa của cặp migration

**Mô tả:** đọc `data/{library}/api_source_tracks.json` và `deprecated_version` trong `data/api_mapping_versions.json`. Cặp có `deprecated_version: null` được bỏ qua. Script tạo sự kiện deprecated tại version này; LLM chọn và nhóm các sự kiện đã có thành đường tiến hóa của cặp API.

**Chọn API gọi model (chọn một):**

OpenRouter:

```powershell
$env:OPENROUTER_API_KEY = "sk-or-v1-..."
$provider = "openrouter"
```

OpenAI trực tiếp:

```powershell
$env:OPENAI_API_KEY = "sk-..."
$provider = "openai"
```

**Chạy từng thư viện:**

```powershell
python build_migration_evolution.py --library numpy --mapping-id 1 --provider $provider --dry-run
python build_migration_evolution.py --library numpy --provider $provider
python build_migration_evolution.py --library numpy --provider $provider --resume
```

**Output:** `data/{library}/migration_evolution.json`. Khi chọn `--mapping-id`, script ghi `migration_evolution_mapping_{id}.json` để không ghi đè output toàn thư viện.

**Schema dự kiến:**

```text
[]                                           # Một phần tử cho mỗi cặp migration
└── mapping_id, library                      # ID cặp, tên thư viện
    ├── seed                                 # deprecated_api, replacement_api
    ├── deprecated_version: string           # Version có thông báo deprecated từ bước trước
    ├── milestones[]                         # Các mốc hợp nhất theo thứ tự version
    │   ├── version: string
    │   ├── compared_to_version: string|null # Mốc trước đó; null ở mốc đầu tiên
    │   ├── api_states[]                     # Trạng thái của cả hai API tại mốc này
    │   │   ├── name, status
    │   │   ├── signature: string|null
    │   │   └── call_syntax: object|null
    │   └── changes[]                        # Các thay đổi được chọn cho mốc này
    │       ├── api, type, parameter
    │       ├── old_value, new_value, from_version
    │       └── source, replace_to, source_evidence[]
    └── model, input_digest                   # Model đã dùng, mã băm của input
```

- `status`: `active`, `deprecated`, `removed`, `absent` hoặc `unknown`.
- Với API được xác nhận có mặt tại mốc, `signature` là chữ ký khai báo. Nếu `signature` là `null`, `call_syntax` chứa cú pháp gọi thay thế từ bước 5 (ví dụ indexer hoặc ufunc). Nếu có `signature`, `call_syntax` là `null`.
- Với API vắng mặt hoặc chưa xác định tại mốc, cả `signature` và `call_syntax` đều là `null`.
- `changes[]` ghi sự kiện deprecated, thay đổi source hoặc trạng thái API; `source_evidence[]` giữ bằng chứng từ bước 5 nếu có.
- `from_version` là version gốc của thay đổi, có thể khác `compared_to_version` của mốc.

## Bước 7 — Gán các mốc version khả dĩ cho lời gọi API

**Mô tả:** đọc `data/{library}/migration_evolution.json`. Với từng mẫu trong tập O (`data/icsme_data/O.json`) và tập U (`data/icsme_data/U.json`), trích lời gọi API hoặc cú pháp indexer, rồi dùng LLM chọn các mốc có trạng thái phù hợp. Model so lời gọi với `signature`; khi trường này là `null`, model dùng `call_syntax` của indexer hoặc ufunc.

**Chạy từng thư viện:**

```powershell
python build_callsite_versions.py --library numpy --file O --dry-run
python build_callsite_versions.py --library numpy --file O
python build_callsite_versions.py --library numpy --file U

# Hoặc chạy thử 10 mẫu O rồi tiếp tục (thay cho lệnh O đầy đủ ở trên).
python build_callsite_versions.py --library numpy --file O --max-samples 10
python build_callsite_versions.py --library numpy --file O --resume

# Mặc định tối đa 8 lời gọi model đồng thời; OpenAI tự giãn nhịp và thử lại khi gặp 429 tạm thời.
python build_callsite_versions.py --library numpy --file O --provider openai --workers 4 --resume
```

**Output:** `data/{library}/O_with_versions.json` hoặc `U_with_versions.json`.

- Mỗi mẫu giữ nguyên các trường đầu vào, thêm `api_call` (lời gọi API hoặc biểu thức indexer cùng đầy đủ đối số, hoặc `null` nếu không tìm thấy) ngay trước `versions[]`. Model dùng `api_call` thay cho `reference` để chọn các **mốc của timeline** phù hợp.
- `--max-samples` tạo output một phần; `--resume` dùng file tiến độ cùng phiên bản prompt để xử lý các mẫu còn lại. File tiến độ tạo trước thay đổi này cần chạy lại bằng `--overwrite`.

## Bước 8 — Rà soát và sửa version

**Mô tả:**

- `audit_step7.py` đối chiếu kết quả bước 7 với migration path (xem [báo cáo](reports/step7/README.md)).
- `build_step7_modified.py` dùng ba CSV đã rà soát `invalid_selected_versions.csv`, `possible_omitted_versions_diy.csv`, `empty_samples_reassessed.csv` để xóa/thêm `versions` và điền 4 `api_call` còn null.

**Chạy:**

```powershell
python audit_step7.py
python build_step7_modified.py
```

**Output:** các CSV trong `reports/step7/output/` và `data/{library}/O_modified.json`, `U_modified.json`.
