# Rà soát kết quả bước 7

## 1. Script `audit_step7.py`

**Input:** `data/{library}/O_with_versions.json`, `U_with_versions.json` và `migration_evolution_v2.json`.

**Cơ chế:**

1. Đọc hai file `O/U_with_versions.json` và migration path của từng thư viện.
2. Duyệt từng mẫu; O xét API cũ, U xét API thay thế, rồi ghép timeline theo cặp API. Nếu `alias dict` có tên lời gọi, kiểm tra nó trỏ đúng API đang xét trước khi so chữ ký.
3. Với `versions=[]`, suy ra nguyên nhân và thử `api_call` với `effective_signature` của các mốc callable.
4. Với mẫu đã có version, thử mọi mốc callable chưa chọn; nếu `effective_signature` nhận `api_call`, ghi là **nghi bỏ sót**, không tự thêm vào `versions`.
5. Với từng version đã chọn, kiểm tra trạng thái API và ghép đối số của `api_call` vào `effective_signature`; ghi mốc có mâu thuẫn.
6. Ghi bốn CSV và in thống kê.

**Output:** thống kê vào bốn CSV trong `reports/step7/output/`: [version gán sai](output/invalid_selected_versions.csv), [mẫu rỗng đã phân loại](output/empty_samples_audit.csv), [755 mốc nghi bị bỏ sót](output/possible_omitted_versions.csv), [API cần xem thêm](output/accessor_needs_review.csv).

## 2. Mẫu bị gán sai version

Đối chiếu `api_call` với trạng thái và `effective_signature` trong migration path. **137 mẫu** có ít nhất một version sai (272 lượt gán). Chi tiết từng mẫu/version: [invalid_selected_versions.csv](output/invalid_selected_versions.csv).

| Thư viện | O: số mẫu | U: số mẫu |
|---|---:|---:|
| numpy | 0 | 0 |
| pandas | 0 | 0 |
| pytorch | 3 | 1 |
| scikit-learn | 24 | 1 |
| scipy | 0 | 4 |
| seaborn | 65 | 11 |
| tensorflow | 0 | 28 |
| transformers | 0 | 0 |
| **Tổng** | **92** | **45** |

- **numpy, pandas, transformers:** chưa phát hiện version được chọn trái chữ ký; pandas có API `DataFrame.loc` trong [danh sách cần xem thêm](output/accessor_needs_review.csv).
- **pytorch:** O dùng sai tên keyword hoặc thiếu tham số `self` (`svd`, `triangular_solve`, `norm`); U dùng `recompute_scale_factor` trước khi chữ ký `interpolate` nhận tham số này.
- **scikit-learn:** O dùng các keyword chưa có ở `GMM` (`covariance_type`, `n_iter`...); U dùng `n_clusters` thay vì `n_components` cho `GaussianMixture`.
- **scipy:** U truyền quá nhiều positional vào `simpson` sau khi tham số chuyển thành keyword-only.
- **seaborn:** O thiếu tham số bắt buộc của `tsplot` 0.1 hoặc dùng keyword chưa có ở `factorplot`; U truyền trùng `x` hoặc quá nhiều positional cho `lineplot`/`catplot`.
- **tensorflow:** U chọn `print` 1.9.0 dù lời gọi thiếu `data`, thừa positional hoặc dùng keyword chưa được nhận.

## 3. Mẫu nghi gán thiếu version

Có **245 mẫu** (755 mốc) mà `api_call` khớp `effective_signature` ở ít nhất một mốc chưa được chọn. Đây là **nghi vấn**, chưa xác nhận LLM bỏ sót version. Chi tiết: [possible_omitted_versions.csv](output/possible_omitted_versions.csv).

| Thư viện | O: số mẫu | U: số mẫu |
|---|---:|---:|
| numpy | 0 | 0 |
| pandas | 0 | 3 |
| pytorch | 16 | 7 |
| scikit-learn | 69 | 18 |
| scipy | 21 | 1 |
| seaborn | 39 | 30 |
| tensorflow | 5 | 18 |
| transformers | 9 | 9 |
| **Tổng** | **159** | **86** |

## 4. Mẫu chưa được gán version

Có **1.817 mẫu** với `versions=[]`. Bảng dưới đây đếm mẫu; nguyên nhân và các mốc có thể xét nằm trong [empty_samples_audit.csv](output/empty_samples_audit.csv).

| Thư viện | O: số mẫu | U: số mẫu |
|---|---:|---:|
| numpy | 0 | 1 |
| pandas | 0 | 0 |
| pytorch | 15 | 42 |
| scikit-learn | 4 | 1 |
| scipy | 17 | 84 |
| seaborn | 570 | 12 |
| tensorflow | 971 | 100 |
| transformers | 0 | 0 |
| **Tổng** | **1.577** | **240** |

- **numpy:** U có 1 lời gọi trong f-string bị bước trích `api_call` bỏ sót.
- **pandas, transformers:** không có mẫu rỗng.
- **pytorch:** O có 13 lời gọi không khớp chữ ký và 2 lời gọi f-string bị bỏ sót; U có 42 mẫu thiếu thông tin chữ ký để kết luận.
- **scikit-learn:** O có 3 mẫu `SimpleImputer` nghi bỏ sót version theo ánh xạ alias tới `Imputer` và 1 lời gọi không khớp chữ ký; U có 1 lời gọi không khớp chữ ký.
- **scipy:** O có 14 mẫu thiếu timeline, 2 lời gọi không khớp chữ ký, 1 lời gọi f-string bị bỏ sót; U có 81 mẫu thiếu timeline và 3 mẫu không có milestone callable.
- **seaborn:** O có 570, U có 12 mẫu thiếu timeline cho cặp API.
- **tensorflow:** O có 971 mẫu không có milestone callable; U có 96 mẫu tương tự, 3 lời gọi không khớp chữ ký và 1 mẫu model bỏ sót: `index=4656` trong `U_with_versions.json`, `tf.saved_model.load(model_path, tags=tag)` khớp các mốc 2.0.0, 2.3.0, 2.4.0, 2.6.0, 2.7.0, 2.9.0.

## 5. Hạn chế

Ghép đối số với `effective_signature` chỉ kiểm tra cú pháp lời gọi; mốc nghi bỏ sót chưa được xác nhận về hành vi runtime, kiểu giá trị hoặc cách xử lý `**kwargs`.
