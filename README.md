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

**Mô tả:** `trace_source_changes.py` theo dõi từng cặp trong `data/api_mapping_pairs.json` qua các version đã thu thập. Script đọc source tại commit SHA và Git diff để tạo các quan sát về public API và implementation. Git diff chỉ giúp tìm vị trí cần kiểm tra; một diff không tự động trở thành evolution event. Bước 5 chưa dùng LLM hoặc release notes.

**Input:** `data/api_mapping_pairs.json`, `data/pypi_versions_with_github_diy.json` và Git repository tại `cache/{library}/source`. Mỗi version phải được gắn với đúng commit SHA; nếu chưa xác minh được ánh xạ giữa bản phát hành PyPI và source Git, ghi trạng thái này trong kết quả thay vì coi hai nguồn là tương đương.

**Chạy toàn bộ:**

```powershell
python trace_source_changes.py
```

**Chạy riêng:**

```powershell
# Một migration pair trên tất cả version NumPy đã thu thập.
python trace_source_changes.py --mapping-id 3

# Một khoảng version, gồm hai đầu. Output của lần chạy chỉ chứa khoảng đã chọn.
python trace_source_changes.py --mapping-id 3 --from-version 1.24.4 --to-version 1.25.0

# Toàn bộ các pair của một thư viện.
python trace_source_changes.py --library pandas

# In tiến độ sau mỗi khoảng version (mặc định sau mỗi 10 khoảng).
python trace_source_changes.py --library numpy --progress-every 1
```

**Tiến độ:** script in ngay số pair/version, tiến độ kiểm tra tag và version đang so sánh ra terminal. `--progress-every 0` tắt log theo khoảng version. Với NumPy, mỗi pair có 143 khoảng version; một lệnh `git show`/`git diff` có thể mất thêm thời gian nếu Git phải tải blob còn thiếu. Hai file JSON chỉ được ghi sau khi phạm vi đã chọn chạy xong.

**Luồng xử lý:**

1. Với mỗi library, sắp xếp các version đã thu thập và kiểm tra commit SHA có thể đọc được. Giữ nguyên tên version trong metadata, kể cả `1.0` và `1.0.0` nếu có. Đọc snapshot bằng `git show` và so sánh bằng `git diff` theo SHA để không phải liên tục chuyển checkout của repository dùng chung.
2. Với mỗi cặp API ở từng snapshot, lần theo tên public qua import, re-export, alias, wrapper và vị trí định nghĩa. Ghi trạng thái public của deprecated API và replacement API riêng biệt: `present`, `absent` hoặc `unknown`. Trường hợp export động, extension hoặc code sinh ra mà rule chưa giải được phải giữ `unknown`.
3. So sánh từng cặp version liền kề **trong tập đã thu thập** để định vị thay đổi. Dùng Git diff khoanh vùng file, rồi đối chiếu symbol public và source/AST tương ứng ở cả hai snapshot. Tái sử dụng kết quả phân tích một snapshot cho các migration pair cùng library. Nếu thiếu snapshot hoặc không lần được đường từ public API tới implementation, ghi khoảng chưa xác minh.
4. Tạo candidate cho các thay đổi rule nhận diện được: thêm/gỡ public export; thêm/gỡ tham số, đổi giá trị mặc định, thứ tự, kind hoặc annotation; đổi alias, đích delegation hoặc đường code phát deprecation warning. Thay đổi thân hàm được ghi `implementation_changed` với trạng thái `requires_review` nếu chưa có thay đổi public rõ ràng. Rule không tự kết luận hành vi runtime hoặc kiểu dữ liệu thực tế đã đổi.
5. Gắn bằng chứng source và Git diff cho từng candidate: version, commit SHA, file path, symbol, vị trí dòng, diff hash và các hunk header. Tách `source_observed` khỏi trạng thái xác minh như `verified` hoặc `requires_review`; không suy ra `introduced` hoặc `removed` chỉ từ một snapshot đơn lẻ.

