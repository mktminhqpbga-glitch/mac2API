# Hợp đồng mac2API v1 cho MacFlow

MacFlow dùng khóa người dùng `m2c_…` và HTTPS khi gọi qua mạng. HTTP dùng cho kết nối loopback. mac2API quản lý Chrome, cookie, proxy và dữ liệu tác vụ. MacFlow theo dõi tác vụ bằng HTTP và lưu tệp kết quả trong workspace của mình.

## Khả năng và giới hạn

`GET /v1/capabilities` trả HTTP 200 và `Cache-Control: no-store`. Khóa vắng, sai hoặc đã thu hồi trả 401. Khóa quản trị và đường dẫn riêng của nhà cung cấp nằm ngoài quyền của khóa trả 403. Danh sách chỉ gồm các nhà cung cấp được cấp phép. Ba Base URL riêng `/muse/v1`, `/chatgpt/v1` và `/gemini/v1` có cùng cách dùng trong phạm vi tương ứng.

`contractVersion` là 1. `serverVersion` đọc từ `version.json`. `configured` phản ánh cấu hình đã có, `connected` phản ánh kết nối quan sát được, `ready` phản ánh điều kiện cần để nhận tác vụ và `busy` phản ánh khe hoạt động. Các mã `reason` gồm `not_configured`, `session_unverified`, `browser_unavailable`, `browser_idle`, `browser_disconnected`, `credentials_unverified`, `busy` và `ready`.

Khi hết khe Chromium, máy chủ dùng lần xác minh gần nhất của kết nối ChatGPT Web và Gemini Web hiện tại. `ready: true` cùng `busy: true` cho phép gửi vào hàng đợi nếu còn chỗ. Worker kiểm tra tab khi đến lượt thực thi.

Muse đã xác minh tài khoản và có Chromium khả dụng có thể báo `ready: true`, `connected: false`, `reason: "browser_idle"`. mac2API mở Chromium khi thực sự chạy tác vụ. ChatGPT Web và Gemini Web cần kết nối Chrome cục bộ và phiên đã được xác minh đúng nhà cung cấp. Khi hết khe, mac2API hoãn kiểm tra CDP. Kiểm tra khả năng chỉ đọc trạng thái. Quota và khả năng sinh đúng nội dung thực tế do nhà cung cấp quyết định.

Mẫu dưới đây được ghi từ fixture ASGI với khóa chỉ có quyền Muse và một phiên Muse giả lập đã xác minh.

```json
{
  "contractVersion": 1,
  "serverVersion": "1.6.12",
  "providers": [
    {"id": "muse", "configured": true, "connected": false, "ready": true, "busy": false, "reason": "browser_idle"}
  ],
  "models": [
    {"id": "muse/text", "provider": "muse", "type": "text", "configured": true, "connected": false, "ready": true, "busy": false, "reason": "browser_idle", "modes": ["text"], "maxReferences": 0, "allowedMimeTypes": [], "maxReferenceBytes": 0},
    {"id": "muse/image", "provider": "muse", "type": "image", "configured": true, "connected": false, "ready": true, "busy": false, "reason": "browser_idle", "modes": ["text", "singleImage", "multiReference"], "maxReferences": 10, "allowedMimeTypes": ["image/jpeg", "image/png", "image/gif", "image/webp", "image/heic", "image/heif", "image/avif"], "maxReferenceBytes": 41943040},
    {"id": "muse/video", "provider": "muse", "type": "video", "configured": true, "connected": false, "ready": true, "busy": false, "reason": "browser_idle", "modes": ["text", "singleImage"], "maxReferences": 1, "allowedMimeTypes": ["image/jpeg", "image/png", "image/gif", "image/webp", "image/heic", "image/heif", "image/avif"], "maxReferenceBytes": 41943040},
    {"id": "muse-spark", "provider": "muse", "type": "text", "configured": true, "connected": false, "ready": true, "busy": false, "reason": "browser_idle", "modes": ["text"], "maxReferences": 0, "allowedMimeTypes": [], "maxReferenceBytes": 0},
    {"id": "muse-image", "provider": "muse", "type": "image", "configured": true, "connected": false, "ready": true, "busy": false, "reason": "browser_idle", "modes": ["text", "singleImage", "multiReference"], "maxReferences": 10, "allowedMimeTypes": ["image/jpeg", "image/png", "image/gif", "image/webp", "image/heic", "image/heif", "image/avif"], "maxReferenceBytes": 41943040},
    {"id": "muse-video", "provider": "muse", "type": "video", "configured": true, "connected": false, "ready": true, "busy": false, "reason": "browser_idle", "modes": ["text", "singleImage"], "maxReferences": 1, "allowedMimeTypes": ["image/jpeg", "image/png", "image/gif", "image/webp", "image/heic", "image/heif", "image/avif"], "maxReferenceBytes": 41943040}
  ],
  "limits": {"perClientPending": 2, "perKindPending": 8},
  "features": {"idempotency": true, "lookupByRequestKey": true, "cancel": true}
}
```

