// Each action runs in the isolated world of an explicitly chosen provider tab.
const bridgeSites = {
  chatgpt: {
    url: 'https://chatgpt.com/',
    origin: 'https://chatgpt.com',
    path: '/',
    account: [
      '[data-testid="accounts-profile-button"]',
      '[data-testid="profile-button"]',
      '[data-testid="profile-menu-button"]',
      '[data-testid="profile-menu-trigger"]',
      'button[aria-label="Open profile menu" i]',
      'button[aria-label="Mở trình đơn hồ sơ" i]',
      'button[aria-label="Mở menu hồ sơ" i]',
    ].join(', '),
    history: [
      'a[data-testid^="history-item-"][href]',
      ':is(nav, #sidebar, #stage-slideover-sidebar) a[href]',
    ].join(', '),
    signIn: [
      '[data-testid="login-button"]',
      '[data-testid="signup-button"]',
      'header a[href^="/auth/login"]',
      'header a[href^="/auth/signup"]',
    ].join(', '),
    composer: [
      '#prompt-textarea',
      '[data-testid="prompt-textarea"][contenteditable="true"]',
      'textarea[data-testid="prompt-textarea"]',
      'textarea[name="prompt-textarea"]',
      '[data-testid="prompt-textarea"] :is(textarea, [contenteditable])',
      'form[data-type="unified-composer"] [role="textbox"][contenteditable="true"]',
      'form[data-type="unified-composer"] .ProseMirror[contenteditable="true"]',
      '#thread-bottom-container :is(textarea, [role="textbox"][contenteditable], .ProseMirror[contenteditable])',
    ].join(', '),
    formEditor: 'textarea, [contenteditable]',
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
  const editable = element =>
    visible(element) && getComputedStyle(element).visibility === 'visible' &&
    !element.closest('[inert], [aria-disabled="true"], [aria-readonly="true"], dialog, [role="dialog"]') &&
    (element.isContentEditable || (element.matches('textarea') && !element.matches(':disabled') && !element.readOnly));
  const controls = [...document.querySelectorAll(site.send)];
  let editor = [...document.querySelectorAll(site.composer)].find(editable);
  let composerForm;
  if (!editor && site.formEditor) {
    const forms = new Set(controls.map(button => button.form || button.closest('form')).filter(Boolean));
    const candidates = [...forms].flatMap(form => [...form.querySelectorAll(site.formEditor)].filter(editable));
    if (candidates.length === 1) {
      [editor] = candidates;
      composerForm = editor.closest('form');
    }
  }
  // A collapsed sidebar can hide the account control in a signed-in profile.
  const signIn = site.signIn && [...document.querySelectorAll(site.signIn)].some(element =>
    visible(element) && getComputedStyle(element).visibility === 'visible'
  );
  const history = !signIn && site.history && [...document.querySelectorAll(site.history)].some(link =>
    visible(link) && link.origin === site.origin &&
    /^\/c\/[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\/?$/i.test(link.pathname)
  );
  const signedIn = !signIn && Boolean(document.querySelector(site.account) || history);
  if (action === 'verify') return {signedIn, composerReady: Boolean(editor), ...(signIn ? {signedOut: true} : {})};
  const replies = [...document.querySelectorAll(site.reply)].filter(element =>
    visible(element) && !element.parentElement?.closest(site.reply)
  );
  const stopping = Boolean(find(site.stop));
  const send = controls.find(element =>
    (!composerForm || element.form === composerForm || composerForm.contains(element)) &&
    visible(element) && !element.disabled && element.getAttribute('aria-disabled') !== 'true',
  );
  if (action === 'prepare' || action === 'send') {
    if (!signedIn) return {signedIn: false};
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
