# Thời gian chờ và thời gian tạo nội dung

Thời gian tạo ảnh hoặc video bắt đầu khi tác vụ có cả khe xử lý và tài khoản. Thời gian chờ ban đầu và thời gian chờ tài khoản khi thử lại tính vào ngân sách hàng đợi.

Trong **Muse → Cài đặt nâng cao**, đặt thời gian chờ riêng. API có xác thực `GET/PUT /admin/generation-queue` đọc hoặc lưu `{"queue_timeout":3600}` trong `data/generation-queue.json`. Giá trị `0` cho phép chờ không giới hạn. Các giới hạn tạo ảnh và video được cấu hình riêng.

Bản ghi tác vụ giữ tài khoản, lần thử, lỗi gốc, thời gian chờ, thời gian tạo và tổng thời gian. Nếu hết thời gian thử lại, hệ thống giữ lỗi gốc. Bản ghi cũ tiếp tục hiển thị thời lượng đã lưu.

Video luôn chạy bất đồng bộ. Với ảnh có thể chạy lâu hơn thời gian chờ HTTP của client, dùng `/v1/images/tasks` và kiểm tra trạng thái bằng mã tác vụ. Nếu lần kiểm tra bị timeout, tiếp tục dùng mã cũ. Nếu POST mất phản hồi trước khi nhận mã tác vụ, tra biên nhận bằng `GET /tasks/by-request` trên Base URL đã dùng và gửi kèm `Idempotency-Key` gốc. [Hướng dẫn theo dõi tác vụ](MAC2API_API_CONTRACT_TESTS.md#theo-dõi-tác-vụ-mất-kết-nối-và-hủy)

Sau khi khởi động lại, tác vụ ảnh và video còn trong hàng chờ có dữ liệu tạm được xử lý tiếp với cùng mã tác vụ và tính cả thời gian đã chờ. Quyền tài khoản được kiểm tra lại trước khi gửi. mac2API lưu nội dung và tệp tham chiếu tạm trong `data/pending-media/` cho yêu cầu quản trị và khóa người dùng có bật lưu nội dung. Tệp tạm được dọn khi tác vụ bắt đầu hoặc bị hủy.

Tác vụ đang chờ thiếu dữ liệu phục hồi được đánh dấu `SERVICE_RESTARTED_BEFORE_SEND`. Hãy gửi yêu cầu mới nếu vẫn cần kết quả. Tác vụ đang xử lý khi khởi động lại được đánh dấu cần đối chiếu, để bạn kiểm tra kết quả phía nhà cung cấp trước khi gửi lại.
