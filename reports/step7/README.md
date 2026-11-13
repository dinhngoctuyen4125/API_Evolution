# Rà soát kết quả bước 7

## 1. Script `audit_step7.py`

**Input:** `data/{library}/O_with_versions.json`, `U_with_versions.json` và `migration_evolution.json`.

**Chạy từ root của dự án**:

```powershell
python audit_step7.py
```

**Cơ chế:**

1. Đọc `O/U_with_versions.json` và `migration_evolution.json` của từng thư viện.
2. Duyệt từng mẫu, ghép timeline theo cặp API; O xét API cũ, U xét API thay thế. Dùng `alias dict` khi đối chiếu lời gọi với API đích; mốc gọi được phải có trạng thái `active`/`deprecated`.
3. Với `versions=[]`, phân loại nguyên nhân và thử `api_call` với các chữ ký trong `signatures` của từng mốc callable; nếu danh sách rỗng, dùng `call_syntax`.
4. Với mẫu đã có version, thử các mốc callable chưa chọn. Nếu lời gọi có thể khớp thì ghi **nghi gán thiếu**, không tự thêm version.
5. Với từng version đã chọn, kiểm tra trạng thái API rồi ghép positional/keyword vào từng chữ ký trong `signatures` hoặc kiểm tra `call_syntax`.
6. Ghi ba CSV và thống kê.

**Output:** thống kê vào ba CSV trong `reports/step7/output/`: [mốc nghi gán sai](output/invalid_selected_versions.csv), [mốc nghi bỏ sót](output/possible_omitted_versions.csv), [mẫu rỗng đã phân loại](output/empty_samples_audit.csv).

Trong các bảng dưới đây, mỗi ô là **số mẫu**; một mẫu có thể đóng góp nhiều **mốc version** vào CSV.

## 2. Có `versions` nhưng nghi gán sai

- **59 mẫu, 202 mốc** trong [invalid_selected_versions.csv](output/invalid_selected_versions.csv).

| Thư viện | O: số mẫu | U: số mẫu |
|---|---:|---:|
| numpy | 0 | 0 |
| pandas | 0 | 0 |
| pytorch | 3 | 14 |
| scikit-learn | 3 | 25 |
| scipy | 0 | 0 |
| seaborn | 8 | 3 |
| tensorflow | 0 | 3 |
| transformers | 0 | 0 |
| **Tổng** | **14** | **45** |

- **pytorch:** O có `norm` dùng keyword `dtype` chưa có; U có `interpolate` dùng keyword `recompute_scale_factor` chưa có ở các mốc được báo.
- **scikit-learn:** O dùng keyword `tol` hoặc `covariance_type` chưa có ở `GMM`; U chủ yếu dùng `as_frame` trước khi `fetch_openml` nhận keyword này, thêm hai mẫu `SimpleImputer(add_indicator=...)`.
- **seaborn:** O có `factorplot(order=...)`, `despine(offset=...)` hoặc lời gọi `tsplot` thiếu/trùng đối số; U truyền trùng `x` vào `catplot`/`lineplot`.
- **tensorflow U:** `cast` thiếu `dtype`, `print` thiếu `data`, `global_variables` thừa positional.

## 3. Có `versions` nhưng nghi gán thiếu

**116 mẫu, 329 mốc** trong [possible_omitted_versions.csv](output/possible_omitted_versions.csv).

| Thư viện | O: số mẫu | U: số mẫu |
|---|---:|---:|
| numpy | 2 | 0 |
| pandas | 0 | 4 |
| pytorch | 2 | 8 |
| scikit-learn | 1 | 14 |
| scipy | 1 | 8 |
| seaborn | 14 | 4 |
| tensorflow | 0 | 56 |
| transformers | 1 | 1 |
| **Tổng** | **21** | **95** |

- **numpy:** `product`/`cumproduct`; **pandas:** `DataFrame.loc`/`transpose`.
- **pytorch:** chủ yếu `interpolate`, thêm `cholesky` và các API `linalg`; **scikit-learn:** chủ yếu `GaussianMixture`, `SimpleImputer` và `fetch_openml`.
- **scipy:** `special.comb` và `misc.logsumexp`; **seaborn:** `tsplot` và `lineplot`.
- **tensorflow:** chủ yếu `cast` và `global_variables_initializer`; **transformers:** hai API xử lý ảnh `SegformerFeatureExtractor`/`ConvNextImageProcessor`.

## 4. `versions=[]`

Có **147 mẫu** trong [empty_samples_audit.csv](output/empty_samples_audit.csv).

| Thư viện | O: số mẫu | U: số mẫu |
|---|---:|---:|
| numpy | 0 | 1 |
| pandas | 0 | 0 |
| pytorch | 14 | 106 |
| scikit-learn | 3 | 6 |
| scipy | 4 | 5 |
| seaborn | 0 | 0 |
| tensorflow | 0 | 6 |
| transformers | 0 | 2 |
| **Tổng** | **21** | **126** |

Phân loại: **81** `possible_false_negative` (có mốc khớp nhưng bước 7 trả rỗng), **66** `no_compatible_milestone` (không thấy mốc khớp).

- **pytorch:** 63/120 mẫu rỗng có mốc nghi bị bỏ sót, 57 không thấy mốc khớp; **scikit-learn:** 6 nghi bỏ sót, 3 không khớp.
- **scipy:** 6/9 mẫu nghi bỏ sót, 3 không khớp; mẫu U `special.betainc(a=..., b=..., x=...)` không khớp vì ba đầu vào phải là positional, còn `sps.betainc(a, b, x, dtype=dtype)` được xếp nghi bỏ sót.
- **numpy:** 1 mẫu U nghi bỏ sót; **tensorflow:** 3 mẫu U nghi bỏ sót, 3 không khớp; **transformers:** 2 mẫu U nghi bỏ sót. **pandas** và **seaborn** không còn mẫu rỗng.

## 5. Hạn chế

Ghép đối số với `signatures` hoặc `call_syntax` chỉ kiểm tra cú pháp lời gọi; mốc nghi bỏ sót chưa được xác nhận về hành vi runtime, kiểu giá trị hoặc cách xử lý `*args`/`**kwargs`.
