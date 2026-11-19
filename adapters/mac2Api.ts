type MediaInput =
  | { type: "url"; url: string; mimeType?: string }
  | { type: "base64"; data: string; mimeType: string }
  | { type: "binary"; data: Uint8Array; mimeType: string };

type CommonRequest = { model: string; prompt: string; ratio?: string; other?: Record<string, unknown> };
type ImageRequest = CommonRequest & {
  images?: MediaInput[]; mask?: MediaInput; n?: number; size?: string; quality?: string; outputFormat?: string;
};
type VideoRequest = CommonRequest & {
  mode?: string | string[]; images?: MediaInput[]; videos?: MediaInput[]; audios?: MediaInput[];
  firstFrame?: MediaInput; lastFrame?: MediaInput; duration?: number; resolution?: string;
  generateAudio?: boolean; watermark?: boolean;
};
type MediaAsset = { mediaType: "image" | "video"; type: "url"; url: string };
type Task = { id: string; kind: "image" | "video"; base: string };
type Context = {
  config: { baseUrl?: string; apiKey?: string };
  signal?: AbortSignal;
  tool: { fetch: typeof globalThis.fetch };
  saveTaskData?: (data: Task) => Promise<void>;
};
type Json = Record<string, unknown>;

function connection(context: Context) {
  const entered = context.config.baseUrl?.trim();
  const key = context.config.apiKey?.trim();
  if (!entered || !key) throw new Error("Hãy nhập Base URL và API Key m2a_ của mac2API.");
  let url: URL;
  try { url = new URL(entered); }
  catch { throw new Error("Địa chỉ mac2API không hợp lệ."); }
  if (!["https:", "http:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error("Base URL phải là địa chỉ HTTP(S) của mac2API và không chứa khóa hoặc tham số.");
  }
  const root = url.href.replace(/\/+$/, "");
  return { base: root.endsWith("/v1") ? root : root + "/v1", key };
}

async function request(context: Context, endpoint: string, signal: AbortSignal, body?: Json): Promise<Json> {
  const { base, key } = connection(context);
  const response = await context.tool.fetch(base + endpoint, {
    method: body ? "POST" : "GET",
    headers: { Authorization: "Bearer " + key, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
    redirect: "error",
    signal,
  });
  if (!response.ok) {
    if (response.status === 401) throw new Error("API Key mac2API không hợp lệ (HTTP 401).");
    if (response.status === 503) throw new Error("Dịch vụ hoặc phiên trình duyệt mac2API hiện chưa sẵn sàng (HTTP 503).");
    if (response.status === 404) throw new Error("Không tìm thấy endpoint hoặc tác vụ mac2API (HTTP 404).");
    throw new Error("Yêu cầu mac2API thất bại (HTTP " + response.status + ").");
  }
  const result: unknown = await response.json();
  if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("Phản hồi mac2API không hợp lệ.");
  return result as Json;
}

function wait(signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    signal.throwIfAborted();
    const abort = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, 2000);
    signal.addEventListener("abort", abort, { once: true });
  });
}

function validId(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);
}

