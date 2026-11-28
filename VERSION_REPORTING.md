# Phiên bản đã cài và cập nhật

Kiểm tra phiên bản báo riêng `version.json`, commit cục bộ và trạng thái kho cập nhật. mac2API đọc đầu nhánh bằng Git và đối chiếu lịch sử trong thư mục tạm nếu GitHub API không phản hồi. Thao tác kiểm tra giữ nguyên thư mục đang chạy. Khi chưa đối chiếu được, bảng cập nhật hiển thị lý do.

`MUSE2API_REPOSITORY` mặc định là `mktminhqpbga-glitch/mac2API` và `MUSE2API_REPO_REF` mặc định là `main`. Có thể chỉ định nguồn khác bằng hai biến này. Chỉ commit từ xa nối tiếp commit đang chạy mới được báo là bản cập nhật. Nguồn cũ hơn, nhánh đã tách hoặc quan hệ chưa xác minh đều không được cập nhật trực tiếp. Khi dựng image Docker, dùng `--build-arg BUILD_SHA=$(git rev-parse HEAD)` để nhúng commit. SHA nhúng được ưu tiên hơn tệp `.installed_sha` cũ. Render dùng `RENDER_GIT_COMMIT` khi chưa nhúng SHA và được nhận diện là môi trường triển khai quản lý. Các nền tảng khác có thể đặt `MUSE2API_MANAGED_DEPLOYMENT=1` để cập nhật qua bản dựng image.

Khi nâng cấp bản cài từ mã nguồn, hệ thống ghim SHA đầy đủ đã xác minh, kiểm tra kết quả lệnh Git và giữ `.env` cùng `data/`. SHA mới được ghi sau khi nâng cấp thành công. Chờ hàng đợi trống và tác vụ đang chạy kết thúc trước khi nâng cấp.

Gói ZIP từ GitHub kể từ 1.6.30 chứa mã commit của gói. Nếu bản cũ báo thiếu mã commit, tải và giải nén gói mới vào thư mục riêng rồi dùng lại thư mục cấu hình cũ qua `MUSE2API_HOME` theo README.

`start-mac2api.command` tự mở lại dịch vụ khi cập nhật thành công. Nếu chạy trực tiếp bằng Uvicorn, bảng điều khiển sẽ nhắc đóng và mở lại ứng dụng. Bản Docker và Render cập nhật bằng cách dựng lại image từ nguồn đã cấu hình.
