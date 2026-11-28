# Tài liệu API mac2API v1

Áp dụng cho mac2API **1.6.33**. Dùng tài liệu này để kết nối ứng dụng với Muse, ChatGPT và Gemini đã thiết lập trong mac2API.

## Địa chỉ và khóa

Trong mac2API, mở dịch vụ cần dùng rồi chọn **API đầu ra → Tạo khóa riêng**. Chọn các tài khoản được phép dùng và sao chép địa chỉ cùng khóa người dùng `m2c_…`.

| Dịch vụ | Base URL trên cùng máy | Mô hình văn bản | Mô hình ảnh | Mô hình video |
| --- | --- | --- | --- | --- |
| Muse | `http://127.0.0.1:18610/muse/v1` | `muse/text` | `muse/image` | `muse/video` |
| ChatGPT | `http://127.0.0.1:18610/chatgpt/v1` | `chatgpt/text` | `chatgpt/image` | — |
| Gemini | `http://127.0.0.1:18610/gemini/v1` | `gemini-web/text` | `gemini-web/image` | — |

Gửi khóa bằng header `Authorization: Bearer <khóa>`. Dùng địa chỉ HTTPS nếu ứng dụng gọi từ máy khác. Trong Docker hoặc khi triển khai Toonflow trên máy chủ, `127.0.0.1` là chính máy hoặc container chạy ứng dụng gọi API. Chọn địa chỉ mà backend ứng dụng truy cập được.

Base URL chung `http://127.0.0.1:18610/v1` nhận cả ba dịch vụ theo quyền của khóa. Khi dùng địa chỉ chung, điền `model` tường minh. Với địa chỉ riêng, bỏ qua `model` để máy chủ chọn mô hình theo dịch vụ và loại tác vụ. Giữ cùng Base URL và khóa khi gửi, tra cứu hoặc hủy một tác vụ.

Các ID Muse cũ `muse-spark`, `muse-image` và `muse-video` tiếp tục dùng được ở những tuyến tương ứng.

### Kiểm tra kết nối

Các ví dụ dùng shell macOS hoặc Linux. Chọn Base URL và khóa của dịch vụ đang thử. Lệnh đọc khóa bên dưới ẩn nội dung khi bạn dán.

```bash
MAC2API_ORIGIN='http://127.0.0.1:18610'
BASE_URL="$MAC2API_ORIGIN/muse/v1"

printf 'Dán khóa người dùng mac2API rồi nhấn Enter. '
read -rs MAC2API_KEY
printf '\n'

curl -fsS "$MAC2API_ORIGIN/healthz"
curl -fsS "$BASE_URL/models" -H "Authorization: Bearer $MAC2API_KEY"
curl -fsS "$BASE_URL/capabilities" -H "Authorization: Bearer $MAC2API_KEY"
```

- `/healthz` trả `status: "ok"` khi máy chủ phản hồi. Đọc `/capabilities` để biết mô hình đã sẵn sàng.
- `/models` trả `data[]` với mã mô hình. `/capabilities` trả `contractVersion: 1`, danh sách nhà cung cấp, mô hình, giới hạn và các tính năng.
- Trong `models[]`, đọc `ready` và `reason`. Khi `ready: true` và `busy: true`, tác vụ media vẫn có thể vào hàng chờ nếu còn chỗ. `session_unverified` yêu cầu kiểm tra phiên trong mac2API.
- Đọc `modes`, `maxReferences`, `allowedMimeTypes` và `maxReferenceBytes` để kiểm tra tham chiếu trước khi gửi.
- Trong `features`, các cờ `idempotency`, `lookupByRequestKey` và `cancel` cho biết máy chủ có hỗ trợ chống tạo trùng, tra biên nhận và hủy tác vụ chờ hay không.
- Dùng khóa `m2c_…` cho `/capabilities`, tra cứu bằng `Idempotency-Key`, hủy tác vụ và xem lịch sử sử dụng. Khóa quản trị phục vụ bảng điều khiển.

## Danh sách tuyến API

Ghép đường dẫn với Base URL của dịch vụ. Dùng địa chỉ máy chủ cho lịch sử sử dụng và các URL tệp tương đối.

