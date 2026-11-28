type MediaInput =
    | { type: "url"; url: string; mimeType?: string }
    | { type: "base64"; data: string; mimeType: string }
    | { type: "binary"; data: Uint8Array; mimeType: string };

type CommonRequest = { model: string; prompt: string; ratio?: string; other?: Record<string, unknown> };
type ImageRequest = CommonRequest & {
    images?: MediaInput[]; mask?: MediaInput; n?: number; size?: string; quality?: string; outputFormat?: string;
};
type VideoRequest = CommonRequest & {
    mode?: string | string[]; images?: MediaInput[]; firstFrame?: MediaInput; lastFrame?: MediaInput;
    videos?: MediaInput[]; audios?: MediaInput[]; duration?: number; resolution?: string;
    generateAudio?: boolean; watermark?: boolean;
};

type TaskKind = "image" | "video";
type MediaAsset = { mediaType: TaskKind; type: "binary"; mimeType: string; data: Uint8Array };
type PendingTask = { kind: TaskKind; base: string; model?: string; requestKey?: string; canCancel: boolean };
type Task = PendingTask & { id: string };
type StoredTask = Record<string, unknown>;
type Context = {
    config: { baseUrl?: string; apiKey?: string };
    signal?: AbortSignal;
    saveTaskData?: (data: StoredTask) => Promise<void>;
    tool: { fetch: typeof fetch; errorMessage?: (data: unknown) => string };
};
type Payload = { model: string; prompt: string; image?: string; images?: string[]; [field: string]: unknown };
type ModelStatus = { id?: string; ready?: boolean; provider?: string; reason?: string; maxReferences?: number };
type ApiReply = {
    id?: string; task_id?: string; kind?: string; provider?: string; status?: string;
    url?: string; video?: { url?: string }; result?: { url?: string }; data?: { id?: string; url?: string }[];
    error?: unknown; last_error?: unknown; detail?: unknown; needs_reconcile?: boolean;
    contractVersion?: number; features?: { idempotency?: boolean; lookupByRequestKey?: boolean; cancel?: boolean };
    models?: ModelStatus[]; providers?: ModelStatus[];
};

