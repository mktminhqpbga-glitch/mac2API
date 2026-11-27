/* Muse2API Cookie 导入 —— 读取 muse.ai 的 cookie 并 POST 到 muse2api 服务。
 *
 * 关键点：用 chrome.cookies 而不是 document.cookie。
 * muse.ai 的关键会话 cookie（hatch_sess / hatch_vml）带 httpOnly，
 * 网页 JS 读不到，只有浏览器扩展的 cookies 接口能拿到。
 * hatch_gw 是可重新签发的路由缓存，不应阻止账号导入。
 */

const $ = (id) => document.getElementById(id);
const STORE = 'muse2api_ext_cfg';
const BRIDGE_STORE = 'mac2apiChromeBridge';
const LOCAL_BASE = 'http://127.0.0.1:18610';

const ESSENTIAL = ['hatch_sess'];

/* ================= 界面语言 / UI language =================
 * 中文是源语言；英文通过下面的词典替换。
 * 新增中文文案时，请在 I18N_EN（整句，可含 {name} 占位符）或 I18N_EN_PATTERNS（服务端返回的含变量文案）里补上英文。 */
const LANG_STORE = 'muse2api_lang';
const LANG = (() => {
  try { const v = localStorage.getItem(LANG_STORE); if (v === 'vi' || v === 'zh' || v === 'en') return v; } catch (e) {}
  return 'vi';
})();
function setLang(v) {
  try { localStorage.setItem(LANG_STORE, v); } catch (e) {}
  location.reload();
}

