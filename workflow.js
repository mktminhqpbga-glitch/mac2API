// Dashboard workflow UI. Runs and their outputs are stored on the server.
const byId = id => document.getElementById(id);
const defaults = {title: '', input: '', context: '', split: true,
  steps: [{name: 'Bước 1', kind: 'text', instruction: '', model: '', aspect_ratio: '', duration: null, input_image: false}]};
const kinds = {text: 'Văn bản', image: 'Ảnh', video: 'Video'};
const ratios = ['1:1', '9:16', '16:9', '3:4', '4:3'];
const states = {
  queued: ['Chờ xử lý', 'mute'], running: ['Đang chạy', 'info'], completed: ['Hoàn tất', 'ok'],
  failed: ['Lỗi', 'bad'], errors: ['Hoàn tất một phần', 'warn'],
  pausing: ['Đang tạm dừng', 'warn'], paused: ['Đã tạm dừng', 'mute'],
  blocked: ['Chờ bước lỗi', 'mute'],
};
let draft;
let selected;
let lastRun;
let models = [];
let loaded = false;
let timer;
let busy = false;
let viewVersion = 0;
let historyUnavailable = false;

function readLocal(name, fallback) {
  try { return JSON.parse(localStorage.getItem('mac2apiWorkflow:' + name)) ?? fallback; }
  catch { return fallback; }
}

function writeLocal(name, value) {
  try { localStorage.setItem('mac2apiWorkflow:' + name, JSON.stringify(value)); }
  catch {
    byId('workflowDraftStatus').textContent = 'Trình duyệt chưa lưu được bản nháp trên máy này.';
  }
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, action, disabled = false) {
  const node = element('button', text, 'ghost mini');
  node.type = 'button';
  node.disabled = disabled;
  node.onclick = action;
  return node;
}

function label(text, input) {
  const node = element('label');
  node.append(text, input);
  return node;
}

function inputFor(step, key, tag = 'input') {
  const field = document.createElement(tag);
  field.value = step[key] ?? '';
  field.oninput = () => {
    step[key] = field.type === 'number' ? (field.value ? field.valueAsNumber : null) : field.value;
    writeLocal('draft', draft);
  };
  return field;
}

function moveStep(index, to) {
  const [step] = draft.steps.splice(index, 1);
  draft.steps.splice(to, 0, step);
  writeLocal('draft', draft);
  renderSteps();
}

