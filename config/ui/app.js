(() => {
'use strict';

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const nf = new Intl.NumberFormat('ru-RU');

let status = null;      // последний ответ /api/status
let cfg = null;         // настройки
let lastHash = '';      // чтобы не перерисовывать без изменений
let dismissedAt = 0;    // время завершения, баннер которого скрыли
let timer = null;
let pickerKind = '';
let pickerPath = '';


/* ---------------------------------------------------------------- сеть */

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Muzick': '1' },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* пустой ответ */ }
  if (!r.ok) throw new Error(data.error || ('Ошибка ' + r.status));
  return data;
}

let toastTimer = null;
function toast(msg, bad) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = 'toast'; }, bad ? 5000 : 2200);
}

/* ---------------------------------------------------------------- вспомогательное */

function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + ' сек';
  const m = Math.floor(sec / 60);
  if (m < 60) return m + ' мин';
  return Math.floor(m / 60) + ' ч ' + (m % 60) + ' мин';
}
function fmtWhen(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleString('ru-RU', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
}
const ICON = { pending: '·', idle: '·', running: '', waiting: '?', done: '✓', skipped: '–', error: '!' };

/* ---------------------------------------------------------------- вкладки */

function showTab(name) {
  if (!['home', 'settings', 'history'].includes(name)) name = 'home';
  $$('.view').forEach((v) => { v.hidden = v.id !== 'tab-' + name; });
  $$('.tab').forEach((t) => t.classList.toggle('is-active', t.dataset.tab === name));
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  if (name === 'history') loadHistory();
  if (name === 'settings') loadSettings();
}
$$('.tab').forEach((t) => t.addEventListener('click', () => showTab(t.dataset.tab)));
$('#link-settings').addEventListener('click', (e) => { e.preventDefault(); showTab('settings'); });

/* ---------------------------------------------------------------- главная */

function stepHtml(s, idx) {
  const off = s.enabled === false;
  const cls = off ? 'is-off' : 'is-' + s.status;
  const shown = (s.status !== 'idle' && s.status !== 'pending' && s.msg) ? s.msg : s.desc;
  let extra = '';
  if (s.status === 'running') {
    const indet = !s.pct;
    extra += '<div class="bar sm' + (indet ? ' indet' : '') + '"><div style="width:' + Math.round(s.pct || 0) + '%"></div></div>';
  }
  if (s.status === 'waiting') {
    extra += '<div class="confirm"><p>' + esc(s.msg) + '</p>';
    if (s.preview && s.preview.length) {
      extra += '<ul class="preview">' + s.preview.map((l) => '<li>' + esc(l) + '</li>').join('') + '</ul>';
    }
    extra += '<div class="confirm-actions">' +
      '<button class="btn btn-primary" type="button" data-confirm="1">Применить</button>' +
      '<button class="btn btn-ghost" type="button" data-confirm="0">Пропустить</button></div></div>';
  } else if (s.preview && s.preview.length && s.status === 'done') {
    extra += '<ul class="preview">' + s.preview.map((l) => '<li>' + esc(l) + '</li>').join('') + '</ul>';
  }
  const tag = off ? '<span class="step-tag">выключено</span>' : '';
  const body = (s.status === 'waiting') ? '' : '<div class="step-desc">' + esc(shown) + '</div>';
  return '<li class="step ' + cls + '"><span class="step-ic">' + ((s.status === 'idle' || s.status === 'pending') ? String(idx + 1) : (ICON[s.status] || '')) + '</span>' +
    '<div class="step-body"><div class="step-title">' + esc(s.title) + '</div>' + body + extra + '</div>' + tag + '</li>';
}

function renderHome(s) {
  const key = JSON.stringify([s.summary, s.todo, s.run, s.steps, s.health, s.plan, Math.round(s.overall), s.net_checking, dismissedAt]);
  if (key === lastHash) return;
  lastHash = key;

  $('#n-tracks').textContent = nf.format(s.summary.tracks);
  $('#n-albums').textContent = nf.format(s.summary.albums);
  $('#n-artists').textContent = nf.format(s.summary.artists);

  const run = s.run;
  const btn = $('#btn-main');
  if (run.active) {
    btn.textContent = 'Остановить';
    btn.className = 'btn btn-stop btn-xl';
    btn.disabled = false;
  } else {
    btn.textContent = s.summary.tracks ? 'Навести порядок' : 'Собрать мою музыку';
    btn.className = 'btn btn-primary btn-xl';
    btn.disabled = s.plan.count === 0;
  }
  let note;
  if (run.active) {
    note = run.awaiting ? 'Нужно ваше решение: посмотрите шаг ниже.' : 'Работаю. Страницу можно закрыть: процесс продолжится.';
  } else if (s.plan.count === 0) {
    note = 'Включите хотя бы один шаг в настройках.';
  } else {
    note = 'Шагов в плане: ' + s.plan.count + '. ' + (s.plan.confirm ? 'Перед правкой названий я спрошу вас.' : 'Правки применяются без вопросов.');
  }
  $('#plan-note').textContent = note;

  // баннер с итогом
  const b = $('#banner');
  if (!run.active && run.finished && run.finished !== dismissedAt) {
    const dur = fmtDur(run.finished - run.started);
    const texts = {
      ok: ['ok', 'Готово', 'Всё выполнено за ' + dur + '. Результат ниже.'],
      error: ['error', 'Не всё получилось', 'Один из шагов закончился ошибкой. Подробности ниже.'],
      cancelled: ['cancelled', 'Остановлено', 'Вы остановили работу. Сделанное сохранено.'],
    };
    const t = texts[run.result] || texts.ok;
    b.className = 'banner ' + t[0];
    b.innerHTML = '<div><b>' + t[1] + '</b>' + esc(t[2]) + '</div><button class="x" type="button" aria-label="Скрыть">×</button>';
    b.hidden = false;
    $('.x', b).addEventListener('click', () => { dismissedAt = run.finished; lastHash = ''; b.hidden = true; });
  } else {
    b.hidden = true;
  }

  // шаги
  $('#steps-title').textContent = (run.active || run.finished) ? (run.title || 'Ход работы') : 'Что будет сделано';
  const ov = $('#overall');
  ov.hidden = !(run.active || run.finished);
  $('#overall-bar').style.width = Math.round(s.overall) + '%';
  $('#steps').innerHTML = s.steps.map((st, i) => stepHtml(st, i)).join('');

  // что улучшить
  const todo = $('#todo');
  $('#todo-card').hidden = !s.todo.length;
  todo.innerHTML = s.todo.map((t) =>
    '<li><span class="todo-t">' + esc(t.text) + '</span>' +
    '<button class="btn btn-ghost btn-sm" type="button" data-fix="' + esc(t.step) + '"' + (run.active ? ' disabled' : '') + '>' +
    (t.step === 'report' ? 'Посмотреть' : 'Исправить') + '</button></li>').join('');

  // здоровье
  $('#health').innerHTML = s.health.map((h) =>
    '<li><span class="dot ' + esc(h.level) + '"></span><span><b>' + esc(h.title) + '</b> ' + esc(h.text) +
    (h.hint ? '<small>' + esc(h.hint) + '</small>' : '') + '</span></li>').join('');
  $('#btn-net').disabled = !!s.net_checking;
  $('#btn-net').textContent = s.net_checking ? 'Проверяю…' : 'Проверить подключение';
}

$('#btn-main').addEventListener('click', async () => {
  try {
    if (status && status.run.active) {
      if (!confirm('Остановить работу? Уже сделанное сохранится.')) return;
      await api('/api/cancel', {});
    } else {
      dismissedAt = status ? status.run.finished : 0;
      await api('/api/run', { all: true });
    }
    refresh();
  } catch (e) { toast(e.message, true); }
});

$('#steps').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-confirm]');
  if (!b) return;
  try { await api('/api/confirm', { answer: b.dataset.confirm === '1' }); refresh(); }
  catch (err) { toast(err.message, true); }
});