**Output:** `data/source_change_candidates.json` **chỉ lưu khoảng có ít nhất một candidate liên quan đến deprecated API hoặc replacement API**; không yêu cầu cả hai API cùng thay đổi. Mỗi record có `mapping_id`, `library`, hai tên API, `from_version`, `to_version`, hai commit SHA, trạng thái từng API trước/sau, `changes` và `evidence`; mỗi change có `evidence_refs`. `data/source_trace_unresolved.json` lưu riêng các khoảng không kiểm tra được đầy đủ, kể cả khi vẫn phát hiện được một candidate ở API còn lại. Khoảng không có candidate và không có lỗi không được ghi thành event. Hai file này được ghi lại theo phạm vi của mỗi lần chạy và được Git ignore.

**Giới hạn:** resolver xử lý Python `import`, re-export, `__all__` tĩnh, alias, hàm, method và một số chuỗi kế thừa có thể lần theo tĩnh. Import có điều kiện được đánh dấu `requires_review`; namespace động, kế thừa không giải được, extension và source không parse được có thể thành `unresolved`. Script kiểm tra repository gốc, tag, SHA và `source_url` nhưng chưa so sánh nội dung wheel PyPI với Git source (`artifact_source_match_status: not_verified`). Git ở bước này có thể tải blob còn thiếu của repository clone một phần. `comparison_base_version` trong evolution path là milestone có thay đổi API trước đó được giữ lại, không phải version liền kề dùng để quét ở bước này.

Các rule bổ sung cho NumPy cũ nhận diện import nội bộ kiểu Python 2 (`from core import *`), theo dõi `__all__` cập nhật bằng literal, và giữ `unknown` khi namespace được nạp qua `pkgload`. Với `__getattr__`, chỉ kết luận API vắng mặt khi có thể chứng minh nhánh truy cập tên đó kết thúc bằng `AttributeError`. Wrapper chỉ chuyển tiếp `*args, **kwargs` nguyên vẹn được so sánh theo chữ ký của hàm đích; output giữ cả `declared_parameters` và `effective_parameters`. Evidence Git diff chứa những hunk giao với vị trí symbol liên quan (`relevant_hunks`), thay vì toàn bộ diff của file. Các suy luận tĩnh qua import có điều kiện vẫn cần review; `public_api_added` không được khẳng định nếu snapshot trước có loader động.

## Bước 6 — Trích xuất thông báo thay đổi API từ release notes

**Mô tả:** `trace_release_notes.py` đọc HTML đã cache ở bước 3 để lấy các thông báo có liên quan tới từng cặp API. Bước này chạy **độc lập với source candidates của bước 5**: một thông báo chỉ xuất hiện trong release notes vẫn có thể được ghi nhận. Kết quả ở đây là `documented` claim, chưa phải kết luận rằng source code đã thay đổi. Bước đối chiếu hai nguồn sẽ được thực hiện sau.

**Input:** `data/api_mapping_pairs.json`, tập version đã thu thập trong `data/pypi_versions_with_github_diy.json`, `data/release_notes.json` và các file `cache/{library}/docs/{version}/release_notes.html`. Phiên bản không có record release note hoặc không có file HTML được ghi riêng, không suy đoán nội dung còn thiếu.

**Xem trước đoạn sẽ gửi cho LLM (không gọi API, không ghi output):**

```powershell
python trace_release_notes.py --mapping-id 3 --version 1.25.0 --dry-run
```

**Chạy với OpenAI API:**

```powershell
$env:OPENAI_API_KEY = "<API key của bạn>"

# Một cặp / một version trước khi mở rộng phạm vi.
python trace_release_notes.py --mapping-id 3 --version 1.25.0

# Một thư viện hoặc toàn bộ 8 thư viện.
python trace_release_notes.py --library numpy
python trace_release_notes.py
```

**Chạy với Gemini API:**

```powershell
$env:GEMINI_API_KEY = "<API key của bạn>"
python trace_release_notes.py --provider gemini --mapping-id 3 --version 1.25.0
```