export default {
    id: "mac2ApiMuse",
    label: "mac2API · Muse",
    version: "2.0.12",
    readme: "Tạo ảnh với tối đa 20 ảnh tham chiếu trên bản mac2API hỗ trợ. Trong cài đặt video, ô Thời lượng tùy chỉnh (giây) ghi đè thời lượng chọn nhanh khi có giá trị. Kết quả thực tế phụ thuộc vào Muse. Nhập Base URL và khóa người dùng mac2API (m2c_…).",
    rules: [
        {
            type: "input",
            field: "baseUrl",
            title: "Base URL mac2API",
            value: "http://127.0.0.1:18610/muse/v1",
            validate: [
                {
                    required: true,
                    message: "Hãy nhập Base URL mac2API."
                }
            ]
        },
        {
            type: "input",
            field: "apiKey",
            title: "API Key mac2API",
            value: "",
            props: {
                type: "password",
                showPassword: true,
                autocomplete: "off"
            },
            validate: [{ required: true, message: "Hãy nhập khóa người dùng mac2API." }]
        }
    ],
    models: [
        {
            id: "muse/image",
            label: "Muse Ảnh",
            type: "image",
            mode: [
                "text",
                "singleImage",
                "multiReference"
            ],
            imageSizes: [
                "740",
                "1080",
                "1K",
                "2K",
                "4K"
            ],
            imageRatios: [
                "1:1",
                "9:16",
                "16:9",
                "3:4",
                "4:3"
            ]
        },
        {
            id: "muse/video",
            label: "Muse Video",
            type: "video",
            mode: [
                "text",
                "singleImage"
            ],
            audio: false,
            imageRatios: [
                "1:1",
                "9:16",
                "16:9",
                "3:4",
                "4:3"
            ],
            durationResolutionMap: [
                {
                    duration: [
                        5,
                        10,
                        15,
                        20,
                        30,
                        45,
                        60
                    ],
                    resolution: [
                        "740",
                        "1080",
                        "1K",
                        "2K",
                        "4K"
                    ]
                }
            ],
            rules: [
                {
                    type: "input",
                    field: "mac2ApiVideoDuration",
                    title: "Thời lượng tùy chỉnh (giây)",
                    value: "",
                    required: false,
                    props: {
                        type: "number",
                        min: 1,
                        step: 1,
                        placeholder: "Dùng thời lượng chọn nhanh"
                    }
                },
                {
                    type: "select",
                    field: "mac2ApiVideoRatio",
                    title: "Khung hình Muse",
                    value: "",
                    options: [
                        {
                            label: "Theo tỷ lệ chung",
                            value: ""
                        },
                        {
                            label: "1:1",
                            value: "1:1"
                        },
                        {
                            label: "9:16",
                            value: "9:16"
                        },
                        {
                            label: "16:9",
                            value: "16:9"
                        },
                        {
                            label: "3:4",
                            value: "3:4"
                        },
                        {
                            label: "4:3",
                            value: "4:3"
                        }
                    ]
                }
            ]
        }
    ],
    async generateImage(this: Context, input: ImageRequest): Promise<MediaAsset[]> {
        checkPrompt(input);
        if (input.model !== "muse/image") {
            throw new Error("Mô-đun ảnh không hợp lệ.");
        }
        if (input.mask || input.n != null && input.n !== 1 || input.outputFormat) {
            throw new Error("Mô-đun chỉ trả một ảnh và chưa hỗ trợ mặt nạ hoặc định dạng đầu ra tùy chọn.");
        }
        if (input.images != null && !Array.isArray(input.images)) throw new Error("Danh sách ảnh tham chiếu không hợp lệ.");
        if ((input.images?.length ?? 0) > 20) {
            throw new Error("Chọn tối đa 20 ảnh tham chiếu cho một tác vụ tạo ảnh.");
        }
        return submit(this, "image", {
            model: input.model,
            prompt: input.prompt,
            response_format: "url",
            ...input.images?.length ? {
                images: input.images.map(imageInput)
            } : {},
            ...imageOptions(input)
        });
    },
    async generateVideo(this: Context, input: VideoRequest): Promise<MediaAsset[]> {
        checkPrompt(input, [
            "mac2ApiVideoRatio",
            "mac2ApiVideoDuration"
        ]);
        if (input.model !== "muse/video" ||
            (input.mode != null && input.mode !== "text" && input.mode !== "singleImage")) {
            throw new Error("Chỉ hỗ trợ Muse Video từ văn bản hoặc một ảnh mở đầu.");
        }
        if (input.lastFrame) throw new Error("Muse Video chưa hỗ trợ ảnh khung cuối.");
        if (input.videos?.length || input.audios?.length) {
            throw new Error("Muse Video chưa hỗ trợ âm thanh hoặc video làm tệp tham chiếu.");
        }
        if (input.generateAudio === true) throw new Error("Muse Video chưa hỗ trợ bật tùy chọn Tạo âm thanh.");
        if (input.watermark === true) throw new Error("Muse Video chưa hỗ trợ bật tùy chọn Hình mờ.");
        if (input.images != null && !Array.isArray(input.images)) throw new Error("Danh sách ảnh tham chiếu không hợp lệ.");
        const images = input.images ?? [];
        if (images.length > 1 || input.firstFrame && images.length) {
            throw new Error("Chỉ gửi một ảnh mở đầu.");
        }
        const first = input.firstFrame ?? images[0];
        if (input.mode === "singleImage" && !first) throw new Error("Hãy thêm ảnh mở đầu.");
        if (input.mode === "text" && first) throw new Error("Hãy chọn chế độ có ảnh mở đầu.");
        return submit(this, "video", {
            model: input.model,
            prompt: input.prompt,
            ...first ? {
                image: imageInput(first)
            } : {},
            ...videoOptions(input)
        });
    },
    async resumeTask(this: Context, data: StoredTask): Promise<MediaAsset[]> {
        const { base } = connection(this);
        if (!data || !validKind(data.kind) || data.base !== base ||
            (data.model !== undefined && data.model !== "muse/" + data.kind) ||
            (!validId(data.id) && !validRequestKey(data.requestKey))) {
            throw new Error("Thông tin tác vụ mac2API đã lưu không hợp lệ hoặc Base URL đã đổi.");
        }
        const pending: PendingTask = {
            kind: data.kind, base, model: "muse/" + data.kind,
            requestKey: validRequestKey(data.requestKey) ? data.requestKey : undefined,
            canCancel: data.canCancel === true
        };
        const id = data.id;
        return withDeadline(this, signal => validId(id)
            ? followTask(this, { ...pending, id }, signal)
            : reconcile(this, pending, signal));
    }
};
function connection(context: Context) {
    const entered = optionalText(context.config.baseUrl, "Base URL");
    const key = optionalText(context.config.apiKey, "Khóa API");
    if (!entered || !key) throw new Error("Hãy nhập Base URL và khóa người dùng mac2API (m2c_…).");
    let url;
    try {
        url = new URL(entered);
    } catch  {
        throw new Error("Địa chỉ mac2API không hợp lệ.");
    }
    if (![
        "https:",
        "http:"
    ].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
        throw new Error("Base URL phải là địa chỉ HTTP(S) của mac2API và không chứa khóa hoặc tham số.");
    }
    if (url.protocol === "http:" && !["127.0.0.1", "[::1]", "localhost"].includes(url.hostname)) {
        throw new Error("Dùng HTTPS khi kết nối đến mac2API từ xa.");
    }
    const root = url.href.replace(/\/+$/, "");
    return {
        base: root.endsWith("/v1") ? root : root + "/v1",
        key
    };
}
async function request(context: Context, endpoint: string, signal: AbortSignal,
                       body?: Payload | Record<string, never>, requestKey?: string): Promise<ApiReply> {
    const { base, key } = connection(context);
    let response: Response;
    try {
        response = await context.tool.fetch(base + endpoint, {
            method: body ? "POST" : "GET",
            headers: {
                Authorization: "Bearer " + key,
                ...(requestKey ? { "Idempotency-Key": requestKey } : {}),
                ...(body ? { "Content-Type": "application/json" } : {})
            },
            body: body ? JSON.stringify(body) : undefined,
            redirect: "error",
            signal
        });
    } catch {
        signal.throwIfAborted();
        throw new Error("Mất kết nối với mac2API. Hãy kiểm tra ứng dụng và địa chỉ đã nhập.");
    }
    const result: ApiReply | null = await response.json().catch(() => null);
    signal.throwIfAborted();
    if (!response.ok) {
        const { status } = response;
        const detail = result?.error ?? result?.detail;
        const errorInfo = detail && typeof detail === "object" ? detail as Record<string, unknown> : {};
        const reason = taskErrorDetail(context, errorInfo.message ?? detail);
        const retryAfter = response.headers.get("Retry-After") ?? "";
        const messages: Record<number, string> = {
            400: reason || "Dữ liệu đầu vào chưa hợp lệ. Hãy kiểm tra mô tả và tham chiếu.",
            401: "Khóa mac2API không hợp lệ.",
            403: "Cần khóa người dùng m2c_ có quyền phù hợp và kết nối HTTPS từ xa.",
            404: "Không tìm thấy địa chỉ hoặc tác vụ mac2API.",
            409: reason || "Trạng thái tác vụ chưa cho phép thao tác này.",
            410: "Tệp kết quả đã hết hạn hoặc đã được dọn khỏi kho media.",
            422: "Dữ liệu đầu vào chưa đúng định dạng. Hãy kiểm tra các tùy chọn.",
            429: "mac2API đang giới hạn yêu cầu" + (/^\d+$/.test(retryAfter) ? " trong " + retryAfter + " giây." : "."),
            503: "mac2API hoặc tài khoản của dịch vụ chưa sẵn sàng."
        };
        throw Object.assign(new Error(messages[status] ?? "mac2API chưa xử lý được yêu cầu. Hãy kiểm tra mục Tác vụ."), {
            status, ...(typeof errorInfo.code === "string" ? { code: errorInfo.code } : {})
        });
    }
    if (!result || typeof result !== "object" || Array.isArray(result)) {
        throw new Error("Phản hồi mac2API không hợp lệ. Hãy kiểm tra tác vụ trước khi gửi lại.");
    }
    return result;
}

