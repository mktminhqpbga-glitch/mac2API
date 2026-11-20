import assert from "node:assert/strict";
import { test } from "node:test";

const base = "http://127.0.0.1:18610/v1";
const adapters = {
    mac2ApiMuse: "muse/image",
    mac2ApiChatGpt: "chatgpt/image",
    mac2ApiGemini: "gemini-web/image",
};
const requestKey = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const receipt = { task_id: "task_fixture", id: "task_fixture", kind: "image", status: "queued", progress: 0 };

function fixture(handler, options = {}) {
    const traffic = [];
    const saved = [];
    return {
        traffic, saved, context: {
            config: { baseUrl: base, apiKey: "fixture-only" },
            signal: options.signal,
            async saveTaskData(value) {
                saved.push(value);
            },
            tool: {
                async fetch(url, init) {
                    assert.equal(init.headers.Authorization, "Bearer fixture-only");
                    assert.equal(init.redirect, "error");
                    assert.ok(url.startsWith(base));
                    const path = url.slice(base.length);
                    traffic.push([init.method, path]);
                    return handler(path, init, saved);
                }
            }
        }
    };
}

function discovery(model) {
    return Response.json({ contractVersion: 1, features: {
        idempotency: true, lookupByRequestKey: true, cancel: true
    }, providers: [], models: [{ id: model, ready: true }] });
}

const imageRequest = model => ({ model, prompt: "Tạo ảnh thử", size: "4K", ratio: "3:4" });
const result = () => Response.json({ status: "completed", data: [{ url: "/v1/media/fixture.png?access=demo" }] });

for (const [name, model] of Object.entries(adapters)) {
    const { default: adapter } = await import(`../adapters/2.0.7/${name}.ts`);

    test(`${name} persists the UUID before POST and recovers a lost receipt via GET only`, async () => {
        const f = fixture((path, init, saved) => {
            if (path === "/capabilities") return discovery(model);
            if (path === "/images/tasks") {
                assert.equal(saved.length, 1);
                assert.match(saved[0].requestKey, requestKey);
                assert.equal(init.headers["Idempotency-Key"], saved[0].requestKey);
                assert.equal(JSON.parse(init.body).aspect_ratio, "3:4");
                throw new Error("Fixture lost the POST receipt");
            }
            if (path === "/tasks/by-request") {
                assert.equal(init.method, "GET");
                assert.equal(init.headers["Idempotency-Key"], saved[0].requestKey);
                return Response.json(receipt);
            }
            assert.equal(saved[1].id, receipt.id);
            return result();
        });
        const assets = await adapter.generateImage.call(f.context, imageRequest(model));
        assert.deepEqual(assets, [{ mediaType: "image", type: "url", url: base + "/media/fixture.png?access=demo" }]);
        assert.deepEqual(f.traffic, [
            ["GET", "/capabilities"], ["POST", "/images/tasks"], ["GET", "/tasks/by-request"], ["GET", "/images/tasks/task_fixture"]
        ]);

        // A restored pending record still uses the original UUID, with no POST.
        f.traffic.length = 0;
        await adapter.resumeTask.call(f.context, f.saved[0]);
        assert.deepEqual(f.traffic, [["GET", "/tasks/by-request"], ["GET", "/images/tasks/task_fixture"]]);
    });

    test(`${name} treats 429 as a rejection and interrupted as unresolved`, async () => {
        const rejected = fixture((path) => path === "/capabilities" ? discovery(model)
            : Response.json({ error: { code: "QUEUE_FULL" } }, { status: 429, headers: { "Retry-After": "2" } }));
        await assert.rejects(adapter.generateImage.call(rejected.context, imageRequest(model)),
                             error => error.status === 429 && /2 giây/.test(error.message));
        assert.deepEqual(rejected.traffic, [["GET", "/capabilities"], ["POST", "/images/tasks"]]);

        const interrupted = fixture(() => Response.json({ status: "interrupted", needs_reconcile: true }));
        await assert.rejects(adapter.resumeTask.call(interrupted.context, { kind: "image", base, id: "task_fixture" }),
                             error => error.code === "MEDIA_TASK_NEEDS_RECONCILE");
        assert.deepEqual(interrupted.traffic, [["GET", "/images/tasks/task_fixture"]]);
    });

    test(`${name} leaves a missing receipt unresolved and supports the legacy model list`, async () => {
        const absent = fixture((path) => {
            if (path === "/capabilities") return discovery(model);
            if (path === "/images/tasks") return Response.json({}, { status: 503 });
            return Response.json({}, { status: 404 });
        });
        await assert.rejects(adapter.generateImage.call(absent.context, imageRequest(model)),
                             error => error.code === "MEDIA_TASK_NEEDS_RECONCILE");
        assert.equal(absent.traffic.filter(([method]) => method === "POST").length, 1);

        const legacy = fixture(path => {
            if (path === "/capabilities") return Response.json({}, { status: 404 });
            if (path === "/models") return Response.json({ data: [{ id: model }] });
            if (path === "/images/tasks") return Response.json(receipt);
            return result();
        });
        assert.equal((await adapter.generateImage.call(legacy.context, imageRequest(model))).length, 1);
        assert.deepEqual(legacy.traffic.slice(0, 2), [["GET", "/capabilities"], ["GET", "/models"]]);
    });

    test(`${name} requests upstream cancellation on host abort only when enabled`, async () => {
        const controller = new AbortController();
        const stop = new Error("cancelled by fixture");
        const f = fixture((path, init) => {
            if (path === "/capabilities") return discovery(model);
            if (path === "/images/tasks") return Response.json(receipt);
            if (path.endsWith("/cancel")) {
                assert.equal(init.method, "POST");
                assert.equal(init.signal.aborted, false);
                return Response.json({ code: "CANNOT_CANCEL_RUNNING", status: "processing" }, { status: 409 });
            }
            controller.abort(stop);
            return Response.json({ status: "processing" });
        }, { signal: controller.signal });
        await assert.rejects(adapter.generateImage.call(f.context, imageRequest(model)), error => error === stop);
        assert.deepEqual(f.traffic.at(-1), ["POST", "/images/tasks/task_fixture/cancel"]);
    });
}

