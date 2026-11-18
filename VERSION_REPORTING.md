# Phiên bản đã cài và cập nhật

Kiểm tra phiên bản báo riêng `version.json`, commit cục bộ và trạng thái kho cập nhật. Thao tác chỉ đọc. Khi kiểm tra thất bại, trạng thái phiên bản là chưa xác minh.

`MUSE2API_REPOSITORY` mặc định là `czg86389-hub/muse2api` và `MUSE2API_REPO_REF` mặc định là `main`. Có thể đổi hai biến khi triển khai fork. Khi dựng image Docker, dùng `--build-arg BUILD_SHA=$(git rev-parse HEAD)` để nhúng commit. SHA nhúng được ưu tiên hơn tệp `.installed_sha` cũ. Đặt `MUSE2API_MANAGED_DEPLOYMENT=1` để cập nhật bằng bản dựng image.

Khi nâng cấp bản cài từ mã nguồn, hệ thống ghim SHA đầy đủ đã xác minh, kiểm tra kết quả lệnh Git và giữ `.env` cùng `data/`. SHA mới được ghi sau khi nâng cấp thành công. Chờ hàng đợi trống và tác vụ đang chạy kết thúc trước khi nâng cấp.