| Nhu cầu | Phương thức và đường dẫn | Dịch vụ | Ghi chú |
| --- | --- | --- | --- |
| Liệt kê mô hình | `GET /models` | Cả ba | Danh sách theo quyền của khóa |
| Đọc khả năng | `GET /capabilities` | Cả ba | Phiên, lựa chọn đầu vào, giới hạn |
| Chat | `POST /chat/completions` | Cả ba | JSON hoặc SSE |
| Responses | `POST /responses` | Muse | Văn bản, hỗ trợ một phần giao thức OpenAI Responses |
| Tạo ảnh qua hàng chờ | `POST /images/tasks` | Cả ba | HTTP 202 và mã tác vụ |
| Theo dõi ảnh | `GET /images/tasks/{task_id}` | Cả ba | Kết quả hoặc lỗi của tác vụ |
| Tạo ảnh đồng bộ | `POST /images/generations` | Cả ba | Đợi kết quả ngay trong phản hồi |
| Chỉnh sửa dựa trên tham chiếu | `POST /images/edits` | Muse | JSON hoặc tải tệp multipart |
| Tạo video | `POST /videos` | Muse | HTTP 200 và mã tác vụ |
| Theo dõi video | `GET /videos/{task_id}` | Muse | Kết quả hoặc lỗi của tác vụ |
| Tìm lại biên nhận | `GET /tasks/by-request` | Cả ba | Gửi lại header `Idempotency-Key` trong yêu cầu GET |
| Hủy tác vụ còn chờ | `POST /images/tasks/{task_id}/cancel`, `POST /videos/{task_id}/cancel` | Theo loại tác vụ | Dùng mã đã nhận |
| Tải tệp | — | Cả ba | Gọi URL trong kết quả, thường là `GET /v1/media/{name}` |
| Lịch sử sử dụng | — | Cả ba | `GET /v1/usage` và `GET /v1/usage/{job_id}` từ địa chỉ máy chủ |

## Văn bản

Gửi `messages[]` với `role` và nội dung chuỗi. Ví dụ dưới đây dùng Muse. Có thể thay hai dòng đầu bằng cặp Base URL và mô hình ChatGPT hoặc Gemini trong bảng, với khóa được cấp quyền tương ứng.

```bash
BASE_URL="$MAC2API_ORIGIN/muse/v1"
CHAT_MODEL='muse/text'

curl -fsS "$BASE_URL/chat/completions" \
  -H "Authorization: Bearer $MAC2API_KEY" \
  -H 'Content-Type: application/json' \
  -d "$(printf '{"model":"%s","messages":[{"role":"user","content":"Viết một lời chào ngắn bằng tiếng Việt."}],"stream":false}' "$CHAT_MODEL")"
```

Đọc câu trả lời từ `choices[0].message.content`. Dưới đây là phản hồi rút gọn.

```json
{
  "object": "chat.completion",
  "choices": [
    {"index": 0, "message": {"role": "assistant", "content": "Xin chào!"}, "finish_reason": "stop"}
  ]
}
```

Dùng `"stream": true` và `curl -N` để đọc SSE. Lấy chữ từ `choices[].delta.content`, đọc lỗi trong sự kiện nếu có và chờ `data: [DONE]` để xác nhận luồng đã kết thúc. Với tab web, mac2API có thể trả chữ sau khi trang hoàn tất trả lời.

Tuyến `/responses` của Muse nhận `input` dạng chuỗi hoặc danh sách, có thể kèm `instructions` và `stream`. Với JSON, đọc phần tử `output[].content[]` có `type: "output_text"`. Với SSE, đọc `response.output_text.delta` đến `response.completed` hoặc `response.failed`.

Đối với các khóa được cấp mô hình API chính thức dạng `openai/<model>` hoặc `gemini/<model>`, dùng `/v1/chat/completions` và chọn ID từ `/v1/models`. Mô-đun ChatGPT Web và Gemini Web nhận nội dung văn bản. Khả năng gọi công cụ tùy theo tuyến và cấu hình nhà cung cấp.

## Ảnh

Dùng `POST /images/tasks` khi tạo ảnh trong Toonflow hoặc khi ứng dụng cần theo dõi sau khi mất kết nối.

