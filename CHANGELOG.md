# Nhật ký và bàn giao mac2API

## 1.6.21

- Tiện ích Chrome 1.0.13 cho phép liên kết hồ sơ ChatGPT đã đăng nhập trong lúc ô nhập chưa sẵn sàng. Khi chạy tác vụ, tiện ích kiểm tra ô nhập trong tab riêng trước khi gửi.
- Cải thiện nhận diện ô nhập ChatGPT và phân biệt lỗi đăng nhập với lỗi nhận diện ô nhập.

## 1.6.20

- ChatGPT và Gemini có hai cách kết nối trong mac2API. Tài khoản web dùng tiện ích Chrome 1.0.12. OpenAI API và Google Gemini API hỗ trợ văn bản và ảnh.
- MacFlow dùng `/chatgpt/v1` hoặc `/gemini/v1` với khóa người dùng mac2API. Chọn tài khoản được phép dùng cho từng khóa và chọn kết nối mặc định trong tab của dịch vụ.

## 1.6.19

- Thêm kết nối Gemini-Web2API cho `gemini-web/text` và `gemini-web/image`. MacFlow tiếp tục dùng địa chỉ `/gemini/v1`.
- Kết nối Gemini qua Chrome tiếp tục là mặc định cho đến khi chọn Gemini-Web2API và kiểm tra địa chỉ, khóa và mô hình. Có thể thử trước bằng một khóa người dùng được cấp riêng kết nối Gemini-Web2API.

## 1.6.18

- Kết nối ChatGPT qua máy chủ ChatGPT2API. Vào tab ChatGPT để lưu địa chỉ, khóa và kiểm tra mô hình.
- Giữ địa chỉ `/chatgpt/v1` cùng hai mô-đun `chatgpt/text` và `chatgpt/image`. Chọn ChatGPT2API trong phần quyền của khóa từng dùng hồ sơ Chrome được chỉ định.
- Tiện ích Chrome 1.0.11 hỗ trợ kết nối Muse và Gemini.

## 1.6.17

- Tiện ích Chrome 1.0.10 mở lại với chiều rộng đầy đủ. Phiên bản và ngôn ngữ nằm trên hàng riêng, còn thông báo kết nối nằm ngay dưới các nút.
- Khi liên kết ChatGPT, tiện ích nhận diện thêm ô nhập trong khu vực soạn tin và kiểm tra cả khi tab vẫn đang tải.

## 1.6.16

- Tải lại Muse một lần trong cùng phiên nếu ô nhập đã xuất hiện nhưng còn bị khóa. Chỉ gửi mô tả khi ô nhập thật sự sẵn sàng.

## 1.6.15

- Tiện ích Chrome 1.0.9 có nút **Kiểm tra kết nối**. Khi nhập Muse, tiện ích xác minh địa chỉ và khóa quản trị, rồi chỉ báo thành công khi nhận được mã tài khoản do mac2API trả về.
- Sau khi mac2API khởi động lại, tác vụ ảnh và video đang chờ có dữ liệu tạm sẽ tiếp tục với mã cũ. Tác vụ có thể đã gửi cần đối chiếu. Tác vụ dùng khóa đã tắt lưu nội dung được báo dừng trước khi gửi.

## 1.6.14

- Luồng tự động đọc lại tiến độ sau mất kết nối và tìm lại yêu cầu đã lưu nếu tải lại trang giữa lúc bấm Chạy. Nút Chạy lại tiếp tục kiểm tra tác vụ ảnh hoặc video đã gửi khi chưa đọc được kết quả.
- Giao diện giữ các thao tác chính ở màn hình làm việc. Địa chỉ kết nối, thời gian chờ và mã lỗi hiển thị trong **Cài đặt nâng cao**.
- Bảng điều khiển vẫn mở được khi trình duyệt chặn lưu dữ liệu cục bộ. Tệp ảnh và video tải xuống được khi dùng địa chỉ kết nối công khai.

## Luồng tự động và rà soát vận hành

- Bảng quản trị chạy tuần tự từng mục theo các bước văn bản, ảnh hoặc video, kèm ngữ cảnh chung và kết quả bước trước. Tiến độ được lưu, lỗi được cô lập theo mục và có thể chạy lại đúng bước.
- Lưu khóa quản trị và hồ sơ Docker qua lần khởi động lại. Cập nhật Git theo commit đã xác minh, kiểm tra gói trước khi thay tệp và xác nhận phiên dịch vụ mới trước khi tải lại bảng quản trị.
- Báo lỗi nhập sai khi chỉnh ảnh, thêm nút hủy tác vụ đang chờ và hiển thị khi tệp kết quả đã được dọn hoặc xóa.

## Cấp khóa API theo nhà cung cấp và tài khoản