function renderSteps() {
  const rows = draft.steps.map((step, index) => {
    const row = element('div', undefined, 'workflow-step');
    const header = element('div', undefined, 'workflow-step-head');
    const actions = element('div', undefined, 'acts');
    header.append(element('strong', 'Bước ' + (index + 1)), actions);
    actions.append(button('Lên', () => moveStep(index, index - 1), index === 0),
      button('Xuống', () => moveStep(index, index + 1), index === draft.steps.length - 1),
      button('Xóa', () => {
        draft.steps.splice(index, 1);
        writeLocal('draft', draft);
        renderSteps();
      }, draft.steps.length === 1));
    const name = inputFor(step, 'name');
    name.type = 'text';
    name.required = true;
    name.maxLength = 80;
    const type = element('select');
    type.append(...Object.entries(kinds).map(([value, text]) => new Option(text, value)));
    type.value = step.kind;
    type.onchange = () => {
      Object.assign(step, {kind: type.value, model: '', input_image: false, aspect_ratio: '', duration: null});
      writeLocal('draft', draft);
      renderSteps();
    };
    const instruction = inputFor(step, 'instruction', 'textarea');
    instruction.rows = 2;
    instruction.maxLength = 8000;
    instruction.placeholder = 'Ví dụ: Viết tiêu đề và mô tả cho mục này';
    const basic = element('div', undefined, 'client-options');
    basic.append(label('Tên bước', name), label('Kết quả cần tạo', type));
    row.append(header, basic, label('Yêu cầu ở bước này', instruction));

    const advanced = element('details');
    const advancedHeading = element('summary', 'Cài đặt nâng cao');
    const model = element('select');
    model.append(new Option('Muse mặc định', ''));
    const candidates = models.filter(candidate => candidate.type === step.kind);
    for (const candidate of candidates) model.append(new Option(candidate.id, candidate.id));
    if (step.model && !candidates.some(candidate => candidate.id === step.model)) {
      model.append(new Option(step.model, step.model));
    }
    model.value = step.model;
    advanced.append(advancedHeading, label('Mô hình AI', model));
    const reference = element('input');
    reference.type = 'checkbox';
    reference.checked = step.input_image;
    reference.onchange = () => { step.input_image = reference.checked; writeLocal('draft', draft); };
    const referenceLabel = label('Dùng ảnh từ bước trước làm tham chiếu ', reference);
    const canUseReference = draft.steps.slice(0, index).some(previous => previous.kind === 'image');
    referenceLabel.hidden = !step.input_image &&
      (!canUseReference || Boolean(step.model && !step.model.startsWith('muse')));
    model.onchange = () => {
      step.model = model.value;
      if (step.model && !step.model.startsWith('muse')) {
        reference.checked = step.input_image = false;
        referenceLabel.hidden = true;
      } else if (canUseReference) {
        referenceLabel.hidden = false;
      }
      writeLocal('draft', draft);
    };
    if (step.kind !== 'text') {
      const ratio = element('select');
      ratio.append(new Option('Mặc định', ''), ...ratios.map(value => new Option(value, value)));
      ratio.value = step.aspect_ratio;
      ratio.onchange = () => { step.aspect_ratio = ratio.value; writeLocal('draft', draft); };
      advanced.append(label('Tỉ lệ khung hình', ratio), referenceLabel);
    }
    if (step.kind === 'video') {
      const duration = inputFor(step, 'duration');
      duration.type = 'number';
      duration.min = 1;
      duration.max = 600;
      duration.placeholder = 'Mặc định';
      advanced.append(label('Thời lượng yêu cầu (giây)', duration));
    }
    const routes = {text: '/v1/chat/completions', image: '/v1/images/tasks', video: '/v1/videos'};
    advanced.append(element('p', 'API mac2API · ' + routes[step.kind], 'hint'));
    row.append(advanced);
    return row;
  });
  byId('workflowSteps').replaceChildren(...rows);
  byId('workflowAdd').disabled = draft.steps.length >= 10;
}

function showDraft() {
  byId('workflowTitle').value = draft.title;
  byId('workflowInput').value = draft.input;
  byId('workflowContext').value = draft.context;
  byId('workflowSplit').checked = draft.split;
  renderSteps();
}

function badge(status) {
  const [text, tone] = states[status] || states.queued;
  return element('span', text, 'badge ' + tone);
}

function selectRun(id) {
  selected = id;
  lastRun = undefined;
  writeLocal('selection', id);
  byId('workflowRunSelect').value = id;
}