/* 整句：中文 → 英文 */
const I18N_EN = {
  '检查连接': 'Check connection',
  '正在检查 mac2API…': 'Checking mac2API…',
  '服务地址无效。请从 mac2API 管理页面复制地址。': 'Invalid service URL. Copy it from the mac2API dashboard.',
  '无法连接 mac2API。请检查地址和应用是否已启动。': 'Cannot reach mac2API. Check the address and whether the app is running.',
  '管理密钥无效。请从 mac2API 管理页面复制正确的密钥。': 'Invalid admin key. Copy the correct key from the mac2API dashboard.',
  '此地址未返回有效的 mac2API 状态。请检查服务地址。': 'This address did not return mac2API status. Check the service URL.',
  'mac2API 地址和管理密钥有效。打开已登录的 AI 标签页后可检查该账号。': 'The mac2API address and admin key are valid. Open a signed-in AI tab to check the account.',
  'mac2API 连接正常，找到 Muse 登录信息。导入后请在 mac2API 检查账号。': 'mac2API is reachable and Muse sign-in data is present. Test the account in mac2API after import.',
  'mac2API 连接正常，已验证 {site} 的登录标签页。': 'mac2API is reachable and the signed-in {site} tab was verified.',
  'mac2API 未确认保存 Muse 账号。请在管理页面检查后再重试。': 'mac2API did not confirm the Muse account was saved. Check the dashboard before retrying.',
  '高级连接设置': 'Advanced connection settings',
  '使用扩展直接运行 Gemini': 'Run Gemini directly in Chrome',
  '已连接 {site}。请保持 Chrome 打开，并在 mac2API 中试聊。': 'Connected {site}. Keep Chrome open and try chat in mac2API.',
  '打开已登录的 AI 标签页，然后选择对应的服务。': 'Open a signed-in AI tab and select its service.',
  '默认连接本机 mac2API，使用当前 Chrome 个人资料。': 'Uses your current Chrome profile and local mac2API by default.',
  '通过 Chrome DevTools 连接时，请先在 mac2API 中授权 Chrome。': 'For Chrome DevTools, authorize Chrome in mac2API first.',
  '只有 Muse 会将 Cookie 发送至填写的 mac2API 地址。': 'Muse sign-in data is sent to the mac2API address you enter.',
  "请在当前窗口打开 {site} 后重试。": "Open {site} in the current window and try again.",
  "请在已授权 Chrome 的普通窗口中连接网页服务。": "Connect web services from the connected Chrome browser in a regular window.",
  "正在验证 {site} 标签页…": "Verifying the {site} tab…",
  "已验证 {site} 标签页。请在控制台对应页签试聊。": "Verified the {site} tab. Try chat in its dashboard tab.",
  "连接失败 (HTTP {status})": "Connection failed (HTTP {status})",
  // popup.html 静态文案
  'mac2API 账号连接': 'mac2API – Connect an account',
  '账号连接': 'Connect an account',
  '管理密钥': 'Admin key',
  '把当前浏览器里 muse.ai 的登录态，一键同步到你的 muse2api 服务。':
    'Sync the muse.ai login session from this browser to your muse2api service in one click.',
  'mac2API 地址': 'mac2API address',
  '账号标签（可留空）': 'Account label (optional)',
  '例如 muse-main': 'e.g. muse-main',
  '读取并导入': 'Read and import',
  // popup.js 日志 / 状态
  '请先填服务地址': 'Please enter the service URL first',
  '请先填 API Key': 'Please enter the API Key first',
  '正在读取 muse.ai 的 Cookie…': 'Reading muse.ai cookies…',
  '无法确认当前窗口的 Cookie 存储区。请在已登录 Muse 的窗口中打开扩展，并确认已允许扩展在无痕/InPrivate 模式运行。':
    'Cannot identify the current window\'s cookie store. Open the extension in the window signed in to Muse, and allow it in Incognito/InPrivate mode if needed.',
  '正在读取当前{window}窗口的 muse.ai Cookie…': 'Reading muse.ai cookies from the current {window} window…',
  '无痕/InPrivate': 'Incognito/InPrivate',
  '普通': 'regular',
  '\nCookie 来源：当前{window}窗口。导入仅保存登录态，请到管理页测试账号。':
    '\nCookie source: current {window} window. Import only saves the session; test the account on the admin page.',
  '没读到 muse.ai 的 Cookie。\n请先在这个浏览器里打开并登录 https://muse.ai/ ，再回来点一次。':
    'No muse.ai cookies found.\nOpen and log in to https://muse.ai/ in this browser first, then click again.',
  '读到 {n} 条 Cookie，但缺核心项：{missing}\n未读到登录会话，请检查当前浏览器的登录状态和扩展权限。':
    'Read {n} cookies, but missing core items such as: {missing}\nThe login session could not be read. Check this browser\'s login and extension permissions.',
  '读到 {n} 条 Cookie，正在上传到 {base} …': 'Read {n} cookies, uploading to {base} …',
  'API Key 不对（服务返回 401）。\n请到管理页「账号池」页顶部复制正确的 API Key。':
    'Invalid API Key (the service returned 401).\nCopy the correct API Key from the top of the "Account Pool" tab on the admin page.',
  '导入失败：HTTP {status}\n{body}': 'Import failed: HTTP {status}\n{body}',
  '(自动)': '(auto)',
  '未知': 'unknown',
  '✓ 导入成功\n账号标签：{label}\n账号 ID：{id}\nCookie 条数：{count}\n有效期到：{expires}\n':
    '✓ Imported successfully\nAccount label: {label}\nAccount ID: {id}\nCookie count: {count}\nValid until: {expires}\n',
  '\n注意：{warning}': '\nNote: {warning}',
  '出错了：{msg}\n\n常见原因：\n· 服务地址填错或服务没启动\n· 这个地址不是 https（或证书不被信任）\n· 浏览器拦截了跨域请求':
    'Something went wrong: {msg}\n\nCommon causes:\n· The service URL is wrong or the service is not running\n· The URL is not https (or the certificate is not trusted)\n· The browser blocked the cross-origin request',
  // 服务端（/admin/accounts）可能返回的整句
  '未解析到任何 cookie，请检查格式': 'No cookies could be parsed; please check the format',
  '缺少 Authorization: Bearer <key>': 'Missing Authorization: Bearer <key>',
  'API key 无效': 'Invalid API key',
};

/* 服务端返回的含变量文案：[正则, 替换串] */
const I18N_EN_PATTERNS = [
  [/^缺少核心 cookie：(.*)，该账号可能无法生成$/s, 'Missing core cookies: $1; this account may not be able to generate'],
];

