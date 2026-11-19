export default {
    id: "mac2ApiChatGpt",
    label: "mac2API · ChatGPT Web",
    version: "2.0.8",
    readme: "Tạo ảnh từ văn bản qua ChatGPT Web. mac2API cần chạy cùng Chrome đã đăng nhập. Độ phân giải và tỷ lệ là thông số yêu cầu gửi tới mô hình. Kết quả thực tế phụ thuộc vào mô hình. Nhập Base URL đang dùng và khóa người dùng mac2API (m2c_…).",
    rules: [
        {
            type: "input",
            field: "baseUrl",
            title: "Base URL mac2API",
            value: "http://127.0.0.1:18610/v1",
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
            }
        }
    ],
    models: [
        {
            id: "chatgpt/image",
            label: "ChatGPT Web Ảnh",
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
    async generateImage (input) {
        checkPrompt(input);
        if (input.model !== "chatgpt/image") {
            throw new Error("Mô-đun ảnh không hợp lệ.");
        }
        if (input.mask || input.n != null && input.n !== 1 || input.outputFormat) {
            throw new Error("Mô-đun chỉ trả một ảnh và chưa hỗ trợ mặt nạ hoặc định dạng đầu ra tùy chọn.");
        }
        if (input.images?.length) {
            throw new Error("ChatGPT Web hiện chỉ tạo ảnh từ mô tả văn bản.");
        }
        return submit(this, "image", {
            model: input.model,
            prompt: input.prompt,
            ...imageOptions(input)
        });
    },
    async resumeTask (data) {
        if (!data || !["image"].includes(data.kind) || connection(this).base !== data.base
            || (!validId(data.id) && !validRequestKey(data.requestKey))) {
            throw new Error("Thông tin tác vụ mac2API đã lưu không hợp lệ hoặc Base URL đã đổi.");
        }
        const signal = taskSignal(this);
        const pending = { kind: data.kind, base: data.base, requestKey: data.requestKey, canCancel: data.canCancel === true };
        if (!validId(data.id)) return reconcile(this, pending, signal);
        return followTask(this, { ...pending, id: data.id }, signal);
    }
};
function connection(context) {
    const entered = context.config.baseUrl?.trim();
    const key = context.config.apiKey?.trim();
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
async function request(context, endpoint, signal, body, requestKey) {
    const { base, key } = connection(context);
    const response = await context.tool.fetch(base + endpoint, {
        method: body ? "POST" : "GET",
        headers: {
            Authorization: "Bearer " + key,
            ...(requestKey ? { "Idempotency-Key": requestKey } : {}),
            ...body ? {
                "Content-Type": "application/json"
            } : {}
        },
        body: body ? JSON.stringify(body) : undefined,
        redirect: "error",
        signal
    });
    if (!response.ok) {
        const status = response.status;
        const retryAfter = response.headers.get("Retry-After");
        const messages = {
            401: "API Key mac2API không hợp lệ.",
            403: "Cần khóa người dùng m2c_ có quyền phù hợp và kết nối HTTPS từ xa.",
            404: "Không tìm thấy endpoint hoặc tác vụ mac2API.",
            429: "mac2API đang giới hạn yêu cầu" + (/^\d+$/.test(retryAfter ?? "") ? " trong " + retryAfter + " giây." : "."),
            503: "Dịch vụ hoặc phiên trình duyệt mac2API chưa sẵn sàng."
        };
        throw Object.assign(new Error(messages[status] ?? ("Yêu cầu mac2API thất bại (HTTP " + status + ").")), { status });
    }
    const result = await response.json();
    if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("Phản hồi mac2API không hợp lệ.");
    return result;
}
function wait(signal) {
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
function validId(value) {
    return typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);
}
function taskErrorDetail(context, value) {
    if (typeof value !== "string" || !value.trim()) return "";
    const key = context.config.apiKey?.trim();
    const upload = value.match(/参考素材上传未确认\s*[（(]\s*(\d+)\s*\/\s*(\d+)\s*[）)]/);
    if (upload) return `Muse chưa xác nhận tệp tham chiếu (${upload[1]}/${upload[2]}). Yêu cầu tạo nội dung chưa được gửi.`;
    const detail = key ? value.split(key).join("[đã ẩn]") : value;
    return detail.replace(/\bBearer\s+\S+/gi, "Bearer [đã ẩn]").replace(/\bm2a_[\w-]+/gi, "[đã ẩn]").replace(/\b(?:https?|socks5h?):\/\/[^\s<>"']+/gi, "[đã ẩn URL]").replace(/data:[^\s<>"']+/gi, "[đã ẩn dữ liệu]").replace(/\s+/g, " ").trim().slice(0, 400);
}
async function poll(context, task, signal) {
    if (connection(context).base !== task.base) throw new Error("Base URL đã đổi. Hãy khôi phục địa chỉ của tác vụ gốc.");
    const endpoint = "/images/tasks/";
    while(true){
        signal.throwIfAborted();
        const result = await request(context, endpoint + task.id, signal);
        if (result.status === "failed" || result.status === "cancelled" || result.status === "expired") {
            const state = result.status === "failed" ? "thất bại" : result.status === "cancelled" ? "bị hủy" : "hết hạn";
            const raw = typeof result.error === "string" && result.error.trim() ? result.error : result.last_error;
            const reason = taskErrorDetail(context, raw);
            throw Object.assign(new Error(`Tác vụ mac2API ${task.id} ${state}.${reason ? " Lý do: " + reason : " Xem chi tiết trong mục Tác vụ của mac2API."}`), {
                code: "MEDIA_TASK_FAILED"
            });
        }
        if (result.status === "interrupted" || result.status === "needs_reconcile") {
            throw Object.assign(new Error("mac2API đã gián đoạn tác vụ " + task.id + ". Hãy đối chiếu trước khi gửi lại."), {
                code: "MEDIA_TASK_NEEDS_RECONCILE"
            });
        }
        if (result.status === "completed") {
            const candidate = Array.isArray(result.data) ? result.data[0]?.url : undefined;
            if (typeof candidate !== "string") throw new Error("Tác vụ hoàn tất nhưng không có đường dẫn đến tệp.");
            const url = new URL(candidate, task.base);
            if (url.origin !== new URL(task.base).origin || url.username || url.password || !url.pathname.includes("/v1/media/")) {
                throw new Error("Đường dẫn tệp nằm ngoài địa chỉ media mac2API đã cấu hình.");
            }
            return [
                {
                    mediaType: task.kind,
                    type: "url",
                    url: url.href
                }
            ];
        }
        if (result.status !== "queued" && result.status !== "processing") {
            throw new Error("Trạng thái tác vụ chưa rõ. Hãy kiểm tra tác vụ cũ trước khi gửi lại.");
        }
        await wait(signal);
    }
}
function taskSignal(context) {
    const timeout = AbortSignal.timeout(15 * 60 * 1000);
    return context.signal ? AbortSignal.any([
        timeout,
        context.signal
    ]) : timeout;
}
function requestUuid() {
    // This UUID deduplicates a request within an authenticated owner, rather than authenticating it.
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, token => {
        const digit = Math.floor(Math.random() * 16);
        return (token === "x" ? digit : (digit & 3) | 8).toString(16);
    });
}

function validRequestKey(value) {
    return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

async function discover(context, model, signal) {
    let capability;
    try {
        capability = await request(context, "/capabilities", signal);
    } catch (error) {
        if (error.status !== 404) throw error;
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
        const provider = capability.providers?.find(item => item?.id === selected.provider);
        const reason = {
            not_configured: "chưa cấu hình tài khoản",
            session_unverified: "chưa xác minh phiên đăng nhập",
            browser_unavailable: "chưa tìm thấy trình duyệt trên máy chạy mac2API",
            browser_disconnected: "mất kết nối trình duyệt"
        }[provider?.reason];
        throw new Error("Mô-đun mac2API chưa sẵn sàng" + (reason ? " vì " + reason : "") + ".");
    }
    return capability.features.cancel === true;
}

async function reconcile(context, pending, signal) {
    let receipt;
    try {
        receipt = await request(context, "/tasks/by-request", signal, undefined, pending.requestKey);
    } catch (error) {
        if (error.status !== 404) throw error;
        throw Object.assign(new Error("Chưa tìm thấy biên nhận. Hãy kiểm tra mục Tác vụ trước khi gửi lại."), {
            code: "MEDIA_TASK_NEEDS_RECONCILE"
        });
    }
    return saveReceipt(context, pending, receipt, signal);
}

async function saveReceipt(context, pending, receipt, signal) {
    const id = receipt.task_id ?? receipt.id;
    if (!validId(id) || (receipt.kind !== undefined && receipt.kind !== pending.kind)) {
        throw Object.assign(new Error("Chưa xác nhận được biên nhận của tác vụ."), {
            code: "MEDIA_TASK_NEEDS_RECONCILE"
        });
    }
    const task = { ...pending, id };
    await context.saveTaskData?.(task);
    return followTask(context, task, signal);
}

async function followTask(context, task, signal) {
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

async function submit(context, kind, body) {
    const signal = taskSignal(context);
    signal.throwIfAborted();
    const { base } = connection(context);
    const canCancel = await discover(context, body.model, signal);
    if (typeof context.saveTaskData !== "function") {
        throw new Error("MacFlow cần hỗ trợ lưu trạng thái tác vụ trước khi gửi yêu cầu mac2API.");
    }
    const pending = { kind, base, requestKey: requestUuid(), canCancel };
    await context.saveTaskData(pending);
    signal.throwIfAborted();
    const endpoint = kind === "image" ? "/images/tasks" : "/videos";
    let receipt;
    try {
        receipt = await request(context, endpoint, signal, body, pending.requestKey);
    } catch (error) {
        if (signal.aborted || (error.status >= 400 && error.status < 500)) throw error;
        return reconcile(context, pending, signal);
    }
    if (!validId(receipt.task_id ?? receipt.id)) return reconcile(context, pending, signal);
    return saveReceipt(context, pending, receipt, signal);
}
function checkPrompt(request) {
    if (!request.prompt?.trim()) throw new Error("Hãy nhập mô tả.");
    if (Object.keys(request.other ?? {}).length) throw new Error("Mô-đun này chưa hỗ trợ tham số tùy chỉnh.");
}
function requestedRatio(value) {
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
function resolutionPreset(value) {
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
function imageOptions(input) {
    const ratio = requestedRatio(input.ratio);
    const size = input.size?.trim();
    const quality = input.quality?.trim().toLowerCase();
    const preferred = resolutionPreset(size) || resolutionPreset(quality);
    const explicitSize = size && ![
        "auto",
        "default"
    ].includes(size.toLowerCase()) ? size : undefined;
    if (explicitSize && !resolutionPreset(explicitSize) && !/^\d{2,5}x\d{2,5}$/i.test(explicitSize)) {
        throw new Error("Hãy chọn mức ảnh 740, 1080, 1K, 2K hoặc 4K.");
    }
    if (quality && !resolutionPreset(quality) && ![
        "auto",
        "default",
        "standard",
        "low",
        "medium",
        "high",
        "hd"
    ].includes(quality)) {
        throw new Error("Mức chất lượng ảnh không hợp lệ.");
    }
    const outputSize = preferred || explicitSize;
    const extra = outputSize ? "Độ phân giải ảnh mong muốn: " + outputSize + "." : quality && [
        "low",
        "medium",
        "high",
        "hd"
    ].includes(quality) ? "Chất lượng ảnh mong muốn: " + quality + "." : "";
    return {
        ...ratio ? {
            aspect_ratio: ratio
        } : {},
        ...outputSize ? {
            size: outputSize
        } : {},
        ...extra ? {
            extra
        } : {}
    };
}
