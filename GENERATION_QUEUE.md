# Thời gian chờ và thời gian tạo nội dung

Thời gian tạo ảnh hoặc video bắt đầu khi tác vụ có cả khe xử lý và tài khoản. Thời gian chờ ban đầu và thời gian chờ tài khoản khi thử lại tính vào ngân sách hàng đợi.

Trong mục Quản lý tài khoản, đặt thời gian chờ riêng. API có xác thực `GET/PUT /admin/generation-queue` đọc hoặc lưu `{"queue_timeout":3600}` trong `data/generation-queue.json`. Giá trị `0` cho phép chờ không giới hạn. Cài đặt áp dụng cho tác vụ mới. Các giới hạn tạo ảnh và video được cấu hình riêng.

Bản ghi tác vụ giữ tài khoản, lần thử, lỗi gốc, thời gian chờ, thời gian tạo và tổng thời gian. Nếu hết thời gian thử lại, hệ thống giữ lỗi gốc. Bản ghi cũ tiếp tục hiển thị thời lượng đã lưu.

Video luôn chạy bất đồng bộ. Với ảnh có thể chạy lâu hơn thời gian chờ HTTP của client, dùng `/v1/images/tasks` và kiểm tra trạng thái bằng mã tác vụ. Nếu lần kiểm tra bị timeout, hãy tiếp tục dùng mã cũ để tránh tạo trùng. Nếu POST video hoặc ảnh bất đồng bộ mất phản hồi, gửi lại cùng nội dung và `Idempotency-Key` đã dùng để lấy mã tác vụ đã tiếp nhận.
