#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Muzick: простая веб-панель для наведения порядка в музыкальной коллекции (beets).

Работает внутри контейнера beets. Интерфейс лежит в папке ui/ рядом с настройками
(/config/ui), поэтому его можно менять без перезапуска.

Задачам, которым нужен MusicBrainz, панель умеет запускать отдельный контейнер в сети VPN
(через Docker API; для этого в контейнер панели пробрасывается сокет Docker).
Если MusicBrainz доступен напрямую, VPN не нужен.
"""
import base64
import fcntl
import hmac
import http.client
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import urllib.parse
from collections import Counter, defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------- окружение

CONFIG_DIR = os.environ.get("MUZICK_CONFIG", "/config")
SRC = os.environ.get("MUZICK_SRC", "/downloads")
LIB = os.environ.get("MUZICK_LIB", "/music")
PORT = int(os.environ.get("MUZICK_PORT", "8338"))
PASS = os.environ.get("PANEL_PASS", "")
DOCKER_SOCK = os.environ.get("MUZICK_DOCKER_SOCK", "/var/run/docker.sock")
IMAGE = os.environ.get("MUZICK_IMAGE", "lscr.io/linuxserver/beets:latest")
HOST_CONFIG = os.environ.get("HOST_CONFIG", "")
HOST_LIB = os.environ.get("HOST_LIB", "")
HOST_SRC = os.environ.get("HOST_SRC", "")
FILE_UID = os.environ.get("FILE_UID", "0")
FILE_GID = os.environ.get("FILE_GID", "0")
DEFAULT_VPN = os.environ.get("VPN_CONTAINER", "VPN") or "VPN"

UI_DIR = os.environ.get("MUZICK_UI") or os.path.join(CONFIG_DIR, "ui")
FAV_DIRS = [d for d in (os.environ.get("MUZICK_FAV"), "/fav.ico", os.path.join(CONFIG_DIR, "fav.ico")) if d]

BEET = os.environ.get("MUZICK_BEET") or shutil.which("beet") or "/lsiopy/bin/beet"
PY = os.environ.get("MUZICK_PY") or ("/lsiopy/bin/python3" if os.path.exists("/lsiopy/bin/python3") else sys.executable)
IMAGE_BEET, IMAGE_PY = "/lsiopy/bin/beet", "/lsiopy/bin/python3"   # пути внутри образа для задач через VPN

DB = os.path.join(CONFIG_DIR, "musiclibrary.db")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "panel-settings.json")
HISTORY_FILE = os.path.join(CONFIG_DIR, "panel-history.json")
STATE_FILE = os.path.join(CONFIG_DIR, "panel-state.json")
LOCK_FILE = os.path.join(CONFIG_DIR, ".muzick.lock")      # тот же замок, что у muzick.sh / lockrun.py
CONFIG_YAML = os.path.join(CONFIG_DIR, "config.yaml")
FIX_TAGS = os.path.join(CONFIG_DIR, "fix_tags.py")
ALIASES = os.path.join(CONFIG_DIR, "artist-aliases.tsv")
SUGGEST = os.path.join(CONFIG_DIR, "artist-aliases.suggested.tsv")
LOG_SCAN = os.path.join(CONFIG_DIR, "import-all.log")
LOG_MB = os.path.join(CONFIG_DIR, "import-mb.log")
LOG_FP = os.path.join(CONFIG_DIR, "import-single.log")
CONFIG_MARK = "# Файл создаётся веб-панелью Muzick. Меняйте настройки в панели, а не здесь."

AUDIO_EXT = (".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma", ".ape", ".wv")
SKIP_DIRS = {"@eaDir", "#recycle", "lost+found"}
MB_URL = "https://musicbrainz.org/ws/2/"

RLOCK = threading.RLock()
LOG = deque(maxlen=300)


def log(msg):
    LOG.append("%s %s" % (time.strftime("%H:%M:%S"), msg))


# ----------------------------------------------------------------------------- шаги

STEPS = [
    {"id": "scan", "title": "Собрать музыку в порядке",
     "desc": "Копирует файлы из исходной папки в аккуратную библиотеку: Исполнитель → Альбом → трек."},
    {"id": "encoding", "title": "Починить русские названия",
     "desc": "Исправляет «кракозябры» вроде Àëèñà → Алиса."},
    {"id": "artists", "title": "Объединить одних и тех же исполнителей",
     "desc": "INXS, inxs и InXS окажутся в одной папке."},
    {"id": "musicbrainz", "title": "Уточнить данные в интернете",
     "desc": "Подбирает правильные названия, годы и обложки в базе MusicBrainz."},
    {"id": "fingerprint", "title": "Узнать песни без названия",
     "desc": "Определяет неподписанные файлы по звучанию."},
    {"id": "covers", "title": "Найти обложки альбомов",
     "desc": "Скачивает обложки для альбомов, где их нет."},
    {"id": "report", "title": "Проверить результат",
     "desc": "Покажет возможные дубликаты и что осталось без данных. Ничего не удаляет."},
]
STEP_IDS = [s["id"] for s in STEPS]
STEP_INFO = {s["id"]: s for s in STEPS}

PATH_STYLES = {
    "simple": "$albumartist/$album%aunique{}/$track - $title",
    "year": "$albumartist/%if{$year,$year - }$album%aunique{}/$track - $title",
}

DEFAULTS = {
    "steps": {"scan": True, "encoding": True, "artists": True, "musicbrainz": True,
              "fingerprint": True, "covers": False, "report": True},
    "confirm": True,              # спрашивать перед изменением тегов
    "organize_after": True,       # раскладывать файлы по папкам после правки тегов
    "copy_mode": "copy",          # copy | move
    "path_style": "simple",       # simple | year
    "covers_on_import": True,     # скачивать обложки при импорте
    "net_mode": "auto",           # auto | vpn | direct
    "vpn_container": DEFAULT_VPN,
    "schedule": {"enabled": False, "every_hours": 24},
    # Пути относительно безопасных Docker-монтирований; пустая строка = корень.
    "folder_paths": {"source": "", "library": ""},
}
CONFIG_KEYS = ("copy_mode", "path_style", "covers_on_import")


def _merge(base, patch):
    out = json.loads(json.dumps(base))
    for k, v in (patch or {}).items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


SETTINGS = _merge(DEFAULTS, read_json(SETTINGS_FILE, {}))
STATE = read_json(STATE_FILE, {})

# Браузер папок работает только внутри уже смонтированных корней. Он не даёт
# произвольного доступа к файловой системе NAS и не требует пересоздания контейнеров.
FOLDER_ROOTS = {"source": "/downloads", "library": "/music"}

def _safe_rel(value):
    if not isinstance(value, str):
        raise ValueError("Некорректный путь")
    value = value.strip().replace("\\", "/")
    parts = [x for x in value.split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts) or value.startswith("/"):
        raise ValueError("Выход за пределы разрешённой папки запрещён")
    return "/".join(parts)

def selected_path(kind):
    root = FOLDER_ROOTS[kind]
    rel = _safe_rel(SETTINGS.get("folder_paths", {}).get(kind, ""))
    path = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([os.path.realpath(root), path]) != os.path.realpath(root):
        raise ValueError("Путь выходит за пределы разрешённой папки")
    return path

# Поддерживаем старую схему: если пользователь ничего не выбирал, используются корни mounts.
SRC = selected_path("source")
LIB = selected_path("library")


def save_state():
    try:
        write_json(STATE_FILE, STATE)
    except OSError:
        pass


def validate_patch(patch):
    """Проверяет присланные настройки и возвращает только допустимые значения."""
    clean = {}
    if not isinstance(patch, dict):
        return clean
    steps = patch.get("steps")
    if isinstance(steps, dict):
        clean["steps"] = {k: bool(v) for k, v in steps.items() if k in STEP_IDS}
    for key in ("confirm", "organize_after", "covers_on_import"):
        if key in patch:
            clean[key] = bool(patch[key])
    for key, allowed in (("copy_mode", ("copy", "move")), ("path_style", tuple(PATH_STYLES)),
                         ("net_mode", ("auto", "vpn", "direct"))):
        if patch.get(key) in allowed:
            clean[key] = patch[key]
    name = patch.get("vpn_container")
    if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name.strip()):
        clean["vpn_container"] = name.strip()
    folder_paths = patch.get("folder_paths")
    if isinstance(folder_paths, dict):
        clean["folder_paths"] = {}
        for key in ("source", "library"):
            if key in folder_paths:
                clean["folder_paths"][key] = _safe_rel(folder_paths[key])
    sch = patch.get("schedule")
    if isinstance(sch, dict):
        clean["schedule"] = {}
        if "enabled" in sch:
            clean["schedule"]["enabled"] = bool(sch["enabled"])
        if sch.get("every_hours") in (6, 12, 24, 72, 168):
            clean["schedule"]["every_hours"] = sch["every_hours"]
    return clean


def render_config(s):
    copy = s["copy_mode"] == "copy"
    return """%s
