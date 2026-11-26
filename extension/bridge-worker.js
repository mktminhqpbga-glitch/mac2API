importScripts('bridge-site.js');

const BRIDGE_CONFIG = 'mac2apiChromeBridge';
const TASK_PREFIX = 'mac2apiChromeTask:';
const WAKE_ALARM = 'mac2apiChromeBridge';
const loops = new Map();
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function localBase(value) {
  let url;
  try { url = new URL(value); } catch { /* Display the same guidance for a malformed URL. */ }
  if (!url || !['http:', 'https:'].includes(url.protocol) ||
      !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname) ||
      url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('Hãy nhập địa chỉ localhost của mac2API, ví dụ http://127.0.0.1:18610.');
  }
  return url.origin;
}

async function bridgeFetch(base, token, path, {method = 'GET', body, timeout = 18000} = {}) {
  return fetch(base + path, {
    method, body, redirect: 'error', credentials: 'omit', cache: 'no-store',
    headers: {Authorization: 'Bearer ' + token, ...(body ? {'Content-Type': 'application/json'} : {})},
    signal: AbortSignal.timeout(timeout),
  });
}

async function execute(tabId, func, args) {
  const [frame] = await chrome.scripting.executeScript({target: {tabId}, func, args});
  return frame?.result;
}

async function connect(message) {
  const site = bridgeSites[message.provider];
  if (!site) throw new Error('Không tìm thấy dịch vụ đã chọn.');
  const base = localBase(message.base);
  const tab = await verifyTab(message.tabId, site);
  const stored = await chrome.storage.local.get(BRIDGE_CONFIG);
  const config = stored[BRIDGE_CONFIG] || {profile: crypto.randomUUID().replaceAll('-', ''), providers: {}};
  const response = await bridgeFetch(base, message.key, '/admin/web-providers/' + message.provider + '/extension', {
    method: 'POST', body: JSON.stringify({profile: config.profile, label: message.label}),
  }).catch(() => {
    throw new Error('Tiện ích chưa kết nối được mac2API. Hãy kiểm tra địa chỉ localhost và dịch vụ đang chạy.');
  });
  if (response.status === 404) {
    throw new Error('Máy chủ mac2API chưa có đường dẫn kết nối trực tiếp. Hãy cập nhật và khởi động lại mac2API rồi thử lại.');
  }
  const data = await response.json().catch(() => ({}));
  if (response.status !== 201 || !data.token) {
    throw new Error(typeof data.detail === 'string' ? data.detail : 'Không liên kết được tiện ích (HTTP ' + response.status + ').');
  }
  config.providers[message.provider] = {base, token: data.token, windowId: tab.windowId, label: message.label};
  await chrome.storage.local.set({[BRIDGE_CONFIG]: config});
  await wake();
}

async function verifyTab(tabId, site) {
  const deadline = Date.now() + 6000;
  let status;
  do {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab?.url || tab.incognito || new URL(tab.url).origin !== site.origin) {
      throw new Error('Mở đúng tab AI đã đăng nhập trong cửa sổ Chrome thường.');
    }
    if (tab.status === 'complete') {
      status = await execute(tab.id, bridgeSiteAction, [site, 'verify']).catch(() => {
        throw new Error('Chrome chưa cho tiện ích đọc tab này. Kiểm tra Quyền truy cập trang web trong chrome://extensions.');
      });
      if (status?.signedIn && status.composerReady) return tab;
    }
    await sleep(250);
  } while (Date.now() < deadline);
  if (!status?.composerReady) {
    throw new Error('Không tìm thấy ô chat trên tab đang mở. Hãy tải lại trang AI rồi bấm tên dịch vụ để thử lại.');
  }
  throw new Error('Đã thấy ô chat nhưng chưa xác minh được đăng nhập. Hãy kiểm tra hồ sơ Chrome và mở một cuộc trò chuyện mới.');
}

function jobAction(connection, id, action, report) {
  return bridgeFetch(connection.base, connection.token, '/browser-bridge/jobs/' + id + '/' + action, {
    method: 'POST', body: report && JSON.stringify(report), timeout: action === 'finish' ? 7000 : 4000,
  });
}