| Trường JSON | Cách dùng |
| --- | --- |
| `model` | `muse/image`, `chatgpt/image` hoặc `gemini-web/image` |
| `prompt` | Mô tả ảnh cần tạo |
| `aspect_ratio` | Tỷ lệ mong muốn, chẳng hạn `1:1`, `9:16`, `16:9`, `3:4` hoặc `4:3` |
| `size` | Mức kích thước mong muốn, chẳng hạn `1080p`, `2K` hoặc `1024x1024` |
| `n` | Dùng `1` để tạo một ảnh |
| `images` | Danh sách tham chiếu cho Muse. Mô-đun ảnh ChatGPT và Gemini chỉ nhận mô tả |
| `response_format` | `url` mặc định. Chọn `b64_json` để nhận ảnh Base64 ở phản hồi hoàn tất |
| `timeout` | Số giây xử lý tùy chọn, từ 1 đến 600. Thời gian chờ lượt được tính riêng |
| `extra` | Chỉ dẫn thêm bằng văn bản |

Tỷ lệ và kích thước là yêu cầu gửi tới nhà cung cấp. Kích thước thật được đọc từ tệp trả về. Nếu gửi cả tỷ lệ và kích thước cho ChatGPT hoặc Gemini, ghi kích thước trong `extra` để giữ cả hai chỉ dẫn.

```bash
BASE_URL="$MAC2API_ORIGIN/muse/v1"
IMAGE_MODEL='muse/image'
REQUEST_KEY=$(uuidgen | tr '[:upper:]' '[:lower:]')

curl -fsS "$BASE_URL/images/tasks" \
  -H "Authorization: Bearer $MAC2API_KEY" \
  -H "Idempotency-Key: $REQUEST_KEY" \
  -H 'Content-Type: application/json' \
  -d "$(printf '{"model":"%s","prompt":"Một chú mèo ngồi bên cửa sổ","aspect_ratio":"1:1","size":"2K","response_format":"url"}' "$IMAGE_MODEL")"
```

Sao chép `task_id` hoặc `id` từ HTTP 202. Hai trường cùng chỉ một tác vụ. Ví dụ biên nhận rút gọn dưới đây có trạng thái chờ.

```json
{"id": "task_example", "task_id": "task_example", "kind": "image", "status": "queued", "progress": 0}
```

```bash
TASK_ID='task_điền_mã_nhận_được'

curl -fsS "$BASE_URL/images/tasks/$TASK_ID" \
  -H "Authorization: Bearer $MAC2API_KEY"
```

Khi `status` là `completed`, đọc `data[0].url` hoặc `data[0].b64_json` theo định dạng đã chọn. Nếu tạo đồng bộ, dùng cùng JSON với `POST /images/generations` và đọc `data[0]` ngay từ phản hồi. Tuyến đồng bộ `/images/generations` cũng nhận `"async": true` để trả biên nhận.

### Ảnh tham chiếu Muse

Với JSON, gửi `images` theo đúng thứ tự. Phần tử có thể là URL HTTP(S) công khai, Data URL, Base64 hợp lệ hoặc object `{"url":"https://example.com/ref.png"}`. Thay URL mẫu bằng địa chỉ tệp đang dùng. Chọn mảng `images` hoặc một trường ảnh cũ như `image` hay `reference_image` cho nhóm ảnh.

Ví dụ dưới đây gửi một tệp `ref.png` từ thư mục đang mở. Dùng Base URL và khóa Muse.

```bash
curl -fsS "$BASE_URL/images/edits" \
  -H "Authorization: Bearer $MAC2API_KEY" \
  -H "Idempotency-Key: $(uuidgen)" \
  -F 'model=muse/image' \
  -F 'prompt=Thay phông nền ảnh tham chiếu thành màu xanh nhạt' \
  -F 'image=@ref.png' \
  -F 'async=true'
```

Lặp trường multipart `image` để gửi nhiều ảnh. Khi đặt `async=true`, theo dõi bằng `GET /images/tasks/{task_id}`.

## Video Muse

Gửi `prompt` và `model: "muse/video"` vào `POST /videos`. Dùng `aspect_ratio` cho tỷ lệ và `resolution` cho độ phân giải. `duration` là số giây nguyên dương tùy chọn. Khi bỏ trống, mac2API để Muse quyết định theo mô tả và thiết lập của nó. Tỷ lệ, độ phân giải và thời lượng thực tế đọc từ tệp kết quả.

