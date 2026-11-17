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
├── github_crawler.py                            # Bước 9: crawl mẫu gọi API từ GitHub
├── build_crawled_versions.py                    # Bước 10: ghép mẫu crawl và gán thêm mốc
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
│   └── callsite_version.txt                     # Prompt bước 7 và bước 10
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
│       ├── O_crawled_dependency.json            # Kết quả bước 9 (tập O, mốc từ dependency)
│       ├── U_crawled_dependency.json            # Kết quả bước 9 (tập U, mốc từ dependency)
│       ├── O_crawled_not_dependency.json        # Kết quả bước 9 (tập O, chưa có mốc)
│       ├── U_crawled_not_dependency.json        # Kết quả bước 9 (tập U, chưa có mốc)
│       ├── O_crawled.json                       # Kết quả bước 10 (tập O)
│       ├── U_crawled.json                       # Kết quả bước 10 (tập U)
│       ├── *.cache.jsonl                        # Cache câu trả lời model bước 10
│       └── *.progress.jsonl                     # Tiến độ bước 7 khi chạy
├── reports/step7/                               # Báo cáo rà soát bước 8
├── cache/step3,4/{library}/                     # Dữ liệu tải ở bước 3 và 4
│   ├── docs/{version}/release_notes.html
│   └── source/                                  # Một Git repository cho mỗi thư viện
├── cache/step9/                                 # Repository GitHub tải ở bước 9
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

Bước 4 cần chương trình Git (`git.exe`), nên sau khi kích hoạt môi trường, PowerShell phải chạy được lệnh `git --version`.

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

**Mô tả:** đọc bản chỉnh tay ở bước 2, tìm trang tài liệu hoặc trang GitHub theo từng version.

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
- `release_path` là đường dẫn tương đối từ thư mục dự án, dạng `cache/step3,4/{library}/docs/{version}/release_notes.html`.

## Bước 4 — Tải source code

**Mô tả:** mỗi thư viện tải source code tại Github, lần đầu lấy commit của nhánh mặc định. Dùng `--version` để chuyển sang commit SHA của version trong `data/pypi_versions_with_github_diy.json`.

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

**Output:** `cache/step3,4/{library}/source`, một Git repository cho mỗi thư viện.

## Bước 5 — Dựng đường tiến hóa source cho từng API

**Mô tả:** đọc các cặp trong `data/api_mapping_pairs.json`, truy source ở từng commit của `data/pypi_versions_with_github_diy.json` và ghi đường tiến hóa riêng cho deprecated API và replacement API.

**Chiến lược tìm kiếm:**

1. Xác thực commit/tag của release; dùng lại kết quả nếu danh sách đường dẫn và các file liên quan không đổi.

2. Thử quy tắc export riêng của Transformers, rồi phân giải định nghĩa, import, alias, wildcard import và thành viên class bằng AST.

3. Nếu tìm thấy API, xác định public binding. Nếu chưa, thử dynamic/lazy export rồi decorator export của TensorFlow.

4. Nếu vẫn chưa xác nhận được, kiểm tra ufunc sinh mã của SciPy hoặc Python binding của PyTorch.

5. Lấy chữ ký `def` đã khai báo; với alias hoặc hàm sao chép, lần theo hàm nguồn. Wrapper giữ chữ ký `def` của chính nó.

6. Với accessor `DataFrame.loc` và ufunc `special.betainc`, ghi cú pháp được source xác nhận vào `call_syntax`; `signatures` chứa các chữ ký xác định được, hoặc `[]` nếu không có.

**Chạy riêng:**

```powershell
python build_api_source_tracks.py --library seaborn
```

**Output:** `data/{library}/api_source_tracks.json`.

**Schema output:**

