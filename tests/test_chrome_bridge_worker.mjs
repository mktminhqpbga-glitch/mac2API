import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const root = new URL('../extension/', import.meta.url);
const worker = fs.readFileSync(new URL('bridge-worker.js', root), 'utf8');

async function runFixture(permit, verifyStates, {provider = 'gemini-web', pair = false} = {}) {
  const tabUrl = provider === 'chatgpt' ? 'https://chatgpt.com/' : 'https://gemini.google.com/app';
  const events = [];
  let now = 0;
  const listener = {addListener() {}};
  const context = vm.createContext({
    URL, AbortSignal, console, crypto,
    Date: {now: () => now},
    setTimeout(callback, delay) {now += delay; queueMicrotask(callback);},
    importScripts(name) {
      vm.runInContext(fs.readFileSync(new URL(name, root), 'utf8'), context);
    },
    fetch: async (address, options) => {
      const action = new URL(address).pathname.split('/').at(-1);
      events.push([action, options.body && JSON.parse(options.body)]);
      if (action === 'extension') {
        assert.equal(address, 'http://127.0.0.1:18610/admin/web-providers/chatgpt/extension');
        assert.equal(options.headers.Authorization, 'Bearer fixture-admin');
        return {status: 201, json: async () => ({token: 'scoped-bridge-key'})};
      }
      assert.equal(options.headers.Authorization, 'Bearer scoped-bridge-key');
      return {status: 200, ok: true, json: async () => ({ok: action !== 'permit' || permit})};
    },
    chrome: {
      storage: {
        local: {get: async () => ({}), set: async () => events.push(['paired'])},
        session: {set: async () => events.push(['track']), remove: async () => events.push(['untrack'])},
      },
      runtime: {onMessage: listener, onStartup: listener, onInstalled: listener},
      alarms: {onAlarm: listener},
      windows: {
        onCreated: listener,
        getAll: async () => [{id: 2, incognito: true}, {id: 3, incognito: false}, {id: 4, incognito: false}],
      },
      tabs: {
        create: async options => {
          events.push(['open', options.windowId, options.active]);
          return {id: 101};
        },
        update: async (id, options) => {events.push(['navigate', id, options.url]);},
        get: async id => {
          if (id === 27 && verifyStates) return {id, status: 'loading', url: tabUrl};
          assert.equal(id, 101);
          return {status: 'complete', url: 'https://gemini.google.com/app'};
        },
        remove: async id => events.push(['close', id]),
      },
      scripting: {
        executeScript: async ({target, args, injectImmediately}) => {
          if (target.tabId === 27 && verifyStates) {
            assert.equal(args[1], 'verify');
            assert.equal(injectImmediately, true);
            events.push(['probe']);
            const outcome = verifyStates.shift() || {signedIn: true, composerReady: true};
            if (outcome instanceof Error) throw outcome;
            return [{result: outcome}];
          }
          assert.equal(target.tabId, 101);
          const action = args[1];
          if (action === 'prepare' || action === 'send') {
            events.push([action, args[2]]);
          }
          const result = {
            prepare: {ready: true}, send: {sent: true},
            read: {busy: false, complete: true, text: 'Kết quả Gemini'},
          };
          return [{result: result[action]}];
        },
      },
    },
  });
  vm.runInContext(worker, context);
  if (verifyStates) {
    if (pair) {
      await vm.runInContext(`connect({provider: ${JSON.stringify(provider)}, tabId: 27,
        base: 'http://127.0.0.1:18610', key: 'fixture-admin', label: 'Fixture'})`, context);
    } else {
      const tab = await vm.runInContext(`verifyTab(27, bridgeSites[${JSON.stringify(provider)}])`, context);
      assert.equal(tab.id, 27);
    }
    return events;
  }
  await vm.runInContext(`runJob('gemini-web', {base: 'http://127.0.0.1:18610', token: 'scoped-bridge-key', windowId: 2},
    {id: 'test', image: false, prompt: 'Prompt mới', remaining_ms: 15000})`, context);
  assert.deepEqual(events.find(event => event[0] === 'open'), ['open', 3, false]);
  assert.deepEqual(events.find(event => event[0] === 'navigate'), ['navigate', 101, 'https://gemini.google.com/app']);
  assert.ok(events.findIndex(event => event[0] === 'close') < events.findIndex(event => event[0] === 'finish'));
  return events;
}

test('Chrome bridge sends once in its own tab and closes it before returning the reply', async () => {
  const events = await runFixture(true);
  assert.deepEqual(events.filter(event => event[0] === 'send'), [['send', 'Prompt mới']]);
  assert.deepEqual(events.at(-1), ['finish', {text: 'Kết quả Gemini'}]);
});

test('Chrome bridge closes the task tab when permission was revoked before Send', async () => {
  const events = await runFixture(false);
  assert.ok(!events.some(event => event[0] === 'send'));
  assert.deepEqual(events.at(-1), ['finish', {error: 'interrupted'}]);
});

test('Gemini linking retries detached frames and verifies an editable composer before the tab finishes loading', async () => {
  const events = await runFixture(true, [
    new Error('The frame was removed.'),
    {signedIn: true, composerReady: false},
    {signedIn: false, composerReady: true},
  ]);
  assert.deepEqual(events, [['probe'], ['probe'], ['probe'], ['probe']]);
});

test('ChatGPT can link a verified profile while its composer is unavailable', async () => {
  const events = await runFixture(true, [{signedIn: true, composerReady: false}], {provider: 'chatgpt', pair: true});
  assert.deepEqual(events.map(event => event[0]), ['probe', 'extension', 'paired']);
});

test('ChatGPT linking rejects a signed-out tab even with a usable composer', async () => {
  const states = Array.from({length: 50}, () => ({signedIn: false, composerReady: true}));
  await assert.rejects(runFixture(true, states, {provider: 'chatgpt', pair: true}), /Chưa xác minh được đăng nhập/);
});

test('ChatGPT reports the sign-in prompt when it is present', async () => {
  const states = Array.from({length: 50}, () => ({signedIn: false, composerReady: true, signedOut: true}));
  await assert.rejects(runFixture(true, states, {provider: 'chatgpt', pair: true}), /đang hiện nút đăng nhập/);
});
