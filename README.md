# API Evolution

## Cấu trúc thư mục

```text
API_Evolution/
├── crawl_pypi_versions.py                       # Bước 1: lấy version từ PyPI
├── enrich_pypi_versions.py                      # Bước 2: ghép Git tag và commit SHA
├── crawl_release_notes.py                       # Bước 3: tải release notes
├── crawl_source.py                              # Bước 4: tải và chuyển version source
├── build_api_source_tracks_v2.py                # Bước 5: truy source và chữ ký hiệu dụng
├── build_migration_evolution_v2.py              # Bước 6: tổng hợp đường tiến hóa cặp API
├── build_callsite_versions.py                   # Bước 7: gán mốc version cho mẫu gọi API O/U
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
│       ├── api_source_tracks_v2.json            # Kết quả bước 5
│       ├── migration_evolution_v2.json          # Kết quả bước 6
│       ├── O_with_versions.json                 # Kết quả bước 7 (tập O)
│       ├── U_with_versions.json                 # Kết quả bước 7 (tập U)
│       └── *.progress.jsonl                     # Tiến độ bước 7 khi chạy
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

- Với Transformers, thử quy tắc export riêng trước.
- Với các API còn lại, tái sử dụng kết quả phân giải nếu danh sách đường dẫn và các file đã đọc không đổi; nếu không, truy định nghĩa, import, alias, wildcard import và thành viên class bằng AST.
- Khi đã tìm thấy API, xác định điểm public binding; tái sử dụng binding đã lưu nếu file export không đổi.
- Nếu chưa tìm thấy, thử các cơ chế lazy export được hỗ trợ, rồi thử decorator export đối với TensorFlow.
- Nếu vẫn chưa xác nhận được API, kiểm tra ufunc sinh mã của SciPy hoặc Python binding của PyTorch.
- Với API đã xác nhận, lần theo wrapper và alias nhận diện được để lấy chữ ký hiệu dụng; trường hợp không đủ căn cứ được ghi là chưa phân giải.

**Chạy từng thư viện:**

```powershell
python build_api_source_tracks_v2.py --library seaborn

python build_api_source_tracks_v2.py --library numpy --mapping-id 3 --from-version 1.15.0 --to-version 1.15.1 --output data/numpy/probe_v2.json
```

**Output:** `data/{library}/api_source_tracks_v2.json`.

**Schema dự kiến:**

```text
[]
└── mapping_id, library
    └── api_states[]                        # lịch sử API cũ và API thay thế
        ├── role, api
        ├── versions[]                      # các release xác nhận có API
        │   ├── version, commit_sha
        │   ├── source_location             # path, line_start, line_end
        │   ├── public_binding              # kind, path, target
        │   ├── signature, effective_signature, return_annotation
        │   └── evidence_refs[]; generated_signatures nếu có
        ├── transitions[]                   # so sánh hai release liền kề
        │   ├── from_version, to_version, status, evidence_refs[]
        │   └── changes[]                   # kind, scope, parameter/method, before, after, evidence_refs[]
        ├── coverage                        # version/khoảng có, vắng, chưa rõ; chữ ký chưa giải được
        └── evidence[]                      # source, binding, Git diff, so sánh AST theo ID
```

## Bước 6 — Tổng hợp đường tiến hóa của cặp migration

**Mô tả:** dùng LLM ghép `deprecated_version` và đường tiến hóa của hai API ở bước 5 thành đường tiến hóa hoàn chỉnh cho từng cặp migration.

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
python build_migration_evolution_v2.py --library numpy --mapping-id 1 --provider $provider --dry-run
python build_migration_evolution_v2.py --library numpy --provider $provider
python build_migration_evolution_v2.py --library numpy --provider $provider --resume
```

**Output:** `data/{library}/migration_evolution_v2.json`. Khi chọn `--mapping-id`, script ghi `migration_evolution_v2_mapping_{id}.json` để không ghi đè output toàn thư viện.

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
    │   │   └── name, status, signature, effective_signature
    │   └── changes[]                        # Các thay đổi được chọn cho mốc này
    │       ├── api, type, parameter
    │       ├── old_value, new_value, from_version
    │       └── source, delegates_to, source_evidence[]
    └── model, input_digest                   # Model đã dùng, mã băm của input
```

- `status`: `active`, `deprecated`, `removed`, `absent` hoặc `unknown`.
- `signature` là chữ ký khai báo; `effective_signature` là chữ ký sau khi giải wrapper hoặc alias, có thể là `null`.
- `changes[]` ghi thay đổi tham số, wrapper, implementation hoặc trạng thái API; `source_evidence[]` giữ bằng chứng từ bước 5 nếu có.
- `from_version` là version gốc của thay đổi, có thể khác `compared_to_version` của mốc.

## Bước 7 — Gán các mốc version khả dĩ cho lời gọi API

**Mô tả:** với từng mẫu trong tập O (`data/icsme_data/O.json`) và tập U (`data/icsme_data/U.json`), trích lời gọi API cùng đầy đủ tham số, rồi dùng LLM chọn các mốc có trạng thái và signature phù hợp.

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

- Mỗi mẫu giữ nguyên các trường đầu vào, thêm `api_call` (lời gọi API cùng đầy đủ tham số, hoặc `null` nếu không tìm thấy) ngay trước `versions[]`. Model dùng `api_call` thay cho `reference` để chọn các **mốc của timeline** phù hợp.
- `--max-samples` tạo output một phần; `--resume` dùng file tiến độ cùng phiên bản prompt để xử lý các mẫu còn lại. File tiến độ tạo trước thay đổi này cần chạy lại bằng `--overwrite`.