const I18N_VI = {
  '检查连接': 'Kiểm tra kết nối',
  '正在检查 mac2API…': 'Đang kiểm tra kết nối mac2API…',
  '服务地址无效。请从 mac2API 管理页面复制地址。': 'Địa chỉ chưa hợp lệ. Hãy sao chép địa chỉ từ bảng quản trị mac2API.',
  '无法连接 mac2API。请检查地址和应用是否已启动。': 'Chưa kết nối được mac2API. Hãy kiểm tra địa chỉ và mở ứng dụng.',
  '管理密钥无效。请从 mac2API 管理页面复制正确的密钥。': 'Khóa quản trị chưa đúng. Hãy sao chép lại từ bảng quản trị mac2API.',
  '此地址未返回有效的 mac2API 状态。请检查服务地址。': 'Địa chỉ này chưa trả về thông tin của mac2API. Hãy kiểm tra lại địa chỉ.',
  'mac2API 地址和管理密钥有效。打开已登录的 AI 标签页后可检查该账号。': 'Địa chỉ và khóa quản trị hợp lệ. Mở tab AI đã đăng nhập để kiểm tra tài khoản.',
  'mac2API 连接正常，找到 Muse 登录信息。导入后请在 mac2API 检查账号。': 'Đã kết nối mac2API và tìm thấy thông tin đăng nhập Muse. Sau khi nhập, hãy kiểm tra tài khoản trong mac2API.',
  'mac2API 连接正常，已验证 {site} 的登录标签页。': 'Đã kết nối mac2API và xác minh tab {site} đã đăng nhập.',
  'mac2API 未确认保存 Muse 账号。请在管理页面检查后再重试。': 'mac2API chưa xác nhận lưu tài khoản Muse. Hãy xem danh sách tài khoản trước khi thử lại.',
  '高级连接设置': 'Tùy chọn kết nối nâng cao',
  '使用扩展直接运行 Gemini': 'Chạy Gemini trực tiếp bằng tiện ích',
  '已连接 {site}。请保持 Chrome 打开，并在 mac2API 中试聊。': 'Đã liên kết {site}. Hãy giữ Chrome mở rồi thử chat trong mac2API.',
  '打开已登录的 AI 标签页，然后选择对应的服务。': 'Mở tab AI đã đăng nhập rồi chọn dịch vụ.',
  '默认连接本机 mac2API，使用当前 Chrome 个人资料。': 'Mặc định dùng mac2API trên máy này và hồ sơ Chrome đang mở.',
  '通过 Chrome DevTools 连接时，请先在 mac2API 中授权 Chrome。': 'Nếu dùng Chrome DevTools, hãy cấp quyền cho Chrome trong mac2API trước.',
  '只有 Muse 会将 Cookie 发送至填写的 mac2API 地址。': 'Phiên đăng nhập Muse được gửi đến địa chỉ mac2API bạn nhập.',
  "请在当前窗口打开 {site} 后重试。": "Hãy mở {site} trong cửa sổ hiện tại rồi thử lại.",
  "请在已授权 Chrome 的普通窗口中连接网页服务。": "Hãy kết nối dịch vụ web từ cửa sổ thường của Chrome đã cấp quyền.",
  "正在验证 {site} 标签页…": "Đang xác minh tab {site}…",
  "已验证 {site} 标签页。请在控制台对应页签试聊。": "Đã xác minh tab {site}. Hãy thử chat trong mục tương ứng của bảng quản trị.",
  "连接失败 (HTTP {status})": "Kết nối thất bại (HTTP {status})",
  'mac2API 账号连接': 'mac2API – Kết nối tài khoản',
  '账号连接': 'Kết nối tài khoản',
  "管理密钥": "Khóa quản trị",
  "把当前浏览器里 muse.ai 的登录态，一键同步到你的 muse2api 服务。": "Đồng bộ phiên đăng nhập Muse.ai từ trình duyệt này sang dịch vụ Muse2API của bạn.",
  'mac2API 地址': 'Địa chỉ mac2API',
  "账号标签（可留空）": "Nhãn tài khoản (tùy chọn)",
  "例如 muse-main": "Ví dụ: muse-main",
  "读取并导入": "Đọc và nhập",
  "请先填服务地址": "Nhập địa chỉ dịch vụ trước",
  "请先填 API Key": "Hãy nhập khóa quản trị",
  "正在读取 muse.ai 的 Cookie…": "Đang đọc cookie Muse.ai…",
  "无法确认当前窗口的 Cookie 存储区。请在已登录 Muse 的窗口中打开扩展，并确认已允许扩展在无痕/InPrivate 模式运行。": "Không xác định được kho cookie của cửa sổ hiện tại. Hãy mở tiện ích trong cửa sổ đã đăng nhập Muse và cho phép chạy ở chế độ ẩn danh nếu cần.",
  "正在读取当前{window}窗口的 muse.ai Cookie…": "Đang đọc cookie Muse.ai từ cửa sổ {window} hiện tại…",
  "无痕/InPrivate": "ẩn danh/InPrivate",
  "普通": "thông thường",
  "\nCookie 来源：当前{window}窗口。导入仅保存登录态，请到管理页测试账号。": "\nNguồn cookie: cửa sổ {window} hiện tại. Hãy kiểm tra tài khoản trong trang quản trị sau khi nhập.",
  "没读到 muse.ai 的 Cookie。\n请先在这个浏览器里打开并登录 https://muse.ai/ ，再回来点一次。": "Không tìm thấy cookie Muse.ai.\nHãy đăng nhập https://muse.ai/ trên trình duyệt này rồi thử lại.",
  "读到 {n} 条 Cookie，但缺核心项：{missing}\n未读到登录会话，请检查当前浏览器的登录状态和扩展权限。": "Đã đọc {n} cookie nhưng thiếu mục chính: {missing}\nHãy kiểm tra phiên đăng nhập và quyền của tiện ích.",
  "读到 {n} 条 Cookie，正在上传到 {base} …": "Đã đọc {n} cookie, đang gửi tới {base}…",
  "API Key 不对（服务返回 401）。\n请到管理页「账号池」页顶部复制正确的 API Key。": "API Key không hợp lệ (HTTP 401).\nHãy sao chép khóa từ mục Muse → Kết nối và khóa quản trị.",
  "导入失败：HTTP {status}\n{body}": "Nhập thất bại (HTTP {status})\n{body}",
  "(自动)": "(tự động)",
  "未知": "chưa rõ",
  "✓ 导入成功\n账号标签：{label}\n账号 ID：{id}\nCookie 条数：{count}\n有效期到：{expires}\n": "✓ Đã nhập thành công\nNhãn: {label}\nID tài khoản: {id}\nSố cookie: {count}\nCó hiệu lực đến: {expires}\n",
  "\n注意：{warning}": "\nLưu ý: {warning}",
  "出错了：{msg}\n\n常见原因：\n· 服务地址填错或服务没启动\n· 这个地址不是 https（或证书不被信任）\n· 浏览器拦截了跨域请求": "Lỗi: {msg}\n\nHãy kiểm tra địa chỉ dịch vụ, chứng chỉ HTTPS hoặc chính sách truy cập khác nguồn của trình duyệt.",
  "未解析到任何 cookie，请检查格式": "Không đọc được cookie. Hãy kiểm tra định dạng",
  "缺少 Authorization: Bearer <key>": "Thiếu Authorization: Bearer <key>",
  "API key 无效": "API key không hợp lệ"
};

