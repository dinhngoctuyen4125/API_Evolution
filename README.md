# API Evolution

Dự án thu thập version của 8 thư viện Python trên PyPI, ghép với Git tag và commit, rồi lưu trang release notes tương ứng. Danh sách cặp API migration được thu thập thủ công để dùng ở các bước phân tích tiếp theo.

## Chuẩn bị môi trường

```powershell
conda create -n api_crawl python=3.10 -y
conda activate api_crawl
pip install -r requirements.txt
```

Chạy các lệnh dưới đây từ thư mục gốc của dự án, theo thứ tự các bước. Hai file kết quả của bước 1 và 2 được tạo khi chạy script; bản đã chỉnh tay ở bước 3 được lưu riêng.

## Bước 1 — Lấy version từ PyPI

**Mô tả:** `crawl_pypi_versions.py` đọc lịch sử phát hành của NumPy, pandas, PyTorch (`torch` trên PyPI), TensorFlow, SciPy, scikit-learn, seaborn và Transformers. Script lấy các tên version chỉ gồm số và dấu chấm, như `X.Y`, `X.Y.Z`, rồi sắp xếp tăng dần theo từng thư viện.

**Chạy:**

```powershell
python crawl_pypi_versions.py
```

**Output:** `data/pypi_versions.json`, mỗi bản ghi có `library` và `version`. File kết quả này được Git ignore.

**Ngoại lệ / xử lý tay:** Version bị yanked hoặc không có file phân phối vẫn được giữ nếu tên đúng dạng số. Các tên chứa `rc`, `b`, `dev`, `post` hoặc phần khác ngoài dạng số bị loại; bước này không cần chỉnh tay.

## Bước 2 — Ghép Git tag và commit SHA

**Mô tả:** `enrich_pypi_versions.py` đọc output bước 1, tìm Git tag tương ứng trong repository của từng thư viện và lấy commit SHA. Script cũng xét tag khác cách viết nhưng tương đương về số version, chẳng hạn `0.3` và `v0.3.0`.

**Chạy:**

```powershell
python enrich_pypi_versions.py
```

**Output:** `data/pypi_versions_with_github.json`, giữ `library`, `version` và thêm `tag`, `commit_sha`, `source_url`. `source_url` trỏ tới cây mã nguồn theo commit SHA. File kết quả này được Git ignore.

**Ngoại lệ / xử lý tay:** Khi không ghép được tag hoặc có nhiều tag tương đương nhưng khác commit, ba trường bổ sung là `null`; các trường hợp cần xem lại được xử lý ở bước 3. Nếu GitHub API báo giới hạn lượt gọi, đặt `$env:GITHUB_TOKEN = "<GitHub token của bạn>"` rồi chạy lại lệnh trên.

## Bước 3 — Chốt danh sách version đã kiểm tra

**Mô tả:** Rà soát output bước 2 và lưu bản đã kiểm tra riêng để crawler release notes dùng làm đầu vào. Chỉ khởi tạo file bằng lệnh dưới đây nếu file chỉnh tay chưa tồn tại; sau đó sửa các ngoại lệ trong editor.

**Chạy:**

```powershell
if (-not (Test-Path data/pypi_versions_with_github_diy.json)) {
    Copy-Item data/pypi_versions_with_github.json data/pypi_versions_with_github_diy.json
}
```

**Output:** `data/pypi_versions_with_github_diy.json`, hiện có 821 bản ghi với `library`, `version`, `tag`, `commit_sha`, `source_url`. File này được theo dõi bởi Git; chạy lại bước 2 không ghi đè bản chỉnh tay. `data/sample_step_1.json` là dữ liệu mẫu của giai đoạn này.

