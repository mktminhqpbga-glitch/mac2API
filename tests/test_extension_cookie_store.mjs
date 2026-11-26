import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../extension/popup.js', import.meta.url), 'utf8');
const cookie = (value, extra = {}) => ({name: 'hatch_sess', value, domain: '.muse.ai', path: '/', httpOnly: true, ...extra});

function fixture({incognito = false, tab = true, stores, cookies, locale = null, url = "https://muse.ai/"} = {}) {
  const active = tab ? {id: 27, incognito, url} : undefined;
  const storeId = incognito ? 'private-7' : 'regular-3';
  const calls = {reads: [], uploads: [], messages: []};
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {value: '', innerHTML: '', addEventListener() {}});
    return elements.get(id);
  };
  element('base').value = 'https://test.invalid';
  element('key').value = 'fixture-key';
  const context = vm.createContext({
    document: {getElementById: element, querySelectorAll: () => [], documentElement: {}, title: ''},
    localStorage: {getItem: () => locale},
    URL,
    crypto: {getRandomValues: bytes => bytes.fill(0xab)},
    chrome: {
      runtime: {
        getManifest: () => ({version: '1.0.8'}),
        sendMessage: async message => {calls.messages.push(message); return {ok: true};},
      },
      storage: {local: {get: async () => ({}), set: async () => {}}},
      tabs: {query: async () => active ? [active] : []},
      scripting: {executeScript: async () => [{result: true}]},
      cookies: {
        getAllCookieStores: async () => stores ?? [
          {id: 'wrong-default', tabIds: [99]}, {id: storeId, tabIds: [27]},
        ],
        getAll: async query => {
          calls.reads.push(query);
          assert.equal(query.storeId, storeId, 'must use the active tab store, including regular mode');
          assert.equal(query.url, 'https://muse.ai/api/session');
          assert.equal(query.domain, undefined, 'must not collect every Muse subdomain');
          return cookies ?? [cookie(incognito ? 'private-valid' : 'regular-valid')];
        },
      },
    },
    fetch: async (url, request) => {
      calls.uploads.push({url, body: JSON.parse(request.body)});
      if (url.endsWith('/link')) {
        return {status: 200, ok: true, json: async () => ({ok: true, provider: url.split('/').at(-2)})};
      }
      return {status: 200, ok: true, text: async () => JSON.stringify({added: [{id: 'fixture', label: 'fixture'}]})};
    },
  });
  vm.runInContext(source, context);
  return {calls, element, run: code => vm.runInContext(code, context)};
}

let checks = 0;
for (const incognito of [false, true]) {
  const f = fixture({incognito});
  const result = await f.run('grabCookies()');
  assert.equal(result.cookies.hatch_sess, incognito ? 'private-valid' : 'regular-valid');
  assert.equal(f.calls.reads.length, 1);
  checks++;
}
for (const options of [{tab: false}, {stores: [{id: 'wrong-default', tabIds: [99]}]}]) {
  const f = fixture(options);
  await assert.rejects(f.run('grabCookies()'), /Không xác định được kho cookie/);
  assert.equal(f.calls.reads.length, 0, 'unknown store must fail closed');
  checks++;
}
{
  const f = fixture({cookies: [cookie('path-specific', {path:'/api', expirationDate:2000000000}), cookie('root-lower-priority', {expirationDate:2100000000})]});
  const result = await f.run('grabCookies()');
  assert.equal(result.cookies.hatch_sess, 'path-specific');
  assert.equal(result.expires.hatch_sess, 2000000000);
  checks++;
}
{
  const f = fixture({cookies: [cookie('earlier-session'), cookie('later-cookie', {expirationDate:2100000000})]});
  const result = await f.run('grabCookies()');
  assert.equal(result.cookies.hatch_sess, 'earlier-session');
  assert.equal(result.expires.hatch_sess, undefined, 'do not copy expiry from a discarded duplicate');
  checks++;
}
{
  const f = fixture({incognito: true, cookies: [cookie('httpOnly%3Afixture%2Bvalue', {expirationDate:2000000000})]});
  await f.run('run()');
  assert.equal(f.calls.uploads.length, 1);
  assert.equal(f.calls.uploads[0].body.cookies.hatch_sess, 'httpOnly%3Afixture%2Bvalue');
  assert.equal(f.calls.uploads[0].body.expires.hatch_sess, 2000000000);
  assert.match(f.element('log').innerHTML, /Nguồn cookie: cửa sổ ẩn danh\/InPrivate hiện tại/);
  assert.match(f.element('log').innerHTML, /kiểm tra tài khoản trong trang quản trị/);
  checks++;
}
for (const options of [{tab: false}, {cookies: [cookie('')]}]) {
  const f = fixture(options);
  await f.run('run()');
  assert.equal(f.calls.uploads.length, 0);
  assert.equal(f.element('go').disabled, false);
  checks++;
}
{
  const f = fixture({incognito: true, locale: 'en'});
  await f.run('run()');
  assert.match(f.element('log').innerHTML, /current Incognito\/InPrivate window/);
  checks++;
}
console.log(`PASS: ${checks} extension cookie-store, URL scope, duplicate precedence and import checks; no live cookies/network`);

for (const [provider, origin, prefix] of [['chatgpt', 'https://chatgpt.com', 'chatgpt'], ['gemini-web', 'https://gemini.google.com', 'gemini']]) {
  const f = fixture({url: origin + '/'});
  f.element('base').value = `https://test.invalid/${prefix}/v1`;
  await f.run(`linkWebProvider('${provider}')`);
  assert.equal(f.calls.reads.length, 0, 'web links never read cookies');
  assert.equal(f.calls.uploads.length, 1);
  assert.equal(f.calls.uploads[0].url, `https://test.invalid/admin/web-providers/${provider}/link`);
  assert.equal(f.calls.uploads[0].body.marker, 'ab'.repeat(16));
  assert.equal(f.calls.uploads[0].body.extension_version, '1.0.8');
  assert.match(f.element('log').innerHTML, /Đã xác minh tab/);
}
for (const url of ['https://chatgpt.com.example/', 'https://gemini.google.com/']) {
  const f = fixture({url});
  await f.run("linkWebProvider('chatgpt')");
  await f.run('run()');
  assert.equal(f.calls.uploads.length, 0);
  assert.equal(f.calls.reads.length, 0);
}
{
  const f = fixture({url: 'https://chatgpt.com/'});
  f.element('base').value = '';
  f.element('directChrome').checked = true;
  await f.run("linkWebProvider('chatgpt')");
  assert.equal(f.calls.messages.length, 1);
  assert.equal(f.calls.messages[0].base, 'http://127.0.0.1:18610');
  assert.equal(f.calls.messages[0].provider, 'chatgpt');
  assert.equal(f.calls.uploads.length, 0);
  assert.match(f.element('log').innerHTML, /Đã liên kết ChatGPT/);
}
console.log('PASS: exact provider origin, matching route and no web cookie import');
