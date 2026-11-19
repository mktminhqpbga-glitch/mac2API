# Nhật ký và bàn giao mac2API

## Adapter Muse Video 2.0.2

- Giá trị tắt mặc định của tùy chọn âm thanh và hình mờ được chấp nhận khi tạo video. Các tùy chọn bật hoặc tệp tham chiếu chưa được adapter hỗ trợ có thông báo riêng bằng tiếng Việt. Bản sửa có trong `mac2ApiMuse.ts` và `mac2Api.ts`.

## mac2API 1.6.12 · 19/11/2026

Bản tổng hợp gồm máy chủ, bảng quản trị, tiện ích 1.0.5 và bộ chuyển đổi MacFlow/Toonflow 2.0.1. Tài liệu kết nối Chrome hiện tại nằm trong README.

### Ổn định hàng đợi, trạng thái tác vụ và kết nối Chrome

- Lệnh DevTools có thời hạn riêng, tính cả thời gian đợi kết nối dùng chung. Sau khi khởi động lại, chế độ hồ sơ Chrome đang đăng nhập chờ xác minh lại trước khi chạy tác vụ.
- API video hỗ trợ `Idempotency-Key`. Yêu cầu tạo ảnh hoặc video lặp lại có thể lấy mã đã lưu trước khi tải lại tệp tham chiếu. Các khóa ảnh đã lưu ở bản trước vẫn dùng được.
- Lỗi ghi tác vụ mới trả HTTP 507 và trả lại khe hàng đợi. Tác vụ bị lỗi khi khởi động worker được đánh dấu thất bại. Nếu kho trả lỗi ghi trong lúc chạy, trạng thái quan sát được vẫn có thể đọc từ tiến trình và lỗi ghi được ghi vào nhật ký.
- Cài đặt hàng đợi hỏng dùng giá trị mặc định khi khởi động. Ảnh Base64 đã bị dọn khỏi kho trả HTTP 410 với lý do rõ ràng.

### Dùng hồ sơ Chrome đang đăng nhập

- Hai tab ChatGPT Web và Gemini Web có nút **Kết nối Chrome** dùng quyền gỡ lỗi từ xa do Chrome 144 trở lên cấp. Chọn dịch vụ trong tiện ích để xác minh hồ sơ tương ứng. Mỗi tác vụ chỉ mở và đóng tab do mac2API tạo.
- Chuyển thông tin tải tiện ích sang **Nhập cookie**. Tiện ích 1.0.5 nhận cả Base URL chung và Base URL theo nhà cung cấp.

### Base URL theo nhà cung cấp và khóa người dùng

- Thêm `/muse/v1`, `/chatgpt/v1` và `/gemini/v1`. Danh sách model và truy vấn tác vụ theo đúng nhà cung cấp. Giữ `/v1` dùng chung và khóa quản trị hiện có.
- Trang **Khóa người dùng** tạo và thu hồi từng khóa `m2c_…`, giới hạn nhà cung cấp, tần suất gọi và số tác vụ đồng thời. Mỗi khóa chỉ truy vấn tác vụ của chính nó. Tệp media từ các khóa này có liên kết tải có chữ ký, hỗ trợ tải kết quả từ MacFlow.

### Xác nhận ảnh tham chiếu và chẩn đoán tác vụ

- Nhận ảnh xem trước đã tải trong ô nhập, kể cả khi nút gỡ tệp không xuất hiện. Chờ xác nhận từng tệp trước khi đưa tệp tiếp theo vào ô tải lên. Lỗi ghi số tệp xác nhận và vị trí tệp đang chờ.
- Adapter MacFlow 2.0.1 hiển thị mã tác vụ, trạng thái và lý do từ mac2API. Thông báo ẩn khóa và URL trong chi tiết lỗi.

### Kết nối tương thích qua cổng DevTools cố định

- Cấu hình cổng cố định qua `MUSE2API_WEB_CDP_PORT` vẫn dùng được cho các máy đang vận hành theo cách này.
- Khi chạy trên Render, bảng điều khiển giải thích rằng Chrome trên máy Mac không thể dùng làm trình duyệt cục bộ của Render.