function renderRun(run, force = false) {
  if (authOK === false) return;
  if (lastRun?.id === run.id && (lastRun.updated_at > run.updated_at ||
      (!force && lastRun.updated_at === run.updated_at &&
       lastRun.state === run.state && lastRun.error === run.error))) return;
  lastRun = run;
  const runState = byId('workflowRunState');
  const indicator = badge(run.state);
  runState.className = indicator.className;
  runState.textContent = indicator.textContent;
  runState.hidden = false;
  byId('workflowPause').hidden = run.state !== 'running';
  byId('workflowResume').hidden = !['pausing', 'paused'].includes(run.state);
  byId('workflowActions').hidden = false;
  const total = run.items.length * run.config.steps.length;
  const finished = run.items.flatMap(item => item.steps).filter(step => step.status === 'completed').length;
  const progress = byId('workflowProgress');
  progress.max = total;
  progress.value = finished;
  progress.hidden = false;
  const suffix = run.state === 'pausing' ? ' · Dừng sau bước đang chạy.' : '';
  byId('workflowRunStatus').textContent = run.error || `${run.config.title} · ${finished}/${total} bước hoàn tất${suffix}`;
  const openItems = new Set([...byId('workflowItems').querySelectorAll('details[open]')].map(node => node.dataset.item));
  const outputs = [];
  const items = run.items.map((item, itemIndex) => {
    const details = element('details', undefined, 'workflow-item');
    details.dataset.item = String(itemIndex);
    const statuses = item.steps.map(step => step.status);
    const failed = statuses.includes('failed');
    const running = statuses.includes('running');
    const state = failed ? 'failed' : running ? 'running' : statuses.every(status => status === 'completed') ? 'completed' : 'queued';
    details.open = failed || running || openItems.has(details.dataset.item);
    const title = element('summary', `Mục ${itemIndex + 1} · ${item.input.slice(0, 85)} `);
    title.append(badge(state));
    details.append(title);
    item.steps.forEach((status, stepIndex) => {
      const step = run.config.steps[stepIndex];
      const row = element('div', undefined, 'workflow-step-state');
      const blocked = status.status === 'queued' && statuses.slice(0, stepIndex).includes('failed');
      row.append(element('strong', step.name), badge(blocked ? 'blocked' : status.status));
      if (status.status === 'failed') {
        row.append(element('p', friendlyFailure(status.error)));
        if (status.check_before_retry && status.task_id) {
          row.append(button('Xem tác vụ đã gửi', () => {
            byId('taskProvider').value = '';
            byId('taskState').value = '';
            byId('taskSearch').value = status.task_id;
            byId('taskSearch').dispatchEvent(new Event('input'));
            goTab('tasks');
          }));
        }
        row.append(button('Chạy lại bước này',
          () => {
            if (status.check_before_retry && !confirm(
              'Yêu cầu trước có thể đang được xử lý. Bạn đã kiểm tra và muốn gửi lại bước này?')) return;
            return control('retry', {item: itemIndex, step: stepIndex});
          }));
      }
      if (status.result) {
        outputs.push(renderResult(status.result, `Mục ${itemIndex + 1} · ${step.name}`,
          `mac2api-muc-${itemIndex + 1}-buoc-${stepIndex + 1}`));
      }
      details.append(row);
    });
    return details;
  });
  byId('workflowItems').replaceChildren(...items);
  byId('workflowResults').replaceChildren(...(outputs.length ? outputs
    : [element('p', 'Kết quả sẽ hiện khi từng bước hoàn tất.', 'hint')]));
}

function renderResult(result, title, filename) {
  const box = element('article', undefined, 'workflow-result');
  box.append(element('strong', title));
  const actions = element('div', undefined, 'acts');
  if (typeof result.text === 'string') {
    box.append(element('pre', result.text || ''));
    actions.append(button('Sao chép', () => copyText(result.text)), button('Tải văn bản', () => {
      const url = URL.createObjectURL(new Blob([result.text], {type: 'text/plain;charset=utf-8'}));
      const link = element('a');
      link.href = url;
      link.download = filename + '.txt';
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }));
  } else if (result.url) {
    const open = element('a', 'Mở kết quả', 'btnlike ghostlike');
    open.href = result.url;
    open.target = '_blank';
    open.rel = 'noopener';
    const download = element('a', 'Tải tệp', 'btnlike ghostlike');
    download.href = result.download_url || result.url;
    download.download = result.filename;
    actions.append(open, download);
  } else {
    box.append(element('p', 'Tệp kết quả đã được dọn hoặc xóa.', 'hint'));
  }
  box.append(actions);
  return box;
}

async function refreshRun(force = false) {
  const id = selected;
  const version = viewVersion;
  if (!id || busy || authOK === false) return;
  const run = await api('/admin/workflows/' + id);
  if (id === selected && version === viewVersion) {
    renderRun(run, force);
    byId('workflowConnection').textContent = '';
  }
}

function setPoll() {
  clearTimeout(timer);
  const pending = readLocal('pending', {});
  const lookingUp = pending.id && pending.lookupUntil > Date.now();
  if (authOK === false || !byId('tab-workflows').classList.contains('active') ||
      (!historyUnavailable && !lookingUp && !['running', 'pausing'].includes(lastRun?.state))) return;
  timer = setTimeout(async () => {
    if (historyUnavailable || lookingUp) {
      await loadWorkflows();
      return;
    }
    const version = viewVersion;
    try { await refreshRun(); }
    catch {
      if (version === viewVersion) byId('workflowConnection').textContent =
        'Tạm mất kết nối. Tiến độ trước đó đã được lưu. Đang thử kết nối lại…';
    }
    finally { setPoll(); }
  }, byId('workflowConnection').textContent ? 4000 : 1600);
}

