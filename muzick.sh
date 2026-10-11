#!/usr/bin/env bash
# muzick — управление beets и веб-панелью на Synology.
# Настройки читаются из muzick.env рядом со скриптом (его создаёт install.sh).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
if [ -f "$HERE/.env" ]; then
  . "$HERE/.env"
elif [ -f "$HERE/muzick.env" ]; then
  . "$HERE/muzick.env"
else
  echo "Не найден .env или muzick.env рядом с muzick.sh" >&2; exit 1
fi

CFG="$BASE_DIR/config"
SELF="$HERE/muzick.sh"

COMMON=(
  --user "$PUID:$PGID"
  -e BEETSDIR=/config -e HOME=/config -e PYTHONUNBUFFERED=1
  -v "$CFG:/config"
  -v "$LIB_DIR:/music"
  -v "$SRC_DIR:/downloads:rw"
  -v "$WORK_DIR:/work:rw"
)

# Сеть VPN-контейнера, если он задан и запущен (нужна для MusicBrainz).
vpn_net() {
  if [ -n "${VPN_CONTAINER:-}" ] && docker ps --format '{{.Names}}' | grep -qx "$VPN_CONTAINER"; then
    printf '%s' "--network container:$VPN_CONTAINER"
  fi
}

# Возвращает 0 (истина), если уже что-то импортируется: два импорта в одну базу запускать нельзя.
busy() {
  if docker ps --format '{{.Names}}' | grep -qx beets-mb; then
    echo "Уже идёт перетегирование (контейнер beets-mb). Остановить: $SELF mb-stop"
    return 0
  fi
  if docker ps --format '{{.Names}}' | grep -qx beets-single; then
    echo "Уже идёт определение по отпечатку (контейнер beets-single). Остановить: $SELF single-stop"
    return 0
  fi
  if docker top beets-panel 2>/dev/null | grep -Eq 'beet .*import|beet import'; then
    echo "В панели идёт импорт. Дождитесь окончания или остановите его в панели."
    return 0
  fi
  return 1
}

# Скрипт исправления тегов (внутри контейнера, нужна запись в /music).
fix_tags() {
  if [ ! -f "$CFG/fix_tags.py" ]; then
    echo "Команда недоступна: fix_tags.py отсутствует в репозитории и каталоге $CFG." >&2
    echo "Остальные функции beets работают; файл библиотеки не изменён." >&2
    return 2
  fi
  docker run --rm -it "${COMMON[@]}" --entrypoint /lsiopy/bin/python3 "$IMAGE" \
    /config/lockrun.py /lsiopy/bin/python3 /config/fix_tags.py "$@"
}

beet_run() {
  # Интерактивные команды без VPN.
  docker run --rm -it "${COMMON[@]}" --entrypoint /lsiopy/bin/python3 "$IMAGE" \
    /config/lockrun.py /lsiopy/bin/beet "$@"
}

beet_run_plain() {
  # Без TTY — для перенаправления вывода и конвейеров.
  docker run --rm "${COMMON[@]}" --entrypoint /lsiopy/bin/python3 "$IMAGE" \
    /config/lockrun.py /lsiopy/bin/beet "$@"
}

beet_vpn_run() {
  # MusicBrainz-запросы идут через VPN, но база остаётся общей.
  # shellcheck disable=SC2046
  docker run --rm -it $(vpn_net) "${COMMON[@]}" --entrypoint /lsiopy/bin/python3 "$IMAGE" \
    /config/lockrun.py /lsiopy/bin/beet "$@"
}

up() {
  docker rm -f beets beets-panel >/dev/null 2>&1 || true
  docker run -d --name beets --restart unless-stopped \
    -e PUID="$PUID" -e PGID="$PGID" -e TZ="$TZ_NAME" \
    -p "$WEB_PORT:8337" \
    -v "$CFG:/config" -v "$LIB_DIR:/music" -v "$SRC_DIR:/downloads:rw" \
    "$IMAGE" >/dev/null
  # Доступ к Docker нужен панели, чтобы запускать задачи MusicBrainz через VPN без терминала.
  # Отключается параметром DOCKER_ACCESS=0 при установке.
  local extra=()
  if [ "${DOCKER_ACCESS:-1}" = 1 ] && [ -S /var/run/docker.sock ]; then
    extra+=(-v /var/run/docker.sock:/var/run/docker.sock --group-add "$(stat -c %g /var/run/docker.sock)")
  fi
  if [ -d "$BASE_DIR/fav.ico" ]; then
    extra+=(-v "$BASE_DIR/fav.ico:/fav.ico:ro")
  fi
  docker run -d --name beets-panel --restart unless-stopped \
    --user "$PUID:$PGID" \
    -p "$PANEL_PORT:8338" \
    -e BEETSDIR=/config -e HOME=/config -e PYTHONUNBUFFERED=1 -e PANEL_PASS="$PANEL_PASS" -e WORK_DIR_HOST="$WORK_DIR" \
    -e MUZICK_IMAGE="$IMAGE" -e HOST_CONFIG="$CFG" -e HOST_LIB="$LIB_DIR" -e HOST_SRC="$SRC_DIR" \
    -e VPN_CONTAINER="${VPN_CONTAINER:-VPN}" -e FILE_UID="$PUID" -e FILE_GID="$PGID" \
    -v "$CFG:/config" -v "$LIB_DIR:/music" -v "$SRC_DIR:/downloads:rw" -v "$WORK_DIR:/work:rw" \
    ${extra[@]+"${extra[@]}"} \
    --entrypoint python3 "$IMAGE" "/config/${PANEL_FILE:-panel.py}" >/dev/null
}

