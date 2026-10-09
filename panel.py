import os, sys, json, time, shutil, signal, sqlite3, base64, threading, subprocess, fcntl
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SRC, WORK = "/downloads", "/work"
DB, DIRECT_DB, LOGF = "/config/musiclibrary.db", "/config/musiclibrary-direct.db", "/config/import-all.log"
DIRECT_CONFIG = "/config/config-direct.yaml"
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

DIRECT_JOBS = {
    "direct_tags": ("Работа в указанной папке без копирования", ["import", "-A", "-q", "-l", "/config/direct-import.log"], True),
    "direct_mb": ("Работа в папке без копирования + MusicBrainz", ["import", "-q", "-l", "/config/direct-import.log"], True),
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


def db_counts(db=DB):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
        try:
            return (con.execute("select count(*) from items").fetchone()[0],
                    con.execute("select count(*) from albums").fetchone()[0])
        finally:
            con.close()
    except Exception:
        return 0, 0


def db_health():
    out = {"no_artist": 0, "no_title": 0, "no_album": 0, "no_albumartist": 0,
           "duplicate_track_groups": 0, "duplicate_album_groups": 0,
           "missing_mb_trackid": 0, "missing_mb_albumid": 0}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        try:
            row = con.execute("""select
              sum(case when trim(coalesce(artist,''))='' then 1 else 0 end),
              sum(case when trim(coalesce(title,''))='' then 1 else 0 end),
              sum(case when trim(coalesce(album,''))='' then 1 else 0 end),
              sum(case when trim(coalesce(albumartist,''))='' then 1 else 0 end),
              sum(case when trim(coalesce(mb_trackid,''))='' then 1 else 0 end),
              sum(case when trim(coalesce(mb_albumid,''))='' then 1 else 0 end)
              from items""").fetchone()
            for key, value in zip(("no_artist", "no_title", "no_album", "no_albumartist",
                                   "missing_mb_trackid", "missing_mb_albumid"), row):
                out[key] = value or 0
            out["duplicate_track_groups"] = con.execute("""select count(*) from (
              select 1 from items where trim(coalesce(artist,''))<>'' and trim(coalesce(title,''))<>''
              group by lower(trim(artist)), lower(trim(title)) having count(*)>1)""").fetchone()[0]
            out["duplicate_album_groups"] = con.execute("""select count(*) from (
              select 1 from albums where trim(coalesce(albumartist,''))<>'' and trim(coalesce(album,''))<>''
              group by lower(trim(albumartist)), lower(trim(album)) having count(*)>1)""").fetchone()[0]
        finally:
            con.close()
    except Exception:
        pass
    return out


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
    direct = job in DIRECT_JOBS
    table = DIRECT_JOBS if direct else JOBS
    if job not in table:
        return {"error": "неизвестная задача"}, 400
    title, args, needs_path = table[job]
    args = list(args)
    root = os.path.realpath(WORK if direct else SRC)
    if needs_path:
        try:
            path = os.path.realpath(os.path.join(root, folder)) if folder else root
            if os.path.commonpath([path, root]) != root or not os.path.isdir(path):
                return {"error": "папка должна находиться внутри разрешённого корня"}, 400
        except (ValueError, OSError):
            return {"error": "недопустимая папка"}, 400
        args.append(path)
        title += " — " + (folder or "вся папка")
    command = [BEET, "-c", DIRECT_CONFIG] + args if direct else [BEET] + args
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
            lines.append("$ " + " ".join(command))
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL,
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
    items, albums = db_counts(DB)
    direct_items, direct_albums = db_counts(DIRECT_DB)
    now = time.time()
    if not samples or now - samples[-1][0] > 5:
        samples.append((now, items))
    while samples and now - samples[0][0] > 600:
        samples.popleft()
    rate = 0.0
    if len(samples) > 1 and now > samples[0][0]:
        rate = (items - samples[0][1]) / (now - samples[0][0]) * 60
    p = state["proc"]
    running = bool(p and p.poll() is None)
    return {"items": items, "albums": albums, "rate": round(rate),
            "job": {"name": state["name"], "running": running,
                    "rc": None if (running or not p) else p.returncode,
                    "started": state["started"]},
            "log": list(lines), "import_log": tail(LOGF), "mb": mb_status(),
            "health": db_health(), "direct_items": direct_items, "direct_albums": direct_albums,
            "direct_log": tail("/config/direct-import.log")}


def folders(mode="import"):
    root = os.path.realpath(WORK if mode == "direct" else SRC)
    found = []
    try:
        for current, dirs, _files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for d in dirs:
                found.append(os.path.relpath(os.path.join(current, d), root))
                if len(found) >= 3000:
                    return sorted(found)
    except OSError:
        return []
    return sorted(found)


PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><title>Muzick · beets</title>
<style>
:root{color-scheme:light dark;--bg:#f4f6fa;--card:#fff;--ink:#182230;--muted:#697586;--line:#e3e8ef;--accent:#5b5bd6;--soft:#eeefff;--good:#13795b;--warn:#a45b00;--danger:#b42318;--shadow:0 8px 28px rgba(24,34,48,.06)}
@media(prefers-color-scheme:dark){:root{--bg:#10151d;--card:#171f2a;--ink:#e8edf5;--muted:#a4afbf;--line:#2a3544;--accent:#a5a5ff;--soft:#282845;--good:#58d5a3;--warn:#ffc078;--danger:#ff8a80;--shadow:none}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:1160px;margin:auto;padding:28px 20px 56px}.hero{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:24px}.brand{display:flex;align-items:center;gap:13px}.logo{width:48px;height:48px;border-radius:15px;background:var(--accent);color:white;display:grid;place-items:center;font-size:25px;font-weight:800}
h1{font-size:clamp(25px,4vw,34px);line-height:1.15;margin:0;letter-spacing:-.04em}h2{font-size:19px;margin:0 0 5px;letter-spacing:-.02em}.sub{color:var(--muted);margin-top:4px}.pill{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:99px;padding:7px 11px;color:var(--muted);font-size:12px;white-space:nowrap}.dot{width:7px;height:7px;border-radius:50%;background:var(--good)}
.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:16px}.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:20px;box-shadow:var(--shadow);min-width:0}.span-12{grid-column:span 12}.span-8{grid-column:span 8}.span-6{grid-column:span 6}.span-4{grid-column:span 4}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.stat{padding:15px;border:1px solid var(--line);border-radius:13px}.stat .num{font-size:clamp(23px,3vw,30px);font-weight:750;letter-spacing:-.04em}.stat .label{font-size:12px;color:var(--muted)}
.headrow{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:16px}.muted{color:var(--muted);font-size:13px}.hint{border-radius:12px;background:var(--soft);padding:11px 13px;color:var(--muted);font-size:13px;margin-top:14px}
label{display:block;font-size:13px;font-weight:650;margin:12px 0 6px}select{width:100%;min-width:0;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:11px 12px;font:inherit}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}button{appearance:none;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:10px 13px;border-radius:10px;font:600 13px system-ui;cursor:pointer;transition:transform .12s,background .12s}button:hover:enabled{border-color:var(--accent);transform:translateY(-1px)}button:disabled{opacity:.45;cursor:not-allowed}.primary{background:var(--accent);border-color:var(--accent);color:white}.stop{color:var(--danger)}.badge{display:inline-block;border-radius:7px;background:var(--soft);color:var(--accent);font-size:11px;padding:3px 7px;font-weight:700}
.health-list{display:grid;grid-template-columns:1fr 1fr;gap:9px}.health-item{display:flex;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px solid var(--line);font-size:13px}.health-item strong{font-variant-numeric:tabular-nums}
.progress{height:7px;border-radius:99px;background:var(--line);overflow:hidden;margin:12px 0}.progress>div{height:100%;width:0;background:var(--accent);transition:width .3s}
pre{margin:10px 0 0;background:#0b1020;color:#dce5f7;padding:14px;border-radius:12px;min-height:100px;max-height:290px;overflow:auto;font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere}.logsmall{min-height:60px;max-height:160px}details summary{cursor:pointer;font-weight:650}.foot{color:var(--muted);font-size:12px;margin-top:18px}
@media(max-width:800px){.span-8,.span-6,.span-4{grid-column:span 12}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.hero{align-items:flex-start;flex-direction:column}.wrap{padding:18px 12px 36px}}
@media(max-width:440px){.card{padding:15px;border-radius:14px}.health-list{grid-template-columns:1fr}.stats{gap:8px}.stat{padding:12px}}
</style></head><body><main class="wrap">
<header class="hero"><div class="brand"><div class="logo">♫</div><div><h1>Muzick <span style="color:var(--accent)">/ beets</span></h1><div class="sub">Управление музыкальной библиотекой</div></div></div><div class="pill"><span class="dot"></span><span id="connection">Проверка подключения…</span></div></header>
<section class="grid">
<div class="card span-12"><div class="headrow"><div><h2>Обзор библиотеки</h2><div class="muted">Состояние основной базы beets</div></div><button onclick="refresh()">↻ Обновить</button></div>
<div class="stats"><div class="stat"><div class="num" id="items">—</div><div class="label">Треков</div></div><div class="stat"><div class="num" id="albums">—</div><div class="label">Альбомов</div></div><div class="stat"><div class="num" id="directitems">—</div><div class="label">Треков в режиме «на месте»</div></div><div class="stat"><div class="num" id="rate">—</div><div class="label">Изменение записей/мин</div></div></div>
<div class="hint" id="job">Загрузка состояния задачи…</div></div>

<div class="card span-6"><div class="headrow"><div><h2>1. Импорт в отдельную библиотеку</h2><div class="muted">Исходники доступны только для чтения; файлы копируются в целевую библиотеку.</div></div><span class="badge">КОПИЯ</span></div>
<label for="srcfolder">Папка в исходной коллекции</label><select id="srcfolder"><option value="">Вся коллекция</option></select><div class="actions"><button class="primary run" onclick="run('import_tags','import')">Импорт по тегам</button><button class="run" onclick="run('import_mb','import')">С MusicBrainz</button><button class="run" onclick="run('import_single','import')">По отпечатку</button><button onclick="loadFolders('import')">Обновить папки</button></div></div>

<div class="card span-6"><div class="headrow"><div><h2>2. Работа в выбранной папке</h2><div class="muted">Файлы остаются в исходных каталогах. Теги могут записываться непосредственно в файлы.</div></div><span class="badge">БЕЗ КОПИИ</span></div>
<label for="directfolder">Папка внутри разрешённого корня</label><select id="directfolder"><option value="">Корневая папка</option></select><div class="actions"><button class="primary run" onclick="run('direct_tags','direct')">Обработать на месте по тегам</button><button class="run" onclick="run('direct_mb','direct')">Обработать на месте + MusicBrainz</button><button onclick="loadFolders('direct')">Обновить папки</button></div><div class="hint">Используется отдельная база. Перед записью тегов проверьте права доступа и сделайте резервную копию файлов.</div></div>

<div class="card span-8"><div class="headrow"><div><h2>Состояние задачи</h2><div class="muted">Последние сообщения процесса</div></div><button class="stop" id="stopbtn" onclick="stopJob()" disabled>Остановить</button></div><pre id="log">Задачи ещё не запускались.</pre></div>

<div class="card span-4"><h2>Качество метаданных</h2><div class="muted">Проверка основной базы</div><div class="health-list" style="margin-top:12px">
<div class="health-item"><span>Без исполнителя</span><strong id="noartist">—</strong></div><div class="health-item"><span>Без названия</span><strong id="notitle">—</strong></div><div class="health-item"><span>Без альбома</span><strong id="noalbum">—</strong></div><div class="health-item"><span>Без исполнителя альбома</span><strong id="noalbumartist">—</strong></div><div class="health-item"><span>Группы совпадений треков</span><strong id="duptracks">—</strong></div><div class="health-item"><span>Группы совпадений альбомов</span><strong id="dupalbums">—</strong></div><div class="health-item"><span>Без MusicBrainz Track ID</span><strong id="nombtrack">—</strong></div><div class="health-item"><span>Без MusicBrainz Album ID</span><strong id="nombalbum">—</strong></div></div><p class="muted">Совпадение названий — повод проверить записи, а не автоматическая команда на удаление.</p></div>

<div class="card span-6"><h2>Инструменты</h2><div class="muted">Операции с основной базой</div><div class="actions"><button class="run" onclick="run('stats','tool')">Статистика beets</button><button class="run" onclick="run('albums','tool')">Список альбомов</button><button class="run" onclick="run('duplicates','tool')">Дубликаты beets</button><button class="run" onclick="run('no_artist','tool')">Без исполнителя</button><button class="run" onclick="run('fetchart','tool')">Загрузить обложки</button></div></div>

<div class="card span-6"><h2>MusicBrainz</h2><div class="muted">Привязка альбомов к MusicBrainz в основной базе</div><div class="progress"><div id="mbbar"></div></div><div id="mbnums" class="muted">Загрузка…</div><div id="mbstate" class="muted"></div><details style="margin-top:12px"><summary>Журнал MusicBrainz</summary><pre id="mblog" class="logsmall"></pre></details></div>

<div class="card span-12"><details><summary>Журнал импорта</summary><pre id="importlog" class="logsmall"></pre><details style="margin-top:10px"><summary>Журнал обработки без копирования</summary><pre id="directlog" class="logsmall"></pre></details></details></div>
</section><div class="foot">Режим «без копии» работает только в разрешённом каталоге /work. Не публикуйте панель по открытому HTTP в Интернете.</div>
</main>
<script>
const $=id=>document.getElementById(id);
const fmt=n=>Number(n||0).toLocaleString('ru-RU');
async function api(p,body){const r=await fetch(p,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});let d={};try{d=await r.json()}catch(e){}if(!r.ok&&!d.error)d.error='HTTP '+r.status;return d}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
async function loadFolders(mode){const sel=$(mode==='direct'?'directfolder':'srcfolder');const cur=sel.value;const list=await api('/api/folders?mode='+mode);if(list.error)return;sel.innerHTML='<option value="">'+(mode==='direct'?'Корневая папка':'Вся коллекция')+'</option>'+list.map(f=>'<option value="'+esc(f)+'">'+esc(f)+'</option>').join('');sel.value=cur}
async function run(job,mode){const folder=mode==='direct'?$('directfolder').value:mode==='import'?$('srcfolder').value:'';const label=mode==='direct'?'без копирования':mode==='import'?'импорт с копированием':'инструмент';if(!confirm('Запустить «'+job+'» ('+label+')'+(folder?' для папки «'+folder+'»':'')+'?'))return;const r=await api('/api/run',{job,folder});if(r.error)alert(r.error);await refresh()}
async function stopJob(){if(confirm('Остановить текущую задачу?')){const r=await api('/api/stop',{});if(r.error)alert(r.error);await refresh()}}
async function refresh(){let s;try{s=await api('/api/status')}catch(e){$('connection').textContent='Нет соединения';return}
$('connection').textContent='Панель подключена';$('items').textContent=fmt(s.items);$('albums').textContent=fmt(s.albums);$('directitems').textContent=fmt(s.direct_items);$('rate').textContent=fmt(s.rate);
const j=s.job;$('job').textContent=!j.name?'Задач ещё не запускали':(j.running?'Выполняется: ':'Последняя задача: ')+j.name+(j.rc!=null?' · код завершения '+j.rc:'');
document.querySelectorAll('button.run').forEach(b=>b.disabled=j.running);$('stopbtn').disabled=!j.running;
const log=$('log'),atEnd=log.scrollTop+log.clientHeight>=log.scrollHeight-20;log.textContent=(s.log||[]).join(String.fromCharCode(10))||'Нет сообщений';if(atEnd)log.scrollTop=log.scrollHeight;
const h=s.health||{};for(const [id,key] of [['noartist','no_artist'],['notitle','no_title'],['noalbum','no_album'],['noalbumartist','no_albumartist'],['duptracks','duplicate_track_groups'],['dupalbums','duplicate_album_groups'],['nombtrack','missing_mb_trackid'],['nombalbum','missing_mb_albumid']])$(id).textContent=fmt(h[key]);
$('importlog').textContent=s.import_log||'(пусто)';$('directlog').textContent=s.direct_log||'(пусто)';
const m=s.mb||{};$('mbbar').style.width=Math.min(100,m.pct||0)+'%';$('mbnums').textContent=fmt(m.matched)+' альбомов с MusicBrainz ID из '+fmt(m.albums);$('mbstate').textContent=m.running?'Выполняется':'Не запущено / завершено';$('mblog').textContent=m.log||'(пусто)'}
loadFolders('import');loadFolders('direct');refresh();setInterval(refresh,2500);
</script></body></html>"""


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
        elif urlparse(self.path).path == "/api/folders":
            mode = parse_qs(urlparse(self.path).query).get("mode", ["import"])[0]
            self._json(folders("direct" if mode == "direct" else "import"))
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
