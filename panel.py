import os, sys, json, time, shutil, signal, sqlite3, base64, threading, subprocess, fcntl
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SRC, DB, LOGF = "/downloads", "/config/musiclibrary.db", "/config/import-all.log"
MB_RUN, MB_DONE, MB_LOG = "/config/mb.running", "/config/mb.done", "/config/import-mb.log"
PASS = os.environ.get("PANEL_PASS", "")
BEET = shutil.which("beet") or "/lsiopy/bin/beet"
SKIP_DIRS = {"@eaDir", "#recycle"}

# задача: (название, аргументы beet, нужна ли папка)
JOBS = {
    "import_tags":   ("Импорт по существующим тегам", ["import", "-A", "-q", "-l", LOGF], True),
    "import_mb":     ("Импорт с поиском в MusicBrainz (тихий)", ["import", "-q", "-l", LOGF], True),
    "import_single": ("Импорт отдельных треков по отпечатку", ["import", "-s", "-q", "-l", LOGF], True),
    "stats":         ("Статистика библиотеки", ["stats"], False),
    "duplicates":    ("Поиск дубликатов", ["duplicates"], False),
    "no_artist":     ("Альбомы без исполнителя", ["ls", "-a", "albumartist::^$"], False),
    "fetchart":      ("Загрузка недостающих обложек", ["fetchart"], False),
    "albums":        ("Список альбомов", ["ls", "-a", "-f", "$albumartist — $album"], False),
}

lock = threading.Lock()
state = {"proc": None, "name": "", "started": 0.0, "lock_handle": None}
lines = deque(maxlen=600)
samples = deque()
total = {"n": None}


def count_src():
    total["n"] = None
    n = 0
    for root, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        n += sum(f.lower().endswith((".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma", ".ape", ".wv")) for f in files)
    total["n"] = n


def db_counts():
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        try:
            return (con.execute("select count(*) from items").fetchone()[0],
                    con.execute("select count(*) from albums").fetchone()[0])
        finally:
            con.close()
    except Exception:
        return 0, 0


def tail(path, n=12):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