usage() {
  cat <<EOF
Использование: $SELF <команда> [аргументы]

  status             состояние контейнеров и статистика библиотеки
  tags [папка]       импорт с копированием по тегам; папка — внутри $SRC_DIR
  direct-tags [папка] импорт по тегам прямо в исходной папке, без копирования
  direct-mb [папка]  импорт с MusicBrainz прямо в папке, без копирования; папка — внутри $WORK_DIR
  fix-encoding [--apply]  исправить кракозябры в тегах (без --apply только отчёт)
  artists-suggest    найти варианты написания одного исполнителя (inxs/INXS, ё/е) -> черновик соответствий
  artists-preview    показать, что изменит artist-aliases.tsv
  artists-apply      применить artist-aliases.tsv
  organize           переложить файлы по новым тегам (сначала предпросмотр, затем вопрос)
  mb-test <артист>   интерактивное перетегирование одного исполнителя через MusicBrainz (VPN)
  mb-all [запрос]    перетегирование всей библиотеки в фоне (прогресс в веб-панели)
  mb-stop            остановить перетегирование
  single [папка]     импорт отдельных треков по звуковому отпечатку в фоне через VPN (прогресс в панели)
  single-stop        остановить определение по отпечатку
  beet <аргументы>   любая команда beet (через VPN, если он запущен), например: beet import -s /downloads/Папка
  dups               дубликаты альбомов и треков по правилам beets
  dups-audit         read-only аудит повторяющихся записей SQLite
  logs               логи контейнеров
  up                 пересоздать контейнеры beets и beets-panel
  update             скачать свежий образ и пересоздать контейнеры
  down               удалить контейнеры (данные и конфиг остаются)
EOF
}

cmd="${1:-help}"
shift || true