$('#todo').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-fix]');
  if (!b) return;
  try { dismissedAt = status ? status.run.finished : 0; await api('/api/run', { steps: [b.dataset.fix] }); refresh(); }
  catch (err) { toast(err.message, true); }
});

async function checkNet() {
  try { await api('/api/check-net', {}); toast('Проверяю подключение…'); refresh(); }
  catch (e) { toast(e.message, true); }
}
$('#btn-net').addEventListener('click', checkNet);
$('#btn-net2').addEventListener('click', checkNet);

/* ---------------------------------------------------------------- настройки */

async function loadSettings() {
  try { cfg = await api('/api/settings'); renderSettings(); } catch (e) { toast(e.message, true); }
}

function renderSettings() {
  if (!cfg) return;
  const s = cfg.settings;
  $('#set-steps').innerHTML = cfg.steps.map((st) =>
    '<label class="row"><span class="row-text"><b>' + esc(st.title) + '</b><small>' + esc(st.desc) + '</small></span>' +
    '<span class="switch"><input type="checkbox" data-step="' + esc(st.id) + '"' + (s.steps[st.id] ? ' checked' : '') +
    '><i></i></span></label>').join('');
  $$('input[data-key]').forEach((i) => { i.checked = !!s[i.dataset.key]; });
  $$('[data-radio]').forEach((g) => {
    $$('input', g).forEach((i) => { i.checked = i.value === s[g.dataset.radio]; });
  });
  if (document.activeElement !== $('#vpn-name')) $('#vpn-name').value = s.vpn_container;
  $('#sch-enabled').checked = !!s.schedule.enabled;
  $('#path-src').textContent = cfg.paths.source;
  $('#path-lib').textContent = cfg.paths.library;
  $('#path-docker').textContent = cfg.docker ? 'доступно (панель может работать через VPN)' : 'нет доступа (VPN-шаги недоступны)';
}

