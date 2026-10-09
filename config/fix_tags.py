#!/usr/bin/env python3
"""Исправление тегов в библиотеке beets (запускается внутри контейнера beets).

  fix_tags.py encoding [--apply]   кракозябры -> кириллица (Windows-1251, прочитанная как Latin-1)
  fix_tags.py artists suggest      найти варианты написания одного исполнителя (регистр, ё/е, знаки)
  fix_tags.py artists preview      показать, что изменит /config/artist-aliases.tsv
  fix_tags.py artists apply        применить /config/artist-aliases.tsv

Без --apply (и в режимах suggest/preview) ничего не меняется: только отчёт.
Перед изменением делается копия базы. Файлы после этого нужно переложить: muzick.sh organize
"""
import argparse
import os
import re
import shutil
import sys
import time
import unicodedata
from collections import Counter, defaultdict

DB_DEFAULT = "/config/musiclibrary.db"
DIR_DEFAULT = "/music"
ALIASES = "/config/artist-aliases.tsv"
SUGGEST = "/config/artist-aliases.suggested.tsv"
REPORT = "/config/fix-encoding-report.tsv"

TEXT_FIELDS = (
    "artist", "albumartist", "album", "title", "genre", "composer", "lyricist", "arranger",
    "comments", "grouping", "artist_sort", "albumartist_sort", "artist_credit",
    "albumartist_credit", "albumdisambig", "work", "label", "catalognum", "lyrics",
    "artists", "albumartists", "genres", "composers", "lyricists", "arrangers", "remixers",
    "artists_sort", "albumartists_sort", "artists_credit", "albumartists_credit",
)
ARTIST_FIELDS = ("artist", "albumartist", "artists", "albumartists")

CYR = re.compile(r"[\u0400-\u04FF]")
SUSPECT = re.compile(r"[\u00C0-\u00FF]")
NEUTRAL = set("«»№—–…“”„ ")


# ---------------------------------------------------------------- логика текста

def _nonascii_runs(s):
    """(число не-ASCII символов, из них в сериях длиной >= 2)."""
    total = in_runs = run = 0
    for ch in s + "\0":
        if ord(ch) > 127:
            run += 1
        else:
            total += run
            if run >= 2:
                in_runs += run
            run = 0
    return total, in_runs


def fix_text(s):
    """Возвращает исправленную строку или ту же самую, если это не кракозябры."""
    if not isinstance(s, str) or not SUSPECT.search(s) or CYR.search(s):
        return s
    total, in_runs = _nonascii_runs(s)
    # настоящие кракозябры состоят из серий не-ASCII символов, а не из одиночных «é» и «ö»
    if total < 2 or in_runs * 10 < total * 6:
        return s
    raw = None
    for enc in ("latin-1", "cp1252"):
        try:
            raw = s.encode(enc)
            break
        except UnicodeEncodeError:
            continue
    if raw is None:
        return s
    # вариант 1: UTF-8, прочитанный как Latin-1
    try:
        u = raw.decode("utf-8")
        if CYR.search(u):
            return u
    except UnicodeDecodeError:
        pass
    # вариант 2: Windows-1251, прочитанная как Latin-1 (самый частый случай)
    try:
        c = raw.decode("cp1251")
    except UnicodeDecodeError:
        return s
    idx = [i for i, ch in enumerate(s) if ord(ch) > 127]
    good = sum(1 for i in idx if CYR.match(c[i]) or c[i] in NEUTRAL)
    if idx and good * 10 >= len(idx) * 8:
        return c
    return s


def norm_key(name):
    """Ключ для поиска вариантов написания: без регистра, ё=е, без пробелов и знаков."""
    s = unicodedata.normalize("NFKC", name).casefold().replace("ё", "е")
    return re.sub(r"[\W_]+", "", s)


# ---------------------------------------------------------------- работа с beets

def open_lib(db, directory):
    from beets.library import Library  # импорт здесь, чтобы функции выше проверялись без beets
    return Library(db, directory)