async function allowed(connection, job, action) {
  const response = await jobAction(connection, job.id, action);
  return response.ok && (await response.json()).ok === true;
}

function imageSourceAllowed(source, site) {
  const url = new URL(source);
  if (url.protocol === 'blob:') return url.origin === site.origin;
  if (url.protocol === 'data:') return /^data:image\/(?:png|jpeg|webp)[;,]/i.test(source);
  const domains = ['chatgpt.com', 'oaiusercontent.com', 'oaistatic.com', 'gemini.google.com',
    'googleusercontent.com', 'gstatic.com', 'storage.googleapis.com'];
  return url.protocol === 'https:' && !url.username && !url.password && !url.port &&
    domains.some(domain => url.hostname === domain || url.hostname.endsWith('.' + domain));
}

async function generate(connection, site, tabId, job, deadline) {
  let nextHeartbeat = 0;
  const stillActive = async () => {
    if (Date.now() >= deadline) return false;
    if (Date.now() < nextHeartbeat) return true;
    nextHeartbeat = Date.now() + 5000;
    return allowed(connection, job, 'heartbeat');
  };
  let signedIn = false;
  let opened = false;
  const preparedBy = Math.min(deadline, Date.now() + 25000);
  while (Date.now() < preparedBy && await stillActive()) {
    const tab = await chrome.tabs.get(tabId);
    const origin = new URL(tab.url).origin;
    if (tab.status === 'complete' && ['https://accounts.google.com', 'https://auth.openai.com'].includes(origin)) {
      return {error: 'session'};
    }
    if (tab.status === 'complete' && origin === site.origin) {
      const prepared = await execute(tabId, bridgeSiteAction, [site, 'prepare', job.prompt]);
      if (prepared?.dirty) return {error: 'composer'};
      opened ||= Boolean(prepared && !prepared.wrongSite);
      signedIn ||= Boolean(opened && prepared?.signedIn !== false);
      if (prepared?.ready) {
        if (!await allowed(connection, job, 'permit')) return {error: 'interrupted'};
        if (Date.now() >= deadline) return {error: 'timeout'};
        const submitted = await execute(tabId, bridgeSiteAction, [site, 'send', job.prompt]);
        if (!submitted?.sent) return {error: 'composer'};
        return waitForReply(site, tabId, job, deadline, stillActive);
      }
    }
    await sleep(400);
  }
  if (Date.now() >= deadline) return {error: 'timeout'};
  return {error: !opened ? 'page_unavailable' : signedIn ? 'composer' : 'session'};
}

async function waitForReply(site, tabId, job, deadline, stillActive) {
  let sawStop = false;
  let last;
  let unchangedAt = Date.now();
  while (await stillActive()) {
    const state = await execute(tabId, bridgeSiteAction, [site, 'read']);
    if (!state || state.wrongSite) return {error: 'interrupted'};
    sawStop ||= state.busy;
    const current = job.image ? state.image || state.text : state.text;
    if (current !== last || state.busy) {
      last = current;
      unchangedAt = Date.now();
    }
    if (current && !state.busy && (state.complete || sawStop)) {
      const missingImage = job.image && !state.image;
      if (Date.now() - unchangedAt >= (missingImage ? 5000 : 1250)) {
        if (!job.image) return {text: state.text};
        if (missingImage) return {error: 'media_missing'};
        if (!imageSourceAllowed(state.image, site)) return {error: 'media_unreadable'};
        const timeout = Math.min(12000, Math.max(1, deadline - Date.now()));
        const image = await execute(tabId, readBridgeImage, [state.image, site.origin, timeout]).catch(() => null);
        return image ? {image} : {error: 'media_unreadable'};
      }
    }
    await sleep(400);
  }
  return {error: 'timeout'};
}

async function closeTaskTab(tabId) {
  try {
    await chrome.tabs.remove(tabId);
    return true;
  } catch {
    return chrome.tabs.get(tabId).then(() => false, () => true);
  }
}