async function loadHistory({recoverPending = false} = {}) {
  const version = viewVersion;
  const pending = recoverPending ? readLocal('pending', {}) : {};
  const {runs} = await api('/admin/workflows');
  if (version !== viewVersion || authOK === false || (recoverPending && busy)) return;
  const preferred = pending.id || selected;
  if (preferred && (!runs.some(run => run.id === preferred) || pending.id)) {
    try {
      const run = await api('/admin/workflows/' + encodeURIComponent(preferred));
      if (version !== viewVersion || authOK === false || (recoverPending && busy)) return;
      if (!runs.some(entry => entry.id === run.id)) runs.unshift({
        id: run.id, state: run.state, title: run.config.title,
      });
      if (pending.id && readLocal('pending', {}).id === pending.id) {
        writeLocal('pending', null);
        selectRun(run.id);
        renderRun(run, true);
        byId('workflowDraftStatus').textContent = '';
      }
    } catch (error) {
      if (![404, 409].includes(error.status)) throw error;
      if (error.status === 409 && pending.id && readLocal('pending', {}).id === pending.id) {
        writeLocal('pending', null);
        byId('workflowDraftStatus').textContent = error.message;
      }
    }
  }
  if (version !== viewVersion || authOK === false) return;
  if (!runs.some(run => run.id === selected)) selectRun(runs[0]?.id || null);
  byId('workflowRunSelect').replaceChildren(...runs.map(run => new Option(
    run.title + ' · ' + (states[run.state]?.[0] || 'Đã lưu'), run.id)));
  byId('workflowHistory').hidden = !runs.length;
  if (selected) byId('workflowRunSelect').value = selected;
}

async function control(action, input) {
  if (busy || !selected) return;
  busy = true;
  const version = viewVersion;
  try {
    const run = await api('/admin/workflows/' + selected + '/' + action,
      {method: 'POST', timeout:20000, body: JSON.stringify(input || {})});
    if (run.id === selected && version === viewVersion) {
      renderRun(run, true);
      byId('workflowConnection').textContent = '';
    }
  } catch (error) {
    if (version === viewVersion) byId('workflowConnection').textContent =
      t(error.message) + ' Bấm Làm mới để xem trạng thái đã lưu.';
  } finally {
    busy = false;
    setPoll();
  }
}