- Chọn nhà cung cấp rồi chọn tài khoản trong cùng một hộp. Khóa mới dùng khả năng đã cấu hình của nhà cung cấp.
- Hạn mức, giá và lưu lịch sử được chỉnh trong **Cài đặt khóa**. Quyền cũ theo từng mô hình được giữ khi đổi tên và có thể mở rộng bằng lựa chọn rõ ràng.

## Đơn giản hóa kết nối Chrome 1.0.8

- Đặt kết nối trực tiếp và địa chỉ localhost làm mặc định cho ChatGPT Web và Gemini Web. Bảng quản trị có nút tải ZIP ghi rõ phiên bản và nút sao chép khóa quản trị.
- Tiện ích đợi ô chat xuất hiện và báo riêng lỗi quyền truy cập trang, đăng nhập hoặc máy chủ cũ. Yêu cầu liên kết từ tiện ích cũ vẫn có hướng dẫn cập nhật nếu xảy ra lỗi.

## Kết nối trực tiếp qua tiện ích Chrome 1.0.7

- ChatGPT Web và Gemini Web dùng hồ sơ Chrome đã đăng nhập thông qua tiện ích trên cùng máy. Mỗi tác vụ có tab riêng và kiểm tra quyền của khóa trước khi gửi.
- Hồ sơ tiện ích xuất hiện trong bảng quản trị và có thể được chọn theo từng khóa người dùng. mac2API theo dõi kết nối, thu hồi quyền ghép nối và ghi nhận hồ sơ thực hiện tác vụ.

## Báo cáo Tổng quan và xem kết quả API

- Tổng quan theo dõi lượt nhận và kết quả theo loại, hoạt động của từng khóa, hạn mức ngày và trạng thái tài khoản AI.
- Lịch sử API và lượt gọi gần đây có nút xem kết quả, xem trước tệp media và thông báo khi nội dung hết hạn hoặc chưa được lưu.

## Bảng quản trị API và tiện ích 1.0.6

- Giao diện mac2API dùng điều hướng theo nhóm, tổng quan nhiều nhà cung cấp và bố cục thích ứng trên thiết bị nhỏ. Có màn hình cấu hình OpenAI và Gemini API chính thức.
- Khóa API có tìm kiếm, bộ lọc và phân trang. Lịch sử API có trang riêng. Tác vụ có bộ lọc theo nhà cung cấp, trạng thái và mã.
- Mục Thử API dùng được khóa quản trị hoặc khóa của khách. Khi truy vấn trạng thái media lỗi, bảng điều khiển hiển thị mã tác vụ để tra cứu.

## Rà soát hàng đợi và đối chiếu sử dụng API

- Kiểm tra lại quyền trước mỗi lần thử gửi trên trình duyệt. Tác vụ Muse chỉ giữ khe Chromium khi đã có tài khoản được phép.
- Hai endpoint ảnh bất đồng bộ cũ dùng lại biên nhận khi gửi cùng Idempotency-Key. Lịch sử đối chiếu lại tác vụ media đã kết thúc sau lỗi ghi và phân biệt mất kết nối phía nhà cung cấp.

## Chỉnh sửa khóa người dùng và theo dõi sử dụng API

- Chọn mô hình và tài khoản theo từng khóa, sửa quyền ngay trên khóa hiện có, tạm khóa và đổi mã. Kiểm tra lại quyền trước khi gửi yêu cầu tới nhà cung cấp.
- Theo dõi tác vụ media và chat, tài khoản xử lý, hạn mức ngày, chi tiết lỗi, tiền thu và chi phí AI ước tính. Nội dung lịch sử có thời hạn khi được bật.

## Kho hồ sơ web và proxy theo tài khoản

- ChatGPT Web và Gemini Web có kho hồ sơ Chromium riêng, đăng nhập trực tiếp và kiểm tra trong bảng quản trị. Mỗi hồ sơ có nút bật, tắt và cấu hình proxy.
- Tác vụ chờ hồ sơ khả dụng trước khi lấy khe Chromium. Các hồ sơ có proxy dùng cấu hình đã lưu khi mở Chromium.

## Hàng đợi ChatGPT Web và Gemini Web

- Khi hết khe Chromium, `/capabilities` dùng lần xác minh gần nhất của kết nối hiện tại. Adapter ảnh có thể đưa tác vụ vào hàng đợi khi phiên hợp lệ và còn chỗ, giống luồng Muse.

## Ảnh tham chiếu và hàng đợi tạo ảnh

- Tác vụ tạo ảnh Muse nhận tối đa 20 ảnh tham chiếu. mac2API kiểm tra thứ tự và số tệp đã xác nhận ngay trước khi gửi.
- Hàng đợi tạo ảnh chứa tối đa 20 tác vụ đang chờ hoặc chạy, với tối đa 4 tác vụ ảnh mỗi khóa. `/capabilities` công bố hai giới hạn này và giới hạn ảnh tham chiếu.

## Thời lượng video Muse

