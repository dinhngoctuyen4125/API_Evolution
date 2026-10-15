# Crawl version từ PyPI và ghép commit GitHub

Lấy version của 8 thư viện trên PyPI, sau đó ghép Git tag và commit SHA tương ứng. Dự án cũng lưu 145 cặp API migration được thu thập thủ công.

## Setup

```powershell
conda create -n api_crawl python=3.10 -y
conda activate api_crawl
pip install -r requirements.txt
```

## Crawl git tags + commits

Đặt GitHub token trong PowerShell nếu cần tăng giới hạn gọi API:

```powershell
$env:GITHUB_TOKEN = "github_pat_xxxxxxxxx"
```

Lấy mọi version có tên dạng số `X.Y`, `X.Y.Z`, ... của 8 thư viện trong lịch sử PyPI, sắp xếp tăng dần theo từng thư viện. Release bị yanked hoặc không có file phân phối (ví dụ NumPy `1.4.0`) vẫn được giữ lại; chỉ các tên version khác dạng số như `rc`, `b`, `dev`, `post` bị loại:

```powershell
python crawl_pypi_versions.py
```

Kết quả được lưu vào `data/pypi_versions.json`.

Ghép từng version PyPI với Git tag và commit SHA (ví dụ `0.3` khớp `v0.3.0`; không tìm thấy thì để `null`):

```powershell
python enrich_pypi_versions.py
```

Kết quả được lưu vào `data/pypi_versions_with_github.json`.

## Crawl release notes

`crawl_release_notes.py` đọc bản đã kiểm tra thủ công `data/pypi_versions_with_github_diy.json` và giữ nguyên các trường `library`, `version`, `tag`, `commit_sha`, `source_url` (URL source code trên GitHub). Hiện `LIBRARIES = ["numpy"]`; chạy toàn bộ version NumPy bằng:

```powershell
python crawl_release_notes.py
```

Để crawl hoặc bổ sung một version vào file kết quả hiện có:

```powershell
python crawl_release_notes.py --version 1.25.0
```

Kết quả `data/release_notes.json` chỉ gồm các version đã có release notes, giữ nguyên các trường từ bản dữ liệu đầu vào rồi thêm `release_url` (đường dẫn release notes trên web) cùng `release_path` (đường dẫn HTML trong máy tính, tính từ thư mục dự án). Ví dụ `cache/numpy/1.5.0/docs/release_notes.html`; thư mục `cache/numpy/1.5.0/source/` dành cho source code tải về sau này. HTML không được nhúng vào JSON. Version chưa có release notes không được ghi vào file kết quả. Crawler thử trang tài liệu theo nhánh `major.minor`, rồi `stable`, sau cùng là `devdocs`; bản đã có trong cache được dùng lại. Khi dùng `--version`, crawler chỉ tải version được chọn và giữ các bản ghi release notes khác đã có trong JSON. Mỗi lần tải thành công, crawler cập nhật JSON để lần chạy bị ngắt vẫn giữ được tiến độ. Nếu trang trên web thay đổi, bản HTML mới đã được kiểm tra tiêu đề sẽ cập nhật file cache. Dùng `--refresh` để tải lại và `--output` để chọn file kết quả khác.

Để chuyển file `data/release_notes.json` cũ đang chứa HTML trong trường `content` sang cấu trúc mới mà không gọi mạng:

```powershell
python crawl_release_notes.py --migrate-only
```

Để mở rộng sang thư viện khác, thêm tên vào `LIBRARIES` và hàm tạo URL tương ứng vào `URL_BUILDERS` trong `crawl_release_notes.py`, vì mỗi thư viện có cấu trúc trang tài liệu riêng.

## Data

- `data/pypi_versions.json`: version từ PyPI.
- `data/pypi_versions_with_github.json`: version đã ghép Git tag và commit.
- `data/pypi_versions_with_github_diy.json`: bản chỉnh thủ công các ngoại lệ bên dưới.
- `data/api_mapping_pairs.json`: 145 cặp API migration thu thập thủ công.
- `data/release_notes.json`: các version có release notes từ bản dữ liệu đã kiểm tra thủ công, bổ sung `release_url` và `release_path`; được Git ignore.
- `cache/<library>/<version>/docs/release_notes.html`: HTML release notes; `source/` dành cho source code tải về sau này. Toàn bộ `cache/` được Git ignore.
- `data/sample_step_1.json`: dữ liệu mẫu.

## API migration mappings

`data/api_mapping_pairs.json` có 145 bản ghi, mỗi bản ghi gồm `id`, `library`, `deprecated_api` và `replacement_api`. Hai tên API là cặp đầu vào để phân tích migration; file chưa chứa version áp dụng, trạng thái API theo từng version hoặc danh sách API đã mở rộng cho mỗi mapping.

Số cặp theo thư viện: NumPy 3, pandas 10, PyTorch 21, SciPy 18, seaborn 3, scikit-learn 4, TensorFlow 57 và Transformers 29. Trường `library` dùng `sklearn` cho scikit-learn, trong khi dữ liệu version PyPI/GitHub dùng `scikit-learn`; cần quy đổi hai nhãn khi nối dữ liệu.

## Ngoại lệ xử lý thủ công

`data/pypi_versions_with_github.json` có 5 trường hợp không ghép được tự động:

- `pandas` `0.1` và `0.2`: các bản quá cũ, không có Git tag tương ứng trong repository hiện tại.
- `transformers` `0.1`: bản PyPI thuộc một package trùng tên, không phải dự án Hugging Face.
- `transformers` `4.25.0`: bản PyPI bị yanked; không tìm thấy Git tag và source code tương ứng trên GitHub nên đã loại khỏi bản dữ liệu chỉnh tay.
- `transformers` `5.10.4`: bản trên PyPI là `5.10.4`, nhưng [release GitHub dùng tag `v5.10.3`](https://github.com/huggingface/transformers/releases/tag/v5.10.3); maintainer đã ghi rõ sự lệch tên này.

File `data/pypi_versions_with_github_diy.json` là bản chỉnh thủ công của file trên: đã xóa `pandas` `0.1`, `pandas` `0.2`, `transformers` `0.1` và `transformers` `4.25.0`; đã điền tag, commit SHA, source URL từ `v5.10.3` cho `transformers` `5.10.4`.