### Xác minh dịch vụ từ tiện ích Chrome

- Ba nút Muse, ChatGPT và Gemini đối chiếu tên miền của tab đang mở. Muse nhập cookie như trước. ChatGPT và Gemini xác minh tab qua mã một lần trên đúng Chrome cục bộ, không gửi cookie web.
- Bảng quản trị hiển thị thời điểm xác minh ở đúng tab nhà cung cấp. Trạng thái này tồn tại trong tiến trình đang chạy. Thử chat để xác nhận tài khoản trả lời.

### Dọn bản nháp và hiển thị lỗi Muse

- Trước khi gửi tác vụ mới, xóa prompt, các mục đính kèm và tệp đã chọn còn sót trong ô nhập. Muse xác nhận ô nhập trống hai lần và thử lại trong phiên riêng nếu tệp cũ bị kẹt. ChatGPT Web và Gemini Web kiểm tra ngay trong tab tác vụ.
- Nếu ô nhập vẫn còn tệp hoặc văn bản cũ, dừng trước khi gửi. Khi không xác định được Muse đã nhận yêu cầu, dừng tác vụ để tránh gửi lặp qua tài khoản khác.
- Ghi chú tài khoản và chi tiết tác vụ dịch lỗi tải tệp tham chiếu cũ sang tiếng Việt. Khi có lỗi mới, nhật ký ghi số tệp đã xác nhận mà không ghi nội dung tệp.

### Mô-đun web và tệp nhập MacFlow/Toonflow

- Thêm định tuyến `muse/text`, `muse/image`, `muse/video`, `chatgpt/text`, `chatgpt/image`, `gemini-web/text` và `gemini-web/image` qua cùng API. Các tên Muse cũ và API chính thức đã cấu hình tiếp tục hoạt động.
- ChatGPT Web và Gemini Web dùng hồ sơ Chrome đã đăng nhập trên cùng máy qua cổng DevTools cục bộ. Đã kiểm thử Chromium trên trang mô phỏng. Cần xác minh bằng tài khoản web thật trong môi trường của người dùng.
- Tệp [mac2Api.ts](adapters/mac2Api.ts) cho màn hình nhập nhà cung cấp media. Adapter tạo và thăm dò tác vụ ảnh/video, lưu mã tác vụ để tiếp tục, hỗ trợ hủy chờ và dùng xác thực của mac2API.

### Bản sửa kiểm tra cập nhật

- Mặc định theo dõi fork `mktminhqpbga-glitch/mac2API`. Kiểm tra quan hệ commit để phân biệt bản mới, nguồn cũ hơn và nhánh đã tách. Khi không xác minh được, bảng quản trị không cho tự ghi đè.
- Render lấy SHA từ thông tin bản dựng hoặc `RENDER_GIT_COMMIT`. Triển khai trên Render qua quy trình của Render.

### Bản làm việc ngày 19/11/2026

- **Chromium tự tắt khi rảnh.** `MUSE2API_CHROMIUM_IDLE_SECONDS` mặc định 60 giây. Đồng hồ tính từ lúc thao tác dùng Chromium cuối cùng trả khe `GEN_LOCK`. Vòng kiểm tra không tắt khi còn khe đang dùng hoặc tác vụ ảnh/video đang chờ. Chỉ tắt tiến trình do ứng dụng khởi động. Yêu cầu Muse sau đó khởi động lại theo luồng sẵn có.
- Khi bật chế độ tự tắt, ứng dụng bỏ bước mở Chromium để làm nóng lúc khởi động. Duy trì phiên bằng HTTP theo lịch vẫn hoạt động. OpenAI và Gemini dùng kết nối HTTPS riêng.
- Đặt `MUSE2API_CHROMIUM_IDLE_SECONDS=0` để giữ Chromium chạy theo cách cũ. Với Render Free, có thể đặt thêm `MUSE2API_CONCURRENCY=1` để giảm đỉnh RAM. Chromium có thể mất vài giây để mở lại. Một tác vụ lớn vẫn có thể vượt giới hạn RAM của dịch vụ.
- Nhật ký ứng dụng ghi `Chromium đã tắt sau 60 giây không có tác vụ Muse.` khi quy trình dừng hoàn tất. Kiểm tra `/readyz` để xem `browser_running`, và Render Metrics để đo RAM thật.