## Gửi tác vụ và tìm biên nhận

1. Lưu UUID mới và ánh xạ `localTaskId` vào bộ nhớ bền trước khi gửi. Dùng UUID làm header `Idempotency-Key` với `POST /v1/images/tasks` hoặc `POST /v1/videos`. Mỗi ý định tạo dùng một UUID. Gửi lại cùng UUID và nội dung chỉ trả lại ID đã nhận. Thay model, loại tác vụ hoặc nội dung với cùng UUID trả 409 `IDEMPOTENCY_CONFLICT`.
2. Lưu `upstreamTaskId` từ biên nhận ảnh HTTP 202 hoặc video HTTP 200. Khi thất lạc phản hồi POST, chỉ gọi `GET /v1/tasks/by-request` với cùng Bearer và UUID. HTTP 200 trả `id`, `task_id`, `status`, `kind` và `progress`. Thiếu hoặc sai định dạng header trả 400. Tác vụ thuộc người khác hoặc biên nhận chưa thấy trả 404. HTTP 404 và HTTP 5xx sau POST đều cần đối chiếu trước khi gửi lại.
3. Gọi `GET /v1/images/tasks/{task_id}` hoặc `GET /v1/videos/{task_id}`. Sau khởi động lại, tác vụ bị gián đoạn giữ ID và báo `status: "interrupted"`, `needs_reconcile: true`. Lỗi mất xác nhận sau khi gửi lên trình duyệt cũng có thể rơi vào trạng thái này. Sau khi quản trị xóa dòng lịch sử có UUID, server vẫn giữ biên nhận tối thiểu với hash để tránh tạo trùng. Chi tiết và kết quả đã xóa khỏi lịch sử có thể trả 404.

Mẫu biên nhận được ghi từ fixture ASGI sau hai POST đồng thời cùng một UUID.

```json
{
  "id": "task_2d6919d757f54135bc76",
  "task_id": "task_2d6919d757f54135bc76",
  "status": "queued",
  "kind": "image",
  "progress": 0
}
```

## Hủy tác vụ và xử lý hàng đầy

Dùng `POST /v1/images/tasks/{task_id}/cancel` hoặc `POST /v1/videos/{task_id}/cancel` với khóa chủ tác vụ. Hủy chuyển trạng thái trong kho bền trước khi trả lời. Tác vụ đang chờ hủy được trước khi worker gửi prompt. Với tác vụ đang chạy, mac2API giữ khe và phiên cho đến khi worker dọn xong.

| HTTP | Kết quả |
|---|---|
| 200 | `{"task_id":"…","status":"cancelled","cancelled":true}` khi hủy được tác vụ đang chờ |
| 409 | `{"code":"CANNOT_CANCEL_RUNNING","status":"processing","message":"Không thể dừng tác vụ đang chạy an toàn."}` |
| 409 | `ALREADY_TERMINAL` khi tác vụ đã kết thúc |
| 404 | Tác vụ vắng, sai loại, sai Base URL hoặc sai chủ |
| 507 | Ghi trạng thái hủy thất bại |

