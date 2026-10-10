import os, sys, json, time, shutil, signal, sqlite3, base64, threading, subprocess, fcntl, mimetypes
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SRC, WORK = "/downloads", "/work"
WORK_LABEL = os.environ.get("WORK_DIR_HOST", "/work")
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
           "duplicate_track_groups": 0, "duplicate_same_album_groups": 0, "duplicate_album_groups": 0,
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
            out["duplicate_same_album_groups"] = con.execute("""select count(*) from (
              select 1 from items where trim(coalesce(artist,''))<>'' and trim(coalesce(title,''))<>''
                and trim(coalesce(album,''))<>''
              group by lower(trim(artist)), lower(trim(title)), lower(trim(album)) having count(*)>1)""").fetchone()[0]
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
            "direct_log": tail("/config/direct-import.log"), "work_root": WORK_LABEL}


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
<meta name="color-scheme" content="light"><meta name="theme-color" content="#f7f8fc">
<link rel="icon" href="/fav.ico/favicon.ico"><link rel="icon" type="image/png" sizes="32x32" href="/fav.ico/favicon-32x32.png"><link rel="apple-touch-icon" href="/fav.ico/apple-icon.png"><title>Muzick — музыкальная библиотека</title>
<style>
:root{--bg:#f6f7fb;--card:#fff;--ink:#1d2635;--muted:#687386;--line:#e5e9f1;--brand:#5267d8;--brand2:#394db8;--soft:#eef1ff;--good:#16805d;--warn:#a65c00;--bad:#b42318;--shadow:0 8px 26px rgba(27,39,69,.055}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}button,select{font:inherit}button{cursor:pointer}button:disabled{opacity:.45;cursor:not-allowed}
.shell{max-width:1120px;margin:auto;padding:24px 20px 48px}.top{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:22px}.brand{display:flex;align-items:center;gap:14px;min-width:0}.brand img{width:54px;height:54px;object-fit:contain;border-radius:14px;background:#fff}.brand h1{font-size:27px;line-height:1.1;letter-spacing:-.7px;margin:0}.brand p{margin:5px 0 0;color:var(--muted);font-size:13px}.connection{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px;border:1px solid var(--line);background:#fff;border-radius:99px;padding:8px 12px;white-space:nowrap}.led{width:8px;height:8px;border-radius:50%;background:#c5cad5}.led.ok{background:#22a06b}
.nav{display:flex;gap:6px;flex-wrap:wrap;padding:5px;background:#e9ecf4;border-radius:13px;margin-bottom:18px}.nav button{border:0;background:transparent;color:#586276;padding:10px 15px;border-radius:9px;font-weight:650;font-size:13px}.nav button.active{background:#fff;color:var(--brand2);box-shadow:0 2px 8px #1d263510}
.view{display:none}.view.active{display:block}.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:15px}.card{grid-column:span 12;background:var(--card);border:1px solid var(--line);border-radius:17px;padding:20px;min-width:0;box-shadow:var(--shadow)}.half{grid-column:span 6}.third{grid-column:span 4}.two-thirds{grid-column:span 8}
h2{font-size:18px;letter-spacing:-.3px;margin:0 0 5px}.lead{font-size:13px;color:var(--muted);margin:0}.cardhead{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:16px}.badge{font-size:10px;font-weight:800;letter-spacing:.5px;padding:5px 8px;border-radius:7px;background:var(--soft);color:var(--brand2);white-space:nowrap}
.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.stat{padding:14px;border:1px solid var(--line);border-radius:12px}.stat b{display:block;font-size:27px;letter-spacing:-.7px;line-height:1.2}.stat span{display:block;color:var(--muted);font-size:12px;margin-top:5px}
.note{margin-top:13px;padding:12px 14px;border-radius:11px;background:#f2f4fa;color:#596477;font-size:13px}.note.safe{background:#edf8f2;color:#216b4e}.note.warn{background:#fff5e8;color:#80500d}
label{display:block;font-size:13px;font-weight:700;margin:13px 0 6px}select{width:100%;padding:11px 12px;border:1px solid #d8deea;border-radius:10px;background:#fff;color:var(--ink);outline:none}select:focus{border-color:var(--brand);box-shadow:0 0 0 3px #5267d81a}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.btn{border:1px solid var(--line);background:#fff;color:var(--ink);border-radius:10px;padding:10px 13px;font-weight:650;font-size:13px}.btn:hover:enabled{border-color:#a9b4e9}.primary{background:var(--brand);color:#fff;border-color:var(--brand)}.primary:hover:enabled{background:var(--brand2)}.danger{color:var(--bad)}.bigaction{display:flex;align-items:center;gap:12px;text-align:left;width:100%;padding:14px;border:1px solid var(--line);border-radius:12px;background:#fff;color:var(--ink);margin-top:9px}.bigaction .symbol{display:grid;place-items:center;width:40px;height:40px;border-radius:11px;background:var(--soft);color:var(--brand2);font-size:19px;flex:0 0 auto}.bigaction strong{display:block;font-size:14px}.bigaction small{display:block;color:var(--muted);font-size:12px;font-weight:400;margin-top:2px}.bigaction:hover:enabled{border-color:#abb6eb;background:#fcfcff}
.statusbox{padding:13px 14px;background:#f3f5fa;border-radius:11px;font-size:13px}.statusbox strong{display:block;font-size:14px;margin-bottom:3px}.progress{height:7px;border-radius:99px;background:#e7eaf2;overflow:hidden;margin:12px 0}.progress div{height:100%;width:0;background:var(--brand);transition:width .3s}.quality{display:grid;grid-template-columns:1fr 1fr;gap:7px 14px}.quality div{display:flex;justify-content:space-between;gap:10px;padding:9px 0;border-bottom:1px solid var(--line);font-size:12px}.quality b{font-variant-numeric:tabular-nums}
.setting{display:flex;justify-content:space-between;align-items:center;gap:18px;padding:14px 0;border-bottom:1px solid var(--line)}.setting:last-child{border-bottom:0}.setting strong{display:block;font-size:14px}.setting small{display:block;color:var(--muted);font-size:12px;margin-top:3px;max-width:620px}.switch{appearance:none;width:42px;height:25px;border-radius:99px;background:#cbd1dd;position:relative;flex:0 0 auto;transition:background .2s}.switch:before{content:"";position:absolute;top:3px;left:3px;width:19px;height:19px;background:white;border-radius:50%;box-shadow:0 1px 3px #0002;transition:transform .2s}.switch:checked{background:var(--brand)}.switch:checked:before{transform:translateX(17px)}
pre{background:#101827;color:#e1e8f5;border-radius:11px;padding:13px;max-height:260px;min-height:75px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.5 ui-monospace,Consolas,monospace;margin:10px 0 0}.hidden{display:none!important}.foot{font-size:12px;color:var(--muted);margin-top:18px;text-align:center}.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);max-width:calc(100% - 28px);background:#202a3a;color:white;border-radius:11px;padding:12px 16px;font-size:13px;box-shadow:0 10px 35px #0002;z-index:10;display:none}
@media(max-width:760px){.shell{padding:15px 12px 34px}.top{align-items:flex-start;flex-direction:column;gap:12px}.brand h1{font-size:24px}.half,.third,.two-thirds{grid-column:span 12}.card{padding:16px}.nav{display:grid;grid-template-columns:1fr 1fr}.nav button{padding:10px 7px}.stats{grid-template-columns:repeat(3,minmax(0,1fr))}.stat{padding:10px}.stat b{font-size:22px}.cardhead{margin-bottom:12px}}
@media(max-width:420px){.stats{grid-template-columns:1fr 1fr}.quality{grid-template-columns:1fr}.connection{white-space:normal}}
</style></head><body><main class="shell">
<header class="top"><div class="brand"><img src="/fav.ico/logo.png" alt="Muzick" onerror="this.onerror=null;this.src='/fav.ico/apple-icon.png'"><div><h1>Muzick <span style="color:var(--brand)">/ beets</span></h1><p>Музыкальная библиотека без команд и терминала</p></div></div><div class="connection"><span id="led" class="led"></span><span id="connection">Подключаемся…</span></div></header>
<nav class="nav" aria-label="Главное меню"><button class="active" data-view="home">Главная</button><button data-view="music">Обработать музыку</button><button data-view="check">Проверка и обложки</button><button data-view="settings">Настройки</button></nav>
<section id="home" class="view active"><div class="grid">
<div class="card"><div class="cardhead"><div><h2>Ваша библиотека</h2><p class="lead">Основная библиотека, куда импортируются обработанные файлы</p></div><button class="btn" onclick="refresh()">↻ Обновить</button></div><div class="stats"><div class="stat"><b id="items">—</b><span>Треков</span></div><div class="stat"><b id="albums">—</b><span>Альбомов</span></div><div class="stat"><b id="directitems">—</b><span>Треков «на месте»</span></div></div><div id="job" class="note">Проверяем состояние…</div></div>
<div class="card half"><h2>Что вы хотите сделать?</h2><p class="lead">Выберите действие. Панель сама покажет ход выполнения.</p>
<button class="bigaction" onclick="showView('music')"><span class="symbol">♫</span><span><strong>Добавить и распознать музыку</strong><small>Импорт, исправление тегов и поиск по MusicBrainz</small></span></button>
<button class="bigaction" onclick="showView('check')"><span class="symbol">✓</span><span><strong>Проверить библиотеку</strong><small>Найти пропуски в тегах, проверить повторы и обложки</small></span></button>
<button class="bigaction" onclick="showView('settings')"><span class="symbol">⚙</span><span><strong>Настроить поведение панели</strong><small>Автообновление, подтверждения и подробные журналы</small></span></button></div>
<div class="card half"><div class="cardhead"><div><h2>Текущая операция</h2><p class="lead">Здесь отображается ход последнего действия</p></div><span class="badge" id="jobbadge">ОЖИДАНИЕ</span></div><div id="jobdetail" class="statusbox"><strong>Пока ничего не запущено</strong>Выберите действие в меню «Обработать музыку» или «Проверка и обложки».</div><div class="actions"><button class="btn danger" id="stopbtn" onclick="stopJob()" disabled>Остановить операцию</button></div><div id="progressarea" class="hidden"><div class="progress"><div id="progressbar"></div></div><p class="lead" id="progresslabel"></p></div></div>
</div></section>
<section id="music" class="view"><div class="grid">
<div class="card half"><div class="cardhead"><div><h2>Вариант 1. Обработать копию</h2><p class="lead">Оригинальные файлы остаются нетронутыми. Обработанные копии попадут в отдельную музыкальную библиотеку.</p></div><span class="badge">БЕЗОПАСНЕЕ</span></div><label for="srcfolder">Какую папку обработать?</label><select id="srcfolder"><option value="">Всю исходную коллекцию</option></select><div class="note safe">Рекомендуется для первого запуска: исходная папка доступна только для чтения.</div><button class="bigaction run" onclick="run('import_tags','import')"><span class="symbol">▤</span><span><strong>Импортировать по текущим тегам</strong><small>Использовать данные, уже записанные в музыкальных файлах</small></span></button><button class="bigaction run" onclick="run('import_mb','import')"><span class="symbol">⌕</span><span><strong>Распознать через MusicBrainz</strong><small>Попытаться найти правильные альбомы и сведения в онлайн-каталоге</small></span></button><button class="bigaction run" onclick="run('import_single','import')"><span class="symbol">◉</span><span><strong>Распознать отдельные треки</strong><small>Использовать аудиоотпечаток, если обычных тегов недостаточно</small></span></button><button class="btn" onclick="loadFolders('import')">Обновить список папок</button></div>
<div class="card half"><div class="cardhead"><div><h2>Вариант 2. Работать с файлами на месте</h2><p class="lead">Музыка не копируется в другую папку. Обработка идёт в выбранном каталоге.</p></div><span class="badge">БЕЗ КОПИИ</span></div><div class="note warn">Некоторые операции могут записывать новые теги непосредственно в файлы. Перед началом желательно иметь резервную копию.</div><p class="lead" style="margin-top:14px">Разрешённая папка на NAS: <strong id="workroot">определяем…</strong></p><label for="directfolder">Какую папку обработать?</label><select id="directfolder"><option value="">Корневую рабочую папку</option></select><button class="bigaction run" onclick="run('direct_tags','direct')"><span class="symbol">✎</span><span><strong>Обработать по текущим тегам</strong><small>Обновить отдельную базу без перемещения файлов</small></span></button><button class="bigaction run" onclick="run('direct_mb','direct')"><span class="symbol">⌕</span><span><strong>Обработать через MusicBrainz</strong><small>Поиск данных в сети с сохранением файлов на месте</small></span></button><button class="btn" onclick="loadFolders('direct')">Обновить список папок</button></div>
<div class="card"><h2>Какой вариант выбрать?</h2><div class="quality"><div><span>Хочу сохранить оригиналы</span><b>Обработка копии</b></div><div><span>Хочу менять теги в текущих файлах</span><b>На месте</b></div><div><span>Нужно распознавание по интернет-каталогу</span><b>MusicBrainz</b></div><div><span>Не уверен, с чего начать</span><b>Импорт по тегам</b></div></div></div>
</div></section>
<section id="check" class="view"><div class="grid">
<div class="card half"><h2>Качество музыкальных тегов</h2><p class="lead">Счётчики показывают, какие данные стоит проверить. Ничего не удаляется автоматически.</p><div class="quality" style="margin-top:12px"><div><span>Нет исполнителя</span><b id="noartist">—</b></div><div><span>Нет названия трека</span><b id="notitle">—</b></div><div><span>Нет названия альбома</span><b id="noalbum">—</b></div><div><span>Нет исполнителя альбома</span><b id="noalbumartist">—</b></div><div><span>Повтор исполнителя и названия</span><b id="duptracks">—</b></div><div><span>Повтор внутри альбома</span><b id="dupsamealbum">—</b></div><div><span>Похожие группы альбомов</span><b id="dupalbums">—</b></div><div><span>Нет MusicBrainz Track ID</span><b id="nombtrack">—</b></div><div><span>Нет MusicBrainz Album ID</span><b id="nombalbum">—</b></div></div><div class="actions"><button class="btn run" onclick="run('duplicates','tool')">Найти дубликаты</button><button class="btn run" onclick="run('no_artist','tool')">Показать альбомы без исполнителя</button><button class="btn run" onclick="run('stats','tool')">Общая статистика</button></div></div>
<div class="card half"><h2>Обложки и MusicBrainz</h2><p class="lead">Дополнительные сведения и оформление альбомов</p><button class="bigaction run" onclick="run('fetchart','tool')"><span class="symbol">▧</span><span><strong>Найти недостающие обложки</strong><small>Попытаться загрузить обложки для альбомов без изображения</small></span></button><button class="bigaction run" onclick="run('albums','tool')"><span class="symbol">☷</span><span><strong>Показать список альбомов</strong><small>Сформировать список из основной библиотеки</small></span></button><div class="progress"><div id="mbbar"></div></div><p class="lead" id="mbnums">Проверяем данные MusicBrainz…</p><p class="lead" id="mbstate"></p><details style="margin-top:12px" class="advanced"><summary>Подробности MusicBrainz</summary><pre id="mblog"></pre></details></div>
<div class="card"><div class="cardhead"><div><h2>Результат последней операции</h2><p class="lead">Технические сообщения скрыты в обычном режиме и доступны ниже.</p></div><button class="btn" onclick="showView('settings')">Настроить отображение</button></div><pre id="log">Операции ещё не запускались.</pre></div>
<div class="card advanced"><details><summary>Дополнительные журналы</summary><p class="lead">Эти сообщения полезны, если нужно разобраться с проблемой.</p><h3>Импорт</h3><pre id="importlog"></pre><h3>Обработка на месте</h3><pre id="directlog"></pre></details></div>
</div></section>
<section id="settings" class="view"><div class="grid">
<div class="card"><h2>Поведение панели</h2><p class="lead">Эти настройки сохраняются в этом браузере. Они не меняют музыкальные файлы и конфигурацию beets.</p>
<div class="setting"><div><strong>Автоматически обновлять состояние</strong><small>Проверять ход операций и статистику без нажатия кнопки. При выключении данные обновляются только вручную.</small></div><input class="switch" id="autoRefresh" type="checkbox" checked></div>
<div class="setting"><div><strong>Обновлять список папок при открытии панели</strong><small>Показывать актуальные каталоги автоматически. При выключении список можно обновить кнопкой.</small></div><input class="switch" id="autoFolders" type="checkbox" checked></div>
<div class="setting"><div><strong>Всегда спрашивать подтверждение перед запуском</strong><small>Рекомендуется оставить включённым, особенно для режима работы на месте.</small></div><input class="switch" id="confirmActions" type="checkbox" checked></div>
<div class="setting"><div><strong>Показывать технические журналы</strong><small>Если вы не разбираетесь в логах, отключите их — интерфейс станет проще.</small></div><input class="switch" id="showLogs" type="checkbox"></div>
<div class="actions"><button class="btn" onclick="resetSettings()">Вернуть рекомендуемые настройки</button></div><div class="note">Автоматические изменения музыкальных тегов, перемещения и удаления файлов не запускаются сами по себе. Каждую обработку вы запускаете отдельно.</div></div>
<div class="card"><h2>О защите данных</h2><p class="lead">Интерфейс рассчитан на управление локальной библиотекой. Не открывайте панель в общедоступный Интернет без HTTPS и дополнительной защиты.</p><p class="lead" style="margin-top:10px">«Обработать копию» и «Работать на месте» используют разные базы. Проверяйте выбранный режим перед запуском.</p><div class="actions"><button class="btn" onclick="showView('home')">Вернуться на главную</button></div></div>
</div></section>
<div class="foot">Muzick · управление музыкальной библиотекой · настройки интерфейса сохраняются в вашем браузере</div></main><div id="toast" class="toast" role="status"></div>
<script>
const $=id=>document.getElementById(id);const fmt=n=>Number(n||0).toLocaleString('ru-RU');let lastStatus=null,refreshTimer=null,toastTimer=null;
const defaults={autoRefresh:true,autoFolders:true,confirmActions:true,showLogs:false};
function getSettings(){try{return Object.assign({},defaults,JSON.parse(localStorage.getItem('muzick-ui-settings')||'{}'))}catch(e){return {...defaults}}}
let settings=getSettings();
function saveSettings(){try{localStorage.setItem('muzick-ui-settings',JSON.stringify(settings))}catch(e){}applySettings();toast('Настройки сохранены')}
function applySettings(){for(const k of Object.keys(defaults))$(k).checked=!!settings[k];document.querySelectorAll('.advanced').forEach(e=>e.classList.toggle('hidden',!settings.showLogs));$('log').classList.toggle('hidden',!settings.showLogs);if(refreshTimer){clearInterval(refreshTimer);refreshTimer=null}if(settings.autoRefresh)refreshTimer=setInterval(refresh,3000)}
function resetSettings(){settings={...defaults};saveSettings()}
function toast(s){const t=$('toast');t.textContent=s;t.style.display='block';if(toastTimer)clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.style.display='none',2600)}
function showView(id){document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id===id));document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('active',b.dataset.view===id));if(id==='check'&&settings.showLogs)refresh()}
document.querySelectorAll('.nav button').forEach(b=>b.addEventListener('click',()=>showView(b.dataset.view)));
for(const k of Object.keys(defaults))$(k).addEventListener('change',()=>{settings[k]=$(k).checked;try{localStorage.setItem('muzick-ui-settings',JSON.stringify(settings))}catch(e){}applySettings();toast('Настройка изменена')});
async function api(p,body){const r=await fetch(p,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,cache:'no-store'});let d={};try{d=await r.json()}catch(e){}if(!r.ok&&!d.error)d.error='Ошибка сервера: '+r.status;return d}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
async function loadFolders(mode){const sel=$(mode==='direct'?'directfolder':'srcfolder'),cur=sel.value;try{const list=await api('/api/folders?mode='+mode);if(list.error)throw Error(list.error);sel.innerHTML='<option value="">'+(mode==='direct'?'Корневая рабочая папка':'Вся исходная коллекция')+'</option>'+list.map(f=>'<option value="'+esc(f)+'">'+esc(f)+'</option>').join('');sel.value=cur;toast('Список папок обновлён')}catch(e){toast('Не удалось получить список папок')}}
async function run(job,mode){const folder=mode==='direct'?$('directfolder').value:mode==='import'?$('srcfolder').value:'';const names={import_tags:'Импорт по текущим тегам',import_mb:'Импорт с распознаванием MusicBrainz',import_single:'Распознавание отдельных треков',direct_tags:'Обработка на месте по тегам',direct_mb:'Обработка на месте через MusicBrainz',stats:'Общая статистика',albums:'Список альбомов',duplicates:'Поиск дубликатов',no_artist:'Альбомы без исполнителя',fetchart:'Поиск недостающих обложек'};const label=names[job]||job;const modeText=mode==='direct'?'Файлы будут обрабатываться в выбранной папке без копирования.':mode==='import'?'Оригиналы не изменяются: импорт идёт в отдельную библиотеку.':'Это проверочная операция.';if(settings.confirmActions&&!confirm(label+'\n\n'+modeText+(folder?'\nПапка: '+folder:'\nПапка: вся коллекция')+'\n\nПродолжить?'))return;try{const r=await api('/api/run',{job,folder});if(r.error){toast(r.error);return}toast('Операция запущена: '+label);showView('home');await refresh()}catch(e){toast('Не удалось запустить операцию. Обновите страницу и попробуйте снова.')} }
async function stopJob(){if(settings.confirmActions&&!confirm('Остановить текущую операцию?'))return;try{const r=await api('/api/stop',{});if(r.error)toast(r.error);else toast('Запрос на остановку отправлен');await refresh()}catch(e){toast('Не удалось остановить операцию')}}
async function refresh(){try{const s=await api('/api/status');if(s.error)throw Error(s.error);lastStatus=s;$('connection').textContent='Панель подключена';$('led').classList.add('ok');$('workroot').textContent=s.work_root||'/work';$('items').textContent=fmt(s.items);$('albums').textContent=fmt(s.albums);$('directitems').textContent=fmt(s.direct_items);const j=s.job||{};$('job').textContent=!j.name?'Готово к работе. Выберите действие выше.':(j.running?'Сейчас выполняется: ':'Последняя операция: ')+j.name+(j.rc!=null?(j.rc===0?' — завершено успешно':' — завершено с кодом '+j.rc):'');$('jobbadge').textContent=j.running?'В РАБОТЕ':j.name?'ЗАВЕРШЕНО':'ОЖИДАНИЕ';$('stopbtn').disabled=!j.running;document.querySelectorAll('.run').forEach(b=>b.disabled=!!j.running);$('jobdetail').innerHTML=j.running?'<strong>Выполняется операция</strong>'+esc(j.name||'Обработка')+'<br>Не закрывайте панель. Она обновит статус автоматически.':'<strong>'+(j.name?'Последняя операция':'Пока ничего не запущено')+'</strong>'+esc(j.name||'Выберите действие в меню «Обработать музыку» или «Проверка и обложки».')+(j.rc!=null?'<br>Код завершения: '+j.rc:'');$('progressarea').classList.toggle('hidden',!j.running);$('progresslabel').textContent=j.running?'Процесс выполняется. Подробный вывод находится в журнале.':'';const log=$('log'),atEnd=log.scrollTop+log.clientHeight>=log.scrollHeight-20;log.textContent=(s.log||[]).join('\n')||'Операции ещё не запускались.';if(atEnd)log.scrollTop=log.scrollHeight;const h=s.health||{};for(const [id,key] of [['noartist','no_artist'],['notitle','no_title'],['noalbum','no_album'],['noalbumartist','no_albumartist'],['duptracks','duplicate_track_groups'],['dupsamealbum','duplicate_same_album_groups'],['dupalbums','duplicate_album_groups'],['nombtrack','missing_mb_trackid'],['nombalbum','missing_mb_albumid']])$(id).textContent=fmt(h[key]);$('importlog').textContent=s.import_log||'(журнал пуст)';$('directlog').textContent=s.direct_log||'(журнал пуст)';const m=s.mb||{};$('mbbar').style.width=Math.min(100,m.pct||0)+'%';$('mbnums').textContent=fmt(m.matched)+' альбомов с MusicBrainz ID из '+fmt(m.albums);$('mbstate').textContent=m.running?'Идёт фоновая обработка':'Фоновая обработка сейчас не выполняется';$('mblog').textContent=m.log||'(журнал пуст)'}catch(e){$('connection').textContent='Нет соединения';$('led').classList.remove('ok')}}
applySettings();if(settings.autoFolders){loadFolders('import');loadFolders('direct')}refresh();
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
        elif urlparse(self.path).path.startswith("/fav.ico/"):
            name = urlparse(self.path).path[len("/fav.ico/"):]
            icon_root = os.path.realpath("/config/fav.ico")
            icon_file = os.path.realpath(os.path.join(icon_root, name))
            if not name or os.path.basename(name) != name or os.path.commonpath([icon_root, icon_file]) != icon_root or not os.path.isfile(icon_file):
                self._send("Not found", "text/plain; charset=utf-8", 404)
                return
            with open(icon_file, "rb") as f:
                data = f.read()
            content_type = mimetypes.guess_type(icon_file)[0] or "application/octet-stream"
            self._send(data, content_type)
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