function t(s, vars) {
  let out = s;
  if (LANG !== 'zh' && typeof s === 'string') {
    const dictionary = LANG === 'vi' ? I18N_VI : I18N_EN;
    if (Object.prototype.hasOwnProperty.call(dictionary, s)) out = dictionary[s];
    else for (const [re, rep] of I18N_EN_PATTERNS) if (re.test(s)) { out = s.replace(re, LANG === 'vi' ? 'Thiếu cookie chính: $1. Tài khoản có thể chưa tạo được nội dung' : rep); break; }
  }
  if (vars) out = out.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
  return out;
}

function applyLang() {
  $('langSel').value = LANG;
  if (LANG === 'zh') return;
  document.documentElement.lang = LANG;
  document.title = t(document.title);
  document.querySelectorAll('[data-i18n-text]').forEach((el) => {
    el.textContent = t(el.textContent.trim());
  });
  document.querySelectorAll('[placeholder]').forEach((el) => {
    el.placeholder = t(el.placeholder);
  });
}

function log(message, cls) {
  const el = $('log');
  el.className = 'show';
  const safe = String(message).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  el.innerHTML = cls ? `<span class="${cls}">${safe}</span>` : safe;
}

/* 规范化服务地址：去掉结尾斜杠和 /v1 后缀 */
function normBase(v) {
  let s = (v || '').trim();
  if (!s) return '';
  if (!/^https?:\/\//i.test(s)) s = 'https://' + s;
  s = s.replace(/\/+$/, '');
  s = s.replace(/\/(?:muse\/|chatgpt\/|gemini\/)?v1$/i, '');
  return s;
}

async function checkServer(base, key) {
  let url;
  try { url = new URL(base); } catch {}
  if (!url || !['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error(t('服务地址无效。请从 mac2API 管理页面复制地址。'));
  }
  const response = await fetch(base + '/admin/status?compact=1', {
    redirect: 'error', cache: 'no-store', credentials: 'omit',
    headers: {Authorization: 'Bearer ' + key}, signal: AbortSignal.timeout(8000),
  }).catch(() => {
    throw new Error(t('无法连接 mac2API。请检查地址和应用是否已启动。'));
  });
  const info = await response.json().catch(() => null);
  if (response.status === 401 && ['API key 无效', '缺少 Authorization: Bearer <key>'].includes(info?.detail)) {
    throw new Error(t('管理密钥无效。请从 mac2API 管理页面复制正确的密钥。'));
  }
  if (!response.ok || typeof info?.browser_running !== 'boolean' ||
      !Array.isArray(info.essential_cookies) || !info.essential_cookies.includes('hatch_sess')) {
    throw new Error(t('此地址未返回有效的 mac2API 状态。请检查服务地址。'));
  }
}

async function checkConnection() {
  const base = normBase($('base').value) || LOCAL_BASE;
  const key = $('key').value.trim();
  if (!key) return log(t('请先填 API Key'), 'bad');
  $('checkConnection').disabled = true;
  log(t('正在检查 mac2API…'));
  try {
    await checkServer(base, key);
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    if (tabAtOrigin(tab, ['https://muse.ai', 'https://www.muse.ai'])) {
      const {cookies} = await grabCookies(true);
      if (!cookies.hatch_sess) {
        return log(t('没读到 muse.ai 的 Cookie。\n请先在这个浏览器里打开并登录 https://muse.ai/ ，再回来点一次。'), 'warn');
      }
      return log(t('mac2API 连接正常，找到 Muse 登录信息。导入后请在 mac2API 检查账号。'), 'ok');
    }
    const provider = Object.entries(WEB_BUTTONS).find(([, site]) => tabAtOrigin(tab, [site.origin]));
    if (provider) {
      const [id, {site}] = provider;
      const result = await chrome.runtime.sendMessage({type:'mac2apiChromeCheck', provider:id, tabId:tab.id,
        base, direct:id === 'chatgpt' || $('directChrome').checked});
      if (!result?.ok) throw new Error(result?.error || t('连接失败 (HTTP {status})', {status: '?'}));
      return log(t('mac2API 连接正常，已验证 {site} 的登录标签页。', {site}), 'ok');
    }
    log(t('mac2API 地址和管理密钥有效。打开已登录的 AI 标签页后可检查该账号。'), 'ok');
  } catch (error) {
    log(error.message, 'bad');
  } finally {
    $('checkConnection').disabled = false;
  }
}
async function loadCfg() {
  const o = await chrome.storage.local.get([STORE, BRIDGE_STORE]);
  const c = o[STORE] || {};
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true}).catch(() => []);
  const service = Object.entries(WEB_BUTTONS).find(([, site]) => tabAtOrigin(tab, [site.origin]))?.[0];
  const bridge = o[BRIDGE_STORE]?.providers?.[service];
  if (bridge) {
    $('base').value = bridge.base;
    $('label').value = bridge.label || '';
    if (c.base === bridge.base && c.key) $('key').value = c.key;
    return;
  }
  if (c.base) $('base').value = c.base;
  if (c.key) $('key').value = c.key;
  if (c.label) $('label').value = c.label;
  // 没有配置过就尝试从当前标签页猜一个（用户在管理页上时）
  if (!c.base) {
    try {
      const u = tab && tab.url ? new URL(tab.url) : null;
      if (u && /\/admin/.test(u.pathname)) {
        $('base').value = u.origin;
        const k = new URLSearchParams(u.search).get('key');
        if (k) $('key').value = k;
      }
    } catch (e) { /* 忽略 */ }
  }
}