function taskErrorDetail(context: Context, value: unknown) {
  if (typeof value !== "string" || !value.trim()) return "";
  const key = context.config.apiKey?.trim();
  const upload = value.match(/参考素材上传未确认\s*[（(]\s*(\d+)\s*\/\s*(\d+)\s*[）)]/);
  if (upload) return `Muse chưa xác nhận tệp tham chiếu (${upload[1]}/${upload[2]}). Yêu cầu tạo nội dung chưa được gửi.`;
  const detail = key ? value.split(key).join("[đã ẩn]") : value;
  return detail
    .replace(/\bBearer\s+\S+/gi, "Bearer [đã ẩn]")
    .replace(/\bm2a_[\w-]+/gi, "[đã ẩn]")
    .replace(/\b(?:https?|socks5h?):\/\/[^\s<>"']+/gi, "[đã ẩn URL]")
    .replace(/data:[^\s<>"']+/gi, "[đã ẩn dữ liệu]")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 400);
}

async function poll(context: Context, task: Task, signal: AbortSignal): Promise<MediaAsset[]> {
  if (connection(context).base !== task.base) throw new Error("Base URL đã đổi. Hãy khôi phục địa chỉ của tác vụ gốc.");
  const endpoint = task.kind === "image" ? "/images/tasks/" : "/videos/";
  while (true) {
    signal.throwIfAborted();
    const result = await request(context, endpoint + task.id, signal);
    if (result.status === "failed" || result.status === "cancelled" || result.status === "expired") {
      const state = result.status === "failed" ? "thất bại" : result.status === "cancelled" ? "bị hủy" : "hết hạn";
      const raw = typeof result.error === "string" && result.error.trim() ? result.error : result.last_error;
      const reason = taskErrorDetail(context, raw);
      throw Object.assign(new Error(
        `Tác vụ mac2API ${task.id} ${state}.${reason ? " Lý do: " + reason : " Xem chi tiết trong mục Tác vụ của mac2API."}`
      ), { code: "MEDIA_TASK_FAILED" });
    }
    if (result.status === "completed") {
      const candidate = Array.isArray(result.data) ? result.data[0]?.url : result.url;
      if (typeof candidate !== "string") throw new Error("Tác vụ hoàn tất nhưng không có đường dẫn đến tệp.");
      const url = new URL(candidate, task.base);
      if (url.origin !== new URL(task.base).origin || url.username || url.password || !url.pathname.includes("/v1/media/")) {
        throw new Error("Đường dẫn tệp nằm ngoài địa chỉ media mac2API đã cấu hình.");
      }
      return [{ mediaType: task.kind, type: "url", url: url.href }];
    }
    if (result.status !== "queued" && result.status !== "processing") {
      throw new Error("Trạng thái tác vụ chưa rõ. Hãy kiểm tra tác vụ cũ trước khi gửi lại.");
    }
    await wait(signal);
  }
}

function taskSignal(context: Context) {
  const timeout = AbortSignal.timeout(15 * 60 * 1000);
  return context.signal ? AbortSignal.any([timeout, context.signal]) : timeout;
}

async function submit(context: Context, kind: Task["kind"], body: Json): Promise<MediaAsset[]> {
  const signal = taskSignal(context);
  signal.throwIfAborted();
  const { base } = connection(context);
  const endpoint = kind === "image" ? "/images/tasks" : "/videos";
  if (body.model === "chatgpt/image" || body.model === "gemini-web/image") {
    const catalog = await request(context, "/models", signal);
    if (!Array.isArray(catalog.data) || !catalog.data.some(item => item?.id === body.model)) {
      throw new Error("Máy chủ hiện chưa bật mô-đun ảnh web này. Kiểm tra Base URL và cấu hình Chrome của mac2API.");
    }
  }
  let result: Json;
  try { result = await request(context, endpoint, signal, body); }
  catch (error) {
    if (signal.aborted) throw error;
    throw new Error((error instanceof Error ? error.message : "Không kết nối được với mac2API.")
      + " Kiểm tra mục Tác vụ trước khi gửi lại.");
  }
  const id = result.task_id ?? result.id;
  if (!validId(id)) throw new Error("Chưa xác nhận được mã tác vụ. Kiểm tra bảng quản trị trước khi gửi lại.");
  const task = { id, kind, base };
  await context.saveTaskData?.(task);
  return poll(context, task, signal);
}

function checkPrompt(request: CommonRequest) {
  if (!request.prompt?.trim()) throw new Error("Hãy nhập mô tả.");
  if (Object.keys(request.other ?? {}).length) throw new Error("Mô-đun này chưa hỗ trợ tham số tùy chỉnh.");
}

function imageInput(input: MediaInput) {
  if (input.type === "url") {
    const url = new URL(input.url);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
      throw new Error("Ảnh tham chiếu phải là URL HTTP(S) hoặc tệp ảnh.");
    }
    return url.href;
  }
  if (!input.mimeType.startsWith("image/")) throw new Error("Tệp tham chiếu phải là ảnh.");
  const data = input.type === "base64" ? input.data : Buffer.from(input.data).toString("base64");
  return "data:" + input.mimeType + ";base64," + data;
}

export default {
  id: "mac2Api",
  label: "mac2API",
  version: "2.0.1",
  readme: "Dùng một Base URL và khóa m2a_ của mac2API. ChatGPT Web và Gemini Web cần Chrome đã đăng nhập trên cùng máy với mac2API. Adapter này thêm mô-đun ảnh và video.",
  rules: [
    { type: "input", field: "baseUrl", title: "Base URL mac2API", value: "https://mac2api.onrender.com/v1",
      validate: [{ required: true, message: "Hãy nhập Base URL mac2API." }] },
    { type: "input", field: "apiKey", title: "API Key mac2API", value: "",
      props: { type: "password", showPassword: true, autocomplete: "off" } },
  ],
  models: [
    { id: "muse/image", label: "Muse Ảnh", type: "image", mode: ["text", "singleImage", "multiReference"],
      imageRatios: ["1:1", "9:16", "16:9", "4:3", "3:4"] },
    { id: "muse/video", label: "Muse Video", type: "video", mode: ["text", "singleImage"] },
    { id: "chatgpt/image", label: "ChatGPT Web Ảnh", type: "image", mode: ["text"] },
    { id: "gemini-web/image", label: "Gemini Web Ảnh", type: "image", mode: ["text"] },
  ],
  async generateImage(this: Context, input: ImageRequest): Promise<MediaAsset[]> {
    checkPrompt(input);
    if (!["muse/image", "chatgpt/image", "gemini-web/image"].includes(input.model)) {
      throw new Error("Mô-đun ảnh không hợp lệ.");
    }
    if (input.mask || (input.n != null && input.n !== 1) || input.quality || input.outputFormat) {
      throw new Error("Mô-đun chỉ trả một ảnh và chưa hỗ trợ mặt nạ, chất lượng hoặc định dạng đầu ra tùy chọn.");
    }
    if (input.images?.length && input.model !== "muse/image") {
      throw new Error("ChatGPT Web và Gemini Web hiện chỉ tạo ảnh từ mô tả văn bản.");
    }
    return submit(this, "image", {
      model: input.model, prompt: input.prompt, ...(input.images?.length ? { images: input.images.map(imageInput) } : {}),
      ...(input.ratio ? { aspect_ratio: input.ratio } : {}), ...(input.size ? { size: input.size } : {}),
    });
  },
  async generateVideo(this: Context, input: VideoRequest): Promise<MediaAsset[]> {
    checkPrompt(input);
    if (input.model !== "muse/video" || (input.mode && !["text", "singleImage"].includes(String(input.mode)))) {
      throw new Error("Chỉ hỗ trợ Muse Video từ văn bản hoặc một ảnh mở đầu.");
    }
    if (input.lastFrame || input.videos?.length || input.audios?.length ||
        input.generateAudio !== undefined || input.watermark !== undefined) {
      throw new Error("Chưa hỗ trợ khung cuối, audio/video tham chiếu hay tùy chọn âm thanh hoặc hình mờ.");
    }
    const images = input.images ?? [];
    if (images.length > 1 || (input.firstFrame && images.length)) {
      throw new Error("Chỉ gửi một ảnh mở đầu.");
    }
    const first = input.firstFrame ?? images[0];
    if (input.mode === "singleImage" && !first) throw new Error("Hãy thêm ảnh mở đầu.");
    if (input.mode === "text" && first) throw new Error("Hãy chọn chế độ có ảnh mở đầu.");
    return submit(this, "video", {
      model: input.model, prompt: input.prompt, ...(first ? { image: imageInput(first) } : {}),
      ...(input.ratio ? { aspect_ratio: input.ratio } : {}), ...(input.duration != null ? { duration: input.duration } : {}),
      ...(input.resolution ? { resolution: input.resolution } : {}),
    });
  },
  async resumeTask(this: Context, data: Record<string, unknown>): Promise<MediaAsset[]> {
    if (!data || !validId(data.id) || (data.kind !== "image" && data.kind !== "video") || typeof data.base !== "string") {
      throw new Error("Thông tin tác vụ mac2API đã lưu không hợp lệ.");
    }
    return poll(this, { id: data.id, kind: data.kind, base: data.base }, taskSignal(this));
  },
};