async function start(event) {
  event.preventDefault();
  if (busy) return;
  busy = true;
  byId('workflowStart').disabled = true;
  const status = byId('workflowDraftStatus');
  status.textContent = 'Đang lưu luồng…';
  const version = viewVersion;
  let id;
  try {
    const config = {...draft, title: draft.title.trim() || 'Luồng mới'};
    const raw = JSON.stringify(config);
    const pending = readLocal('pending', {});
    id = pending.raw === raw && pending.id ? pending.id : Array.from(
      crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
    writeLocal('pending', {id, raw, lookupUntil: Date.now() + 30000});
    const run = await api('/admin/workflows', {method:'POST', timeout:20000, body:JSON.stringify({...config, id})});
    if (version !== viewVersion) return;
    writeLocal('pending', null);
    selectRun(run.id);
    renderRun(run, true);
    try {
      await loadHistory();
      if (status.textContent === 'Đang lưu luồng…') status.textContent = '';
    } catch {
      if (version === viewVersion) status.textContent =
        'Luồng đã được lưu. Bấm Làm mới để cập nhật danh sách.';
    }
  } catch (error) {
    if ([400, 401, 403, 422].includes(error.status) && readLocal('pending', {}).id === id) writeLocal('pending', null);
    if (version === viewVersion) status.textContent = t(error.message);
  } finally {
    busy = false;
    byId('workflowStart').disabled = false;
    setPoll();
  }
}

function initialize() {
  loaded = true;
  const saved = readLocal('draft', {});
  draft = structuredClone(defaults);
  for (const key of ['title', 'input', 'context', 'split']) {
    if (typeof saved[key] === typeof draft[key]) draft[key] = saved[key];
  }
  if (Array.isArray(saved.steps)) {
    const template = defaults.steps[0];
    const steps = saved.steps.slice(0, 10).filter(step => step && Object.hasOwn(kinds, step.kind)).map(step => {
      const restored = {...template};
      for (const [key, value] of Object.entries(template)) {
        if (key !== 'duration' && typeof step[key] === typeof value) restored[key] = step[key];
      }
      if (!ratios.includes(restored.aspect_ratio)) restored.aspect_ratio = '';
      if (Number.isInteger(step.duration) && step.duration >= 1 && step.duration <= 600) restored.duration = step.duration;
      return restored;
    });
    if (steps.length) draft.steps = steps;
  }
  selected = readLocal('selection', null);
  for (const field of ['Title', 'Input', 'Context', 'Split']) {
    const input = byId('workflow' + field);
    const key = field.toLowerCase();
    input.oninput = () => {
      draft[key] = input.type === 'checkbox' ? input.checked : input.value;
      writeLocal('draft', draft);
    };
  }
  byId('workflowAdd').onclick = () => {
    draft.steps.push({...defaults.steps[0], name: 'Bước ' + (draft.steps.length + 1)});
    writeLocal('draft', draft);
    renderSteps();
    byId('workflowSteps').lastElementChild.querySelector('input').focus();
  };
  byId('workflowForm').onsubmit = start;
  byId('workflowPause').onclick = () => control('pause');
  byId('workflowResume').onclick = () => control('resume');
  byId('workflowUseConfig').onclick = () => {
    draft = structuredClone(lastRun.config);
    delete draft.id;
    showDraft();
    writeLocal('draft', draft);
    byId('workflowTitle').focus();
  };
  byId('workflowRunSelect').onchange = async event => {
    if (busy) {
      event.target.value = selected;
      return;
    }
    selectRun(event.target.value);
    const version = viewVersion;
    try { await refreshRun(true); }
    catch (error) {
      if (version === viewVersion) byId('workflowConnection').textContent = t(error.message);
    }
    finally { setPoll(); }
  };
  window.addEventListener('online', () => {
    if (byId('tab-workflows').classList.contains('active')) loadWorkflows();
  });
  window.addEventListener('mac2api:reset', () => {
    viewVersion++;
    clearTimeout(timer);
    lastRun = undefined;
    historyUnavailable = false;
    models = [];
    renderSteps();
    for (const id of ['workflowRunSelect', 'workflowItems', 'workflowResults']) byId(id).replaceChildren();
    for (const id of ['workflowHistory', 'workflowRunState', 'workflowProgress', 'workflowActions']) byId(id).hidden = true;
    byId('workflowRunStatus').textContent = 'Nhập khóa quản trị để xem tiến độ và kết quả.';
    byId('workflowConnection').textContent = '';
  });
  showDraft();
}

export async function loadWorkflows() {
  if (!loaded) initialize();
  if (authOK === false) return;
  const version = viewVersion;
  try {
    await loadHistory({recoverPending: !busy});
    if (version !== viewVersion || authOK === false) return;
    await refreshRun(true);
    if (version !== viewVersion) return;
    historyUnavailable = false;
    byId('workflowConnection').textContent = '';
    if (!models.length && version === viewVersion) {
      const response = await api('/v1/models');
      if (version !== viewVersion) return;
      models = response.data.filter(model => model.id.includes('/')).map(model => ({
        id: model.id,
        type: model.type || (['openai', 'gemini'].includes(model.owned_by) ? 'text' : ''),
      }));
      renderSteps();
    }
  } catch {
    if (version === viewVersion) {
      historyUnavailable = true;
      byId('workflowConnection').textContent =
        'Chưa đọc được luồng đã lưu. Đang thử kết nối lại…';
    }
  } finally { setPoll(); }
}