```text
[]
└── mapping_id, library
    └── api_states[]                             # lịch sử API cũ và API thay thế
        ├── role, api
        ├── versions[]                           # các release xác nhận có API
        │   ├── version, commit_sha
        │   ├── source_location                  # path, line_start, line_end
        │   ├── public_binding                   # kind, path, target
        │   ├── signatures[], return_annotation
        │   ├── call_syntax                      # cú pháp accessor/ufunc (nếu có)
        │   └── evidence_refs[]
        ├── transitions[]                        # so sánh hai release liền kề
        │   ├── from_version, to_version, status, evidence_refs[]
        │   └── changes[]                        # kind, scope, parameter/method, before, after, evidence_refs[]
        ├── coverage                             # version/khoảng có, vắng, chưa rõ
        └── evidence[]                           # source, binding, Git diff, so sánh AST theo ID
```

## Bước 6 — Tổng hợp đường tiến hóa của cặp migration

**Mô tả:** đọc `data/{library}/api_source_tracks.json` và `deprecated_version` trong `data/api_mapping_versions.json`. Script tạo sự kiện deprecated khi có version; với `deprecated_version: null`, không tạo sự kiện này. LLM chọn và nhóm các sự kiện đã có thành đường tiến hóa của cặp API.

**Chọn API gọi model:**

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
[]                                               # Một phần tử cho mỗi cặp migration
└── mapping_id, library                          # ID cặp, tên thư viện
    ├── seed                                     # deprecated_api, replacement_api
    ├── deprecated_version: string|null          # Version có thông báo deprecated từ bước trước, nếu có
    ├── milestones[]                             # Các mốc hợp nhất theo thứ tự version
    │   ├── version: string
    │   ├── compared_to_version: string|null     # Mốc trước đó; null ở mốc đầu tiên
    │   ├── api_states[]                         # Trạng thái của cả hai API tại mốc này
    │   │   ├── name, status
    │   │   ├── signatures: string[]
    │   │   └── call_syntax: object|null
    │   └── changes[]                            # Các thay đổi được chọn cho mốc này
    │       ├── api, type, parameter
    │       ├── old_value, new_value, from_version
    │       └── source, replace_to, source_evidence[]
    └── model, input_digest                      # Model đã dùng, mã băm của input
```

- `status`: `active`, `deprecated`, `removed`, `absent` hoặc `unknown`.
- Với API được xác nhận có mặt tại mốc, `signatures` chứa các chữ ký gọi được; `call_syntax` chứa cú pháp accessor hoặc ufunc nếu có.
- Với API vắng mặt hoặc chưa xác định tại mốc, `signatures` là `[]` và `call_syntax` là `null`.
- `changes[]` ghi sự kiện deprecated, thay đổi source hoặc trạng thái API; `source_evidence[]` giữ bằng chứng từ bước 5 nếu có.
- `from_version` là version gốc của thay đổi, có thể khác `compared_to_version` của mốc.

## Bước 7 — Gán các mốc version khả dĩ cho lời gọi API

**Mô tả:** đọc `data/{library}/migration_evolution.json`. Với từng mẫu trong `O.json` và `U.json`, trích `api_call` rồi dùng LLM chọn các mốc có trạng thái phù hợp. Model so lời gọi với từng chữ ký trong `signatures`; khi danh sách rỗng, model dùng `call_syntax` của indexer hoặc ufunc.

**Chạy từng thư viện:**

```powershell
python build_callsite_versions.py --library numpy --file O --dry-run
python build_callsite_versions.py --library numpy --file O
python build_callsite_versions.py --library numpy --file U

# Hoặc chạy thử 10 mẫu O rồi tiếp tục
python build_callsite_versions.py --library numpy --file O --max-samples 10
python build_callsite_versions.py --library numpy --file O --resume