directory: %s
library: /config/musiclibrary.db

plugins: chroma fetchart embedart duplicates edit info web

import:
  copy: %s
  move: %s
  incremental: yes
  duplicate_action: skip
  write: yes
  autotag: yes
  timid: no
  log: /config/skipped.log
  quiet_fallback: skip

chroma:
  auto: yes

fetchart:
  auto: %s

embedart:
  auto: yes

id3v23: yes

web:
  host: 0.0.0.0
  port: 8337

paths:
  default: "%s"
  comp: "Compilations/$album%%aunique{}/$track - $title"
  singleton: "Non-Album/$artist/$title"

ignore: [".*", "*~", "System Volume Information", "lost+found", "@eaDir", "#recycle"]
""" % (CONFIG_MARK, LIB, "yes" if copy else "no", "no" if copy else "yes",
       "yes" if s["covers_on_import"] else "no", PATH_STYLES[s["path_style"]])


def write_beets_config():
    """Пересоздаёт config.yaml по настройкам панели (старый файл сохраняется один раз)."""
    try:
        old = open(CONFIG_YAML, encoding="utf-8").read() if os.path.exists(CONFIG_YAML) else ""
        if old and CONFIG_MARK not in old:
            backup = CONFIG_YAML + ".before-panel"
            if not os.path.exists(backup):
                shutil.copy2(CONFIG_YAML, backup)
        tmp = CONFIG_YAML + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(render_config(SETTINGS))
        os.replace(tmp, CONFIG_YAML)
        log("Настройки beets обновлены")
    except OSError as e:
        log("Не удалось записать config.yaml: %s" % e)


def apply_settings_patch(patch):
    clean = validate_patch(patch)
    with RLOCK:
        before = {k: SETTINGS[k] for k in CONFIG_KEYS}
        merged = _merge(SETTINGS, clean)
        # Проверяем новые пути до сохранения, чтобы ошибка не оставляла полусохранённую конфигурацию.
        old_settings = dict(SETTINGS)
        SETTINGS.clear()
        SETTINGS.update(merged)
        try:
            candidate_src = selected_path("source")
            candidate_lib = selected_path("library")
            if not os.path.isdir(candidate_src) or not os.path.isdir(candidate_lib):
                raise ValueError("Выбранная папка больше не существует или недоступна")
            common = os.path.commonpath([os.path.realpath(candidate_src), os.path.realpath(candidate_lib)])
            if common in (os.path.realpath(candidate_src), os.path.realpath(candidate_lib)):
                raise ValueError("Папки загрузок и библиотеки не должны совпадать или находиться одна внутри другой")
        except Exception:
            SETTINGS.clear()
            SETTINGS.update(old_settings)
            raise
        write_json(SETTINGS_FILE, SETTINGS)
        changed = any(SETTINGS[k] != before[k] for k in CONFIG_KEYS)
        global SRC, LIB
        SRC, LIB = candidate_src, candidate_lib
    if changed or "folder_paths" in clean:
        write_beets_config()
    if "net_mode" in clean or "vpn_container" in clean:
        NET.clear()
    return clean


# ----------------------------------------------------------------------------- база и файлы

def db_rows(sql, args=(), default=None):
    if not os.path.exists(DB):
        return default
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()
    except Exception:
        return default


def db_one(sql, args=(), default=0):
    rows = db_rows(sql, args)
    return rows[0][0] if rows and rows[0][0] is not None else default


def items_added_since(ts):
    return db_one("select count(*) from items where added >= ?", (ts,)) if ts else 0


def count_audio(root):
    """(файлов, папок с музыкой)."""
    files = dirs_n = 0
    for r, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        n = sum(1 for f in names if f.lower().endswith(AUDIO_EXT))
        if n:
            files += n
            dirs_n += 1
    return files, dirs_n


SOURCE = {"files": None, "dirs": None, "ts": 0.0, "busy": False}


def refresh_source_count(force=False):
    if SOURCE["busy"] or (not force and time.time() - SOURCE["ts"] < 900 and SOURCE["files"] is not None):
        return

    def work():
        SOURCE["busy"] = True
        try:
            if os.path.isdir(SRC):
                SOURCE["files"], SOURCE["dirs"] = count_audio(SRC)
            SOURCE["ts"] = time.time()
        finally:
            SOURCE["busy"] = False

    threading.Thread(target=work, daemon=True).start()


_FT = []


def fix_tags_module():
    if not _FT:
        try:
            spec = importlib.util.spec_from_file_location("fix_tags", FIX_TAGS)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _FT.append(mod)
        except Exception:
            _FT.append(None)
    return _FT[0]


def tail_file(path, n=15):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [l.rstrip("\n") for l in f.readlines()[-n:]]
    except OSError:
        return []


def count_log_tasks(path):
    """Сколько папок/файлов beets уже обработал (строки лога импорта)."""
    n = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith(("asis ", "skip ", "duplicate-")):
                    n += 1
    except OSError:
        pass
    return n


# ----------------------------------------------------------------------------- Docker API

class DockerError(Exception):
    pass


class _UnixConn(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


def docker_call(method, path, body=None, timeout=30):
    if not os.path.exists(DOCKER_SOCK):
        raise DockerError("Нет доступа к Docker")
    conn = _UnixConn(DOCKER_SOCK, timeout)
    try:
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, raw
    except (OSError, http.client.HTTPException) as e:
        raise DockerError(str(e))
    finally:
        conn.close()


def docker_ok():
    try:
        status, _ = docker_call("GET", "/_ping", timeout=5)
        return status == 200
    except DockerError:
        return False


def docker_container_running(name):
    try:
        status, raw = docker_call("GET", "/containers/%s/json" % name, timeout=5)
        return status == 200 and bool(json.loads(raw)["State"]["Running"])
    except (DockerError, ValueError, KeyError):
        return False


def docker_start_job(entry, cmd, with_mounts=True):
    """Создаёт и запускает одноразовый контейнер в сети VPN. Возвращает id."""
    vpn = SETTINGS["vpn_container"]
    host = {"NetworkMode": "container:%s" % vpn}
    if with_mounts:
        if not (HOST_CONFIG and HOST_LIB and HOST_SRC):
            raise DockerError("Панель не знает путей к папкам на NAS. Обновите её командой muzick.sh up")
        host["Binds"] = ["%s:/config" % HOST_CONFIG, "%s:/music" % HOST_LIB, "%s:/downloads:rw" % HOST_SRC]
    body = {"Image": IMAGE, "Entrypoint": [entry], "Cmd": cmd, "User": "%s:%s" % (FILE_UID, FILE_GID),
            "Env": ["BEETSDIR=/config", "HOME=/config", "PYTHONUNBUFFERED=1"], "Tty": True,
            "Labels": {"muzick": "job"}, "HostConfig": host}
    status, raw = docker_call("POST", "/containers/create?name=muzick-job-%d" % int(time.time() * 1000), body)
    if status != 201:
        raise DockerError("Docker не создал задачу: %s" % raw.decode("utf-8", "replace")[:200])
    cid = json.loads(raw)["Id"]
    status, raw = docker_call("POST", "/containers/%s/start" % cid)
    if status not in (204, 304):
        docker_remove(cid)
        raise DockerError("Docker не запустил задачу: %s" % raw.decode("utf-8", "replace")[:200])
    return cid


def docker_state(cid):
    status, raw = docker_call("GET", "/containers/%s/json" % cid, timeout=10)
    if status != 200:
        return {"Running": False, "ExitCode": -1}
    return json.loads(raw)["State"]


def docker_logs(cid, tail=60):
    try:
        status, raw = docker_call("GET", "/containers/%s/logs?stdout=1&stderr=1&tail=%d" % (cid, tail), timeout=15)
        return raw.decode("utf-8", "replace").replace("\r", "").splitlines() if status == 200 else []
    except DockerError:
        return []


def docker_stop(cid):
    try:
        docker_call("POST", "/containers/%s/stop?t=5" % cid, timeout=20)
    except DockerError:
        pass


def docker_remove(cid):
    try:
        docker_call("DELETE", "/containers/%s?force=1" % cid, timeout=20)
    except DockerError:
        pass


def docker_cleanup_jobs():
    try:
        flt = json.dumps({"label": ["muzick=job"]})
        status, raw = docker_call("GET", "/containers/json?all=1&filters=%s" % urllib.request.quote(flt), timeout=10)
        if status == 200:
            for c in json.loads(raw):
                docker_remove(c["Id"])
    except (DockerError, ValueError):
        pass


# ----------------------------------------------------------------------------- проверка сети

NET = {}   # {"direct": (ok, ts), "vpn": (ok, ts)}
_HEALTH = {"ts": 0.0, "data": []}


def _cached(key, max_age):
    v = NET.get(key)
    return v[0] if v and time.time() - v[1] < max_age else None


def check_direct(max_age=600):
    cached = _cached("direct", max_age)
    if cached is not None:
        return cached
    ok = False
    try:
        urllib.request.urlopen(urllib.request.Request(MB_URL, headers={"User-Agent": "muzick-panel"}), timeout=8)
        ok = True
    except urllib.error.HTTPError:
        ok = True            # сервер ответил, значит, достижим
    except Exception:
        ok = False
    NET["direct"] = (ok, time.time())
    _HEALTH["ts"] = 0
    return ok


def check_vpn(max_age=600):
    cached = _cached("vpn", max_age)
    if cached is not None:
        return cached
    ok = False
    if docker_ok() and docker_container_running(SETTINGS["vpn_container"]):
        code = ("import urllib.request as u, urllib.error as e\n"
                "try:\n u.urlopen('%s', timeout=15)\nexcept e.HTTPError:\n pass\n" % MB_URL)
        cid = None
        try:
            cid = docker_start_job(IMAGE_PY, ["-c", code], with_mounts=False)
            deadline = time.time() + 40
            while time.time() < deadline:
                st = docker_state(cid)
                if not st.get("Running"):
                    ok = st.get("ExitCode") == 0
                    break
                time.sleep(1)
        except (DockerError, ValueError):
            ok = False
        finally:
            if cid:
                docker_stop(cid)
                docker_remove(cid)
    NET["vpn"] = (ok, time.time())
    _HEALTH["ts"] = 0
    return ok


def mb_route():
    """Как идти в MusicBrainz: ('direct'|'vpn', '') или (None, причина)."""
    mode = SETTINGS["net_mode"]
    if mode in ("auto", "direct"):
        if check_direct():
            return "direct", ""
        if mode == "direct":
            return None, "MusicBrainz недоступен напрямую из вашей сети. Включите VPN-режим в настройках."
    if not docker_ok():
        return None, ("MusicBrainz недоступен напрямую, а панель не может запустить задачу через VPN: "
                      "нет доступа к Docker. Переустановите панель с доступом к Docker или откройте MusicBrainz другим способом.")
    if not docker_container_running(SETTINGS["vpn_container"]):
        return None, "VPN-контейнер «%s» не запущен. Запустите его и повторите." % SETTINGS["vpn_container"]
    if check_vpn():
        return "vpn", ""
    return None, "Даже через VPN MusicBrainz не отвечает. Попробуйте позже."


# ----------------------------------------------------------------------------- движок задач

class Cancelled(Exception):
    pass


class StepError(Exception):
    def __init__(self, msg, details=None):
        super().__init__(msg)
        self.details = details or []


RUN = {"active": False, "title": "", "started": 0.0, "finished": 0.0, "result": "",
       "trigger": "", "steps": [], "awaiting": False, "cancel": False}
CONFIRM = {"event": threading.Event(), "answer": None}
CURRENT = {"proc": None, "cid": None}


def new_step(sid):
    info = STEP_INFO[sid]
    return {"id": sid, "title": info["title"], "desc": info["desc"], "status": "pending",
            "msg": "", "pct": 0, "preview": [], "details": []}


def acquire_file_lock():
    try:
        fh = open(LOCK_FILE, "a", encoding="utf-8")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except (BlockingIOError, OSError):
        return None


def release_file_lock(fh):
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
    except OSError:
        pass


def supervise(alive, stop, step, progress):
    last = 0.0
    while alive():
        if RUN["cancel"]:
            stop()
            raise Cancelled()
        time.sleep(0.5)
        if progress and time.time() - last >= 2:
            last = time.time()
            try:
                progress(step)
            except Exception:
                pass
    if RUN["cancel"]:       # процесс завершили по кнопке «Остановить», это не ошибка
        raise Cancelled()


def run_local(args, step, progress=None):
    """Запускает команду в этом контейнере. Возвращает (код, строки вывода)."""
    out = deque(maxlen=400)
    env = dict(os.environ, BEETSDIR=CONFIG_DIR, HOME=CONFIG_DIR, PYTHONUNBUFFERED="1")
    log("$ " + " ".join(args[:6]))
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", env=env, start_new_session=True)
    CURRENT["proc"] = proc

    def pump():
        for line in proc.stdout:
            out.append(line.rstrip("\n"))

    t = threading.Thread(target=pump, daemon=True)
    t.start()

    def stop():
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass

    try:
        supervise(lambda: proc.poll() is None, stop, step, progress)
    finally:
        CURRENT["proc"] = None
    t.join(timeout=5)
    return proc.returncode, list(out)


def run_docker(args, step, progress=None):
    """Запускает beet в одноразовом контейнере в сети VPN. Возвращает (код, строки вывода)."""
    cid = None
    try:
        cid = docker_start_job(IMAGE_BEET, args)
        CURRENT["cid"] = cid
        log("Задача запущена через VPN (%s)" % cid[:8])

        def alive():
            return bool(docker_state(cid).get("Running"))

        supervise(alive, lambda: docker_stop(cid), step, progress)
        code = docker_state(cid).get("ExitCode", -1)
        return code, docker_logs(cid)
    except DockerError as e:
        raise StepError("Не удалось запустить задачу через VPN: %s" % e)
    finally:
        CURRENT["cid"] = None
        if cid:
            docker_remove(cid)


def run_beet(args, step, progress=None, route="direct"):
    if route == "vpn":
        return run_docker(args, step, progress)
    return run_local([BEET] + args, step, progress)


def need_ok(rc, out, what="Команда"):
    if RUN["cancel"]:
        raise Cancelled()
    if rc != 0:
        raise StepError("%s завершилась с ошибкой (код %s). Подробности в разделе «История»." % (what, rc),
                        [l for l in out[-15:] if l.strip()])


def ask_confirm(step, text, preview):
    """Ждёт подтверждения при ручном запуске; автоматический запуск работает без диалога."""
    if not SETTINGS["confirm"] or RUN.get("trigger") == "auto":
        return True
    step.update(status="waiting", msg=text, preview=preview[:15])
    RUN["awaiting"] = True
    CONFIRM["event"].clear()
    CONFIRM["answer"] = None
    deadline = time.time() + 12 * 3600
    while not CONFIRM["event"].wait(1):
        if RUN["cancel"]:
            RUN["awaiting"] = False
            raise Cancelled()
        if time.time() > deadline:
            break
    RUN["awaiting"] = False
    step.update(status="running", preview=[])
    return bool(CONFIRM["answer"])


def organize(step):
    """Раскладывает файлы по папкам в соответствии с тегами (beet move)."""
    if not SETTINGS["organize_after"]:
        return ""
    step["msg"] = "Раскладываю файлы по папкам…"
    rc, out = run_local([BEET, "move"], step)
    need_ok(rc, out, "Раскладка файлов")
    return " Файлы разложены по папкам."


def fix_tags_run(step, *args):
    rc, out = run_local([PY, FIX_TAGS] + list(args), step)
    need_ok(rc, out, "Проверка тегов")
    return out


def arrow_lines(out, limit=15, strip_field=False):
    """Строки «было -> стало» из вывода fix_tags.py в читаемом виде (заголовок таблицы пропускаем)."""
    lines = []
    for l in out:
        if "->" not in l or l.lstrip().startswith("$") or "станет" in l:
            continue
        a, _, b = l.partition("->")
        a = a.strip()
        if strip_field:
            a = re.sub(r"^\S+\s+", "", a)      # убираем имя поля (artist, album, title...)
        b = re.sub(r"\s*\((\d+)\)\s*$", lambda m: "  · треков: %s" % m.group(1), b.strip())
        line = "%s → %s" % (a, b)
        if line not in lines:
            lines.append(line)
        if len(lines) >= limit:
            break
    return lines


# --- отдельные шаги

def step_scan(step):
    if not os.path.isdir(SRC) or not os.listdir(SRC):
        step.update(status="skipped", msg="Исходная папка пуста или недоступна.")
        return
    step["msg"] = "Считаю файлы…"
    files, dirs_n = count_audio(SRC)
    SOURCE.update(files=files, dirs=dirs_n, ts=time.time())
    if not files:
        step.update(status="skipped", msg="В исходной папке нет музыкальных файлов.")
        return
    start = time.time()
    open(LOG_SCAN, "w").close()

    def progress(st):
        done = count_log_tasks(LOG_SCAN)
        st["pct"] = min(99, done * 100 / max(dirs_n, 1))
        st["msg"] = "Обработано папок: %d из %d · добавлено треков: %d" % (min(done, dirs_n), dirs_n, items_added_since(start))

    rc, out = run_local([BEET, "import", "-A", "-q", "-l", LOG_SCAN, SRC], step, progress)
    need_ok(rc, out, "Сбор музыки")
    added = items_added_since(start)
    step.update(pct=100, msg=("Добавлено новых треков: %d." % added) if added
                else "Новых файлов нет: всё уже собрано.")


def step_encoding(step):
    step["msg"] = "Ищу «кракозябры»…"
    out = fix_tags_run(step, "encoding")
    text = "\n".join(out)
    m = re.search(r"Треков с кракозябрами: (\d+), различных замен: (\d+)", text)
    if not m or int(m.group(1)) == 0 or "Менять нечего" in text:
        step.update(pct=100, msg="Всё в порядке: неправильных кодировок не найдено.")
        return
    n = int(m.group(1))
    preview = arrow_lines(out, strip_field=True)
    if not ask_confirm(step, "Найдено треков с неправильной кодировкой: %d. Исправить? Примеры замен ниже." % n, preview):
        step.update(status="skipped", pct=100, msg="Пропущено по вашему выбору.")
        return
    step["msg"] = "Исправляю названия…"
    out = fix_tags_run(step, "encoding", "--apply")
    m = re.search(r"треков (\d+), тегов записано (\d+), ошибок записи (\d+)", "\n".join(out))
    fixed = m.group(1) if m else str(n)
    errs = int(m.group(3)) if m else 0
    msg = "Исправлено треков: %s." % fixed + (" Не удалось записать теги у %d файлов." % errs if errs else "")
    step.update(pct=100, msg=msg + organize(step))


def merge_aliases():
    """Добавляет автоматически найденные варианты в artist-aliases.tsv (ручные строки не трогает)."""
    def parse(path):
        rows = {}
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip() and not line.lstrip().startswith("#"):
                        parts = line.rstrip("\n").split("\t")
                        if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
                            rows[parts[0].strip()] = parts[1].strip()
        except OSError:
            pass
        return rows
    manual, found = parse(ALIASES), parse(SUGGEST)
    new = {k: v for k, v in found.items() if k not in manual}
    if new:
        with open(ALIASES, "a", encoding="utf-8") as f:
            if not manual:
                f.write("# вариант<TAB>каноническое имя. Строки можно править вручную.\n")
            f.write("# добавлено автоматически\n")
            for k, v in sorted(new.items()):
                f.write("%s\t%s\n" % (k, v))
    return len(manual) + len(new)


def step_artists(step):
    step["msg"] = "Ищу разные написания одних и тех же исполнителей…"
    fix_tags_run(step, "artists", "suggest")
    total = merge_aliases()
    if not total:
        step.update(pct=100, msg="Всё в порядке: повторяющихся исполнителей не найдено.")
        return
    out = fix_tags_run(step, "artists", "preview")
    m = re.search(r"затронуто треков: (\d+)", "\n".join(out))
    n = int(m.group(1)) if m else 0
    if n == 0:
        step.update(pct=100, msg="Всё в порядке: объединять нечего.")
        return
    preview = arrow_lines(out)
    if not ask_confirm(step, "Будут объединены исполнители у %d треков. Применить? Примеры ниже." % n, preview):
        step.update(status="skipped", pct=100, msg="Пропущено по вашему выбору.")
        return
    step["msg"] = "Объединяю исполнителей…"
    out = fix_tags_run(step, "artists", "apply")
    step.update(pct=100, msg="Объединено исполнителей у треков: %d." % n + organize(step))


def step_musicbrainz(step):
    route, why = mb_route()
    if not route:
        step.update(status="skipped", msg=why)
        return
    albums = db_one("select count(*) from albums")
    if not albums:
        step.update(status="skipped", msg="В библиотеке пока нет альбомов.")
        return
    before = db_one("select count(*) from albums where coalesce(mb_albumid,'') <> ''")
    open(LOG_MB, "w").close()
    step["msg"] = "Подключаюсь к MusicBrainz%s…" % (" через VPN" if route == "vpn" else "")

    def progress(st):
        matched = db_one("select count(*) from albums where coalesce(mb_albumid,'') <> ''")
        done = matched + count_log_tasks(LOG_MB)
        st["pct"] = min(99, done * 100 / albums)
        st["msg"] = "Найдено в MusicBrainz: %d · проверено альбомов: %d из %d" % (matched, min(done, albums), albums)

    rc, out = run_beet(["import", "-L", "-q", "-l", LOG_MB], step, progress, route)
    need_ok(rc, out, "Поиск в MusicBrainz")
    matched = db_one("select count(*) from albums where coalesce(mb_albumid,'') <> ''")
    step.update(pct=100, msg=("Найдено новых альбомов: %d из %d." % (matched - before, albums)) + organize(step))


def step_fingerprint(step):
    candidates = db_one("select count(*) from items where coalesce(artist,'')='' or coalesce(title,'')=''")
    if not candidates:
        step.update(pct=100, msg="Всё в порядке: у всех файлов есть исполнитель и название.")
        return
    route, why = mb_route()
    if not route:
        step.update(status="skipped", msg=why)
        return
    open(LOG_FP, "w").close()
    step["msg"] = "Слушаю %d файлов без названия…" % candidates

    def progress(st):
        left = db_one("select count(*) from items where coalesce(artist,'')='' or coalesce(title,'')=''")
        st["pct"] = min(99, (candidates - left) * 100 / candidates)
        st["msg"] = "Определено: %d из %d" % (max(candidates - left, 0), candidates)

    args = ["import", "-L", "-s", "-q", "-l", LOG_FP, "artist::^$", ",", "title::^$"]
    rc, out = run_beet(args, step, progress, route)
    need_ok(rc, out, "Определение по звучанию")
    left = db_one("select count(*) from items where coalesce(artist,'')='' or coalesce(title,'')=''")
    step.update(pct=100, msg=("Определено песен: %d из %d." % (candidates - left, candidates)) + organize(step))


def step_covers(step):
    route, why = mb_route()
    if not route:
        step.update(status="skipped", msg=why)
        return
    step["msg"] = "Ищу обложки…"
    rc, out = run_beet(["fetchart"], step, None, route)
    need_ok(rc, out, "Поиск обложек")
    step.update(pct=100, msg="Поиск обложек завершён.")


def duplicate_groups(limit=15):
    rows = db_rows("""select albumartist, album, track, title, count(*) n from items
                      group by lower(albumartist), lower(album), track, lower(title), cast(length as int)
                      having n > 1 order by n desc""", default=[]) or []
    return len(rows), ["%s — %s, №%s «%s» (копий: %d)" % (r[0] or "?", r[1] or "?", r[2], r[3] or "?", r[4])
                       for r in rows[:limit]]


def step_report(step):
    n, preview = duplicate_groups()
    no_tags = db_one("select count(*) from items where coalesce(artist,'')='' or coalesce(title,'')=''")
    parts = []
    parts.append("Возможных дубликатов: %d." % n if n else "Дубликатов не найдено.")
    parts.append("Файлов без исполнителя или названия: %d." % no_tags if no_tags else "У всех файлов есть исполнитель и название.")
    step.update(pct=100, msg=" ".join(parts), preview=preview if n else [])


STEP_FUNCS = {"scan": step_scan, "encoding": step_encoding, "artists": step_artists,
              "musicbrainz": step_musicbrainz, "fingerprint": step_fingerprint,
              "covers": step_covers, "report": step_report}


def plan_ids():
    return [sid for sid in STEP_IDS if SETTINGS["steps"].get(sid)]


def add_history(entry):
    hist = read_json(HISTORY_FILE, [])
    hist.insert(0, entry)
    try:
        write_json(HISTORY_FILE, hist[:60])
    except OSError:
        pass


def _run_thread(lock):
    result = "ok"
    try:
        steps = RUN["steps"]
        for step in steps:
            if RUN["cancel"]:
                step.update(status="skipped", msg="Остановлено.")
                result = "cancelled"
                continue
            step["status"] = "running"
            log("Шаг: %s" % step["title"])
            try:
                STEP_FUNCS[step["id"]](step)
                if step["status"] == "running":
                    step["status"] = "done"
            except Cancelled:
                step.update(status="skipped", msg="Остановлено.")
                result = "cancelled"
            except StepError as e:
                step.update(status="error", msg=str(e), details=e.details)
                result = "error"
            except Exception as e:  # непредвиденное
                log("Ошибка: %s" % traceback.format_exc().splitlines()[-1])
                step.update(status="error", msg="Неожиданная ошибка: %s" % e,
                            details=traceback.format_exc().splitlines()[-8:])
                result = "error"
            if step["status"] == "error":
                for rest in steps:
                    if rest["status"] == "pending":
                        rest.update(status="skipped", msg="Пропущено из-за ошибки выше.")
                break
        if result == "ok" and any(s["status"] == "error" for s in steps):
            result = "error"
    finally:
        with RLOCK:
            RUN.update(active=False, finished=time.time(), awaiting=False, result=result)
            add_history({"ts": RUN["started"], "duration": RUN["finished"] - RUN["started"],
                         "title": RUN["title"], "result": result, "trigger": RUN["trigger"],
                         "steps": [{k: s[k] for k in ("id", "title", "status", "msg", "details")} for s in RUN["steps"]]})
        release_file_lock(lock)
        ANALYSIS["ts"] = 0
        refresh_source_count(True)
        log("Готово" if result == "ok" else "Завершено: %s" % result)


def start_run(ids, trigger="manual"):
    ids = [i for i in STEP_IDS if i in ids]
    if not ids:
        return {"error": "Не выбрано ни одного шага. Включите их в настройках."}, 400
    with RLOCK:
        if RUN["active"]:
            return {"error": "Сейчас уже выполняется другая задача."}, 409
        lock = acquire_file_lock()
        if lock is None:
            return {"error": "Библиотеку сейчас использует другая задача (например, запущенная из терминала). "
                             "Дождитесь её окончания."}, 409
        title = STEP_INFO[ids[0]]["title"] if len(ids) == 1 else "Наведение порядка"
        RUN.update(active=True, title=title, started=time.time(), finished=0.0, result="", trigger=trigger,
                   cancel=False, awaiting=False, steps=[new_step(i) for i in ids])
        threading.Thread(target=_run_thread, args=(lock,), daemon=True).start()
    return {"ok": True}, 200


def cancel_run():
    if not RUN["active"]:
        return {"error": "Сейчас ничего не выполняется."}, 400
    RUN["cancel"] = True
    CONFIRM["event"].set()
    proc, cid = CURRENT["proc"], CURRENT["cid"]
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
    if cid:
        docker_stop(cid)
    return {"ok": True}, 200


def _source_snapshot():
    """Снимок аудиофайлов для ожидания окончания загрузки."""
    result = []
    if not os.path.isdir(SRC):
        return result
    for root, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        for name in files:
            if not name.lower().endswith(AUDIO_EXT):
                continue
            path = os.path.join(root, name)
            try:
                st = os.stat(path, follow_symlinks=False)
                if not os.path.isfile(path):
                    continue
                result.append((os.path.relpath(path, SRC), st.st_size, int(st.st_mtime)))
            except OSError:
                continue
    return sorted(result)


def scheduler():
    while True:
        time.sleep(60)
        try:
            sch = SETTINGS["schedule"]
            if not sch["enabled"] or RUN["active"]:
                continue
            snapshot = _source_snapshot()
            if not snapshot:
                STATE["source_snapshot"] = []
                save_state()
                continue
            # Любое изменение размера, времени или состава файлов сбрасывает ожидание.
            # Обрабатываем только после двух одинаковых снимков с интервалом 60 секунд.
            previous = STATE.get("source_snapshot")
            STATE["source_snapshot"] = snapshot
            if snapshot != previous:
                save_state()
                continue
            if time.time() - STATE.get("last_auto", 0) < 60:
                continue
            STATE["last_auto"] = time.time()
            save_state()
            ids = plan_ids()
            if ids:
                start_run(ids, "auto")
        except Exception as e:
            log("Ошибка автоматической проверки загрузок: %s" % e)


# ----------------------------------------------------------------------------- состояние для интерфейса

ANALYSIS = {"ts": 0.0, "data": None}


def analyze():
    if ANALYSIS["data"] is not None and time.time() - ANALYSIS["ts"] < 45:
        return ANALYSIS["data"]
    tracks = db_one("select count(*) from items")
    albums = db_one("select count(*) from albums")
    artists = db_one("select count(distinct lower(albumartist)) from items where coalesce(albumartist,'') <> ''")
    todo = []
    ft = fix_tags_module()
    rows = db_rows("select albumartist, artist, album, title from items", default=[]) or []
    if ft and rows:
        bad = sum(1 for r in rows if any(x and ft.fix_text(x) != x for x in r))
        if bad:
            todo.append({"id": "encoding", "count": bad, "step": "encoding",
                         "text": "У %d треков русские названия «поломаны» (кракозябры)." % bad})
        names = Counter()
        for r in rows:
            for x in r[:2]:
                if x:
                    names[x] += 1
        groups = defaultdict(set)
        for name in names:
            key = ft.norm_key(name)
            if key:
                groups[key].add(name)
        dup_art = sum(1 for g in groups.values() if len(g) > 1)
        if dup_art:
            todo.append({"id": "artists", "count": dup_art, "step": "artists",
                         "text": "%d исполнителей записаны по-разному (регистр, «ё»/«е») и лежат в разных папках." % dup_art})
    no_tags = db_one("select count(*) from items where coalesce(artist,'')='' or coalesce(title,'')=''")
    if no_tags:
        todo.append({"id": "fingerprint", "count": no_tags, "step": "fingerprint",
                     "text": "У %d файлов нет исполнителя или названия. Их можно узнать по звучанию." % no_tags})
    no_mb = db_one("select count(*) from albums where coalesce(mb_albumid,'') = ''")
    if albums and no_mb > albums * 0.5:
        todo.append({"id": "musicbrainz", "count": no_mb, "step": "musicbrainz",
                     "text": "У %d альбомов нет данных из интернета (год, обложка, точные названия)." % no_mb})
    src = SOURCE["files"]
    if src and tracks < src * 0.97:
        todo.append({"id": "scan", "count": src - tracks, "step": "scan",
                     "text": "В исходной папке %d файлов, а в библиотеке %d: часть музыки ещё не собрана." % (src, tracks)})
    n_dup, _ = duplicate_groups(0)
    if n_dup:
        todo.append({"id": "report", "count": n_dup, "step": "report",
                     "text": "Возможных дубликатов: %d. Посмотрите список и удалите лишнее вручную." % n_dup})
    data = {"tracks": tracks, "albums": albums, "artists": artists, "todo": todo}
    ANALYSIS.update(ts=time.time(), data=data)
    return data


def human_gb(n):
    return "%.0f ГБ" % (n / 1024 ** 3) if n >= 10 * 1024 ** 3 else "%.1f ГБ" % (n / 1024 ** 3)


def health():
    if time.time() - _HEALTH["ts"] < 10:
        return _HEALTH["data"]
    items = []
    n_src = SOURCE["files"]
    if os.path.isdir(SRC):
        items.append({"id": "source", "level": "ok", "title": "Папка с музыкой",
                      "text": ("Найдено файлов: %d" % n_src) if n_src is not None else "Папка найдена, считаю файлы…"})
    else:
        items.append({"id": "source", "level": "bad", "title": "Папка с музыкой",
                      "text": "Папка не найдена.", "hint": "Проверьте путь при установке (SRC_DIR)."})
    try:
        free = shutil.disk_usage(LIB).free
        writable = os.access(LIB, os.W_OK)
        if not writable:
            items.append({"id": "library", "level": "bad", "title": "Папка библиотеки",
                          "text": "Нет права записи.", "hint": "Проверьте права на папку или параметры FILE_UID/FILE_GID."})
        elif free < 20 * 1024 ** 3:
            items.append({"id": "library", "level": "warn", "title": "Папка библиотеки",
                          "text": "Свободно мало места: %s." % human_gb(free)})
        else:
            items.append({"id": "library", "level": "ok", "title": "Папка библиотеки",
                          "text": "Свободно %s." % human_gb(free)})
    except OSError:
        items.append({"id": "library", "level": "bad", "title": "Папка библиотеки", "text": "Папка недоступна."})
    d = NET.get("direct")
    if d is None:
        items.append({"id": "internet", "level": "info", "title": "Интернет-база MusicBrainz",
                      "text": "Проверка ещё не выполнялась."})
    elif d[0]:
        items.append({"id": "internet", "level": "ok", "title": "Интернет-база MusicBrainz",
                      "text": "Доступна напрямую, VPN не нужен."})
    else:
        items.append({"id": "internet", "level": "warn", "title": "Интернет-база MusicBrainz",
                      "text": "Напрямую недоступна (нужен VPN)."})
    dock = docker_ok()
    vpn = SETTINGS["vpn_container"]
    if dock and docker_container_running(vpn):
        v = NET.get("vpn")
        items.append({"id": "vpn", "level": "ok", "title": "VPN",
                      "text": "Контейнер «%s» работает%s." % (vpn, ", MusicBrainz через него доступен" if v and v[0] else "")})
    elif dock:
        items.append({"id": "vpn", "level": "info" if (d and d[0]) else "warn", "title": "VPN",
                      "text": "Контейнер «%s» не найден или остановлен." % vpn})
    else:
        items.append({"id": "vpn", "level": "info" if (d and d[0]) else "warn", "title": "VPN",
                      "text": "Панель не может управлять VPN (нет доступа к Docker)."
                              if (d is None or not d[0]) else "Не требуется."})
    _HEALTH.update(ts=time.time(), data=items)
    return items


def build_status():
    refresh_source_count()
    a = analyze()
    with RLOCK:
        steps = []
        if RUN["steps"]:
            steps = json.loads(json.dumps(RUN["steps"]))
        else:
            steps = [dict(new_step(i), status="idle") for i in STEP_IDS if SETTINGS["steps"].get(i)]
        for s in steps:
            s["enabled"] = True
        planned = [dict(new_step(i), status="idle", enabled=False) for i in STEP_IDS
                   if not SETTINGS["steps"].get(i)] if not RUN["steps"] else []
        run = {k: RUN[k] for k in ("active", "title", "started", "finished", "result", "trigger", "awaiting")}
    done = sum(1 for s in steps if s["status"] in ("done", "skipped", "error"))
    cur = next((s["pct"] for s in steps if s["status"] in ("running", "waiting")), 0)
    overall = ((done + cur / 100.0) / len(steps) * 100) if steps and RUN["steps"] else 0
    return {"summary": {"tracks": a["tracks"], "albums": a["albums"], "artists": a["artists"]},
            "todo": a["todo"], "run": run, "steps": steps + planned, "overall": min(100, overall),
            "health": health(), "log": list(LOG)[-30:],
            "plan": {"count": len(plan_ids()), "confirm": SETTINGS["confirm"]},
            "net_checking": bool(NET.get("_checking"))}


def directory_listing(kind, rel=""):
    if kind not in FOLDER_ROOTS:
        raise ValueError("Неизвестный тип папки")
    rel = _safe_rel(rel)
    root = os.path.realpath(FOLDER_ROOTS[kind])
    target = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, target]) != root:
        raise ValueError("Выход за пределы разрешённой папки запрещён")
    if not os.path.isdir(target):
        raise ValueError("Каталог недоступен")
    entries = []
    with os.scandir(target) as it:
        for ent in it:
            if ent.name.startswith(".") or ent.name in SKIP_DIRS:
                continue
            try:
                if ent.is_dir(follow_symlinks=False):
                    child_rel = "/".join(x for x in (rel, ent.name) if x)
                    entries.append({"name": ent.name, "path": child_rel})
            except OSError:
                continue
    entries.sort(key=lambda x: x["name"].casefold())
    return {"kind": kind, "root": root, "path": rel, "parent": "/".join(rel.split("/")[:-1]) if rel else None,
            "directories": entries, "selected": SETTINGS.get("folder_paths", {}).get(kind, "")}


def settings_view():
    return {"settings": SETTINGS,
            "steps": [{"id": s["id"], "title": s["title"], "desc": s["desc"]} for s in STEPS],
            "paths": {"source": os.path.join(HOST_SRC, SETTINGS.get("folder_paths", {}).get("source", "")) if HOST_SRC else SRC,
                      "library": os.path.join(HOST_LIB, SETTINGS.get("folder_paths", {}).get("library", "")) if HOST_LIB else LIB,
                      "source_relative": SETTINGS.get("folder_paths", {}).get("source", ""),
                      "library_relative": SETTINGS.get("folder_paths", {}).get("library", "")},
            "docker": docker_ok(), "path_styles": list(PATH_STYLES)}


def start_net_check():
    if NET.get("_checking"):
        return {"ok": True}, 200
    NET.pop("direct", None)
    NET.pop("vpn", None)
    NET["_checking"] = True

    def work():
        try:
            check_direct(0)
            if docker_ok():
                check_vpn(0)
        finally:
            NET.pop("_checking", None)
            _HEALTH["ts"] = 0

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}, 200


# ----------------------------------------------------------------------------- HTTP

def find_fav(name):
    for d in FAV_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def logo_path():
    for name in ("logo.png", "android-icon-192x192.png", "apple-icon-180x180.png", "favicon-96x96.png"):
        p = find_fav(name)
        if p:
            return p
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "Muzick"

    def _auth(self):
        if not PASS:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                given = base64.b64decode(h[6:]).decode("utf-8").split(":", 1)[1]
                if hmac.compare_digest(given.encode(), PASS.encode()):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Muzick"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send(self, body, ctype, code=200, cache=False):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", code)

    def _file(self, path, cache=False):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(data, ctype, cache=cache)

    def do_GET(self):
        if not self._auth():
            return
        path = self.path.split("?", 1)[0]
        if path == "/":
            index = os.path.join(UI_DIR, "index.html")
            if os.path.isfile(index):
                return self._file(index)
            return self._send("<meta charset=utf-8><h1>Не найден интерфейс</h1><p>Папка ui/ не скопирована в %s. "
                              "Запустите install.sh ещё раз.</p>" % UI_DIR, "text/html; charset=utf-8", 500)
        if path in ("/static/app.css", "/static/app.js"):
            return self._file(os.path.join(UI_DIR, os.path.basename(path)))
        if path == "/logo.png":
            p = logo_path()
            return self._file(p, True) if p else self._send(b"", "image/png", 404)
        m = re.fullmatch(r"/fav/([A-Za-z0-9._-]+\.(?:png|ico))", path)
        if m:
            p = find_fav(m.group(1))
            return self._file(p, True) if p else self._send(b"", "image/png", 404)
        if path == "/api/status":
            return self._json(build_status())
        if path == "/api/settings":
            return self._json(settings_view())
        if path == "/api/directories":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                kind = (query.get("kind") or [""])[0]
                rel = (query.get("path") or [""])[0]
                return self._json(directory_listing(kind, rel))
            except (ValueError, OSError) as e:
                return self._json({"error": str(e)}, 400)
        if path == "/api/history":
            return self._json(read_json(HISTORY_FILE, []))
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._auth():
            return
        if self.headers.get("X-Muzick") != "1":
            return self._json({"error": "forbidden"}, 403)
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 65536)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        path = self.path.split("?", 1)[0]
        if path == "/api/run":
            ids = plan_ids() if body.get("all") else [i for i in body.get("steps", []) if isinstance(i, str)]
            res, code = start_run(ids, "manual")
        elif path == "/api/cancel":
            res, code = cancel_run()
        elif path == "/api/confirm":
            CONFIRM["answer"] = bool(body.get("answer"))
            CONFIRM["event"].set()
            res, code = {"ok": True}, 200
        elif path == "/api/settings":
            try:
                apply_settings_patch(body)
                res, code = settings_view(), 200
            except (ValueError, OSError) as e:
                res, code = {"error": str(e)}, 400
        elif path == "/api/folder-select":
            try:
                kind = body.get("kind")
                rel = _safe_rel(body.get("path", ""))
                if kind not in FOLDER_ROOTS:
                    raise ValueError("Неизвестный тип папки")
                target = os.path.realpath(os.path.join(FOLDER_ROOTS[kind], rel))
                root = os.path.realpath(FOLDER_ROOTS[kind])
                if os.path.commonpath([root, target]) != root or not os.path.isdir(target):
                    raise ValueError("Выберите существующую папку в разрешённой области")
                paths = dict(SETTINGS.get("folder_paths", {}))
                paths[kind] = rel
                candidate_source = os.path.realpath(os.path.join(FOLDER_ROOTS["source"], paths.get("source", "")))
                candidate_library = os.path.realpath(os.path.join(FOLDER_ROOTS["library"], paths.get("library", "")))
                if (candidate_source == candidate_library or
                    os.path.commonpath([candidate_source, candidate_library]) in (candidate_source, candidate_library)):
                    raise ValueError("Папки загрузок и библиотеки не должны совпадать или находиться одна внутри другой")
                apply_settings_patch({"folder_paths": paths})
                res, code = settings_view(), 200
            except (ValueError, OSError) as e:
                res, code = {"error": str(e)}, 400
        elif path == "/api/check-net":
            res, code = start_net_check()
        else:
            res, code = {"error": "not found"}, 404
        self._json(res, code)

    def log_message(self, *a):
        pass


def main():
    if not os.path.exists(CONFIG_YAML):
        write_beets_config()
    docker_cleanup_jobs()
    refresh_source_count(True)
    threading.Thread(target=scheduler, daemon=True).start()
    log("Панель запущена")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