`test_trace_release_notes.py` là unit test dùng phản hồi giả, không cần API key và không gọi dịch vụ bên ngoài. Muốn thử LLM thật với một pair/version, chạy `trace_release_notes.py` như hai ví dụ trên. Đặt key trong biến môi trường của phiên PowerShell hiện tại; không ghi key vào file Python hoặc commit lên Git.

Có thể chọn `--provider`, `--model`, `--mapping-id`, `--library` (lặp lại được), `--version`, `--output`, `--unresolved-output` và `--progress-every`. Mặc định dùng `openai`/`gpt-4o-mini`; với `--provider gemini`, mặc định dùng `gemini-3.8-flash`. Mỗi lần chạy ghi lại hai file output theo đúng phạm vi được chọn; nên lưu kết quả thử nghiệm ra đường dẫn riêng nếu cần giữ kết quả của lần chạy trước. Chỉ các note có đoạn ứng viên mới tạo API call; hãy dùng `--dry-run` để kiểm tra phạm vi và chi phí trước khi chạy rộng.

**Luồng xử lý:**

1. Ghép từng pair với các version của thư viện trong metadata. Kiểm tra record và file release note. Thiếu note được ghi `release_note_missing`; file không đọc được được ghi riêng. Không lấy announcement version làm điểm xuất phát nên có thể thấy cả thông báo trước và sau nó trong tập version hiện có.
2. Parse HTML thành các block văn bản, bỏ `script`, `style`, `nav`, `header`, `footer`. Rule tìm tên API đầy đủ và các alias Python phổ biến (ví dụ `numpy.product`/`np.product`), lấy block trúng cùng ngữ cảnh sát bên. Tên ngắn ít đặc trưng cần thêm ngữ cảnh migration. Đây là bước tìm ứng viên, không phải xác minh sự kiện.
3. Gửi tối đa 12 block trong một request tới OpenAI Responses API hoặc Gemini Interactions API với structured output. LLM chỉ được trích xuất phát biểu rõ ràng: deprecation, khuyến nghị thay thế, removal, introduction, thay đổi signature, behavior, compatibility hoặc thay đổi được mô tả khác. Nếu note nói về tương lai/quá khứ, `temporal_status` và `effective_version` giữ thông tin đó tách khỏi `release_version`. Nếu không có claim rõ ràng, trả mảng rỗng.
4. Rule kiểm tra `event_type`, API đích, `block_id`, và `quote` phải là chuỗi con của đúng block đã gửi và có nhắc đến một API của pair. Claim không đạt bị ghi `invalid_or_ungrounded_claims`. `quote_verified` chỉ xác nhận đoạn trích khớp HTML, **không** xác minh ngữ nghĩa hay source; claim quan trọng vẫn cần đối chiếu hoặc review.
5. Chỉ lưu note có claim hợp lệ. Mỗi claim có `observation_type: documented`, `event_type`, `target_api`, `description`, `temporal_status`, `effective_version`, `quote` và `evidence_refs`. Evidence chứa `release_url`, `release_path`, SHA-256 của HTML, `block_id` và block text để truy vết. Lỗi API, claim thiếu bằng chứng và note còn thiếu được ghi ở file unresolved.

**Output:** `data/release_note_claims.json` chứa `mapping_id`, `library`, cặp API, `release_version`, nguồn release note, `provider`, `model`, `claims` và `evidence`. `data/release_note_unresolved.json` chứa các pair/version chưa thể kiểm tra đầy đủ. Cả hai file được Git ignore. Không có mention hoặc LLM không thấy claim rõ ràng thì không tạo event; điều đó không chứng minh API không thay đổi.

**Giới hạn:** rule tìm đoạn có thể bỏ sót cách gọi API không nằm trong alias đã khai báo, nội dung chỉ nêu ý gián tiếp hoặc tài liệu ngoài danh sách release notes; HTML trang web có thể chứa nội dung ngoài phần note. LLM có thể hiểu sai phát biểu dù quote hợp lệ. Các thông báo nguồn tài liệu chưa được đối chiếu với `source_observed` của bước 5, và thiếu release note không được tự nối thành evolution path hoàn chỉnh.