```bash
BASE_URL="$MAC2API_ORIGIN/muse/v1"
REQUEST_KEY=$(uuidgen | tr '[:upper:]' '[:lower:]')

curl -fsS "$BASE_URL/videos" \
  -H "Authorization: Bearer $MAC2API_KEY" \
  -H "Idempotency-Key: $REQUEST_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"muse/video","prompt":"Lá phong rơi nhẹ dưới ánh nắng","aspect_ratio":"9:16","resolution":"1080p","duration":15}'
```

HTTP 200 trả mã tác vụ. Tra bằng `GET /videos/{task_id}` với cùng Base URL và khóa. Khi `completed`, lấy `result.url` hoặc `url`.

```bash
TASK_ID='task_điền_mã_nhận_được'

curl -fsS "$BASE_URL/videos/$TASK_ID" \
  -H "Authorization: Bearer $MAC2API_KEY"
```

Để gửi hình mở đầu, thêm `image` với một ảnh hợp lệ. API Muse cũng nhận mảng tham chiếu. Adapter Toonflow Muse công bố hai chế độ, tạo từ mô tả hoặc từ một khung mở đầu.

### Giới hạn tham chiếu Muse

| Mục | Giới hạn |
| --- | --- |
| API tạo hoặc chỉnh ảnh | Tối đa 20 tệp trong một yêu cầu |
| API tạo video | Tối đa 10 tệp trong một yêu cầu |
| Adapter Toonflow Muse | Tối đa 20 ảnh cho tác vụ ảnh. Một ảnh mở đầu cho tác vụ video |
| Kích thước | Tối đa 40 MiB mỗi tệp và 100 MiB tổng cộng |
| Ảnh | JPEG, PNG, GIF, WebP, HEIC, HEIF, AVIF |
| Video tham chiếu | MP4, MOV, WebM, 3GP. Tối đa 120 giây với tệp đọc được thời lượng |
| Âm thanh và tài liệu tham chiếu API | MP3, WAV, M4A, OGG, FLAC, AAC, Opus, PDF, DOC, DOCX |

Với API Muse, các mảng `images`, `videos`, `audios`, `files` được xử lý theo thứ tự đó. Khóa `m2c_…` nhận URL công khai hoặc dữ liệu đính kèm hợp lệ. Đường dẫn đĩa, localhost và URL mạng riêng bị từ chối khi dùng khóa này.

## Theo dõi tác vụ, mất kết nối và hủy

| `status` | Cách xử lý |
| --- | --- |
| `queued`, `processing` | Tiếp tục GET theo mã tác vụ. Có thể đợi khoảng 2–3 giây giữa các lượt đọc |
| `completed` | Lấy URL hoặc Base64 theo loại tác vụ |
| `failed` | Đọc `error`, `error_code` và `error_stage` khi có |
| `cancelled` | Dừng theo dõi. Tác vụ đã được hủy |
| `interrupted` hoặc `needs_reconcile: true` | Đối chiếu tác vụ cũ trước khi tạo yêu cầu mới |

Trước khi gửi ảnh bất đồng bộ hoặc video, lưu một UUID v4 làm `Idempotency-Key` cho lần tạo đó. Nếu mất phản hồi POST, giữ nguyên UUID và dùng GET để tra biên nhận.

```bash
curl -fsS "$BASE_URL/tasks/by-request" \
  -H "Authorization: Bearer $MAC2API_KEY" \
  -H "Idempotency-Key: $REQUEST_KEY"
```

Nếu tìm thấy, tiếp tục với `task_id`. HTTP 404 nghĩa chưa tìm thấy biên nhận thuộc khóa và địa chỉ này. Với lỗi mạng hoặc chưa có biên nhận, kiểm tra **Tác vụ** trước khi chủ động gửi lại. Nếu gửi lại cùng UUID với nội dung và loại tác vụ cũ, máy chủ có thể trả biên nhận đã lưu. Dùng cùng UUID cho nội dung khác trả `409 IDEMPOTENCY_CONFLICT`.

Hủy tác vụ bằng `POST <đường dẫn theo dõi>/cancel` với khóa chủ tác vụ. HTTP 200 với `cancelled: true` xác nhận đã hủy tác vụ còn chờ. HTTP 409 với `CANNOT_CANCEL_RUNNING` nghĩa tác vụ đã chạy. `ALREADY_TERMINAL` nghĩa tác vụ đã kết thúc. Phản hồi 409 của tuyến hủy có `code`, `status`, `message` ngay ở cấp ngoài cùng.