# Mặc định tối đa 8 workers
python build_callsite_versions.py --library numpy --file O --provider openai --workers 4 --resume
```

**Output:** `data/{library}/O_with_versions.json` hoặc `U_with_versions.json`.

- Mỗi mẫu giữ nguyên các trường đầu vào, thêm `api_call` (lời gọi API) ngay trước `versions[]`. Model dùng `api_call` thay cho `reference` để chọn các **mốc của timeline** phù hợp.
- `--max-samples` tạo output một phần; `--resume` dùng file tiến độ cùng phiên bản prompt để xử lý các mẫu còn lại.

## Bước 8 — Rà soát và sửa version

**Mô tả:**

- `audit_step7.py` kiểm tra tĩnh kết quả bước 7 bằng `signatures`/`call_syntax` trong `migration_evolution.json`; ghi ba CSV để rà soát (xem [hướng dẫn](reports/step7/README.md)).
- `build_step7_modified.py` dùng `invalid_selected_versions.csv`, `possible_omitted_versions.csv` và các dòng `possible_false_negative` trong `empty_samples_audit.csv` để xóa/thêm `versions`.

**Chạy:**

```powershell
python audit_step7.py
python build_step7_modified.py
```

**Output:** các CSV trong `reports/step7/output/` và `data/{library}/O_modified.json`, `U_modified.json`.

## Bước 9 — Crawl thêm mẫu từ GitHub

**Mô tả:** tìm hàm dùng API trong cặp migration, đối chiếu dependency, trạng thái API và chữ ký theo mốc version; tái sử dụng hàm khi hợp lệ ở nhiều mốc.

**Mục tiêu theo ô:** mỗi cặp có các ô (tập O/U × mốc). Mục tiêu của cặp là số mẫu lớn nhất trong các ô của `O_modified.json`/`U_modified.json`; mỗi ô cần thêm `mục tiêu − số mẫu hiện có`. Ô mà API có trạng thái khác `active`/`deprecated` (ví dụ `removed`) không được crawl. Mốc của một lời gọi chỉ do dependency quyết định: mọi ràng buộc version áp dụng cho file phải cùng nằm trong một khoảng `[v1, v2)` thì lời gọi được gán mốc `v1`.

**Pipeline:**

1. **Chọn API cần crawl.** Crawler tính số mẫu thiếu ở từng ô (API × mốc) và chỉ tìm những API còn ô thiếu. Ví dụ ô U của cumprod đã đủ, nên lần chạy numpy chỉ tìm `cumproduct`.

2. **Tìm theo tên ngắn kèm gốc import.** Tên ngắn là phần cuối đầy đủ, như `cumproduct` hay `map`, không phải một phần như `.prod`. Có năm nguồn:
   - **GitHub code search:** `cumproduct numpy`, chia 4 khoảng kích thước file, mỗi khoảng tối đa 1000 file. Nếu một khoảng chạm trần 1000, API đó được tìm thêm với từ khóa khác, mỗi truy vấn tối đa 1000 file: tên class của API (`map DataFrame pandas`) và từ khóa riêng của thư viện (`LIBRARY_QUERY_TERMS`, pandas: `read_csv`, `lambda`).
   - **Sourcegraph:** `lang:python file:has.content(numpy) /\bcumproduct\(/ count:all`, lấy hết kết quả, tối đa 20000 file mỗi API (`SOURCEGRAPH_FILES`, tách riêng khỏi `--max-files`). Gốc import nằm trong `file:has.content()` để chỉ dùng làm bộ lọc file; nếu viết như một từ riêng thì mỗi lần xuất hiện đều bị tính vào giới hạn kết quả. Method của class dùng `\.` thay cho `\b` (`/\.map\(/`), để không bắt nhầm hàm cùng tên như `map()` có sẵn. API dạng indexer (`call_syntax` kind `indexer`, như `DataFrame.loc`) nhận cả `[` lẫn `(` sau tên (`/\.loc[\[(]/`), vì được gọi bằng `df.loc[key]` hoặc `df.loc(axis=...)[key]`.
   - **Commit search:** `cumproduct cumprod`, `cumproduct numpy`, `cumproduct deprecated`, `cumproduct FutureWarning` và `replace cumproduct`, tìm trong commit message. Mỗi repo xét tối đa 3 commit.
   - **Dependency search:** file `requirements.txt` hoặc `pyproject.toml` có tên thư viện, chỉ lấy 30 file.
   - **Dependency theo ô thiếu:** với mỗi bản major.minor (lấy từ PyPI) nằm trong một ô còn thiếu, tìm `"pandas==2.2" filename:requirements.txt`, tối đa 100 file mỗi truy vấn. Repo có dependency ở HEAD rơi vào ô thiếu của API nào thì được tìm lời gọi API đó bằng code search gộp nhiều `repo:` trong một truy vấn (tối đa 256 ký tự, tối đa 60 truy vấn, các ô lần lượt chia lượt, repo nhỏ trước). Lời gọi tìm được đi qua bước lọc sơ bộ như code search.

3. **Lọc sơ bộ bằng AST và import/alias,** trước khi tải repo:
   - Với file từ code search, crawler tải nội dung file hiện tại rồi chạy AST, có phân giải alias, `from x import *`, re-export một cấp và kiểu DataFrame. Bản vendor bị bỏ.
   - Với commit, crawler phân tích AST file tại commit cha (cho dòng bị xóa) và tại commit (cho dòng thêm).
   - Với repo có lời gọi, crawler tải thêm `requirements.txt` và `pyproject.toml` ở gốc repo. Phiên bản đọc ở đây là phiên bản **hiện tại**, chỉ dùng để xếp hạng.

4. **Xếp hạng.** Thứ tự ưu tiên:
   - có lời gọi và dependency ở HEAD rơi đúng ô đang thiếu của API được gọi;
   - có lời gọi, chưa biết phiên bản ở HEAD;
   - có lời gọi, nhưng ô thiếu của API đó đều cũ hơn mốc ở HEAD (chỉ lịch sử commit mới lấp được);
   - có file và có dependency;
   - chỉ có file, hoặc chỉ có dependency. Trong nhóm chỉ có dependency, repo có dependency rơi vào ô thiếu đứng trước; repo đã được tìm lời gọi bằng `repo:` mà không thấy đứng sau;
   - có lời gọi nhưng ô thiếu của API đó đều mới hơn mốc ở HEAD (repo không thể lấp).

   Đồng thời, repo benchmark bị loại và fork trùng commit bị gộp.

5. **Tải repo** bằng partial clone (không tải blob trước). Repo đã xử lý mà HEAD không đổi thì bỏ qua.

6. **Chọn file và commit liên quan.**
   - File nguồn: lấy theo đường dẫn tìm được ở bước 2.
   - File dependency: chỉ lấy những file nằm cùng thư mục hoặc thư mục cha của file nguồn, kể cả file đã có trong lịch sử, cộng các file workflow CI.
   - Commit: tối đa 500, gồm commit thay đổi dòng có tên API, commit thay đổi file dependency, và HEAD.

7. **Xác định phiên bản tại từng commit.**
   - Crawler đọc lại file nguồn và chạy AST để lấy lời gọi thật.
   - Sau đó đọc dependency **tại chính commit đó**. Nguồn ràng buộc phiên bản: khai báo trực tiếp (kể cả `!=`), marker Python/nền tảng, file include `-r`, và pin của thư viện khác tra qua PyPI khi khai báo trực tiếp không đủ.
   - Mọi ràng buộc được gộp thành một khoảng. Khoảng đó phải nằm gọn trong đúng một mốc `[v1, v2)`; trải qua nhiều mốc hoặc mâu thuẫn thì bị loại.

8. **Kiểm tra API với dữ liệu chuẩn** (`migration_evolution.json`):
   - Ở mốc vừa tìm được, API phải ở trạng thái `active` hoặc `deprecated`.
   - Lời gọi phải khớp chữ ký của mốc đó. Crawler thử bind các tham số của lời gọi vào chữ ký trong tài liệu.

9. **Gộp trùng rồi lưu.**
   - Mã giống nhau (bỏ qua định dạng và chú thích) ở nhiều repo hoặc commit được gộp thành một mẫu.
   - Mẫu gần trùng hoặc rơi vào ô đã đủ bị bỏ.
   - Mẫu hợp lệ ghi vào `O_crawled_dependency.json` và `U_crawled_dependency.json` kèm mốc phiên bản. Repo được ghi vào sổ đã xử lý.
   - Crawler dừng khi mọi ô đã đủ.

10. **Lưu riêng lời gọi chưa có mốc** vào `O_crawled_not_dependency.json` và `U_crawled_not_dependency.json`, với `versions` rỗng (gán mốc ở bước sau).
    - Lời gọi được giữ khi dependency không xác định được mốc: không có file dependency (`no_manifest`), không ghi phiên bản (`no_version_spec`), ràng buộc mâu thuẫn (`version_conflict`), hoặc trải qua nhiều mốc (`version_not_single_interval`).
    - Lời gọi bị loại vì `api_unavailable` hay `signature_mismatch` (dependency đã cho mốc và lời gọi sai ở mốc đó), `both_apis`, vendor hay benchmark thì không giữ.
    - Lời gọi phải khớp chữ ký API ở ít nhất một mốc mà API có trạng thái `active`/`deprecated`.
    - Chỉ giữ cho API còn ô thiếu. Mỗi API giữ tối đa bằng tổng số mẫu còn thiếu trong các ô của API đó. Giới hạn chỉ chặn việc thêm mới; mẫu đã lưu không bị xóa vì giới hạn.
    - Lọc trùng với dữ liệu bước 8, với mẫu có dependency và giữa chúng với nhau. Cùng hàm, cùng file, cùng lời gọi nhưng khác commit thì chỉ giữ một mẫu.
    - Mẫu này không tính vào ô và không làm crawler dừng sớm. Khi lời gọi đó có bằng chứng dependency (cùng mã, hoặc cùng hàm và lời gọi ở commit khác), mẫu được chuyển sang file `_dependency`.

**Nhập GITHUB_TOKEN:**

```powershell
$env:GITHUB_TOKEN = "<GitHub token>"
```

**Chạy:**

```powershell
python github_crawler.py --library numpy --mapping-id 2 --max-repos 20
```

Bỏ `--mapping-id 2` để chạy mọi cặp của thư viện. **Output:** `data/{library}/O_crawled_dependency.json` và `U_crawled_dependency.json` (chỉ mẫu mới, không có trường `reference`), cùng `O_crawled_not_dependency.json` và `U_crawled_not_dependency.json` (`versions` rỗng).
Các repository được tải vào `cache/step9/`.
Crawler chỉ tìm API còn ô thiếu mẫu.

## Bước 10 — Ghép mẫu crawl và gán thêm mốc

**Mô tả:** ghép `O|U_crawled_dependency.json` và `O|U_crawled_not_dependency.json` thành `O|U_crawled.json`. Model OpenAI xem lời gọi còn khớp chữ ký ở những mốc nào khác trên đường tiến hóa, dùng cùng prompt với bước 7 (`prompts/callsite_version.txt`).

**Quy tắc ghép:**

- Mẫu có dependency: `versions` = mốc từ dependency ∪ mốc model trả về. Mốc từ dependency luôn được giữ, kể cả khi model không chọn; log đếm các trường hợp này (`model_missed_dependency_version`).
- Mẫu không có dependency: `versions` = mốc model trả về. Model trả về rỗng thì mẫu không được ghi vào output.
- Mẫu không có dependency trùng mã với mẫu có dependency thì bỏ.
- Không giới hạn theo mục tiêu ô: output ghi mọi mốc tương thích.

**Cache:** `data/{library}/O|U_crawled.cache.jsonl`, theo nội dung mẫu (hàm, probing input, lời gọi, alias, API đích và timeline của API đó), không theo vị trí mẫu. Mỗi câu trả lời được ghi ngay khi có. Khi thêm mẫu mới vào hai file đầu vào rồi chạy lại, chỉ mẫu mới gọi model; mẫu đã gán lấy từ cache. Đổi model hoặc prompt không làm gán lại; dùng `--refresh` để hỏi lại mọi mẫu. Output được dựng lại từ đầu vào và cache ở mỗi lần chạy.

**Chạy:**

```powershell
python build_crawled_versions.py --library pandas --dry-run
python build_crawled_versions.py --library pandas
python build_crawled_versions.py --library numpy --file O --workers 4
```

Cần `OPENAI_API_KEY` trong `.env` khi có mẫu chưa nằm trong cache.

**Output:** `data/{library}/O_crawled.json` và `U_crawled.json`.
