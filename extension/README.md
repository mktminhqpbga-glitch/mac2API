# Tiện ích nhập cookie Muse2API

Tiện ích gửi cookie đăng nhập Muse.ai từ cửa sổ hiện tại đến địa chỉ Muse2API bạn cung cấp.

1. Tải tệp ZIP từ bảng quản trị rồi giải nén vào thư mục cố định, chẳng hạn `D:\muse2api-extension` hoặc `~/Documents/muse2api-extension`.
2. Mở `chrome://extensions` hoặc `edge://extensions`, bật **Chế độ dành cho nhà phát triển** và chọn **Tải tiện ích đã giải nén**. Chọn thư mục chứa `manifest.json`.
3. Đăng nhập Muse.ai đến khi thấy giao diện chat. Trong chính cửa sổ đó, mở tiện ích **v1.0.12**, dán địa chỉ dịch vụ và API Key từ mục **Quản lý tài khoản**.
4. Bấm **Kiểm tra kết nối** để kiểm tra địa chỉ, khóa quản trị và dữ liệu đăng nhập. Bấm **Muse** để nhập tài khoản, rồi dùng nút **Kiểm tra** trong mục **Muse** của mac2API để xác minh phiên.

Cấp quyền chạy trong cửa sổ ẩn danh hoặc InPrivate khi cần. Khi cập nhật, thay tệp trong thư mục cũ rồi bấm **Tải lại** cho từng hồ sơ trình duyệt. Với lỗi HTTP 401 từ Muse, hãy xác minh phiên trên Muse.ai rồi nhập lại. Với lỗi API Key của dịch vụ, sao chép khóa mới từ bảng quản trị. Với lỗi kết nối, kiểm tra URL và chứng chỉ HTTPS của dịch vụ.

Tiện ích dùng quyền cookie cho Muse, quyền chạy mã trên tab AI và lịch đánh thức để duy trì kết nối Chrome. Để gỡ, mở trang quản lý tiện ích của Chrome hoặc Edge rồi chọn **Xóa**.

## Kết nối ChatGPT và Gemini

Mở tab AI đã đăng nhập trong cửa sổ Chrome thường. Dán khóa quản trị mac2API rồi bấm **ChatGPT** hoặc **Gemini** theo trang đang mở. Địa chỉ mặc định là `http://127.0.0.1:18610` và có thể đổi nếu mac2API dùng cổng khác. Giữ Chrome mở và thử chat trong mục tương ứng của mac2API. Tiện ích tự mở tab cho từng tác vụ và dùng phiên đăng nhập trong hồ sơ đó.

Với Gemini, nếu đang dùng kết nối Chrome DevTools trong mac2API, mở **Tùy chọn kết nối nâng cao** rồi bỏ chọn chế độ chạy trực tiếp trước khi bấm tên dịch vụ để xác minh tab như trước.
