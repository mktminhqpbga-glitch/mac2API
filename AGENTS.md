# Làm việc với mac2API

- Đọc [README](README.md) để biết cách chạy và [nhật ký bàn giao](CHANGELOG.md) để nắm các thay đổi đã triển khai.
- Dùng checkout hiện có trong môi trường cloud. Chỉ tạo Git worktree khi người dùng yêu cầu.
- Kiểm thử trong thư mục dữ liệu tạm. Giữ cookie, API key, thông tin proxy, cấu hình runtime và kết quả tạo nội dung ngoài Git.
- Giữ tương thích các endpoint và model Muse cũ. Chuyển tiếp model có tiền tố `openai/` hoặc `gemini/` qua API chính thức của nhà cung cấp.
- Mọi thao tác dùng Chromium từ ứng dụng phải giữ khe `GEN_LOCK` cho đến khi đóng phiên riêng. Nếu thêm điểm gọi Chromium, kiểm thử tình huống tác vụ mới bắt đầu lúc trình duyệt đang tắt vì rảnh.
- Khi báo kết quả, ghi riêng kiểm thử mô phỏng, kiểm thử có Chromium, yêu cầu Muse thật và trạng thái triển khai Render.
