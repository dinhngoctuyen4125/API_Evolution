# Crawl version từ PyPI và ghép commit GitHub

Lấy version của 8 thư viện trên PyPI, sau đó ghép Git tag và commit SHA tương ứng.

## Setup

```powershell
conda create -n api_crawl python=3.10 -y
conda activate api_crawl
pip install -r requirements.txt
```

## Crawl

Đặt GitHub token trong PowerShell nếu cần tăng giới hạn gọi API:

```powershell
$env:GITHUB_TOKEN = "github_pat_xxxxxxxxx"
```

Lấy các version chính thức chưa bị yanked của 8 thư viện trên PyPI, sắp xếp tăng dần theo từng thư viện:

```powershell
python crawl_pypi_versions.py
```

Kết quả được lưu vào `data/pypi_versions.json`.

Ghép từng version PyPI với Git tag và commit SHA (ví dụ `0.3` khớp `v0.3.0`; không tìm thấy thì để `null`):

```powershell
python enrich_pypi_versions.py
```

Kết quả được lưu vào `data/pypi_versions_with_github.json`.

## Data

- `data/pypi_versions.json`: version từ PyPI.
- `data/pypi_versions_with_github.json`: version đã ghép Git tag và commit.
- `data/pypi_versions_with_github_diy.json`: bản chỉnh thủ công các ngoại lệ bên dưới.
- `data/sample_step_1.json`: dữ liệu mẫu.

## Ngoại lệ xử lý thủ công

`data/pypi_versions_with_github.json` có 4 trường hợp không ghép được tự động:

- `pandas` `0.1` và `0.2`: các bản quá cũ, không có Git tag tương ứng trong repository hiện tại.
- `transformers` `0.1`: bản PyPI thuộc một package trùng tên, không phải dự án Hugging Face.
- `transformers` `5.10.4`: bản trên PyPI là `5.10.4`, nhưng [release GitHub dùng tag `v5.10.3`](https://github.com/huggingface/transformers/releases/tag/v5.10.3); maintainer đã ghi rõ sự lệch tên này.

File `data/pypi_versions_with_github_diy.json` là bản chỉnh thủ công của file trên: đã xóa 3 trường hợp đầu và điền tag, commit SHA, source URL từ `v5.10.3` cho `transformers` `5.10.4`.