**Ngoại lệ / xử lý tay:** Bản hiện tại đã loại `pandas` `0.1` và `0.2` vì không có tag tương ứng trong repository hiện tại; loại `transformers` `0.1` vì là package trùng tên, không thuộc Hugging Face; loại `transformers` `4.25.0` vì không tìm thấy tag và source code tương ứng trên GitHub. Với `transformers` `5.10.4`, PyPI ghi `5.10.4` nhưng [GitHub dùng tag `v5.10.3`](https://github.com/huggingface/transformers/releases/tag/v5.10.3), nên bản chỉnh tay dùng tag, SHA và URL mã nguồn của `v5.10.3`.

## Bước 4 — Lấy trang release notes

**Mô tả:** `crawl_release_notes.py` đọc bản chỉnh tay ở bước 3, tìm trang tài liệu hoặc trang GitHub theo từng version và lưu toàn bộ HTML vào cache. Mặc định script xử lý cả 8 thư viện.

**Chạy:**

```powershell
python crawl_release_notes.py

# Có thể giới hạn một thư viện hoặc một version.
python crawl_release_notes.py --library transformers
python crawl_release_notes.py --library numpy --version 1.25.0

# Tải lại những bản được chọn khi cần cập nhật HTML.
python crawl_release_notes.py --library transformers --refresh
```

Để chạy riêng 7 thư viện ngoài NumPy, lặp lại `--library`:

```powershell
python crawl_release_notes.py --library pandas --library pytorch --library scipy --library scikit-learn --library seaborn --library tensorflow --library transformers
```

**Output:** `data/release_notes.json` giữ các trường đầu vào và thêm `release_url` (URL trên web), `release_path` (đường dẫn tương đối tới HTML trên máy). Chỉ những bản có trang được chấp nhận và HTML đã lưu mới có trong JSON. Ví dụ `cache/numpy/docs/1.5.0/release_notes.html`; thư mục `cache/<library>/source/<version>/` dành cho mã nguồn tải về sau. HTML không được nhúng vào JSON. `data/release_notes.json` và toàn bộ `cache/` được Git ignore. Khi chạy lại với JSON dùng đường dẫn cũ, script chuyển HTML đã lưu sang cấu trúc mới nếu file còn tồn tại.

Khi chạy lại, script đọc JSON hiện có, giữ bản ghi của các thư viện khác, bỏ qua bản đã có URL và file cache, rồi bổ sung bản còn thiếu. File JSON được ghi lại sau mỗi lần tải thành công. `--refresh` tải lại các bản được chọn; `--output` cho phép chọn file kết quả khác.

**Ngoại lệ / xử lý tay:**

- NumPy, pandas, SciPy và seaborn được tìm trong tài liệu chính thức. Một số trang NumPy ghi bản `X.Y.0` là `X.Y` trong tiêu đề (ví dụ `1.24.0`); crawler chấp nhận cách ghi này. Với NumPy `1.5.1`, crawler thử thêm [trang release notes của NumPy 1.5.x](https://docs.scipy.org/doc/numpy-1.5.x/release.html#numpy-1-5-1-release-notes), xác nhận tiêu đề mục `NumPy 1.5.1 Release Notes` và lưu URL trỏ đúng mục. Với SciPy 0.x, crawler còn thử SourceForge: [0.13.3 có ghi chú](https://sourceforge.net/projects/scipy/files/scipy/0.13.3/), còn [thư mục 0.6.0](https://sourceforge.net/projects/scipy/files/scipy/0.6.0/) chỉ có file tải và README chung nên chưa được tính là release notes.
- Với scikit-learn, mục `Version X.Y` có thể ứng với bản `X.Y.0`; các bản vá cần mục version riêng. `release_url` trỏ tới đúng mục trong trang. Riêng bản `0.22.2`, [mục `0.22.2.post1`](https://scikit-learn.org/0.22/whats_new/v0.22.html#version-0-22-2-post1) ghi rõ hai bản dùng chung các thay đổi, còn post-release chỉ sửa cách đóng gói source distribution. Crawler không tự gán tên khác version, nên trường hợp này cần làm tay theo mẫu bên dưới.
- PyTorch và TensorFlow dùng GitHub Release có phần mô tả. Khi tài liệu không có, pandas, SciPy, scikit-learn và seaborn cũng thử GitHub Release có phần mô tả.
- Với Transformers, URL `/releases/tag/<tag>` đúng tag trong kho `huggingface/transformers` được giữ làm minh chứng ngay cả khi phần mô tả rỗng. Điều đó áp dụng cho `2.1.0`, `4.1.0`, `4.4.1`, `4.33.0`, `4.52.0`, `5.10.0`; `release_url` không nhất thiết chứa ghi chú chi tiết. Trang `/tree` đơn thuần không được dùng thay trang `/releases/tag`.

Để bổ sung thủ công scikit-learn `0.22.2`, trước tiên tải toàn bộ HTML của trang vào `cache/scikit-learn/docs/0.22.2/release_notes.html`, rồi thêm bản ghi sau vào `data/release_notes.json`:

```json
{
  "library": "scikit-learn",
  "version": "0.22.2",
  "tag": "0.22.2",
  "commit_sha": "4b7331eeb746b3facb4d70e1760c58ebe8b47f2e",
  "source_url": "https://github.com/scikit-learn/scikit-learn/tree/4b7331eeb746b3facb4d70e1760c58ebe8b47f2e",
  "release_url": "https://scikit-learn.org/0.22/whats_new/v0.22.html#version-0-22-2-post1",
  "release_path": "cache/scikit-learn/docs/0.22.2/release_notes.html"
}
```

Nếu đang dùng file JSON cũ có HTML trong trường `content`, chuyển sang cấu trúc cache mà không gọi mạng bằng:

```powershell
python crawl_release_notes.py --migrate-only
```

Để mở rộng sang thư viện khác, thêm tên vào `LIBRARIES`, hàm tạo URL vào `URL_BUILDERS` và quy tắc xác nhận trang trong `crawl_release_notes.py`.

## Bước 5 — Kiểm tra tập cặp API migration

**Mô tả:** `data/api_mapping_pairs.json` là tập 145 cặp migration được thu thập thủ công; bước này chỉ kiểm tra file đầu vào, chưa phân tích API theo từng version.

**Chạy:**

```powershell
python -c "import json; from pathlib import Path; rows = json.loads(Path('data/api_mapping_pairs.json').read_text(encoding='utf-8')); print(len(rows))"
```

**Output:** Lệnh in `145`. File `data/api_mapping_pairs.json` có các trường `id`, `library`, `deprecated_api`, `replacement_api` và được theo dõi bởi Git. Số cặp theo thư viện: NumPy 3, pandas 10, PyTorch 21, SciPy 18, seaborn 3, scikit-learn 4, TensorFlow 57, Transformers 29.

**Ngoại lệ / xử lý tay:** Các cặp do người dùng chọn và sửa thủ công; file chưa có version áp dụng, trạng thái API theo version hoặc danh sách API ban đầu đã mở rộng cho từng mapping. Nhãn `library` của scikit-learn trong file này là `scikit-learn`, giống dữ liệu version.