case "$cmd" in
  up)
    if busy; then exit 1; fi
    up
    ;;
  update)
    if busy; then exit 1; fi
    docker pull "$IMAGE"
    up
    ;;
  down)
    docker rm -f beets beets-panel beets-mb beets-single 2>/dev/null || true
    ;;
  status)
    docker ps -a --filter name=beets --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    echo
    docker exec beets beet stats 2>/dev/null || echo "(beets не отвечает)"
    ;;
  tags)
    if busy; then exit 1; fi
    beet_run import -A -q -l /config/import-all.log "/downloads/${1:-}"
    ;;
  direct-tags)
    if busy; then exit 1; fi
    rel="${1:-}"
    case "$rel" in /*|..|../*|*/../*|*/..|./*|*/./*|.) echo "Укажите относительную папку внутри WORK_DIR без сегментов . или .." >&2; exit 2 ;; esac
    beet_run -c /config/config-direct.yaml import -A -q -l /config/direct-import.log "/work/$rel"
    ;;
  direct-mb)
    if busy; then exit 1; fi
    rel="${1:-}"
    case "$rel" in /*|..|../*|*/../*|*/..|./*|*/./*|.) echo "Укажите относительную папку внутри WORK_DIR без сегментов . или .." >&2; exit 2 ;; esac
    beet_vpn_run -c /config/config-direct.yaml import -q -l /config/direct-import.log "/work/$rel"
    ;;
  fix-encoding)
    case " $* " in *" --apply "*) if busy; then exit 1; fi ;; esac
    fix_tags encoding "$@"
    ;;
  artists-suggest)
    fix_tags artists suggest
    ;;
  artists-preview)
    fix_tags artists preview
    ;;
  artists-apply)
    if busy; then exit 1; fi
    fix_tags artists apply
    ;;
  organize)
    if busy; then exit 1; fi
    echo "Предпросмотр перемещений (первые 40):"
    { beet_run_plain move -p | head -n 40; } || true
    read -r -p "Переместить файлы по новым путям? [y/N] " ans
    if [ "$ans" = y ]; then
      beet_run move
    else
      echo "Отменено."
    fi
    ;;
  mb-test)
    [ $# -ge 1 ] || { echo "Укажите исполнителя: $SELF mb-test Accept"; exit 1; }
    if busy; then exit 1; fi
    # shellcheck disable=SC2046
    beet_vpn_run import -L "albumartist:$1"
    ;;
  mb-all)
    if busy; then exit 1; fi
    if [ -z "$(vpn_net)" ]; then
      echo "Внимание: VPN-контейнер '${VPN_CONTAINER:-}' не запущен, MusicBrainz может быть недоступен."
    fi
    docker rm -f beets-mb >/dev/null 2>&1 || true
    rm -f "$CFG/mb.done" "$CFG/mb.running"
    # shellcheck disable=SC2046
    docker run -d --name beets-mb $(vpn_net) "${COMMON[@]}" --entrypoint sh "$IMAGE" -c '
      : > /config/import-mb.log
      rm -f /config/mb.done
      date +%s > /config/mb.running
      /lsiopy/bin/python3 /config/lockrun.py /lsiopy/bin/beet import -L -q -l /config/import-mb.log "$@"
      echo $? > /config/mb.done
      rm -f /config/mb.running' sh "$@" >/dev/null
    echo "Запущено в фоне. Прогресс: веб-панель, блок «Перетегирование через MusicBrainz»."
    ;;
  single)
    if busy; then exit 1; fi
    sub="${1:-}"
    case "$sub" in /*|..|../*|*/../*|*/..|./*|*/./*|.) echo "Укажите относительную папку внутри SRC_DIR без сегментов . или .." >&2; exit 2 ;; esac
    host_dir="$SRC_DIR${sub:+/$sub}"
    [ -d "$host_dir" ] || { echo "Нет папки: $host_dir"; exit 1; }
    if [ -z "$(vpn_net)" ]; then
      echo "Внимание: VPN-контейнер '${VPN_CONTAINER:-}' не запущен, MusicBrainz и AcoustID могут быть недоступны."
    fi
    total="$(find "$host_dir" -type f \( -iname '*.mp3' -o -iname '*.flac' -o -iname '*.m4a' -o -iname '*.mp4' -o -iname '*.ogg' -o -iname '*.opus' -o -iname '*.wav' -o -iname '*.wma' -o -iname '*.ape' -o -iname '*.wv' -o -iname '*.aif' -o -iname '*.aiff' \) -not -path '*/@eaDir/*' | wc -l)"
    printf '%s %s %s\n' "$(date +%s)" "$total" "${sub:-вся коллекция}" > "$CFG/single.info"
    docker rm -f beets-single >/dev/null 2>&1 || true
    # shellcheck disable=SC2046
    docker run -d --name beets-single $(vpn_net) "${COMMON[@]}" --entrypoint sh "$IMAGE" -c '
      : > /config/import-single.log
      rm -f /config/single.done
      touch /config/single.running
      /lsiopy/bin/python3 /config/lockrun.py /lsiopy/bin/beet import -s -q --noincremental -l /config/import-single.log "$1"
      echo $? > /config/single.done
      rm -f /config/single.running' sh "/downloads${sub:+/$sub}" >/dev/null
    echo "Запущено в фоне ($total файлов). Прогресс: веб-панель, блок «Треки по отпечатку»."
    ;;
  single-stop)
    docker rm -f beets-single >/dev/null 2>&1 || true
    rm -f "$CFG/single.running"
    echo "Остановлено."
    ;;
  mb-stop)
    docker rm -f beets-mb >/dev/null 2>&1 || true
    rm -f "$CFG/mb.running"
    echo "Остановлено."
    ;;
  beet)
    beet_vpn_run "$@"
    ;;
  dups)
    docker exec beets /lsiopy/bin/python3 /config/lockrun.py /lsiopy/bin/beet duplicates -a || true
    docker exec beets /lsiopy/bin/python3 /config/lockrun.py /lsiopy/bin/beet duplicates || true
    ;;
  dups-audit)
    docker run --rm "${COMMON[@]}" --entrypoint /lsiopy/bin/python3 "$IMAGE" \
      /config/lockrun.py /lsiopy/bin/python3 /config/diagnose_duplicates.py
    ;;
  logs)
    for c in beets beets-panel beets-mb beets-single; do
      if docker ps -a --format '{{.Names}}' | grep -qx "$c"; then
        echo "=== $c ==="
        docker logs --tail 20 "$c" 2>&1
      fi
    done
    ;;
  help|-h|--help|*)
    usage
    ;;
esac