async function saveCfg(base, key, label) {
  await chrome.storage.local.set({[STORE]: {base, key, label}});
}

async function grabCookies(requireMuseTab = false) {
  // A spanning extension shares its regular context with incognito windows.
  // Select the active tab's store explicitly; never guess or fall back.
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (requireMuseTab && !tabAtOrigin(tab, ['https://muse.ai', 'https://www.muse.ai'])) {
    throw new Error(t('请在当前窗口打开 {site} 后重试。', {site: 'Muse'}));
  }
  const stores = await chrome.cookies.getAllCookieStores();
  const store = tab && stores.find((s) => s.tabIds.includes(tab.id));
  if (!store) {
    throw new Error(t('无法确认当前窗口的 Cookie 存储区。请在已登录 Muse 的窗口中打开扩展，并确认已允许扩展在无痕/InPrivate 模式运行。'));
  }
  const windowKind = t(tab.incognito ? '无痕/InPrivate' : '普通');
  log(t('正在读取当前{window}窗口的 muse.ai Cookie…', { window: windowKind }));
  // URL filtering excludes unrelated subdomains and paths. Chrome orders
  // matches by longest path, then earliest creation time (as cookies.get does).
  const all = await chrome.cookies.getAll({ url: 'https://muse.ai/api/session', storeId: store.id });
  const out = Object.create(null), exp = Object.create(null);
  for (const c of all) {
    if (Object.prototype.hasOwnProperty.call(out, c.name)) continue;
    out[c.name] = c.value;
    if (c.expirationDate) exp[c.name] = Math.floor(c.expirationDate);
  }
  return { cookies: out, expires: exp, windowKind };
}

