import os, sqlite3, time
from http.server import BaseHTTPRequestHandler, HTTPServer

SRC, DB, LOG = "/downloads", "/config/musiclibrary.db", "/config/import-all.log"

def count_src():
    n = 0
    for root, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d not in ("@eaDir", "#recycle")]
        n += sum(f.lower().endswith(".mp3") for f in files)
    return n

TOTAL = count_src()
START = {}

def db_counts():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    try:
        items = con.execute("select count(*) from items").fetchone()[0]
        albums = con.execute("select count(*) from albums").fetchone()[0]
    finally:
        con.close()
    return items, albums

def tail(path, n=10):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return "(лог пока пуст)"

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            items, albums = db_counts()
        except Exception as e:
            items = albums = 0
        now = time.time()
        START.setdefault("t", now); START.setdefault("c", items)
        rate = (items - START["c"]) / max(now - START["t"], 1) * 60
        pct = min(100, items * 100 / TOTAL) if TOTAL else 0
        eta = f"{(TOTAL - items) / rate:.0f} мин" if rate > 0 and items < TOTAL else "—"
        html = f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=5>
<title>Прогресс импорта</title>
<body style="font-family:sans-serif;max-width:640px;margin:2em auto">
<h2>Импорт музыки</h2>
<div style="background:#ddd;border-radius:6px"><div style="width:{pct:.1f}%;background:#3a7;color:#fff;padding:4px;border-radius:6px">{pct:.1f}%</div></div>
<p>Треков в библиотеке: <b>{items}</b> из {TOTAL}<br>Альбомов: <b>{albums}</b><br>
Скорость: {rate:.0f} треков/мин, осталось ≈ {eta}<br>(скорость считается с момента первого открытия страницы)</p>
<h3>Пропущенное (лог)</h3><pre style="background:#f4f4f4;padding:8px;overflow:auto">{tail(LOG)}</pre>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())
    def log_message(self, *a): pass

HTTPServer(("0.0.0.0", 8338), H).serve_forever()