- Kiểm thử đã chạy với đồng hồ mô phỏng ở mốc 60 giây và với Chromium thật ở mốc rút gọn 1 giây. Bộ kiểm thử đồng thời, duy trì phiên, proxy, Quota và ảnh bất đồng bộ đều đạt. Chưa đo RAM trên Render.

### Commit `8845100` trên GitHub

- Quota chờ tối đa 60 giây để lấy khóa tài khoản sau khi có khe xử lý. Các thao tác quản trị khác vẫn báo bận ngay khi có xung đột.
- Chromium nhận diện **Cài đặt → Mức sử dụng**. Bộ phân tích đọc gói, phần trăm, ngày đặt lại và token bổ sung bằng tiếng Việt, Anh và Trung. Thiếu số phần trăm tuần hợp lệ thì giữ cache hạn mức cũ.
- Kiểm thử Quota bao phủ định dạng bản địa hóa, chờ khóa và cách ly phiên. Dữ liệu tài khoản dùng trong kiểm thử là dữ liệu giả.

### Commit `51f5332` trên GitHub

- Việt hóa bảng quản trị, tiện ích Chrome/Edge, tập lệnh Tampermonkey, công cụ nhập cookie và tài liệu. Giao diện của người dùng mới mặc định tiếng Việt. Trình duyệt giữ lựa chọn ngôn ngữ đã lưu.
- Thêm **Ngôn ngữ chỉ dẫn Muse** trong **Quản lý tài khoản**. Giá trị lưu ở `data/muse-prompt.json`. Chọn Việt, Anh hoặc Trung cho chỉ dẫn tự ghép vào yêu cầu Muse. Kết quả phản hồi do Muse quyết định.
- Thêm tab OpenAI và Gemini, cấu hình model và khóa riêng. `/v1/chat/completions` định tuyến theo tiền tố model và hỗ trợ SSE. OpenAI/Gemini cần khóa hợp lệ và quyền kết nối tới tên miền API chính thức. Các tên model Muse cũ vẫn được hỗ trợ.
- Proxy HTTP nhận chuỗi `IP:PORT:USER:PASS`. Dùng **Kiểm tra kết nối** trước khi lưu để phân biệt HTTP 407, kết nối và phiên đăng nhập Muse. SOCKS5/SOCKS5h dùng URL có giao thức.
- Kho media tối đa 5 GB. Từ 3,5 GB, tự dọn tệp cũ nhất và ảnh thu nhỏ. Thư viện hiển thị tệp mới nhất trước. Lịch sử tác vụ được giữ lại, còn URL tệp đã dọn sẽ trả 404.

### Vận hành và kiểm thử

- Mã nguồn nằm ở repo `mktminhqpbga-glitch/mac2API`, nhánh `main`. Render triển khai từ GitHub. Đối chiếu SHA trong Render rồi thử `/healthz` và `/admin` để xác nhận một bản cụ thể đã chạy.
- Khi triển khai lại, giữ biến môi trường và ổ dữ liệu nếu dịch vụ có ổ đĩa bền vững. Render Free có thể mất cấu hình hoặc media lưu cục bộ sau deploy hoặc khởi động lại.
- Kiểm thử liên quan gồm `tests/test_concurrency.py`, `tests/test_quota_localization.py`, `tests/test_quota_account_binding.py`, `tests/test_account_proxy_routing.py`, `tests/test_chat_providers.py`, `tests/test_muse_prompt_language.py`, `tests/test_media_storage.py`, `tests/test_proxy_url_import.py` và `node --test tests/*.mjs`.
- MacFlow chưa được cấu hình trong công việc này. Các giá trị kết nối là Base URL của mac2API có hậu tố `/v1`, API Key của mac2API và model ID tương ứng.