- Adapter Muse 2.0.10 giữ các mốc chọn nhanh và thêm ô nhập số giây tùy chỉnh. Mục Thử API cũng nhận số giây dương ngoài các mốc có sẵn.
- API chuyển thời lượng đã chọn thành mục tiêu cho Muse. Khi bỏ trống, Muse làm theo mô tả và thiết lập của mình. Thời lượng thực tế tùy clip Muse trả về.

## Chẩn đoán lỗi tạo ảnh và video Muse

- Dùng cùng ô nhập đang hiển thị để điền mô tả và kiểm tra tệp. Cho nút Gửi tối đa 10 giây trong thời hạn tác vụ. Chi tiết tác vụ phân biệt ô nhập làm mất mô tả, thiếu nút Gửi và nút Gửi bị khóa.
- Yêu cầu chuyển trang phục giữa hai ảnh báo thiếu đầu vào ngay tại API nếu chỉ có một ảnh. Khi Muse phản hồi thiếu ảnh thứ hai, chi tiết tác vụ giữ nguyên phản hồi kèm mã chẩn đoán.
- Kiểm tra tệp tham chiếu từ khi nhận yêu cầu. Trình duyệt kiểm tra lại prompt và số tệp hiện có ngay lúc bấm Gửi.
- Khi trang Muse tải nhưng thiếu ô chat, thử tải lại một lần trong cùng phiên và tuyến proxy, sử dụng phần thời gian còn lại. Nếu trang vẫn kết nối workspace, đợi trên trang hiện tại.
- Chi tiết tác vụ hiển thị giai đoạn lỗi, số tệp đã nhận và mã lỗi. Đọc lại lỗi cũ để nhận diện trường hợp Muse báo thiếu tệp, giới hạn video theo khu vực hoặc đề nghị phương án thay thế.
- Sau khi bấm Gửi, mac2API đợi phản hồi và giữ trạng thái cần đối chiếu khi mất xác nhận. Adapter Muse 2.0.9 hiển thị lỗi thiếu tham chiếu khi API từ chối yêu cầu.

- Lời đề nghị tạo ảnh thay thế của Muse được báo là chờ duyệt với mã `MUSE_CONFIRMATION_REQUIRED`. Chỉ dẫn tạo ảnh và video cho phép chọn mặc định ở các chi tiết phụ còn thiếu.

## Hợp đồng API v1 cho MacFlow

- Thêm danh mục khả năng theo quyền nhà cung cấp, tra cứu bằng UUID và hủy tác vụ đang chờ. Tác vụ gián đoạn giữ biên nhận để đối chiếu.
- Giữ khóa người dùng và giới hạn hàng đợi. HTTP 429 trả mã lỗi và `Retry-After`. Kho media dùng URL ký; các trình duyệt gọi từ xa cần HTTPS.
- Ba adapter độc lập lưu UUID trước khi gửi và dùng GET để đối chiếu nếu mất biên nhận. Tài liệu và mẫu kiểm thử nằm trong [hợp đồng API](MAC2API_API_CONTRACT_TESTS.md).

## Khởi động bản đóng gói trên Mac

- `start-mac2api.command` chạy từ thư mục chứa mã, kiểm tra tệp giao diện và giữ cổng trước khi khởi tạo dịch vụ. Terminal in địa chỉ bảng quản trị hoặc báo cổng đang được sử dụng.
- Nếu thiếu tệp giao diện hoặc tệp không đọc được, `/` và `/admin` trả HTTP 503 với hướng dẫn tiếng Việt. Nhật ký máy chủ ghi đường dẫn và lỗi chi tiết.

## Tạo ảnh và video trực tiếp trong Muse

- Chỉ dẫn tạo media yêu cầu Muse đính kèm tệp kết quả ngay trong lượt hiện tại khi mô tả đã đủ.
- Khi Muse chỉ gửi phương án chờ duyệt hoặc trả văn bản, mac2API báo rõ lý do bằng tiếng Việt. Adapter Muse 2.0.4 hiển thị tiếng Việt cho cả lỗi chỉ trả văn bản được lưu ở bản cũ.

## Kiểm tra trang Muse và thông báo quá hạn

- Muse có thể bắt đầu khi ô chat đã sẵn sàng trong lúc tài nguyên phụ còn tải. Lỗi điều hướng rõ ràng được phát hiện sớm. Một số lỗi kết nối tạm thời được thử lại một lần trong cùng phiên trình duyệt và cùng tuyến proxy của tài khoản.
- Khi ô chat chưa xuất hiện, tác vụ dừng trước khi gửi nội dung và giữ trạng thái xác thực đã có. Cookie được Muse đổi trong lần kiểm tra được lưu ngay cả khi trang chưa sẵn sàng.
- Adapter Muse 2.0.3 dịch cả lỗi quá hạn đã lưu từ phiên bản cũ sang tiếng Việt.

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