async function run() {
  const base = normBase($('base').value) || LOCAL_BASE;
  const key = $('key').value.trim();
  const label = $('label').value.trim();

  if (!key) return log(t('请先填 API Key'), 'bad');

  $('go').disabled = true;
  log(t('正在检查 mac2API…'));

  try {
    await checkServer(base, key);
    const { cookies, expires, windowKind } = await grabCookies(true);
    const names = Object.keys(cookies);
    if (!names.length) {
      return log(t('没读到 muse.ai 的 Cookie。\n请先在这个浏览器里打开并登录 '
                 + 'https://muse.ai/ ，再回来点一次。'), 'bad');
    }
    const missing = ESSENTIAL.filter((n) => !cookies[n]);
    if (missing.length) {
      log(t('读到 {n} 条 Cookie，但缺核心项：{missing}\n'
          + '未读到登录会话，请检查当前浏览器的登录状态和扩展权限。',
          { n: names.length, missing: missing.join(LANG === 'zh' ? '、' : ', ') }), 'warn');
      return;
    }

    log(t('读到 {n} 条 Cookie，正在上传到 {base} …', { n: names.length, base }));

    const r = await fetch(base + '/admin/accounts', {
      method: 'POST',
      redirect: 'error',
      credentials: 'omit',
      signal: AbortSignal.timeout(12000),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + key,
      },
      body: JSON.stringify({ label, cookies, expires }),
    });

    const text = await r.text();
    let data;
    try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }

    if (r.status === 401) {
      return log(t('管理密钥无效。请从 mac2API 管理页面复制正确的密钥。'), 'bad');
    }
    if (!r.ok) {
      return log(typeof data?.detail === 'string' ? t(data.detail)
        : t('mac2API 未确认保存 Muse 账号。请在管理页面检查后再重试。'), 'bad');
    }

    const a = Array.isArray(data?.added) && data.added[0];
    if (data?.count !== 1 || typeof a?.id !== 'string' || !a.id.trim()) {
      return log(t('mac2API 未确认保存 Muse 账号。请在管理页面检查后再重试。'), 'bad');
    }
    await saveCfg(base, key, label).catch(() => {});
    log(t('✓ 导入成功\n账号标签：{label}\n账号 ID：{id}\nCookie 条数：{count}\n有效期到：{expires}\n', {
          label: a.label || label || t('(自动)'),
          id: a.id || '?',
          count: a.cookie_count || names.length,
          expires: a.expires_at ? new Date(a.expires_at * 1000).toLocaleString(LANG) : t('未知'),
        })
        + t('\nCookie 来源：当前{window}窗口。导入仅保存登录态，请到管理页测试账号。', { window: windowKind })
        + (data.warning ? t('\n注意：{warning}', { warning: t(data.warning) }) : ''), 'ok');
  } catch (e) {
    log(e instanceof TypeError || e.name === 'TimeoutError'
      ? t('mac2API 未确认保存 Muse 账号。请在管理页面检查后再重试。') : e.message, 'bad');
  } finally {
    $('go').disabled = false;
  }
}

