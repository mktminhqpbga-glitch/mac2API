import assert from 'node:assert/strict';
import { test } from 'node:test';

const base = 'http://127.0.0.1:18610';
const request = {
  model: 'muse/video', prompt: 'Tạo video meme của một con chó', mode: 'text',
  ratio: '1:1', resolution: '720p', duration: 14, images: [], videos: [], audios: [],
};
const frame = { type: 'url', url: 'https://example.invalid/frame.png' };

function fixture() {
  const calls = [];
  const saved = [];
  const context = {
    config: { baseUrl: base, apiKey: 'fixture-key' },
    tool: { fetch: async (url, options) => {
      calls.push({ url, method: options.method, body: options.body && JSON.parse(options.body) });
      return options.method === 'POST'
        ? Response.json({ id: 'task_fixture', status: 'queued' })
        : Response.json({ status: 'completed', url: '/v1/media/fixture.mp4' });
    } },
    saveTaskData: async (data) => { saved.push(data); },
  };
  return { context, calls, saved };
}

for (const name of ['mac2ApiMuse', 'mac2Api']) {
  test(`${name} accepts default-off switches and creates a Muse video task`, {
    skip: !process.features.typescript && 'Node TypeScript stripping is required',
  }, async () => {
    const { default: adapter } = await import(`../adapters/${name}.ts`);
    const { context, calls, saved } = fixture();
    const assets = await adapter.generateVideo.call(context, { ...request, generateAudio: false, watermark: false });
    assert.deepEqual(calls, [
      { url: `${base}/v1/videos`, method: 'POST', body: {
        model: 'muse/video', prompt: request.prompt, aspect_ratio: '1:1', duration: 14, resolution: '720p',
      } },
      { url: `${base}/v1/videos/task_fixture`, method: 'GET', body: undefined },
    ]);
    assert.deepEqual(saved, [{ id: 'task_fixture', kind: 'video', base: `${base}/v1` }]);
    assert.deepEqual(assets, [{ mediaType: 'video', type: 'url', url: `${base}/v1/media/fixture.mp4` }]);

    await adapter.generateVideo.call(context, { ...request, mode: 'singleImage', firstFrame: frame, generateAudio: false });
    assert.equal(calls[2].body.image, frame.url);
  });

  test(`${name} reports which unsupported setting is enabled before submitting`, {
    skip: !process.features.typescript && 'Node TypeScript stripping is required',
  }, async () => {
    const { default: adapter } = await import(`../adapters/${name}.ts`);
    const { context, calls } = fixture();
    for (const [input, error] of [
      [{ lastFrame: frame }, /ảnh khung cuối/],
      [{ videos: [frame] }, /âm thanh hoặc video làm tệp tham chiếu/],
      [{ audios: [frame] }, /âm thanh hoặc video làm tệp tham chiếu/],
      [{ generateAudio: true }, /bật tùy chọn Tạo âm thanh/],
      [{ watermark: true }, /bật tùy chọn Hình mờ/],
    ]) {
      await assert.rejects(adapter.generateVideo.call(context, { ...request, ...input }), error);
    }
    assert.deepEqual(calls, []);
  });
}