async function save(patch) {
  try { cfg = await api('/api/settings', patch); renderSettings(); toast('Сохранено'); lastHash = ''; }
  catch (e) { toast(e.message, true); }
}

async function openFolderPicker(kind) {
  pickerKind = kind; pickerPath = '';
  $('#picker-title').textContent = kind === 'source' ? 'Папка загрузок' : 'Музыкальная библиотека';
  $('#folder-picker').hidden = false;
  await renderFolderPicker();
}
async function renderFolderPicker() {
  const list = $('#picker-list');
  $('#picker-path').textContent = (pickerKind === 'source' ? '/downloads' : '/music') + (pickerPath ? '/' + pickerPath : '');
  list.innerHTML = '<div class="muted">Загрузка…</div>';
  try {
    const q = new URLSearchParams({ kind: pickerKind, path: pickerPath });
    const d = await api('/api/directories?' + q.toString());
    const rows = [];
    if (d.parent !== null) rows.push('<button class="folder-entry" data-folder-path="' + esc(d.parent) + '"><span>↰</span><span>На уровень выше</span></button>');
    for (const item of d.directories) rows.push('<button class="folder-entry" data-folder-path="' + esc(item.path) + '"><span>📁</span><span>' + esc(item.name) + '</span><span>›</span></button>');
    list.innerHTML = rows.length ? rows.join('') : '<div class="muted">Вложенных папок нет.</div>';
    $$('.folder-entry', list).forEach((b) => b.addEventListener('click', async () => { pickerPath = b.dataset.folderPath; await renderFolderPicker(); }));
  } catch (e) { list.innerHTML = '<div class="muted">' + esc(e.message) + '</div>'; }
}
$('#pick-source').addEventListener('click', () => openFolderPicker('source'));
$('#pick-library').addEventListener('click', () => openFolderPicker('library'));
$('#picker-close').addEventListener('click', () => { $('#folder-picker').hidden = true; });
$('#picker-select').addEventListener('click', async () => {
  try {
    cfg = await api('/api/folder-select', { kind: pickerKind, path: pickerPath });
    renderSettings(); $('#folder-picker').hidden = true;
    toast('Папка сохранена'); lastHash = '';
  } catch (e) { toast(e.message, true); }
});

$('#tab-settings').addEventListener('change', (e) => {
  const t = e.target;
  if (t.dataset.step) save({ steps: { [t.dataset.step]: t.checked } });
  else if (t.dataset.key) save({ [t.dataset.key]: t.checked });
  else if (t.type === 'radio') save({ [t.name]: t.value });
  else if (t.id === 'sch-enabled') save({ schedule: { enabled: t.checked } });
  else if (t.id === 'vpn-name') save({ vpn_container: t.value.trim() });
});

/* ---------------------------------------------------------------- история */

async function loadHistory() {
  const box = $('#history');
  try {
    const list = await api('/api/history');
    if (!list.length) { box.innerHTML = '<div class="empty">Здесь появится список выполненных работ.</div>'; return; }
    const labels = { ok: 'Готово', error: 'Ошибка', cancelled: 'Остановлено' };
    box.innerHTML = list.map((h) => {
      const steps = (h.steps || []).map((s) =>
        '<li><b>' + esc(s.title) + '</b>: ' + esc(({ done: 'сделано', skipped: 'пропущено', error: 'ошибка' })[s.status] || s.status) +
        (s.msg ? '<small>' + esc(s.msg) + '</small>' : '') +
        ((s.details && s.details.length) ? '<pre>' + esc(s.details.join('\n')) + '</pre>' : '') + '</li>').join('');
      return '<details><summary><span class="badge ' + esc(h.result) + '">' + esc(labels[h.result] || h.result) + '</span>' +
        '<span class="h-title">' + esc(h.title) + (h.trigger === 'auto' ? ' (по расписанию)' : '') + '</span>' +
        '<span class="h-when">' + esc(fmtWhen(h.ts)) + ' · ' + esc(fmtDur(h.duration)) + '</span></summary>' +
        '<ul class="h-steps">' + steps + '</ul></details>';
    }).join('');
  } catch (e) { box.innerHTML = '<div class="empty">' + esc(e.message) + '</div>'; }
}

/* ---------------------------------------------------------------- обновление */

async function refresh() {
  try {
    status = await api('/api/status');
    renderHome(status);
  } catch (e) { /* связь пропала: попробуем снова */ }
  clearTimeout(timer);
  timer = setTimeout(refresh, status && status.run.active ? 2000 : 5000);
}

$('#logo').addEventListener('error', function () {
  this.removeAttribute('src');
  this.style.visibility = 'hidden';
});

showTab((location.hash || '#home').slice(1));
refresh();
})();