def read_first(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def mb_status():
    """Прогресс перетегирования через MusicBrainz (запуск: muzick.sh mb-all)."""
    out = {"running": os.path.exists(MB_RUN), "started": None, "rc": None, "idle": None,
           "albums": 0, "matched": 0, "skipped": 0, "pct": 0, "log": tail(MB_LOG, 8)}
    first = read_first(MB_RUN)
    if first:
        try:
            out["started"] = float(first.split()[0])
        except ValueError:
            pass
    if not out["running"]:
        out["rc"] = read_first(MB_DONE)
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        try:
            out["albums"] = con.execute("select count(*) from albums").fetchone()[0]
            out["matched"] = con.execute(
                "select count(*) from albums where coalesce(mb_albumid,'') <> ''").fetchone()[0]
        finally:
            con.close()
    except Exception:
        pass
    try:
        mtimes = [os.path.getmtime(p) for p in (DB, MB_LOG) if os.path.exists(p)]
        if mtimes:
            out["idle"] = max(0, time.time() - max(mtimes))
    except OSError:
        pass
    if out["albums"]:
        out["pct"] = min(100, out["matched"] * 100 / out["albums"])
    return out


def reader(proc):
    for raw in proc.stdout:
        lines.append(raw.rstrip("\n"))
    proc.wait()
    lines.append("— завершено, код %s —" % proc.returncode)
    handle = None
    with lock:
        handle = state.get("lock_handle")
        state["lock_handle"] = None
    if handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def start(job, folder):
    if job not in JOBS:
        return {"error": "неизвестная задача"}, 400
    title, args, needs_path = JOBS[job]
    args = list(args)
    if needs_path:
        path = os.path.realpath(os.path.join(SRC, folder)) if folder else SRC
        if os.path.commonpath([path, SRC]) != SRC or not os.path.isdir(path):
            return {"error": "недопустимая папка"}, 400
        args.append(path)
        title += " — " + (folder or "вся коллекция")
    with lock:
        p = state["proc"]
        if p and p.poll() is None:
            return {"error": "уже выполняется другая задача"}, 409
        handle = open("/config/.muzick.lock", "a", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return {"error": "Другая задача работает с базой beets через CLI или панель"}, 409
        try:
            lines.clear()
            lines.append("$ beet " + " ".join(args))
            proc = subprocess.Popen([BEET] + args, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    bufsize=1, start_new_session=True)
            state.update(proc=proc, name=title, started=time.time(), lock_handle=handle)
        except Exception:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            raise
    threading.Thread(target=reader, args=(proc,), daemon=True).start()
    return {"ok": True}, 200


def stop():
    p = state["proc"]
    if p and p.poll() is None:
        os.killpg(p.pid, signal.SIGTERM)
        return {"ok": True}, 200
    return {"error": "нечего останавливать"}, 400


def status():
    items, albums = db_counts()
    now = time.time()
    if not samples or now - samples[-1][0] > 5:
        samples.append((now, items))
    while samples and now - samples[0][0] > 600:
        samples.popleft()
    rate = 0.0
    if len(samples) > 1 and now > samples[0][0]:
        rate = (items - samples[0][1]) / (now - samples[0][0]) * 60
    t = total["n"]
    p = state["proc"]
    running = bool(p and p.poll() is None)
    eta = None
    if t and rate > 0 and items < t:
        eta = round((t - items) / rate)
    return {"total": t, "items": items, "albums": albums,
            "pct": min(100, items * 100 / t) if t else 0,
            "rate": round(rate), "eta": eta,
            "job": {"name": state["name"], "running": running,
                    "rc": None if (running or not p) else p.returncode,
                    "started": state["started"]},
            "log": list(lines), "skipped": tail(LOGF), "mb": mb_status()}


def folders():
    try:
        return sorted(d for d in os.listdir(SRC)
                      if os.path.isdir(os.path.join(SRC, d))
                      and d not in SKIP_DIRS and not d.startswith("."))
    except OSError:
        return []


PAGE = r"""<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Панель beets</title>
<style>
body{font-family:system-ui,sans-serif;max-width:860px;margin:1.5em auto;padding:0 1em;color:#222}
h2{margin:.2em 0}h3{margin:1.2em 0 .4em}
.card{border:1px solid #ddd;border-radius:8px;padding:12px 16px;margin:12px 0}
.bar{background:#e3e3e3;border-radius:6px;overflow:hidden}
.bar div{background:#2e9e6a;color:#fff;padding:4px 8px;white-space:nowrap;min-width:3em}
button{padding:7px 12px;margin:3px 3px 3px 0;border:1px solid #888;border-radius:6px;background:#f6f6f6;cursor:pointer}
button:hover:enabled{background:#e8e8e8}button:disabled{opacity:.45;cursor:default}
button.stop{border-color:#c33;color:#a00}
select{padding:6px;max-width:100%}
pre{background:#111;color:#ddd;padding:10px;border-radius:6px;height:260px;overflow:auto;font-size:12px;white-space:pre-wrap}
pre.small{background:#f4f4f4;color:#222;height:auto;max-height:140px}
.muted{color:#666;font-size:13px}
</style>
<h2>Панель управления beets</h2>

<div class="card">
  <div class="bar"><div id="bar" style="width:0%">0%</div></div>
  <p id="nums" class="muted">загрузка…</p>
  <p id="job" class="muted"></p>
</div>

<div class="card" id="mbbox">
  <h3 style="margin-top:0">Перетегирование через MusicBrainz (VPN)</h3>
  <div class="bar"><div id="mbbar" style="width:0%">0%</div></div>
  <p id="mbnums" class="muted"></p>
  <p id="mbstate" class="muted"></p>
  <pre id="mblog" class="small"></pre>
</div>

<div class="card">
  <h3 style="margin-top:0">Импорт</h3>
  <label>Папка: <select id="folder"><option value="">(вся коллекция)</option></select></label>
  <button onclick="loadFolders()">↻</button>
  <p class="muted" style="margin:6px 0">Файлы копируются в чистую библиотеку, исходники не меняются.</p>
  <button class="run" onclick="run('import_tags',true)">Импорт по тегам</button>
  <button class="run" onclick="run('import_mb',true)">Импорт + MusicBrainz</button>
  <button class="run" onclick="run('import_single',true)">Треки по отпечатку</button>
  <button class="stop" id="stopbtn" onclick="stopJob()" disabled>■ Остановить</button>
</div>

<div class="card">
  <h3 style="margin-top:0">Инструменты</h3>
  <button class="run" onclick="run('stats')">Статистика</button>
  <button class="run" onclick="run('albums')">Список альбомов</button>
  <button class="run" onclick="run('duplicates')">Дубликаты</button>
  <button class="run" onclick="run('no_artist')">Без исполнителя</button>
  <button class="run" onclick="run('fetchart')">Обложки</button>
  <button onclick="recount()">Пересчитать исходные mp3</button>
</div>

<h3>Вывод задачи</h3><pre id="log"></pre>
<h3>Журнал импорта (import-all.log)</h3><pre id="skipped" class="small"></pre>

<script>
const $ = id => document.getElementById(id);
async function api(p, body){
  const r = await fetch(p, {method: body ? 'POST' : 'GET',
    headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return r.json();
}
async function loadFolders(){
  const list = await api('/api/folders'); const sel = $('folder'); const cur = sel.value;
  sel.innerHTML = '<option value="">(вся коллекция)</option>' +
    list.map(f => '<option>' + f.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</option>').join('');
  sel.value = cur;
}
async function run(job, withFolder){
  const folder = withFolder ? $('folder').value : '';
  if (withFolder && !confirm('Запустить: ' + job + ' для «' + (folder || 'всей коллекции') + '»?')) return;
  const r = await api('/api/run', {job, folder}); if (r.error) alert(r.error); refresh();
}
async function stopJob(){ if (confirm('Остановить текущую задачу?')) { await api('/api/stop', {}); refresh(); } }
async function recount(){ await api('/api/recount', {}); refresh(); }
async function refresh(){
  let s; try { s = await api('/api/status'); } catch(e) { return; }
  $('bar').style.width = s.pct.toFixed(1) + '%'; $('bar').textContent = s.pct.toFixed(1) + '%';
  const eta = s.eta != null ? ', осталось ≈ ' + s.eta + ' мин' : '';
  $('nums').textContent = 'Треков в библиотеке: ' + s.items + ' из ≈ ' + (s.total ?? 'считаю…') + ' аудиофайлов в источнике · альбомов: ' + s.albums + ' · скорость: ' + s.rate + ' треков/мин' + eta;
  const j = s.job;
  $('job').textContent = !j.name ? 'Задач ещё не запускали' :
    (j.running ? '▶ Выполняется: ' : '✓ Последняя: ') + j.name + (j.rc != null ? ' (код ' + j.rc + ')' : '');
  document.querySelectorAll('button.run').forEach(b => b.disabled = j.running);
  $('stopbtn').disabled = !j.running;
  const log = $('log'), atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  log.textContent = s.log.join('\n'); if (atEnd) log.scrollTop = log.scrollHeight;
  $('skipped').textContent = s.skipped || '(пусто)';
  const m = s.mb;
  $('mbbar').style.width = m.pct.toFixed(1) + '%'; $('mbbar').textContent = m.pct.toFixed(1) + '%';
  $('mbnums').textContent = 'Альбомов с MusicBrainz ID: ' + m.matched + ' из ' + m.albums +
    ' · это показатель привязки, не точный процент обработанных альбомов';
  const idle = m.idle != null ? ' · последняя активность ' + Math.round(m.idle / 60) + ' мин назад' : '';
  $('mbstate').textContent = m.running ? '▶ Идёт перетегирование' + idle :
    (m.rc !== null ? '✓ Завершено (код ' + m.rc + ')' : 'Не запущено');
  $('mblog').textContent = m.log || '(лог пока пуст)';
}
loadFolders(); refresh(); setInterval(refresh, 2000);
</script></html>"""


class H(BaseHTTPRequestHandler):
    def _auth(self):
        if not PASS:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                if base64.b64decode(h[6:]).decode().split(":", 1)[1] == PASS:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="beets"')
        self.end_headers()
        return False

    def _send(self, body, ctype="application/json; charset=utf-8", code=200):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False), code=code)

    def do_GET(self):
        if not self._auth():
            return
        if self.path == "/api/status":
            self._json(status())
        elif self.path == "/api/folders":
            self._json(folders())
        else:
            self._send(PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        if not self._auth():
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        if self.path == "/api/run":
            res, code = start(body.get("job", ""), body.get("folder", ""))
        elif self.path == "/api/stop":
            res, code = stop()
        elif self.path == "/api/recount":
            threading.Thread(target=count_src, daemon=True).start()
            res, code = {"ok": True}, 200
        else:
            res, code = {"error": "not found"}, 404
        self._json(res, code)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=count_src, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8338), H).serve_forever()