function httpStatus(error: unknown): number | undefined {
    return error && typeof error === "object" && "status" in error && typeof error.status === "number"
        ? error.status : undefined;
}

function wait(signal: AbortSignal): Promise<void> {
    return new Promise((resolve, reject)=>{
        signal.throwIfAborted();
        const abort = ()=>{
            clearTimeout(timer);
            reject(signal.reason);
        };
        const timer = setTimeout(()=>{
            signal.removeEventListener("abort", abort);
            resolve();
        }, 2000);
        signal.addEventListener("abort", abort, {
            once: true
        });
    });
}
function validId(value: unknown): value is string {
    return typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);
}
function validKind(value: unknown): value is TaskKind {
    return value === "image" || value === "video";
}
function taskErrorDetail(context: Context, value: unknown) {
    if (typeof value !== "string") value = context.tool.errorMessage?.(value);
    if (typeof value !== "string" || !value.trim()) return "";
    const key = optionalText(context.config.apiKey, "Khóa API");
    const upload = value.match(/参考素材上传未确认\s*[（(]\s*(\d+)\s*\/\s*(\d+)\s*[）)]/);
    if (upload) return `Muse chưa xác nhận tệp tham chiếu (${upload[1]}/${upload[2]}). Yêu cầu tạo nội dung chưa được gửi.`;
    if (/页面加载超时[（(]未出现聊天输入框[）)]/.test(value)) {
        return "Hết thời gian tải Muse.ai vì ô chat chưa xuất hiện. Tác vụ dừng trước khi gửi yêu cầu. " + "Hãy kiểm tra mạng hoặc proxy của máy chạy mac2API. Chưa xác định phiên đăng nhập hết hiệu lực.";
    }
    const detail = key ? value.split(key).join("[đã ẩn]") : value;
    return detail.replace(/^模型未生成媒体[，,]\s*仅返回文本\s*[:：]\s*/, "Muse chỉ trả văn bản và chưa gửi tệp kết quả. Phản hồi của Muse: ").replace(/\bBearer\s+\S+/gi, "Bearer [đã ẩn]").replace(/\bm2[ac]_[\w.-]+/gi, "[đã ẩn]").replace(/\b(?:https?|socks5h?):\/\/[^\s<>"']+/gi, "[đã ẩn URL]").replace(/data:[^\s<>"']+/gi, "[đã ẩn dữ liệu]").replace(/\s+/g, " ").trim().slice(0, 400);
}
const maxResultBytes = 100 * 1024 * 1024;

function imageMime(bytes: Buffer): string | undefined {
    const signature = bytes.subarray(0, 12).toString("hex");
    if (signature.startsWith("89504e470d0a1a0a")) return "image/png";
    if (signature.startsWith("ffd8ff")) return "image/jpeg";
    if (/^474946383[79]61/.test(signature)) return "image/gif";
    if (/^52494646[0-9a-f]{8}57454250/.test(signature)) return "image/webp";
    if (["ftypavif", "ftypavis"].includes(bytes.toString("ascii", 4, 12))) return "image/avif";
    if (signature.startsWith("424d")) return "image/bmp";
    if (/^(49492a00|4d4d002a)/.test(signature)) return "image/tiff";
}

function videoMime(bytes: Buffer): string | undefined {
    if (bytes.length >= 12 && bytes.toString("ascii", 4, 8) === "ftyp") {
        const brand = bytes.toString("ascii", 8, 12);
        if (brand === "qt  ") return "video/quicktime";
        if (/^(?:iso[m0-9]|mp4[12]|avc1|dash|M4V |MSNV|M4VH)$/.test(brand)) return "video/mp4";
    }
    if (bytes.subarray(0, 4).toString("hex") === "1a45dfa3" && bytes.subarray(4, 128).includes("webm")) {
        return "video/webm";
    }
}

async function downloadResult(context: Context, task: Task, candidate: unknown, signal: AbortSignal): Promise<MediaAsset[]> {
    if (typeof candidate !== "string" || !candidate.trim()) throw new Error("Tác vụ hoàn tất nhưng chưa có tệp kết quả.");
    const { base, key } = connection(context);
    const configured = new URL(base);
    let url: URL;
    try {
        url = new URL(candidate, configured);
    } catch {
        throw new Error("Đường dẫn tệp kết quả không hợp lệ.");
    }
    const prefix = configured.pathname.replace(/\/(?:(?:muse|chatgpt|gemini)\/)?v1$/, "");
    const directories = ["/v1/media/", prefix + "/v1/media/"];
    const validPath = directories.some(path => url.pathname.startsWith(path)
        && /^[a-z0-9][a-z0-9_.-]{0,255}$/i.test(url.pathname.slice(path.length)));
    if (url.origin !== configured.origin || url.username || url.password || !validPath) {
        throw new Error("Đường dẫn tệp nằm ngoài kho media của mac2API đã cấu hình.");
    }
    let response: Response;
    try {
        response = await context.tool.fetch(url.href, {
            method: "GET", headers: { Authorization: "Bearer " + key },
            redirect: "error", cache: "no-store", signal
        });
    } catch {
        signal.throwIfAborted();
        throw new Error("Chưa tải được tệp từ mac2API. Hãy mở lại tác vụ đã gửi.");
    }
    if (!response.ok || Number(response.headers.get("Content-Length")) > maxResultBytes) {
        await response.body?.cancel().catch(() => {});
        throw new Error(response.ok ? "Tệp kết quả vượt giới hạn 100 MiB của Toonflow."
            : "Không tải được tệp kết quả. Hãy kiểm tra tệp trong mục Tác vụ của mac2API.");
    }
    const reader = response.body?.getReader();
    if (!reader) throw new Error("Tệp kết quả trống.");
    const chunks: Uint8Array[] = [];
    let size = 0;
    try {
        while (true) {
            signal.throwIfAborted();
            const { done, value } = await reader.read();
            if (done) break;
            size += value.byteLength;
            if (size > maxResultBytes) throw new Error("Tệp kết quả vượt giới hạn 100 MiB của Toonflow.");
            chunks.push(value);
        }
    } finally {
        await reader.cancel().catch(() => {});
    }
    signal.throwIfAborted();
    if (!size) throw new Error("Tệp kết quả trống.");
    const data = Buffer.concat(chunks, size);
    const mimeType = task.kind === "video" ? videoMime(data) : imageMime(data);
    if (!mimeType) throw new Error(task.kind === "image"
        ? "Tệp kết quả chưa phải ảnh Toonflow hỗ trợ."
        : "Tệp kết quả chưa phải video MP4, MOV hoặc WebM hợp lệ.");
    return [{ mediaType: task.kind, type: "binary", mimeType, data }];
}

async function poll(context: Context, task: Task, signal: AbortSignal): Promise<MediaAsset[]> {
    if (connection(context).base !== task.base) throw new Error("Base URL đã đổi. Hãy khôi phục địa chỉ của tác vụ gốc.");
    const endpoint = task.kind === "image" ? "/images/tasks/" : "/videos/";
    while (true) {
        signal.throwIfAborted();
        const result = await request(context, endpoint + task.id, signal);
        if ((result.id !== undefined && result.id !== task.id) ||
            (result.kind !== undefined && result.kind !== task.kind) ||
            (result.provider !== undefined && result.provider !== "muse")) {
            throw new Error("Máy chủ trả về tác vụ khác. Hãy kiểm tra lại tác vụ ban đầu.");
        }
        if (result.needs_reconcile || result.status === "interrupted" || result.status === "needs_reconcile") {
            throw Object.assign(new Error("mac2API đã gián đoạn tác vụ " + task.id + ". Hãy đối chiếu trước khi gửi lại."), {
                code: "MEDIA_TASK_NEEDS_RECONCILE"
            });
        }
        if (result.status === "failed" || result.status === "cancelled" || result.status === "expired") {
            const state = result.status === "failed" ? "thất bại" : result.status === "cancelled" ? "bị hủy" : "hết hạn";
            const raw = typeof result.error === "string" && result.error.trim() ? result.error : result.last_error;
            const reason = taskErrorDetail(context, raw);
            throw Object.assign(new Error(`Tác vụ mac2API ${task.id} ${state}.${reason ? " Lý do: " + reason : " Xem chi tiết trong mục Tác vụ của mac2API."}`), {
                code: "MEDIA_TASK_FAILED"
            });
        }
        if (result.status === "completed") {
            const candidate = task.kind === "image" ? result.data?.[0]?.url ?? result.result?.url
                : result.result?.url ?? result.url ?? result.video?.url;
            return downloadResult(context, task, candidate, signal);
        }
        if (result.status !== "queued" && result.status !== "processing") {
            throw new Error("Trạng thái tác vụ chưa rõ. Hãy kiểm tra tác vụ cũ trước khi gửi lại.");
        }
        await wait(signal);
    }
}

async function withDeadline(context: Context, run: (signal: AbortSignal) => Promise<MediaAsset[]>): Promise<MediaAsset[]> {
    const timeout = new AbortController();
    const timer = setTimeout(() => timeout.abort(new Error(
        "Hết thời gian chờ mac2API. Hãy theo dõi lại tác vụ đã gửi trước khi tạo mới."
    )), 15 * 60 * 1000);
    const signal = context.signal ? AbortSignal.any([timeout.signal, context.signal]) : timeout.signal;
    try {
        return await run(signal);
    } finally {
        clearTimeout(timer);
    }
}

function requestUuid() {
    // This UUID deduplicates a request within an authenticated owner, rather than authenticating it.
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, token => {
        const digit = Math.floor(Math.random() * 16);
        return (token === "x" ? digit : (digit & 3) | 8).toString(16);
    });
}

function validRequestKey(value: unknown): value is string {
    return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

async function discover(context: Context, model: string, signal: AbortSignal, referenceCount: number) {
    let capability;
    try {
        capability = await request(context, "/capabilities", signal);
    } catch (error) {
        if (httpStatus(error) !== 404) throw error;
        const catalog = await request(context, "/models", signal);
        if (!Array.isArray(catalog.data) || !catalog.data.some(item => item?.id === model)) {
            throw new Error("Máy chủ chưa cung cấp mô-đun đã chọn. Kiểm tra Base URL và kết nối trình duyệt.");
        }
        return false;
    }
    if (capability.contractVersion !== 1 || capability.features?.idempotency !== true
        || capability.features?.lookupByRequestKey !== true || !Array.isArray(capability.models)) {
        throw new Error("Bản mac2API này dùng hợp đồng chưa được adapter hỗ trợ.");
    }
    const selected = capability.models.find(item => item?.id === model);
    if (!selected) throw new Error("Khóa API hoặc Base URL chưa có quyền dùng mô-đun đã chọn.");
    if (typeof selected.maxReferences === "number" && Number.isInteger(selected.maxReferences) && referenceCount > selected.maxReferences) {
        throw new Error(`Máy chủ mac2API cho phép tối đa ${selected.maxReferences} ảnh tham chiếu. Tác vụ chưa được gửi.`);
    }
    if (selected.ready !== true) {
        const provider = Array.isArray(capability.providers) ? capability.providers.find(item => item?.id === selected.provider) : undefined;
        const reasons: Record<string, string> = {
            not_configured: "chưa cấu hình tài khoản",
            session_unverified: "chưa xác minh phiên đăng nhập",
            browser_unavailable: "chưa tìm thấy trình duyệt trên máy chạy mac2API",
            browser_disconnected: "mất kết nối trình duyệt"
        };
        const reason = reasons[selected.reason ?? provider?.reason ?? ""];
        throw new Error("Mô-đun mac2API chưa sẵn sàng" + (reason ? " vì " + reason : "") + ".");
    }
    return capability.features.cancel === true;
}

async function reconcile(context: Context, pending: PendingTask, signal: AbortSignal) {
    let receipt;
    try {
        receipt = await request(context, "/tasks/by-request", signal, undefined, pending.requestKey);
    } catch (error) {
        if (httpStatus(error) !== 404) throw error;
        throw Object.assign(new Error("Chưa tìm thấy biên nhận. Hãy kiểm tra mục Tác vụ trước khi gửi lại."), {
            code: "MEDIA_TASK_NEEDS_RECONCILE"
        });
    }
    return saveReceipt(context, pending, receipt, signal);
}

async function saveReceipt(context: Context, pending: PendingTask, receipt: ApiReply, signal: AbortSignal) {
    const id = receipt.task_id ?? receipt.id;
    if (!validId(id) || (receipt.id !== undefined && receipt.id !== id)
        || (receipt.kind !== undefined && receipt.kind !== pending.kind)) {
        throw Object.assign(new Error("Chưa xác nhận được biên nhận của tác vụ."), {
            code: "MEDIA_TASK_NEEDS_RECONCILE"
        });
    }
    const task = { ...pending, id };
    await context.saveTaskData?.(task);
    return followTask(context, task, signal);
}

async function followTask(context: Context, task: Task, signal: AbortSignal) {
    try {
        return await poll(context, task, signal);
    } finally {
        if (context.signal?.aborted && task.canCancel && connection(context).base === task.base) {
            const endpoint = task.kind === "image" ? "/images/tasks/" : "/videos/";
            try {
                await request(context, endpoint + task.id + "/cancel", AbortSignal.timeout(3000), {});
            } catch {
                // A running task or a disconnected server may continue upstream.
            }
        }
    }
}

function submit(context: Context, kind: TaskKind, body: Payload) {
    return withDeadline(context, signal => submitTask(context, kind, body, signal));
}

async function submitTask(context: Context, kind: TaskKind, body: Payload, signal: AbortSignal) {
    signal.throwIfAborted();
    const { base } = connection(context);
    const canCancel = await discover(context, body.model, signal, body.images?.length ?? (body.image ? 1 : 0));
    const pending: PendingTask = { kind, base, model: body.model, requestKey: requestUuid(), canCancel };
    await context.saveTaskData?.(pending);
    signal.throwIfAborted();
    const endpoint = kind === "image" ? "/images/tasks" : "/videos";
    let receipt;
    try {
        receipt = await request(context, endpoint, signal, body, pending.requestKey);
    } catch (error) {
        const status = httpStatus(error);
        if (signal.aborted || (status !== undefined && status >= 400 && status < 500 && status !== 408)) throw error;
        return reconcile(context, pending, signal);
    }
    if (!validId(receipt.task_id ?? receipt.id)) return reconcile(context, pending, signal);
    return saveReceipt(context, pending, receipt, signal);
}
function checkPrompt(request: CommonRequest, allowedOptions: string[] = []) {
    if (typeof request?.prompt !== "string" || !request.prompt.trim()) throw new Error("Hãy nhập mô tả.");
    if (Object.keys(request.other ?? {}).some((key)=>!allowedOptions.includes(key))) throw new Error("Mô-đun này chưa hỗ trợ tham số tùy chỉnh.");
}
function imageInput(input: MediaInput): string {
    if (!input || typeof input !== "object") throw new Error("Ảnh tham chiếu không hợp lệ.");
    if (input.type === "url") {
        try {
            const url = new URL(input.url);
            if (["http:", "https:"].includes(url.protocol) && !url.username && !url.password) return url.href;
        } catch { /* Show the same error for invalid URLs. */ }
        throw new Error("Ảnh tham chiếu phải là URL HTTP(S) hoặc tệp ảnh.");
    }
    const mime = typeof input.mimeType === "string" ? input.mimeType.trim().toLowerCase() : "";
    if (!["image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/heic", "image/heif", "image/avif"].includes(mime)) {
        throw new Error("Định dạng ảnh tham chiếu chưa được Muse hỗ trợ.");
    }
    const limit = 40 * 1024 * 1024;
    let data: string;
    if (input.type === "base64" && typeof input.data === "string") {
        data = input.data.replace(/\s/g, "");
        if (!data || data.length % 4 === 1 || !/^[a-z0-9+/]+={0,2}$/i.test(data)) {
            throw new Error("Dữ liệu Base64 của ảnh tham chiếu không hợp lệ.");
        }
        const bytes = Math.floor(data.replace(/=+$/, "").length * 3 / 4);
        if (!bytes || bytes > limit) throw new Error("Ảnh tham chiếu trống hoặc vượt 40 MiB.");
    } else if (input.type === "binary" && ArrayBuffer.isView(input.data) && input.data.BYTES_PER_ELEMENT === 1) {
        if (!input.data.byteLength || input.data.byteLength > limit) {
            throw new Error("Ảnh tham chiếu trống hoặc vượt 40 MiB.");
        }
        data = Buffer.from(input.data).toString("base64");
    } else {
        throw new Error("Ảnh tham chiếu phải là URL, Base64 hoặc tệp ảnh.");
    }
    return "data:" + (mime === "image/jpg" ? "image/jpeg" : mime) + ";base64," + data;
}

function optionalText(value: unknown, label: string): string {
    if (value == null) return "";
    if (typeof value !== "string") throw new Error(label + " không hợp lệ.");
    return value.trim();
}

function requestedRatio(value: unknown) {
    if (value === undefined || value === null || value === "") return;
    if (typeof value !== "string") throw new Error("Tỷ lệ khung hình không hợp lệ.");
    const ratio = value.trim();
    if (!ratio || ratio.toLowerCase() === "auto") return;
    if (![
        "1:1",
        "9:16",
        "16:9",
        "3:4",
        "4:3"
    ].includes(ratio)) {
        throw new Error("Hãy chọn tỷ lệ 1:1, 9:16, 16:9, 3:4 hoặc 4:3.");
    }
    return ratio;
}
function resolutionPreset(value?: string) {
    const choice = value?.trim().toUpperCase() ?? "";
    const pixels = choice.match(/^(740|1080)P?$/);
    if (pixels) return pixels[1] + "p";
    if ([
        "1K",
        "2K",
        "4K"
    ].includes(choice)) return choice;
    return undefined;
}
function imageOptions(input: ImageRequest) {
    const ratio = requestedRatio(input.ratio);
    const size = optionalText(input.size, "Kích thước ảnh");
    const quality = optionalText(input.quality, "Chất lượng ảnh").toLowerCase();
    const explicitSize = size && !["auto", "default"].includes(size.toLowerCase()) ? size : undefined;
    if (explicitSize && !resolutionPreset(explicitSize) && !/^[1-9]\d{1,4}x[1-9]\d{1,4}$/i.test(explicitSize)) {
        throw new Error("Hãy chọn mức ảnh 740, 1080, 1K, 2K hoặc 4K.");
    }
    const qualitySize = resolutionPreset(quality);
    const qualityHint = ["low", "medium", "high", "hd"].includes(quality);
    if (quality && !qualitySize && !qualityHint && !["auto", "default", "standard"].includes(quality)) {
        throw new Error("Mức chất lượng ảnh không hợp lệ.");
    }
    const sizeFromInput = resolutionPreset(explicitSize) ?? explicitSize;
    if (sizeFromInput && qualitySize && sizeFromInput !== qualitySize) {
        throw new Error("Kích thước và độ phân giải ảnh đang mâu thuẫn. Hãy chọn một mức.");
    }
    const outputSize = sizeFromInput ?? qualitySize;
    const extra = [
        outputSize && "Độ phân giải ảnh mong muốn: " + outputSize + ".",
        qualityHint && "Chất lượng ảnh mong muốn: " + quality + "."
    ].filter(Boolean).join(" ");
    return {
        ...(ratio ? { aspect_ratio: ratio } : {}),
        ...(outputSize ? { size: outputSize } : {}),
        ...(extra ? { extra } : {})
    };
}

function videoOptions(input: VideoRequest) {
    const ratio = requestedRatio(input.other?.mac2ApiVideoRatio) ?? requestedRatio(input.ratio);
    const requestedResolution = optionalText(input.resolution, "Độ phân giải video");
    const resolution = resolutionPreset(requestedResolution) || (requestedResolution?.toLowerCase() === "720p" ? "720p" : undefined);
    if (requestedResolution && !resolution && ![
        "auto",
        "default"
    ].includes(requestedResolution.toLowerCase())) {
        throw new Error("Hãy chọn mức video 740, 1080, 1K, 2K hoặc 4K.");
    }
    const override = input.other?.mac2ApiVideoDuration;
    const value = typeof override === "string" ? override.trim() : override;
    const duration = value == null || value === "" ? input.duration ?? undefined
        : typeof value === "string" && /^\d+$/.test(value) ? Number(value) : value;
    if (duration !== undefined && (typeof duration !== "number" || !Number.isSafeInteger(duration) || duration < 1)) {
        throw new Error("Thời lượng video phải là số nguyên dương (giây).");
    }
    return {
        ...ratio ? {
            aspect_ratio: ratio
        } : {},
        ...duration !== undefined ? {
            duration
        } : {},
        ...resolution ? {
            resolution
        } : {},
        ...ratio ? {
            extra: "Tỷ lệ khung hình video mong muốn: " + ratio + "."
        } : {}
    };
}