Hợp đồng dành HTTP 202 với `status: "cancel_requested"` cho máy chủ có khả năng dừng bất đồng bộ an toàn. Bản này hỗ trợ hủy khi chờ. Nếu host chặn HTTP sau khi người dùng hủy chạy adapter, client cần gửi yêu cầu hủy từ ngữ cảnh HTTP độc lập.

Khi máy chủ từ chối trước khi nhận tác vụ, HTTP 429 có `Retry-After` theo giây và `error.code`. Các mã gồm `CLIENT_RATE_LIMIT`, `CLIENT_PENDING_LIMIT` và `QUEUE_FULL`. Hai trường `perClientImagePending` và `perImagePending` lần lượt công bố 4 tác vụ ảnh mỗi khóa và 20 tác vụ ảnh toàn máy chủ. `perClientPending` và `perKindPending` lần lượt công bố 2 tác vụ media mỗi khóa khi nhận video và 8 tác vụ video toàn máy chủ. Các trạng thái chờ và đang chạy đều tính vào giới hạn. Khóa vẫn có 20 POST/phút và 2 POST đồng thời. `Retry-After` là thời gian tối thiểu nên đợi sau khi yêu cầu bị từ chối xác định. Trạng thái lỗi sau biên nhận thuộc tác vụ đã nhận.

## Media, quyền và tương thích

| Trường hợp | Cách dùng |
|---|---|
| Muse | `muse/image` hỗ trợ văn bản và ảnh tham chiếu. `muse/video` cung cấp chế độ văn bản và một ảnh mở đầu cho adapter. Các tuyến Muse cũ giữ định dạng ảnh, video và bí danh. |
| ChatGPT Web và Gemini Web | `chatgpt/image` và `gemini-web/image` tạo ảnh từ văn bản. Các mô-đun chat dùng tuyến văn bản. Chrome phải chạy trên máy chủ đã xác minh hồ sơ hoặc tab. |
| OpenAI và Gemini chính thức | Giữ tuyến văn bản hiện có. Khả năng báo cấu hình khóa, còn xác nhận tính hợp lệ cần thao tác riêng của quản trị viên. |
| Tỷ lệ, độ phân giải và thời lượng | Giữ `aspect_ratio`, `resolution` và `duration` trong payload và chỉ dẫn Muse, kể cả 9:16 với độ phân giải cao. `duration` nhận số nguyên dương tùy chọn, kể cả ngoài các mốc chọn nhanh. Nếu bỏ trống, API để Muse dùng mô tả và thiết lập mặc định của nó. Kích thước và thời lượng đầu ra thực tế cần đọc từ tệp. |
| Giới hạn tham chiếu Muse | 20 ảnh cho mô-đun ảnh, 40 MiB mỗi tệp và 100 MiB tổng dữ liệu. PNG, JPEG, WebP và GIF nằm trong danh sách MIME cho phép. Các tham chiếu gửi qua khóa client chỉ tải từ địa chỉ công khai hoặc dữ liệu đính kèm hợp lệ. |
| Kho media | URL ký tối đa 7 ngày và có thể hết hạn sớm nếu tệp được dọn, khóa chủ bị thu hồi hoặc khóa ký thay đổi. Lấy URL mới từ GET tác vụ nếu còn dữ liệu. Tải bytes vào workspace MacFlow trước khi báo đầu ra sẵn sàng. Khi ghi media, kho 5 GB dọn tệp cũ theo ngưỡng 70%. |
| URL quản trị cũ chưa ký | Dùng Bearer quản trị để tải hoặc lấy URL mới từ bảng quản trị. Biến `MUSE2API_ALLOW_LEGACY_PUBLIC_MEDIA=1` giữ URL quản trị chưa ký trong thời gian chuyển đổi có chủ đích. URL client vẫn được kiểm tra quyền. |
| Client tích hợp sẵn | Gọi capabilities, lookup và cancel bằng khóa `m2c_…`. Các tuyến cũ vẫn nhận khóa quản trị để bảng điều khiển và tích hợp hiện có tiếp tục hoạt động. API task cũ tiếp tục chấp nhận khóa idempotency không theo UUID theo phạm vi loại tác vụ và nhà cung cấp. |
| Adapter độc lập | Tệp Muse trong `adapters/2.0.7/` có phiên bản 2.0.11 và kiểm tra giới hạn ảnh của máy chủ trước POST. Tệp ChatGPT và Gemini có phiên bản 2.0.8. Cả ba dùng hợp đồng v1 và chuyển sang `/models` khi capabilities trả 404. Host phải hỗ trợ lưu trạng thái tác vụ trước POST. Adapter lưu UUID cho GET tra cứu và dừng nếu kết quả chưa rõ. |
| Máy chủ tự triển khai | Chạy một tiến trình mac2API cho mỗi thư mục dữ liệu. Qua proxy ngược, cấu hình HTTPS và chuyển đúng scheme từ proxy tin cậy. Thêm origin trình duyệt được phép bằng `MUSE2API_CORS_ORIGINS`. Giữ chế độ AIChrome của MacFlow độc lập với phiên do mac2API điều khiển. |

