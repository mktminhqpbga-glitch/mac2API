// Each action runs in the isolated world of an explicitly chosen provider tab.
const bridgeSites = {
  chatgpt: {
    url: 'https://chatgpt.com/',
    origin: 'https://chatgpt.com',
    path: '/',
    account: '[data-testid="accounts-profile-button"], [data-testid="profile-button"], [data-testid="profile-menu-button"]',
    composer: '#prompt-textarea',
    send: '[data-testid="send-button"], #composer-submit-button[data-testid="send-button"]',
    reply: '[data-message-author-role="assistant"]',
    stop: '[data-testid="stop-button"]',
    finished: '[data-testid="copy-turn-action-button"]',
  },
  'gemini-web': {
    url: 'https://gemini.google.com/app',
    origin: 'https://gemini.google.com',
    path: '/app',
    account: 'a[href^="https://accounts.google.com/SignOutOptions"], button[aria-label^="Google Account"], button[aria-label^="Tài khoản Google"]',
    composer: 'rich-textarea .ql-editor[contenteditable="true"], rich-textarea [role="textbox"][contenteditable="true"]',
    send: 'button.send-button:not(.stop-button), button[aria-label="Send message"], button[aria-label="Gửi tin nhắn"]',
    reply: 'model-response, [data-test-id="model-response"]',
    stop: 'button.stop-button, button[aria-label="Stop response"], button[aria-label="Dừng phản hồi"]',
    finished: 'button[aria-label="Copy"], button[aria-label="Sao chép"], [data-test-id="copy-button"]',
  },
};

async function bridgeSiteAction(site, action, prompt) {
  if (location.origin !== site.origin) return {wrongSite: true};
  const visible = element => Boolean(element?.getClientRects().length);
  const find = selector => [...document.querySelectorAll(selector)].find(visible);
  const editor = find(site.composer);
  if (action === 'verify') return {signedIn: Boolean(editor && find(site.account))};
  const replies = [...document.querySelectorAll(site.reply)].filter(element =>
    visible(element) && !element.parentElement?.closest(site.reply)
  );
  const stopping = Boolean(find(site.stop));
  const send = [...document.querySelectorAll(site.send)].find(element =>
    visible(element) && !element.disabled && element.getAttribute('aria-disabled') !== 'true'
  );
  if (action === 'prepare' || action === 'send') {
    if (!find(site.account)) return {signedIn: false};
    if (!editor) return {ready: false};
    if (location.pathname.replace(/\/$/, '') !== site.path.replace(/\/$/, '') || replies.length || stopping) {
      return {dirty: true};
    }
    const lines = value => value.replace(/\r\n/g, '\n').replace(/\n(?:[ \t]*\n)+/g, '\n').trim();
    const expected = lines(prompt);
    const input = () => lines(editor.value ?? editor.innerText ?? '');
    const attachments = () => editor.closest('form, .input-area')?.querySelector(
      '[data-testid="composer-attachment"], [data-testid="remove-attachment"], [aria-label="Remove attachment"], [aria-label="Xóa tệp đính kèm"]'
    );
    if (attachments()) return {dirty: true};
    if (action === 'send') {
      if (input() !== expected) return {dirty: true};
      if (!send) return {ready: false};
      send.click();
      return {sent: true};
    }
    if (input() !== expected) {
      if (input()) return {dirty: true};
      editor.focus();
      if (!document.execCommand('insertText', false, prompt)) return {dirty: true};
    }
    return {ready: input() === expected && Boolean(send)};
  }

  const reply = replies.at(-1);
  const body = reply?.querySelector('.markdown, .markdown-main-panel') || reply;
  const complete = reply && [...(reply.closest('article, model-response') || reply).querySelectorAll(site.finished)]
    .some(visible);
  const images = reply ? [...reply.querySelectorAll('img')].filter(element =>
    visible(element) && element.complete && element.naturalWidth >= 256 && element.naturalHeight >= 256
  ) : [];
  return {
    busy: stopping, complete: Boolean(complete), text: body?.innerText?.trim() || '',
    image: images.at(-1)?.currentSrc || images.at(-1)?.src || '',
  };
}

async function readBridgeImage(source, expectedOrigin, timeout) {
  if (location.origin !== expectedOrigin) return null;
  const limit = 20 * 1024 * 1024;
  const response = await fetch(source, {redirect: 'error', signal: AbortSignal.timeout(timeout)});
  if (!response.ok || Number(response.headers.get('Content-Length')) > limit) return null;
  const reader = response.body?.getReader();
  if (!reader) return null;
  const parts = [];
  let size = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.length;
      if (size > limit) return null;
      parts.push(value);
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
  return new Promise(resolve => {
    const file = new FileReader();
    file.onload = () => resolve(file.result.split(',')[1] || null);
    file.onerror = () => resolve(null);
    file.readAsDataURL(new Blob(parts));
  });
}