function tabAtOrigin(tab, origins) {
  try { return origins.includes(new URL(tab.url).origin); }
  catch { return false; }
}

const WEB_BUTTONS = {
  chatgpt: {button: 'linkChatGPT', site: 'ChatGPT', origin: 'https://chatgpt.com'},
  'gemini-web': {button: 'linkGemini', site: 'Gemini', origin: 'https://gemini.google.com'},
};

async function linkWebProvider(provider) {
  const {button, site, origin} = WEB_BUTTONS[provider];
  const direct = provider === 'chatgpt' || $('directChrome').checked;
  const base = normBase($('base').value) || (direct ? LOCAL_BASE : '');
  const key = $('key').value.trim();
  const label = $('label').value.trim();
  if (!base) return log(t('请先填服务地址'), 'bad');
  if (!key) return log(t('请先填 API Key'), 'bad');
  $(button).disabled = true;
  let tabId, marker;
  try {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    if (!tabAtOrigin(tab, [origin])) {
      return log(t('请在当前窗口打开 {site} 后重试。', {site}), 'bad');
    }
    if (tab.incognito) return log(t('请在已授权 Chrome 的普通窗口中连接网页服务。'), 'bad');
    log(t('正在验证 {site} 标签页…', {site}));
    if (direct) {
      const result = await chrome.runtime.sendMessage({type: 'mac2apiChromeConnect', provider, tabId: tab.id,
        base, key, label});
      if (!result?.ok) return log(t(result?.error || '连接失败 (HTTP {status})', {status: '?'}), 'bad');
      log(t('已连接 {site}。请保持 Chrome 打开，并在 mac2API 中试聊。', {site}), 'ok');
      return;
    }
    tabId = tab.id;
    marker = [...crypto.getRandomValues(new Uint8Array(16))]
      .map(byte => byte.toString(16).padStart(2, '0')).join('');
    const [marked] = await chrome.scripting.executeScript({
      target: {tabId},
      args: [origin, marker],
      func: (expected, nonce) => {
        if (location.origin !== expected) return false;
        document.documentElement.dataset.mac2apiLink = nonce;
        return true;
      },
    });
    if (!marked?.result) return log(t('请在当前窗口打开 {site} 后重试。', {site}), 'bad');
    const response = await fetch(base + '/admin/web-providers/' + provider + '/link', {
      method: 'POST',
      redirect: 'error',
      credentials: 'omit',
      signal: AbortSignal.timeout(12000),
      headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + key},
      body: JSON.stringify({marker, extension_version: chrome.runtime.getManifest().version}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok !== true || data.provider !== provider) {
      return log(typeof data.detail === 'string' ? t(data.detail)
        : t('连接失败 (HTTP {status})', {status: response.status}), 'bad');
    }
    await saveCfg(base, key, label);
    log(t('已验证 {site} 标签页。请在控制台对应页签试聊。', {site}), 'ok');
  } catch (error) {
    log(error.message, 'bad');
  } finally {
    if (tabId !== undefined && marker) {
      await chrome.scripting.executeScript({
        target: {tabId},
        args: [origin, marker],
        func: (expected, nonce) => {
          if (location.origin === expected && document.documentElement.dataset.mac2apiLink === nonce) {
            delete document.documentElement.dataset.mac2apiLink;
          }
        },
      }).catch(() => {});
    }
    $(button).disabled = false;
  }
}

applyLang();
$('extensionVersion').textContent = 'v' + chrome.runtime.getManifest().version;
$('directChrome').addEventListener('change', () => {
  $('directSteps').hidden = !$('directChrome').checked;
  $('cdpSteps').hidden = $('directChrome').checked;
});
$('langSel').addEventListener('change', (e) => setLang(e.target.value));
$('go').addEventListener('click', run);
$('checkConnection').addEventListener('click', checkConnection);
for (const [provider, {button}] of Object.entries(WEB_BUTTONS)) {
  $(button).addEventListener('click', () => linkWebProvider(provider));
}
loadCfg().catch(error => log(error.message, 'bad'));
