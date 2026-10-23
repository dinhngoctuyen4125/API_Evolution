# API Evolution

## Cấu trúc thư mục

```text
API_Evolution/
├── crawl_pypi_versions.py       # Bước 1: lấy version
├── enrich_pypi_versions.py      # Bước 2: ghép Git tag và commit SHA
├── crawl_release_notes.py       # Bước 3: tải release notes
├── crawl_source.py              # Bước 4: tải và chuyển version source
├── build_api_source_tracks.py   # Bước 5: dựng đường tiến hóa source riêng cho từng API
├── build_migration_evolution.py  # Bước 6: tổng hợp đường tiến hóa của cặp bằng GPT-6 Luna
├── source_evolution_rules.py     # Rule phân giải public API và AST trong Git source
├── torch_binding_rules.py        # Rule xác minh Python binding sinh mã của PyTorch
├── scipy_export_rules.py         # Rule truy export SciPy từ Cython và generator
├── prompts/migration_evolution.txt # Prompt tổng hợp cho bước 6
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

**Mô tả:** Với mỗi cặp trong `data/api_mapping_pairs.json`, kiểm tra các Git
commit theo `data/pypi_versions_with_github_diy.json`, rồi dựng hai lịch sử
độc lập: một cho `deprecated_api`, một cho `replacement_api`. Chỉ đưa vào
`versions[]` các release đã xác nhận API hiện diện. `coverage` ghi riêng release
vắng mặt và release chưa thể xác định (`unknown`). Transition so sánh source
giữa các release liền kề được trace; bước này không xác định mốc thông báo
deprecation và không phân tích docstring hay release notes.

**Chạy từng thư viện:**

```powershell
python build_api_source_tracks.py --library numpy

# Kiểm tra một cặp hoặc một khoảng release mà không ghi đè output đầy đủ.
python build_api_source_tracks.py --library scipy --mapping-id 49 --from-version 1.10.0 --to-version 1.10.1 --output data/scipy/mapping_49_probe.json
```

Khi không truyền `--mapping-id` và không giới hạn version, script xử lý toàn bộ
mapping của thư viện trên tất cả release đã tuyển chọn và ghi đè
`data/{library}/api_source_tracks.json`. Dùng `--trace-versions` để xem thời
gian và kết quả của từng API ở từng release.

Với Transformers, resolver đọc `define_import_structure` và `__all__` tại từng
commit để truy từ public API đến module chứa định nghĩa. Tên class chỉ giúp chọn
thư mục model cần kiểm tra. Nếu model ứng viên không xác nhận được export, trạng
thái là `unknown` để tránh quét toàn bộ model và tránh kết luận `absent` thiếu
bằng chứng. Resolver chung xử lý các release không nhận diện được cấu trúc lazy.

```powershell
python build_api_source_tracks.py --library transformers --mapping-id 117 --trace-versions
```

Với TensorFlow, chương trình tìm đường dẫn ứng viên cho mọi API bằng một lượt
quét checkout, rồi truy theo từng release để dùng chung index `tf_export`.
Vì thế log hiển thị `tracing version ...`, rồi `traced ... APIs`; kết quả vẫn là
lịch sử riêng cho từng API. Việc xác nhận từng export dựa trên decorator trong
source tại đúng commit.

```powershell
python build_api_source_tracks.py --library tensorflow --trace-versions
```

Với SciPy, resolver chung duyệt các `import *` trong module liên quan và có
thể dùng `__all__` tĩnh để bỏ qua nhánh không chứa tên cần tìm. Rule riêng xác
nhận một số export `scipy.special` từ `_ufuncs.pyx` hoặc
`functions.json` cùng mã generator; method của đối tượng tạo từ class, như
`scipy.stats.chi2.sf`, được truy qua class và lớp cơ sở. Khi source vẫn thiếu
bằng chứng, trạng thái tiếp tục là `unknown`.

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
    │   └── return_annotation, evidence_refs[]; generated_signatures nếu có
    ├── transitions[]                   # chỉ giữa hai release liền kề có API
    │   └── from_version, to_version, status, changes[], evidence_refs[]
    ├── coverage                        # version có/vắng/chưa rõ; khoảng đã/chưa so sánh
    └── evidence[]                      # source, public binding, Git diff và so sánh AST theo ID
```

`changes: []` nghĩa là không phát hiện thay đổi theo các rule đã chạy; không nối qua version vắng mặt hoặc chưa phân giải. Mốc công bố deprecation thuộc pha đọc release notes, còn đường tiến hóa chung của cặp thuộc pha tổng hợp sau đó. Với PyTorch, tham số binding sinh mã lấy từ ATen schema; `effective_parameters` gộp overload `.out` khi generator xác nhận, còn `annotation` giữ nguyên kiểu ATen.

## Bước 6 — Tổng hợp đường tiến hóa của cặp migration

`build_migration_evolution.py` ghép từng cặp trong `data/api_mapping_pairs.json` với
`deprecated_version` trong `data/api_mapping_version.json` và hai lịch sử API của
`data/{library}/api_source_tracks.json`. Script rút gọn source track thành các
sự kiện có version và bằng chứng; `gpt-6-luna` chọn các mốc để hợp nhất. Output
được kiểm tra lại với input, sau đó script dựng `api_states`, chữ ký và `changes`
từ dữ liệu gốc. Bước này không đọc release notes hay docstring.

```powershell
pip install -r requirements.txt
$env:OPENAI_API_KEY = "..."

# Xem input gửi cho model, không gọi API.
python build_migration_evolution.py --library numpy --mapping-id 3 --dry-run

# Một cặp hoặc cả thư viện. Có thể chạy lại với --resume.
python build_migration_evolution.py --library numpy --mapping-id 3
python build_migration_evolution.py --library numpy --resume
```

Output mặc định là `data/{library}/migration_evolution.json`; khi chọn một
`--mapping-id`, output mặc định là
`data/{library}/migration_evolution_mapping_{id}.json` để không ghi đè file
toàn thư viện. Những cặp thiếu deprecation record được báo và bỏ qua khi chạy
cả thư viện; chọn trực tiếp một cặp thiếu input sẽ báo lỗi. Nếu version
deprecation không nằm trong release đã trace, mốc đó có trạng thái API
`unknown`. Chỉ ghi `removed` khi source track có chuyển tiếp trực tiếp từ
`present` sang `absent`; một khoảng `unknown` không chứng minh API đã bị xóa.
Trường `source` của mốc deprecation dẫn về record trong
`api_mapping_version.json`, vì input này không chứa URL release note.
Trong output, `compared_to_version` là mốc đã chọn ngay trước đó;
`changes[].from_version` giữ bản source thực sự được so sánh tại bước 5.
Mỗi `milestone` gồm `version`, `compared_to_version`, `api_states[]` của cả hai
API và `changes[]` đã chọn. `api_states[].status` có thể là `active`,
`deprecated`, `removed`, `absent` hoặc `unknown`; signature bằng `null` nếu
source không xác nhận được API tại version đó. Mỗi change giữ API, loại thay
đổi, giá trị trước/sau, version gốc để so sánh và bằng chứng source nếu có.
Ví dụ `data/expected_step_5.json` chỉ minh họa cấu trúc: output giữ nguyên
chuỗi version trong input, và không tự thêm diễn giải implementation hay URL
release note khi input không cung cấp.