test("Muse exposes the pre-send missing-reference error and stops", async () => {
    const { default: adapter } = await import("../adapters/2.0.7/mac2ApiMuse.ts");
    const model = "muse/image";
    const f = fixture(path => {
        if (path === "/capabilities") return discovery(model);
        return Response.json({ error: { code: "MUSE_REFERENCES_MISSING",
            message: "Hãy kiểm tra thứ tự và đính kèm đủ tệp." } }, { status: 400 });
    });
    await assert.rejects(adapter.generateImage.call(f.context, imageRequest(model)),
                         error => error.status === 400 && error.code === "MUSE_REFERENCES_MISSING"
                             && error.message.includes("đính kèm đủ tệp"));
    assert.deepEqual(f.traffic, [["GET", "/capabilities"], ["POST", "/images/tasks"]]);
});

test("Muse submits all twenty references when advertised and rejects excess before POST", async () => {
    const { default: adapter } = await import("../adapters/2.0.7/mac2ApiMuse.ts");
    const model = "muse/image";
    const images = Array.from({ length: 20 }, (_, index) => ({
        type: "url", url: `https://example.invalid/reference-${index}.png`
    }));
    for (const maxReferences of [10, 20]) {
        const f = fixture((path, init) => {
            if (path === "/capabilities") return Response.json({ contractVersion: 1,
                features: { idempotency: true, lookupByRequestKey: true },
                models: [{ id: model, ready: true, maxReferences }] });
            if (path === "/images/tasks") {
                assert.deepEqual(JSON.parse(init.body).images, images.map(image => image.url));
                return Response.json(receipt);
            }
            return result();
        });
        const request = { ...imageRequest(model), images };
        if (maxReferences === 10) {
            await assert.rejects(adapter.generateImage.call(f.context, request), /tối đa 10 ảnh/);
            assert.deepEqual(f.traffic, [["GET", "/capabilities"]]);
            assert.equal(f.saved.length, 0);
        } else {
            assert.equal((await adapter.generateImage.call(f.context, request)).length, 1);
            assert.equal(f.traffic.filter(([method]) => method === "POST").length, 1);
        }
    }
});

test("Muse forwards preset and unrestricted durations with the selected ratio and resolution", async () => {
    const { default: adapter } = await import("../adapters/2.0.7/mac2ApiMuse.ts");
    const request = { model: "muse/video", prompt: "Video thử", mode: "text", ratio: "9:16", resolution: "1080" };
    for (const [duration, customDuration, expected] of [
        [45, "", 45], [73, undefined, 73], [5, "125", 125], [10, 120, 120], [undefined, "", undefined],
    ]) {
        const f = fixture((path, init) => {
            if (path === "/capabilities") return discovery("muse/video");
            if (path === "/videos") {
                const payload = JSON.parse(init.body);
                assert.equal(payload.aspect_ratio, "9:16");
                assert.equal(payload.resolution, "1080p");
                assert.equal(payload.duration, expected);
                return Response.json({ ...receipt, kind: "video" });
            }
            return Response.json({ status: "completed", url: "/v1/media/fixture.mp4" });
        });
        const output = await adapter.generateVideo.call(f.context, { ...request, duration,
            other: { mac2ApiVideoDuration: customDuration } });
        assert.equal(output[0].mediaType, "video");
        assert.equal(f.traffic.filter(([method]) => method === "POST").length, 1);
    }
    const invalid = fixture(() => assert.fail("Invalid video duration tried to contact mac2API"));
    for (const customDuration of [0, -1, 2.5, "10 giây", Infinity, Number.MAX_SAFE_INTEGER + 1]) {
        await assert.rejects(adapter.generateVideo.call(invalid.context, { ...request,
            other: { mac2ApiVideoDuration: customDuration } }), /số nguyên dương/);
    }
    assert.equal(invalid.saved.length, 0);
});