Khi máy chủ khởi động lại, tác vụ chờ có dữ liệu phục hồi dùng tiếp mã cũ. Tác vụ có thể đã gửi được đánh dấu `interrupted` để đối chiếu. Tác vụ dừng khi còn chờ và thiếu dữ liệu phục hồi báo `SERVICE_RESTARTED_BEFORE_SEND`. [Tài liệu hàng chờ](GENERATION_QUEUE.md)

### Chẩn đoán tác vụ Muse

| Mã lỗi | Cách đọc |
| --- | --- |
| `MUSE_REFERENCES_MISSING` | Mô tả yêu cầu tệp tham chiếu nhưng dữ liệu gửi thiếu tệp |
| `MUSE_PAGE_NOT_READY`, `MUSE_DRAFT_NOT_READY`, `MUSE_SEND_BUTTON_MISSING`, `MUSE_SEND_BUTTON_DISABLED` | Ô chat, bản nháp hoặc nút gửi chưa sẵn sàng |
| `MUSE_REFERENCE_UPLOAD_UNCONFIRMED` | So sánh `reference_count` với `confirmed_reference_count` để biết số tệp được xác nhận |
| `MUSE_REFERENCES_NOT_SEEN`, `MUSE_REFERENCE_INCOMPLETE` | Muse báo thiếu tệp trong lượt tạo |
| `MUSE_VIDEO_UNAVAILABLE`, `MUSE_CONFIRMATION_REQUIRED` | Muse báo tài khoản chưa tạo được video hoặc đang chờ xác nhận phương án |

`error_stage` có thể báo `before_send` khi dừng trước khi gửi, `muse_reply` khi nhận phản hồi của Muse, hoặc `delivery_unknown` khi trạng thái gửi cần đối chiếu.

## Tải tệp kết quả

Dùng đường dẫn máy chủ trả. Nếu giá trị bắt đầu bằng `/v1/media/`, ghép với **địa chỉ máy chủ** như `http://127.0.0.1:18610`. Các tiền tố `/muse/v1`, `/chatgpt/v1` và `/gemini/v1` dùng để tạo hoặc theo dõi tác vụ.

- URL có `access=` chứa chữ ký tải và có hiệu lực tối đa 7 ngày khi tệp còn trong kho và khóa chủ còn hiệu lực. Dùng toàn bộ URL kèm chữ ký. Có thể gọi lại GET tác vụ để lấy URL mới.
- Để tải với khóa của chủ tệp, gửi `Authorization: Bearer <khóa>` đến đúng máy chủ mac2API. Dùng `Content-Type` và định dạng của tệp thực tế khi lưu.
- Nếu tệp đã bị dọn, mã tác vụ vẫn có thể nằm trong lịch sử và đường dẫn tải trả 404.

```bash
RESULT_URL='dán đường dẫn có chữ ký từ kết quả'
case "$RESULT_URL" in
  /*) RESULT_URL="$MAC2API_ORIGIN$RESULT_URL" ;;
esac
curl -fsS "$RESULT_URL" -o ket-qua
```

## Lỗi, hạn mức và lịch sử

Lỗi HTTP thường có `error.message` và `error.code`. Đối với tác vụ đã nhận, đọc trường lỗi của bản ghi trạng thái.

| HTTP | Ý nghĩa thường gặp |
| --- | --- |
| 400, 422 | Sai mô hình, sai dữ liệu hoặc thiếu tham chiếu |
| 401 | Khóa thiếu, sai hoặc đã bị thu hồi. Phiên nhà cung cấp cũng có thể hết hiệu lực |
| 403 | Khóa thiếu quyền hoặc địa chỉ từ xa cần HTTPS |
| 404 | Không tìm thấy endpoint, tác vụ thuộc khóa hoặc tệp kết quả |
| 409 | Xung đột UUID hoặc tác vụ đang ở trạng thái khác với thao tác |
| 410 | Tệp đã bị dọn khi API cần đọc lại để trả Base64 |
| 429 | Hàng chờ hoặc hạn mức đầy. Đọc `Retry-After` nếu có |
| 502, 503, 504 | Dịch vụ hoặc tài khoản chưa sẵn sàng, mất kết nối hoặc quá thời gian chờ |
| 507 | mac2API không ghi được trạng thái hoặc dữ liệu tác vụ |

