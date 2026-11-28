type MediaInput =
    | { type: "url"; url: string; mimeType?: string }
    | { type: "base64"; data: string; mimeType: string }
    | { type: "binary"; data: Uint8Array; mimeType: string };

type CommonRequest = { model: string; prompt: string; ratio?: string; other?: Record<string, unknown> };
type ImageRequest = CommonRequest & {
    images?: MediaInput[]; mask?: MediaInput; n?: number; size?: string; quality?: string; outputFormat?: string;
};

type TaskKind = "image";
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
    id: "mac2ApiGemini",
    label: "mac2API · Gemini Web",
    version: "2.0.9",
    readme: "Tạo ảnh từ văn bản qua kết nối Gemini đã thiết lập trong mac2API. Độ phân giải và tỷ lệ là thông số yêu cầu. Kết quả thực tế phụ thuộc vào mô hình. Nhập Base URL và khóa người dùng mac2API (m2c_…).",
    rules: [
        {
            type: "input",
            field: "baseUrl",
            title: "Base URL mac2API",
            value: "http://127.0.0.1:18610/gemini/v1",
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
            id: "gemini-web/image",
            label: "Gemini Web Ảnh",
            type: "image",
            mode: [
                "text"
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
        }
    ],
    async generateImage(this: Context, input: ImageRequest): Promise<MediaAsset[]> {
        checkPrompt(input);
        if (input.model !== "gemini-web/image") {
            throw new Error("Mô-đun ảnh không hợp lệ.");
        }
        if (input.mask || input.n != null && input.n !== 1 || input.outputFormat) {
            throw new Error("Mô-đun chỉ trả một ảnh và chưa hỗ trợ mặt nạ hoặc định dạng đầu ra tùy chọn.");
        }
        if (input.images != null && !Array.isArray(input.images)) throw new Error("Danh sách ảnh tham chiếu không hợp lệ.");
        if (input.images?.length) {
            throw new Error("Gemini Web hiện chỉ tạo ảnh từ mô tả văn bản.");
        }
        return submit(this, "image", {
            model: input.model,
            prompt: input.prompt,
            response_format: "url",
            ...imageOptions(input)
        });
    },
    async resumeTask(this: Context, data: StoredTask): Promise<MediaAsset[]> {
        const { base } = connection(this);
        if (!data || !validKind(data.kind) || data.base !== base ||
            (data.model !== undefined && data.model !== "gemini-web/" + data.kind) ||
            (!validId(data.id) && !validRequestKey(data.requestKey))) {
            throw new Error("Thông tin tác vụ mac2API đã lưu không hợp lệ hoặc Base URL đã đổi.");
        }
        const pending: PendingTask = {
            kind: data.kind, base, model: "gemini-web/" + data.kind,
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
    return value === "image";
}
function taskErrorDetail(context: Context, value: unknown) {
    if (typeof value !== "string") value = context.tool.errorMessage?.(value);
    if (typeof value !== "string" || !value.trim()) return "";
    const key = optionalText(context.config.apiKey, "Khóa API");
    const detail = key ? value.split(key).join("[đã ẩn]") : value;
    return detail.replace(/\bBearer\s+\S+/gi, "Bearer [đã ẩn]").replace(/\bm2[ac]_[\w.-]+/gi, "[đã ẩn]").replace(/\b(?:https?|socks5h?):\/\/[^\s<>"']+/gi, "[đã ẩn URL]").replace(/data:[^\s<>"']+/gi, "[đã ẩn dữ liệu]").replace(/\s+/g, " ").trim().slice(0, 400);
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
    const mimeType = imageMime(data);
    if (!mimeType) throw new Error("Tệp kết quả chưa phải ảnh Toonflow hỗ trợ.");
    return [{ mediaType: task.kind, type: "binary", mimeType, data }];
}

async function poll(context: Context, task: Task, signal: AbortSignal): Promise<MediaAsset[]> {
    if (connection(context).base !== task.base) throw new Error("Base URL đã đổi. Hãy khôi phục địa chỉ của tác vụ gốc.");
    const endpoint = "/images/tasks/";
    while (true) {
        signal.throwIfAborted();
        const result = await request(context, endpoint + task.id, signal);
        if ((result.id !== undefined && result.id !== task.id) ||
            (result.kind !== undefined && result.kind !== task.kind) ||
            (result.provider !== undefined && result.provider !== "gemini-web")) {
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
            const candidate = result.data?.[0]?.url ?? result.result?.url;
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

async function discover(context: Context, model: string, signal: AbortSignal) {
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
    const canCancel = await discover(context, body.model, signal);
    const pending: PendingTask = { kind, base, model: body.model, requestKey: requestUuid(), canCancel };
    await context.saveTaskData?.(pending);
    signal.throwIfAborted();
    const endpoint = "/images/tasks";
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
function checkPrompt(request: CommonRequest) {
    if (typeof request?.prompt !== "string" || !request.prompt.trim()) throw new Error("Hãy nhập mô tả.");
    if (Object.keys(request.other ?? {}).length) throw new Error("Mô-đun này chưa hỗ trợ tham số tùy chỉnh.");
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