async function finish(connection, job, report) {
  // A repeated completion is safe. The broker applies a report at most once.
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const response = await jobAction(connection, job.id, 'finish', report);
      if (response.status < 500) return;
    } catch {
      if (!attempt) await sleep(250);
    }
  }
}

async function runJob(provider, connection, job) {
  const site = bridgeSites[provider];
  const deadline = Date.now() + job.remaining_ms;
  const storageKey = TASK_PREFIX + provider;
  let tabId;
  let report = {error: 'interrupted'};
  try {
    const windows = (await chrome.windows.getAll({windowTypes: ['normal']})).filter(window => !window.incognito);
    const window = windows.find(window => window.id === connection.windowId) || windows[0];
    if (!window || !await allowed(connection, job, 'heartbeat')) return;
    const tab = await chrome.tabs.create({windowId: window.id, url: 'about:blank', active: false});
    tabId = tab.id;
    await chrome.storage.session.set({[storageKey]: {tabId, id: job.id}});
    await chrome.tabs.update(tabId, {url: site.url});
    report = await generate(connection, site, tabId, job, deadline);
  } catch (error) {
    console.warn('mac2API Chrome job interrupted', error.name);
  } finally {
    if (tabId !== undefined) {
      if (await closeTaskTab(tabId)) {
        await chrome.storage.session.remove(storageKey);
      } else {
        report = {error: 'close_failed'};
      }
    }
    await finish(connection, job, report);
  }
}

async function poll(provider, connection) {
  const storageKey = TASK_PREFIX + provider;
  while (true) {
    const stored = await chrome.storage.local.get(BRIDGE_CONFIG);
    if (stored[BRIDGE_CONFIG]?.providers[provider]?.token !== connection.token) return;
    if (!(await chrome.windows.getAll({windowTypes: ['normal']})).some(window => !window.incognito)) return;
    const stale = (await chrome.storage.session.get(storageKey))[storageKey];
    if (stale) {
      if (!await closeTaskTab(stale.tabId)) return;
      await chrome.storage.session.remove(storageKey);
      await finish(connection, stale, {error: 'interrupted'});
    }
    try {
      const response = await bridgeFetch(connection.base, connection.token, '/browser-bridge/next');
      if (response.status === 401 || response.status === 403) return;
      if (response.status === 200) {
        await runJob(provider, connection, await response.json());
      } else if (response.status !== 204) {
        await sleep(3000);
      }
    } catch {
      await sleep(3000);
    }
  }
}

async function wake() {
  const stored = await chrome.storage.local.get(BRIDGE_CONFIG);
  const providers = stored[BRIDGE_CONFIG]?.providers || {};
  if (!Object.keys(providers).length) return;
  if (!await chrome.alarms.get(WAKE_ALARM)) {
    await chrome.alarms.create(WAKE_ALARM, {periodInMinutes: 0.5});
  }
  for (const [provider, connection] of Object.entries(providers)) {
    if (loops.get(provider) === connection.token) continue;
    if (loops.has(provider)) continue;
    loops.set(provider, connection.token);
    poll(provider, connection).catch(error => {
      console.warn('mac2API Chrome connection stopped', error.name);
    }).finally(async () => {
      loops.delete(provider);
      const config = (await chrome.storage.local.get(BRIDGE_CONFIG))[BRIDGE_CONFIG];
      if (config?.providers[provider]?.token && config.providers[provider].token !== connection.token) await wake();
    });
  }
}

let pairing = Promise.resolve();
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message?.type !== 'mac2apiChromeConnect' || sender.id !== chrome.runtime.id) return false;
  pairing = pairing.then(() => connect(message));
  pairing.then(() => respond({ok: true}), error => respond({error: error.message}));
  pairing = pairing.catch(() => {});
  return true;
});
chrome.runtime.onStartup.addListener(wake);
chrome.runtime.onInstalled.addListener(wake);
chrome.windows.onCreated.addListener(wake);
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === WAKE_ALARM) void wake();
});
void wake();