Mỗi khóa có giới hạn 20 yêu cầu POST mỗi phút và 2 yêu cầu POST đồng thời. Hàng ảnh có tối đa 20 tác vụ chờ hoặc đang chạy trên toàn máy chủ và 4 tác vụ ảnh trên mỗi khóa. Hàng video có tối đa 8 tác vụ video. Khi nhận video, máy chủ kiểm tra ngưỡng 2 tác vụ media đang chờ hoặc đang chạy trên khóa. Hạn mức ngày theo mô hình do quản trị viên đặt, tính lại lúc 00:00 giờ Việt Nam.

Dùng `GET /v1/usage` để xem lịch sử của chính khóa. Có thể lọc bằng `model`, `status`, `since`, `until` và phân trang qua `limit`, `offset`, `has_more`. Thời điểm lọc dùng Unix timestamp tính bằng giây. `limit` tối đa 100. Phản hồi có `items` và `totals`.

```bash
curl -fsS "$MAC2API_ORIGIN/v1/usage?limit=10" \
  -H "Authorization: Bearer $MAC2API_KEY"
```

Tra `GET /v1/usage/{job_id}` bằng mã tác vụ ảnh hoặc video. Với chat, Responses và tạo ảnh đồng bộ, đọc `X-Mac2API-Usage-Id` từ header của phản hồi có ghi lượt sử dụng. Chi phí tính bằng VND. `charge: null` nghĩa chưa xác định. Chi tiết chỉ có nội dung đã lưu hoặc link còn hiệu lực khi cấu hình lịch sử cho phép.

## Toonflow và MacFlow

1. Mở **Thêm nhà cung cấp media tùy chỉnh**, chọn **Nhập tệp** và chọn tệp của dịch vụ cần dùng trong bảng dưới đây.
2. Trong phần cấu hình nhà cung cấp, dán Base URL và khóa `m2c_…` tương ứng. Trong phần thử mô hình, chọn mô hình và gửi một mô tả ngắn.
3. Để dùng văn bản, mở phần cài đặt mô hình văn bản, chọn kết nối tương thích OpenAI Chat Completions rồi điền địa chỉ, khóa và ID mô hình văn bản trong bảng đầu tài liệu.

| Tệp nhập Toonflow | Phiên bản | Chức năng |
| --- | --- | --- |
| [mac2ApiMuse.ts](adapters/2.0.7/mac2ApiMuse.ts) | 2.0.12 | Ảnh và video |
| [mac2ApiChatGpt.ts](adapters/2.0.7/mac2ApiChatGpt.ts) | 2.0.9 | Ảnh từ mô tả |
| [mac2ApiGemini.ts](adapters/2.0.7/mac2ApiGemini.ts) | 2.0.9 | Ảnh từ mô tả |

Adapter kiểm tra `/capabilities` trước khi gửi và dùng `/models` khi `/capabilities` trả 404. Khi host hỗ trợ lưu tác vụ, adapter lưu UUID trước khi gửi và mã tác vụ sau khi nhận biên nhận. Khi khôi phục, adapter đọc lại tác vụ gốc, tải tệp từ cùng máy chủ mac2API và trả dữ liệu ảnh hoặc video đã xác định định dạng. Giữ Base URL ban đầu khi khôi phục một tác vụ cũ.

Nếu ứng dụng gọi từ trình duyệt trên tên miền khác, thêm địa chỉ của ứng dụng vào `MUSE2API_CORS_ORIGINS` trên máy chủ mac2API. Nếu dùng proxy ngược, cấu hình HTTPS và chuyển đúng giao thức từ proxy tin cậy. [Cấu hình mac2API](README.md#cấu-hình)

## Đối chiếu và kiểm thử

Xem tài liệu tương tác tại `/docs` hoặc lấy OpenAPI JSON tại `/openapi.json` trên máy đang chạy mac2API. Mẫu dưới đây chạy với dữ liệu thử nghiệm.

```bash
python tests/test_mac2api_contract.py
python tests/test_api_clients.py
python tests/test_async_images.py
node --test tests/test_mac2api_contract_adapters.mjs
```

Các kiểm thử bao gồm quyền theo khóa, gửi trùng UUID, tìm lại biên nhận, hủy hàng chờ, tải media và khôi phục tác vụ. [Cài đặt và vận hành mac2API](README.md)
