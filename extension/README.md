# Tiện ích nhập cookie Muse2API

Tiện ích gửi cookie đăng nhập Muse.ai từ cửa sổ hiện tại đến địa chỉ Muse2API bạn cung cấp.

1. Tải tệp ZIP từ bảng quản trị rồi giải nén vào thư mục cố định, chẳng hạn `D:\muse2api-extension` hoặc `~/Documents/muse2api-extension`.
2. Mở `chrome://extensions` hoặc `edge://extensions`, bật **Chế độ dành cho nhà phát triển** và chọn **Tải tiện ích đã giải nén**. Chọn thư mục chứa `manifest.json`.
3. Đăng nhập Muse.ai đến khi thấy giao diện chat. Trong chính cửa sổ đó, mở tiện ích **v1.0.7**, dán địa chỉ dịch vụ và API Key từ mục **Quản lý tài khoản**.
4. Bấm **Muse**, kiểm tra nguồn cookie hiển thị, rồi dùng nút **Kiểm tra** trong bảng quản trị để xác minh phiên đã nhập.

Cấp quyền chạy trong cửa sổ ẩn danh hoặc InPrivate khi cần. Nếu đã dùng phiên bản 1.0.2 hoặc cũ hơn, thay tệp trong thư mục cũ rồi bấm **Tải lại** cho từng hồ sơ trình duyệt. Với lỗi HTTP 401 từ Muse, hãy xác minh phiên trên Muse.ai rồi nhập lại. Với lỗi API Key của dịch vụ, sao chép khóa mới từ bảng quản trị. Với lỗi kết nối, kiểm tra URL và chứng chỉ HTTPS của dịch vụ.

Tiện ích dùng quyền cookie cho Muse, quyền chạy mã trên tab AI và lịch đánh thức để duy trì kết nối Chrome. Để gỡ, mở trang quản lý tiện ích của Chrome hoặc Edge rồi chọn **Xóa**.

## Kết nối ChatGPT Web và Gemini Web

Mở tab AI đã đăng nhập trong cửa sổ Chrome thường. Điền địa chỉ mac2API trên cùng máy, ví dụ `http://127.0.0.1:18610`, khóa quản trị và nhãn hồ sơ. Để bật **Chạy ChatGPT và Gemini trực tiếp bằng tiện ích**, rồi bấm **ChatGPT** hoặc **Gemini**. Giữ Chrome mở và thử chat trong mục tương ứng của mac2API. Tiện ích tự mở tab cho từng tác vụ và dùng phiên đăng nhập trong hồ sơ đó.

Nếu đang dùng kết nối Chrome DevTools trong mac2API, bỏ chọn chế độ chạy trực tiếp trước khi bấm tên dịch vụ để xác minh tab như trước.
