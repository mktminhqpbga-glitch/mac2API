# MUSE2API

Bản tổng hợp mac2API 1.6.12 gồm mã máy chủ, bảng quản trị, tiện ích Chrome/Edge 1.0.5 và các adapter MacFlow/Toonflow. [Nhật ký phiên bản](CHANGELOG.md)

MUSE2API cung cấp API kiểu OpenAI để gọi các tính năng chat, tạo ảnh và tạo video của [Muse.ai](https://muse.ai/). Máy chủ Python chạy FastAPI và điều khiển Chromium qua Chrome DevTools Protocol (CDP). Muse.ai thực hiện việc tạo nội dung. Dịch vụ quản lý cookie, hàng đợi, tài khoản, proxy và kết quả tải về.

## Chạy trực tiếp trên Mac

Giải nén cả gói và mở Terminal tại thư mục chứa `app.py`, `admin.html` và `start-mac2api.command`. Cài thư viện lần đầu rồi khởi động dịch vụ.

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
bash start-mac2api.command
```

Mở địa chỉ Terminal in ra, mặc định là [http://127.0.0.1:18610/admin](http://127.0.0.1:18610/admin). Nếu cổng đang được dùng, chờ tác vụ của bản cũ kết thúc rồi dừng tiến trình đó trước khi chạy lại.

Để dùng lại cấu hình và dữ liệu, đặt `MUSE2API_HOME` thành thư mục chứa `.env` và `data/` của bản cũ khi chạy lệnh khởi động. Bảng quản trị vẫn được đọc từ thư mục mã mới.

```bash
MUSE2API_HOME="/duong/dan/ban-cu" bash start-mac2api.command
```

## Triển khai bằng Docker Compose

Docker Compose là cách được khuyến nghị. Image đã có Chromium và FFmpeg.

```bash
git clone https://github.com/mktminhqpbga-glitch/mac2API.git
cd mac2API
cp .env.example .env
```

Điền `MUSE2API_KEY` của bạn trong `.env`, rồi khởi động:

```bash
docker compose up -d
```

Mở `http://<máy-chủ>:18610/admin` và nhập API Key. Kiểm tra `/healthz` để xác nhận HTTP hoạt động và `/readyz` để xem có tài khoản Muse đang bật hay chưa.

## Triển khai trực tiếp trên Debian hoặc Ubuntu

```bash
sudo apt-get update
sudo apt-get install -y chromium ffmpeg fonts-wqy-zenhei python3 python3-pip python3-venv
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 18610
```

Nếu Chromium nằm ngoài PATH, đặt `MUSE2API_CHROMIUM` thành đường dẫn đến tệp thực thi. Để chạy dưới systemd, điều chỉnh đường dẫn và người dùng trong [tệp dịch vụ mẫu](deploy/muse2api.service). Nếu dùng Nginx, tham khảo [cấu hình mẫu](deploy/nginx.example.conf). Đặt `proxy_read_timeout` và `proxy_send_timeout` thành `600s`, tắt `proxy_buffering` cho phản hồi SSE và đặt giới hạn kích thước tải lên theo nhu cầu.

## Nhập tài khoản Muse

1. Tải tiện ích từ bảng quản trị hoặc nạp [thư mục tiện ích](extension/) dưới dạng tiện ích đã giải nén trong Chrome hoặc Edge.
2. Đăng nhập Muse.ai và mở tiện ích **v1.0.5** trong chính cửa sổ đó. Với chế độ ẩn danh hoặc InPrivate, hãy cấp quyền chạy tiện ích trong chế độ tương ứng.
3. Điền địa chỉ dịch vụ và API Key, rồi bấm **Đọc và nhập**.
4. Trong mục **Quản lý tài khoản**, bấm **Kiểm tra** để mở Muse bằng Chromium trên máy chủ và xác minh phiên.

Cookie bắt buộc là `hatch_sess`. `hatch_vml` và `hatch_gw` có thể vắng mặt. Bạn cũng có thể dùng [tập lệnh Tampermonkey](tools/muse2api_cookie_importer.user.js), [tập lệnh Python](tools/get_muse_cookie.py) hoặc biểu mẫu dán cookie trong bảng quản trị. Sau khi cập nhật máy chủ, hãy tải lại tiện ích đã giải nén trong từng hồ sơ trình duyệt.

## Proxy theo tài khoản

Trong mục **Quản lý tài khoản**, mở **Proxy** bên cạnh tài khoản và chọn **Dùng proxy**. Máy chạy dịch vụ phải truy cập được endpoint HTTP, SOCKS5 hoặc SOCKS5h. Bạn có thể dán dạng `IP:PORT:USER:PASS` cho proxy HTTP (ví dụ `192.0.2.10:8080:user:pass`), dán URL đầy đủ hoặc điền thông tin xác thực riêng. Với định dạng bốn phần, mật khẩu được đọc nguyên văn, kể cả dấu hai chấm. Với SOCKS5, dùng URL có tiền tố `socks5://` hoặc `socks5h://`. Mã hóa phần trăm các ký tự đặc biệt trong thông tin đăng nhập khi đặt chúng trong URL. Endpoint proxy HTTPS, VLESS, VMess và liên kết đăng ký cần một cổng HTTP hoặc SOCKS5 trung gian.

1. Bấm **Kiểm tra kết nối** trước khi lưu. Kết quả ghi riêng kết nối proxy, IP đầu ra và phiên Muse. HTTP 407 nghĩa là proxy từ chối xác thực. Hãy kiểm tra tên đăng nhập và mật khẩu.
2. Bấm **Lưu** để áp dụng cho yêu cầu tiếp theo.
3. Bấm **Kiểm tra** trên tài khoản để xác minh tải được ô chat trong Chromium.

Chat, ảnh, video, tải xuống trong trình duyệt, hạn mức và duy trì phiên dùng proxy của tài khoản. Nếu proxy gặp lỗi, dịch vụ báo lỗi và giữ nguyên tuyến kết nối. Việc tải tệp tham chiếu từ URL bên ngoài có thể đi theo tuyến khác vì được thực hiện trước khi chọn tài khoản. Địa chỉ `127.0.0.1` là máy chạy MUSE2API. API trả HTTP 409 nếu tài khoản đang bận khi đổi proxy.

Gọi API quản trị bằng `Authorization: Bearer <key>`. `PATCH /admin/accounts/{account_id}` nhận `{"proxy":{"server":"socks5://proxy.example.com:1080","username":"user","password":"password"}}`. Giá trị `{"proxy":null}` xóa proxy. `POST /admin/accounts/{account_id}/proxy/test` kiểm tra cấu hình đã lưu hoặc đối tượng `proxy` gửi kèm. Khi địa chỉ và tên đăng nhập giữ nguyên, để trống mật khẩu để giữ mật khẩu đã lưu.

## Gọi API

Các endpoint được bảo vệ cần `Authorization: Bearer <key>`. Trong các ví dụ, đặt `BASE_URL` và `MUSE2API_KEY` trong terminal.

```bash
BASE_URL=http://127.0.0.1:18610
```

| Tác vụ | Endpoint |
|---|---|
| Liệt kê model | `GET /v1/models` |
| Chat và tạo mã | `POST /v1/chat/completions`, `POST /v1/responses` |
| Tạo ảnh | `POST /v1/images/generations` |
| Chỉnh sửa ảnh | `POST /v1/images/edits` |
| Ảnh bất đồng bộ | `POST /v1/images/tasks`, `GET /v1/images/tasks/{task_id}` |
| Video bất đồng bộ | `POST /v1/videos`, `GET /v1/videos/{task_id}` |
| Tải tệp kết quả | `GET /v1/media/{name}` |

MacFlow dùng khóa `m2c_…` để đọc `GET /v1/capabilities`, tra cứu `GET /v1/tasks/by-request` bằng UUID đã lưu và hủy tác vụ chờ qua các tuyến `/cancel`. Xem [hợp đồng API và mẫu HTTP đã kiểm thử](MAC2API_API_CONTRACT_TESTS.md).

Model chính gồm `muse-spark`, `muse-image` và `muse-video`. Bí danh như `gpt-4o`, `claude-sonnet-4` và `gemini-2.5-pro` được ánh xạ sang Muse. Với chat, thêm `"stream":true` để nhận SSE. Khả năng gọi công cụ phụ thuộc vào phản hồi Muse.

### Chat

```bash
curl "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $MUSE2API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"muse-spark","messages":[{"role":"user","content":"Giải thích cách sắp xếp nhanh bằng Python."}],"stream":false}'
```

### Tạo hoặc chỉnh sửa ảnh

```bash
curl "$BASE_URL/v1/images/generations" \
  -H "Authorization: Bearer $MUSE2API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"muse-image","prompt":"Một chú chó Shiba đeo mũ phi hành gia","size":"16:9","response_format":"url"}'
```

Dùng `response_format: b64_json` để nhận dữ liệu base64. Dùng `images` theo thứ tự mong muốn khi có nhiều tham chiếu. Với endpoint chỉnh sửa, có thể lặp trường multipart `image`:

```bash
curl "$BASE_URL/v1/images/edits" \
  -H "Authorization: Bearer $MUSE2API_KEY" \
  -F 'prompt=Phối hai ảnh tham chiếu thành ảnh mới' \
  -F 'image=@subject.png' -F 'image=@style.png'
```

### Video

```bash
curl "$BASE_URL/v1/videos" \
  -H "Authorization: Bearer $MUSE2API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Lá phong rơi dưới ánh nắng","duration":5,"size":"16:9"}'

curl "$BASE_URL/v1/videos/<task_id>" \
  -H "Authorization: Bearer $MUSE2API_KEY"
```

Dùng `images` để gửi ảnh tham chiếu cho video. Ảnh đầu tiên là tham chiếu chính. Thời lượng thực tế do Muse quyết định. Khi tác vụ thành công, đọc `result.url`. Nếu mất kết nối trong lúc kiểm tra tiến độ, tiếp tục dùng mã tác vụ đã có. Xem [quy tắc hàng đợi](GENERATION_QUEUE.md).

## Ngôn ngữ chỉ dẫn Muse

Trong **Quản lý tài khoản**, chọn **Ngôn ngữ chỉ dẫn Muse** rồi bấm **Lưu ngôn ngữ chỉ dẫn**. Có ba lựa chọn là tiếng Việt (mặc định), tiếng Anh và tiếng Trung. Cài đặt lưu trong `data/muse-prompt.json`, áp dụng cho yêu cầu Muse mới và giữ nguyên prompt đầu vào. Phần chỉ dẫn tạo ảnh/video đề nghị Muse xác nhận bằng ngôn ngữ đã chọn. Muse tự quyết định lời đáp thực tế. Chat chỉ có một tin nhắn người dùng vẫn gửi trực tiếp. Khóa và tin nhắn gửi tới OpenAI hoặc Gemini sử dụng luồng riêng.

## Base URL dùng chung hoặc riêng theo nhà cung cấp

Dùng khóa mac2API có quyền gọi nhà cung cấp tương ứng. Thay `http://127.0.0.1:18610` bằng tên máy chủ đang chạy dịch vụ khi truy cập từ xa.

| Base URL | Mô-đun |
|---|---|
| `http://127.0.0.1:18610/v1` | Tất cả mô-đun. Chỉ định trường `model` để chọn dịch vụ |
| `http://127.0.0.1:18610/muse/v1` | Muse Text, Ảnh, Video |
| `http://127.0.0.1:18610/chatgpt/v1` | ChatGPT Web Text, Ảnh |
| `http://127.0.0.1:18610/gemini/v1` | Gemini Web Text, Ảnh |

Mỗi địa chỉ riêng có `GET /models` và chỉ chấp nhận mô-đun phù hợp. Nếu bỏ `model`, API riêng tự chọn theo đường dẫn và loại tác vụ. Ví dụ, `POST /gemini/v1/chat/completions` mặc định dùng `gemini-web/text`. Tạo và thăm dò tác vụ ảnh với cùng Base URL. URL tải tệp kết quả vẫn có dạng `/v1/media/...`. Có thể nhập một trong các Base URL này vào trường **Base URL mac2API** của adapter MacFlow.

### Cấp khóa API cho nhiều người dùng

Trong bảng quản trị, mở **Khóa người dùng**, nhập nhãn rồi chọn các nhà cung cấp được phép. Sao chép khóa `m2c_…` ngay sau khi tạo và điền vào trường **API Key** trong MacFlow hoặc ứng dụng gọi API. Mỗi người hoặc ứng dụng có thể dùng khóa riêng. Khóa quản trị `m2a_…` vẫn dùng được để vận hành dịch vụ và quản lý toàn bộ tác vụ.

Khóa người dùng chỉ xem được tác vụ của chính khóa đó. Tải tệp bằng URL đã ký hoặc Bearer của chủ tệp hay quản trị viên. Chữ ký có hiệu lực tối đa 7 ngày. Gọi lại API tác vụ để lấy URL mới nếu tệp vẫn còn. Khi thu hồi khóa người dùng, các URL ký cho khóa đó hết hiệu lực. URL quản trị chưa ký từ bản cũ có thể tải với Bearer quản trị hoặc lấy lại từ bảng điều khiển. Khi cần thời gian chuyển đổi, đặt `MUSE2API_ALLOW_LEGACY_PUBLIC_MEDIA=1` cho các URL quản trị cũ.

Dùng HTTPS khi gửi Bearer từ xa. Với proxy ngược, chuyển scheme qua proxy tin cậy để ứng dụng nhận biết HTTPS. Thêm từng origin được phép vào `MUSE2API_CORS_ORIGINS`, cách nhau bằng dấu phẩy. Bảng quản trị giữ khóa trong phiên của tab hiện tại.

Mỗi khóa có tối đa 20 yêu cầu `POST` mỗi phút, 2 yêu cầu `POST` đồng thời và 2 tác vụ media đang chờ hoặc đang chạy. Việc đọc trạng thái tác vụ dùng `GET`. Các khóa cùng chia sẻ tài khoản nhà cung cấp, hạn mức upstream, kho media và khe Chromium của tiến trình. Cấu hình này hỗ trợ nhiều người dùng trên một tiến trình mac2API với thư mục `data/` bền vững.

## ChatGPT Web và Gemini Web

Các mô-đun mới dùng một Base URL và khóa mac2API. `muse/text`, `chatgpt/text` và `gemini-web/text` dùng `/v1/chat/completions`. `muse/image`, `chatgpt/image` và `gemini-web/image` dùng `/v1/images/generations` hoặc `/v1/images/tasks`. `muse/video` dùng `/v1/videos`. Tên Muse cũ vẫn dùng được.

Chạy mac2API và Google Chrome 144 trở lên trên cùng máy. Dùng hồ sơ Chrome đã đăng nhập:

1. Dán `chrome://inspect/#remote-debugging` vào thanh địa chỉ Chrome và bật gỡ lỗi từ xa.
2. Trong tab **ChatGPT Web** hoặc **Gemini Web** của mac2API, bấm **Kết nối Chrome**, rồi chọn **Cho phép** trong hộp thoại của Chrome.
3. Mở tab nhà cung cấp đã đăng nhập. Trong tiện ích, nhập Base URL của mac2API và khóa quản trị, rồi bấm **ChatGPT** hoặc **Gemini**. Bảng quản trị hiển thị thời điểm xác minh cho dịch vụ tương ứng. Tùy chọn **Kiểm tra phiên trình duyệt** mở một tab thử trong hồ sơ đã xác minh.

Giữ Chrome mở trong khi chạy tác vụ. Nếu Chrome hoặc mac2API khởi động lại, kết nối và xác minh lại. Sau khi chọn dùng hồ sơ đang đăng nhập, mac2API chờ xác minh lại hồ sơ đó trước khi tạo tác vụ. Khi Chrome không mở được tab cho hồ sơ đã xác minh, đưa cửa sổ của hồ sơ đó lên trước rồi thử lại. Cấu hình cổng DevTools cục bộ qua `MUSE2API_WEB_CDP_PORT` vẫn được hỗ trợ. Render chỉ có thể điều khiển trình duyệt chạy cùng máy với mac2API.

Các mô-đun web tạo một ảnh từ mô tả, tùy quyền của tài khoản. Chat web lấy câu trả lời hoàn chỉnh rồi mới trả về, kể cả khi client yêu cầu SSE. Mỗi yêu cầu mở và đóng tab riêng. mac2API xóa bản nháp và kiểm tra ô nhập trước khi gửi. Muse thử mở một phiên riêng mới nếu vẫn còn tệp hoặc nội dung cũ. Nếu còn bị kẹt, tác vụ dừng và giải thích trong phần **Chi tiết lỗi**. Chỉ các tệp trong ô nhập của tác vụ mới được dọn.

Để thêm media trong MacFlow/Toonflow, nhập từng tệp trong [bộ adapter theo hợp đồng v1](adapters/2.0.7/). Tệp [mac2Api.ts](adapters/mac2Api.ts) dùng cho cấu hình tổng hợp cũ. Các mô-đun chat dùng cấu hình nhà cung cấp văn bản với giao thức OpenAI Chat Completions.

## Tương thích với kết nối API chính thức đã có

Các model dạng `openai/<model>` và `gemini/<model>` dành cho ứng dụng đã cấu hình khóa API chính thức. Các lệnh gọi này có thể tính phí theo gói API. Thuê bao ChatGPT Plus và API OpenAI được thanh toán riêng.

Có thể cấp khóa qua `OPENAI_API_KEY` hoặc `GEMINI_API_KEY`. Khóa từ môi trường được ưu tiên. Model mặc định có thể đặt qua `OPENAI_MODEL` hoặc `GEMINI_MODEL`. Khóa API đã cấu hình ở các bản trước được lưu tại `data/chat-providers.json` và chỉ ghi xuống máy chủ. Hãy bảo vệ thư mục dữ liệu và bản sao lưu. Khi nhập khóa từ xa, dùng HTTPS hoặc đường hầm SSH.

Cả hai cùng dùng `POST /v1/chat/completions` với API Key của **MUSE2API** trong header. Thêm tiền tố vào tên model để chọn nhà cung cấp. Ví dụ `openai/gpt-4.1-mini` hoặc `gemini/gemini-2.5-flash`. Thay model theo danh sách tài khoản của bạn. Bật `"stream":true` để nhận SSE. `GET /v1/models` hiển thị model mặc định của các nhà cung cấp đã cấu hình.

```bash
curl -N "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $MUSE2API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"openai/gpt-4.1-mini","messages":[{"role":"user","content":"Xin chào!"}],"stream":true}'
```

Tên không có tiền tố như `gpt-4o` vẫn theo ánh xạ Muse cũ. Bạn cũng có thể chỉ định `muse/muse-spark` cho chat. OpenAI và Gemini dùng kết nối HTTPS từ máy chủ đến `api.openai.com` và `generativelanguage.googleapis.com`. Proxy Zing gắn với tài khoản Muse chỉ áp dụng ở luồng Muse. Hai kết nối mới hỗ trợ Chat Completions. Các luồng Responses, ảnh và video dùng tích hợp Muse hiện có.

Tài liệu chính thức [OpenAI Chat Completions](https://platform.openai.com/docs/api-reference/chat) và [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai).

## Dung lượng thư viện ảnh và video

Thư viện hiển thị tệp mới nhất trước. Kho kết quả tối đa 5 GB (5.000.000.000 byte). Khi khởi động hoặc khi tệp mới khiến dung lượng đạt 70% (3,5 GB), kho xóa tệp cũ nhất trước, kèm ảnh thu nhỏ, đến khi xuống dưới ngưỡng. Một tệp duy nhất có thể dùng tới 5 GB. Tệp lớn hơn giới hạn bị từ chối. Lịch sử tác vụ vẫn hiển thị sau khi tệp bị dọn theo dung lượng và liên kết của tệp đã dọn sẽ trả HTTP 404.

## Tệp tham chiếu

Gửi ảnh, video, âm thanh hoặc tài liệu tham chiếu qua các luồng hỗ trợ từng loại tệp. Tối đa 10 tệp, 40 MiB mỗi tệp và 100 MiB tổng cộng. URL công khai, Data URL và Base64 hợp lệ được hỗ trợ. Tránh trộn mảng `images` với các trường một ảnh cũ như `image` hoặc `reference_image`. Mặc định, đường dẫn tệp trên máy chủ và URL mạng riêng hoặc localhost bị từ chối.

Tệp được tải lên theo thứ tự các trường `images`, `videos`, `audios`, `files`. Mỗi phần tử có thể là chuỗi URL, Data URL, Base64 hoặc đối tượng như `{"url":"https://example.com/ref.png"}`. Với multipart, có thể lặp các trường `image`, `video`, `audio` và `file`.

| Loại | Định dạng |
|---|---|
| Ảnh | JPG, PNG, GIF, WebP, HEIC, HEIF, AVIF |
| Video | MP4, MOV, WebM, 3GP. Thời lượng đọc được tối đa 120 giây |
| Âm thanh | MP3, WAV, M4A, OGG, FLAC, AAC, Opus |
| Tài liệu | PDF, DOC, DOCX |

Trình duyệt và dịch vụ cần truy cập cùng tệp tham chiếu. Tệp tạm được dọn sau khi tác vụ kết thúc. Nếu dùng Nginx với tệp lớn, đặt `client_max_body_size` khoảng `200M` để tránh HTTP 413.

### Tạo ảnh qua CDN hoặc proxy ngược

Với các yêu cầu dài, gửi `POST /v1/images/tasks` và kiểm tra `GET /v1/images/tasks/{id}` bằng mã trả về trong HTTP 202. Cả `POST /v1/images/tasks` và `POST /v1/videos` hỗ trợ `Idempotency-Key`. Tạo một giá trị riêng cho mỗi tác vụ mới, dùng lại giá trị và dữ liệu đó khi gửi lại yêu cầu bị mất kết nối. API trả mã tác vụ cũ nếu bản ghi còn trong lịch sử, kể cả sau khi khởi động lại. Dùng cùng giá trị với dữ liệu khác trả HTTP 409. Khi đạt giới hạn hàng đợi, API trả HTTP 429. Khi trạng thái là `completed`, đọc `url` hoặc `data[0]`. Khi trạng thái là `failed`, xem `error`.

### Chẩn đoán phiên Muse

- **401 từ `/api/session`**. Kiểm tra đăng nhập trên Muse.ai rồi nhập lại cookie.
- **403**. Kiểm tra quyền, IP đầu ra và khả năng truy cập từ khu vực triển khai.
- **429, 5xx, timeout hoặc phản hồi ngoài dự kiến**. Lần kiểm tra chưa xác minh được phiên. Trạng thái xác nhận gần nhất được giữ lại.
- **Hạn mức**. Kiểm tra thời điểm truy vấn và thông báo lỗi. Khi đọc không thành công, số liệu đã lưu vẫn được hiển thị.
- **Duy trì phiên**. Thời hạn chỉ được cập nhật từ cookie Muse thực sự trả về. Hệ thống xác minh trạng thái máy ảo trước khi báo đã đánh thức thành công.

## Cấu hình

| Biến | Công dụng |
|---|---|
| `MUSE2API_KEY` | Khóa API và bảng quản trị, tự tạo nếu bỏ trống |
| `MUSE2API_HOST`, `MUSE2API_PORT` | Địa chỉ và cổng dịch vụ |
| `MUSE2API_PUBLIC_BASE` | URL công khai dùng cho các liên kết được tạo |
| `MUSE2API_CHROMIUM`, `MUSE2API_CDP_PORT` | Tệp thực thi Chromium và cổng DevTools |
| `MUSE2API_CHROMIUM_IDLE_SECONDS` | Tắt Chromium khi không có tác vụ Muse, mặc định 60 giây. Đặt `0` để giữ Chromium chạy |
| `MUSE2API_CONCURRENCY` | Số tác vụ chạy đồng thời |
| `MUSE2API_IMAGE_TIMEOUT`, `MUSE2API_VIDEO_TIMEOUT`, `MUSE2API_CHAT_TIMEOUT` | Thời gian xử lý tính theo giây |
| `MUSE2API_REF_IMAGE_LOCAL_FILES`, `MUSE2API_REF_IMAGE_PRIVATE_FETCH` | Quyền đọc tệp cục bộ và URL mạng riêng. Mặc định là `0` |
| `MUSE2API_REPOSITORY`, `MUSE2API_REPO_REF` | Nguồn và nhánh dùng để kiểm tra cập nhật |
| `MUSE2API_MANAGED_DEPLOYMENT` | Đặt `1` khi cập nhật bằng cách dựng lại image |

Để kiểm tra cập nhật từ fork của bạn, đặt `MUSE2API_REPOSITORY=mktminhqpbga-glitch/mac2API` và `MUSE2API_REPO_REF=main`. Kiểm tra phiên bản chỉ đọc thông tin. Xem [quy trình cập nhật](VERSION_REPORTING.md). Video trong thư viện dùng FFmpeg để tạo ảnh đại diện theo nhu cầu.

## Kiểm thử

```bash
python tests/test_ref_image_safety.py
python tests/test_media_selection.py
node --test tests/*.mjs
```

Bài kiểm tra chọn media cần Chromium. Các bài kiểm tra này dùng dữ liệu mô phỏng và không tiêu hao hạn mức Muse.

Cookie, mật khẩu proxy, tác vụ và tệp kết quả được lưu trong `data/`. Bảo vệ thư mục và bản sao lưu. Chỉ dùng dịch vụ cho tác vụ phù hợp với điều khoản của Muse và pháp luật áp dụng.

Dự án gốc [muse2api](https://github.com/czg86389-hub/muse2api) được phát hành theo [giấy phép MIT](LICENSE).

## Ghi nhận đóng góp

Cảm ơn các tác giả đã đóng góp vào dự án gốc.

- [@cpt-kenvie](https://github.com/cpt-kenvie) và [@djs-91](https://github.com/djs-91) sửa cấu hình Docker Compose và URL bảng quản trị.
- [@toby-bridges](https://github.com/toby-bridges), [@LF2023](https://github.com/LF2023), [@Yeadon8888](https://github.com/Yeadon8888) và [@Roins-hub](https://github.com/Roins-hub) cải thiện xử lý tệp tham chiếu, lựa chọn kết quả và quản lý phiên.
- [@wuliaolaozi](https://github.com/wuliaolaozi), [@CarloCPP](https://github.com/CarloCPP) và [@gr219](https://github.com/gr219) đóng góp truy vấn hạn mức, tập lệnh nhập cookie và giao diện đa ngôn ngữ.
- [@pandalaohe](https://github.com/pandalaohe), [@QuartzWarrior](https://github.com/QuartzWarrior), [@WXJJ0520](https://github.com/WXJJ0520) và [@ALight777](https://github.com/ALight777) đóng góp bảo vệ tệp tham chiếu, proxy theo tài khoản, kiểm thử Chromium, cách ly cookie, hàng đợi, phiên bản và hình thu nhỏ.