def _transform(value, fn):
    if isinstance(value, list):
        return [fn(x) if isinstance(x, str) else x for x in value]
    return fn(value) if isinstance(value, str) else value


def plan(objs, fn, fields):
    """[(объект, {поле: (было, стало)})] для объектов, у которых что-то меняется."""
    out = []
    for obj in objs:
        changes = {}
        for name in fields:
            value = obj.get(name)
            if not value:
                continue
            new = _transform(value, fn)
            if new != value:
                changes[name] = (value, new)
        if changes:
            out.append((obj, changes))
    return out


def count_pairs(item_plan):
    """Counter {(поле, было, стало): число треков}."""
    stats = Counter()
    for _, changes in item_plan:
        for name, (old, new) in changes.items():
            olds = old if isinstance(old, list) else [old]
            news = new if isinstance(new, list) else [new]
            for o, n in zip(olds, news):
                if o != n:
                    stats[(name, o, n)] += 1
    return stats


def backup_db(db):
    dst = "%s.bak-%s" % (db, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(db, dst)
    print("Копия базы:", dst)


def apply_changes(lib, fn, fields, db):
    """Меняет альбомы, затем треки, и записывает теги в файлы. Возвращает (изменено, ошибок записи)."""
    backup_db(db)
    to_write = set()

    albums = plan(list(lib.albums()), fn, fields)
    with lib.transaction():
        for album, changes in albums:
            for name, (_, new) in changes.items():
                album[name] = new
            album.store()
            to_write.update(i.id for i in album.items())

    # треки перечитываем из базы: часть полей уже обновилась вместе с альбомом
    items = plan(list(lib.items()), fn, fields)
    with lib.transaction():
        for item, changes in items:
            for name, (_, new) in changes.items():
                item[name] = new
            item.store()
            to_write.add(item.id)

    failed = 0
    for n, item_id in enumerate(sorted(to_write), 1):
        item = lib.get_item(item_id)
        if item is None:
            continue
        if not item.try_write(id3v23=True):
            failed += 1
        if n % 500 == 0:
            print("  записано тегов: %d из %d" % (n, len(to_write)))
    return len(albums), len(items), len(to_write), failed


# ---------------------------------------------------------------- команды

def cmd_encoding(args):
    lib = open_lib(args.db, args.dir)
    item_plan = plan(list(lib.items()), fix_text, TEXT_FIELDS)
    stats = count_pairs(item_plan)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("поле\tбыло\tстало\tтреков\n")
        for (name, old, new), n in sorted(stats.items(), key=lambda kv: (-kv[1], kv[0])):
            f.write("%s\t%s\t%s\t%d\n" % (name, old, new, n))
    print("Треков с кракозябрами: %d, различных замен: %d" % (len(item_plan), len(stats)))
    print("Полный отчёт: %s\n" % REPORT)
    print("%-13s %-45s -> %s" % ("поле", "было", "станет (треков)"))
    for (name, old, new), n in sorted(stats.items(), key=lambda kv: (-kv[1], kv[0]))[:40]:
        print("%-13s %-45s -> %s (%d)" % (name, old[:45], new[:60], n))
    if len(stats) > 40:
        print("... и ещё %d замен в отчёте" % (len(stats) - 40))
    if not item_plan:
        print("Менять нечего.")
        return
    if not args.apply:
        print("\nПросмотр, ничего не изменено. Применить: muzick.sh fix-encoding --apply")
        return
    a, i, w, failed = apply_changes(lib, fix_text, TEXT_FIELDS, args.db)
    print("\nГотово: альбомов изменено %d, треков %d, тегов записано %d, ошибок записи %d."
          % (a, i, w - failed, failed))
    print("Теперь переложите файлы по новым именам: muzick.sh organize")


def _artist_counts(lib):
    counts = Counter()
    for item in lib.items():
        for name in ("albumartist", "artist"):
            v = item.get(name)
            if v and isinstance(v, str):
                counts[v] += 1
    return counts


def cmd_artists_suggest(args):
    lib = open_lib(args.db, args.dir)
    counts = _artist_counts(lib)
    groups = defaultdict(list)
    for name, n in counts.items():
        key = norm_key(name)
        if key:
            groups[key].append((name, n))
    lines, shown = [], 0
    for key, variants in sorted(groups.items()):
        if len(variants) < 2:
            continue
        # каноническое имя: самое частое, при равенстве - не «ВСЕ ЗАГЛАВНЫЕ» и не «все строчные»
        variants.sort(key=lambda v: (-v[1], v[0] == v[0].lower() or v[0] == v[0].upper(),
                                     "ё" not in v[0].casefold(), v[0]))
        canon = variants[0][0]
        for name, n in variants[1:]:
            lines.append((name, canon, n))
        shown += 1
        print("%s  <-  %s" % (canon, ", ".join("%s (%d)" % v for v in variants[1:])))
    with open(SUGGEST, "w", encoding="utf-8") as f:
        f.write("# вариант<TAB>каноническое имя<TAB># число записей. Удалите строки, которые не нужны,\n")
        f.write("# исправьте каноническое имя при необходимости и сохраните как artist-aliases.tsv\n")
        for name, canon, n in lines:
            f.write("%s\t%s\t# %d\n" % (name, canon, n))
    print("\nГрупп вариантов: %d. Черновик: %s" % (shown, SUGGEST))
    print("Проверьте его, переименуйте в artist-aliases.tsv и выполните: muzick.sh artists-preview")


def load_aliases():
    if not os.path.exists(ALIASES):
        sys.exit("Нет файла %s. Сначала: muzick.sh artists-suggest, затем переименуйте черновик." % ALIASES)
    mapping = {}
    with open(ALIASES, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
                print("Строка %d пропущена (нужны две колонки через табуляцию): %r" % (ln, line))
                continue
            variant, canon = parts[0].strip(), parts[1].strip()
            if variant != canon:
                mapping[variant] = canon
    # цепочки A->B, B->C сводим к A->C; циклы отбрасываем
    for variant in list(mapping):
        if variant not in mapping:
            continue
        seen, target = {variant}, mapping[variant]
        while target in mapping and target not in seen:
            seen.add(target)
            target = mapping[target]
        if target in seen:
            print("Цикл в соответствиях, строки пропущены:", ", ".join(sorted(seen)))
            for name in seen:
                mapping.pop(name, None)
        else:
            mapping[variant] = target
    return mapping


def cmd_artists(args):
    if args.action == "suggest":
        return cmd_artists_suggest(args)
    mapping = load_aliases()
    lib = open_lib(args.db, args.dir)
    fn = lambda s: mapping.get(s, s)  # noqa: E731
    item_plan = plan(list(lib.items()), fn, ARTIST_FIELDS)
    stats = count_pairs(item_plan)
    per_pair = Counter()
    for (_, old, new), n in stats.items():
        per_pair[(old, new)] += n
    print("Соответствий в файле: %d, затронуто треков: %d\n" % (len(mapping), len(item_plan)))
    for (old, new), n in sorted(per_pair.items(), key=lambda kv: -kv[1])[:60]:
        print("%-40s -> %s (%d)" % (old[:40], new, n))
    if not item_plan:
        print("Менять нечего.")
        return
    if args.action == "preview":
        print("\nПросмотр, ничего не изменено. Применить: muzick.sh artists-apply")
        return
    a, i, w, failed = apply_changes(lib, fn, ARTIST_FIELDS, args.db)
    print("\nГотово: альбомов изменено %d, треков %d, тегов записано %d, ошибок записи %d."
          % (a, i, w - failed, failed))
    print("Теперь переложите файлы: muzick.sh organize")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=DB_DEFAULT)
    p.add_argument("--dir", default=DIR_DEFAULT)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("encoding")
    e.add_argument("--apply", action="store_true")
    a = sub.add_parser("artists")
    a.add_argument("action", choices=["suggest", "preview", "apply"])
    args = p.parse_args()
    if args.cmd == "encoding":
        cmd_encoding(args)
    else:
        cmd_artists(args)


if __name__ == "__main__":
    main()