## Chẩn đoán tác vụ Muse

Các tác vụ mới có `reference_count` là số tệp hợp lệ API nhận. Khi lỗi xảy ra trong ô nhập, `confirmed_reference_count` cho biết số tệp mà trình duyệt quan sát được. `error_stage` có thể là `before_send`, `muse_reply` hoặc `delivery_unknown`.

| Mã | Ý nghĩa |
|---|---|
| `MUSE_REFERENCES_MISSING` | HTTP 400 trước khi tạo tác vụ nếu mô tả có yêu cầu tham chiếu rõ ràng nhưng payload thiếu tệp. |
| `MUSE_PAGE_NOT_READY` | Ô chat Muse chưa sẵn sàng. Trang đã tải nhưng thiếu ô chat có thể được tải lại một lần trong cùng phiên và tuyến proxy, trong thời gian chờ. Nếu vẫn lỗi, dừng lần thử trước khi gửi. |
| `MUSE_DRAFT_NOT_READY` | Không điền được hoặc ô nhập không giữ được toàn bộ mô tả trước khi gửi. |
| `MUSE_SEND_BUTTON_MISSING` | Ô nhập đã giữ mô tả và đúng số tệp, nhưng không tìm thấy nút Gửi trong ô nhập. |
| `MUSE_SEND_BUTTON_DISABLED` | Đã xác nhận mô tả và số tệp, nhưng nút Gửi vẫn bị khóa khi hết thời gian chờ. |
| `MUSE_REFERENCE_UPLOAD_UNCONFIRMED` | Số tệp trong ô nhập khác với số tệp cần gửi. |
| `MUSE_REFERENCES_NOT_SEEN` | Sau khi nhận prompt, Muse phản hồi rằng không thấy tham chiếu. So sánh với số tệp API đã nhận. |
| `MUSE_REFERENCE_INCOMPLETE` | Muse báo thiếu ảnh tham chiếu thứ hai sau khi nhận prompt. |
| `MUSE_VIDEO_UNAVAILABLE` | Muse thông báo tài khoản hiện tại không có tính năng tạo video ở khu vực hiện tại. |
| `MUSE_CONFIRMATION_REQUIRED` | Muse đề nghị duyệt hoặc đổi phương án. |

## Cách chạy kiểm thử

```bash
python tests/test_mac2api_contract.py
python tests/test_api_clients.py
python tests/test_async_images.py
python tests/test_web_modules.py
node --test tests/*.mjs
```

Các fixture kiểm tra chủ tác vụ, song song cùng UUID, khác payload, hồi phục sau restart, hủy và giải phóng khe, giới hạn hàng đợi, chữ ký media, MIME, SSRF và Chrome cục bộ giả lập. Không sử dụng khóa thật hoặc gửi yêu cầu tạo nội dung lên dịch vụ AI.
