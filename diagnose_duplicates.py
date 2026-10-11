#!/usr/bin/env python3
"""Read-only report of possible duplicate records in a beets SQLite library."""
import os
import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "/config/musiclibrary.db"

def columns(con, table):
    try:
        return {row[1] for row in con.execute('PRAGMA table_info("%s")' % table)}
    except sqlite3.Error:
        return set()

def report_groups(con, table, fields, title, limit=20):
    available = columns(con, table)
    fields = [f for f in fields if f in available]
    if not fields:
        print("\n[%s] нет подходящих полей в таблице %s" % (title, table))
        return
    where = " AND ".join("COALESCE(TRIM(CAST(%s AS TEXT)), '') <> ''" % f for f in fields)
    normalized = ", ".join("LOWER(TRIM(CAST(%s AS TEXT)))" % f for f in fields)
    sql = 'SELECT COUNT(*) FROM (SELECT 1 FROM "%s" WHERE %s GROUP BY %s HAVING COUNT(*) > 1)' % (table, where, normalized)
    try:
        groups = con.execute(sql).fetchone()[0]
        print("\n[%s] повторяющихся групп: %s" % (title, groups))
        if not groups:
            return
        selected = ", ".join('"%s"' % f for f in fields)
        sample_sql = 'SELECT COUNT(*) AS n, %s FROM "%s" WHERE %s GROUP BY %s HAVING COUNT(*) > 1 ORDER BY n DESC LIMIT %d' % (selected, table, where, normalized, limit)
        for row in con.execute(sample_sql):
            print("  x%-4s %s" % (row[0], " | ".join(str(v) for v in row[1:])))
    except sqlite3.Error as exc:
        print("  ошибка чтения: %s" % exc)

def main():
    if not os.path.isfile(DB):
        print("База не найдена: %s" % DB, file=sys.stderr)
        return 2
    try:
        con = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(DB), uri=True, timeout=10)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            print("База: %s (режим только чтение)" % DB)
            print("Таблицы: %s" % ", ".join(sorted(tables)))
            if "items" in tables:
                report_groups(con, "items", ["path"], "одинаковый путь")
                report_groups(con, "items", ["mb_trackid"], "одинаковый MusicBrainz track ID")
                report_groups(con, "items", ["artist", "title"], "одинаковые artist + title")
            if "albums" in tables:
                report_groups(con, "albums", ["mb_albumid"], "одинаковый MusicBrainz album ID")
                report_groups(con, "albums", ["albumartist", "album"], "одинаковые albumartist + album")
            print("\nВажно: совпадение artist/title или albumartist/album — только кандидат на дубликат, не команда на удаление.")
        finally:
            con.close()
    except sqlite3.Error as exc:
        print("Не удалось прочитать базу: %s" % exc, file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
