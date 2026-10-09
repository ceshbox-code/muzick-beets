#!/usr/bin/env bash
# muzick — управление beets и веб-панелью на Synology.
# Настройки читаются из muzick.env рядом со скриптом (его создаёт install.sh).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$HERE/muzick.env"

CFG="$BASE_DIR/config"
SELF="$HERE/muzick.sh"

COMMON=(
  --user "$PUID:$PGID"
  -e BEETSDIR=/config -e HOME=/config -e PYTHONUNBUFFERED=1
  -v "$CFG:/config"
  -v "$LIB_DIR:/music"
  -v "$SRC_DIR:/downloads:ro"
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
  if docker top beets-panel 2>/dev/null | grep -q 'beet import'; then
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
  # Без VPN: локальные команды и импорт по готовым тегам.
  docker run --rm -it "${COMMON[@]}" --entrypoint /lsiopy/bin/python3 "$IMAGE" \
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
    -v "$CFG:/config" -v "$LIB_DIR:/music" -v "$SRC_DIR:/downloads:ro" \
    "$IMAGE" >/dev/null
  docker run -d --name beets-panel --restart unless-stopped \
    --user "$PUID:$PGID" \
    -p "$PANEL_PORT:8338" \
    -e BEETSDIR=/config -e HOME=/config -e PYTHONUNBUFFERED=1 -e PANEL_PASS="$PANEL_PASS" \
    -v "$CFG:/config" -v "$LIB_DIR:/music" -v "$SRC_DIR:/downloads:ro" \
    --entrypoint python3 "$IMAGE" /config/panel.py >/dev/null
}

usage() {
  cat <<EOF
Использование: $SELF <команда> [аргументы]

  status             состояние контейнеров и статистика библиотеки
  tags [папка]       импорт по существующим тегам (без интернета); папка — внутри $SRC_DIR
  fix-encoding [--apply]  исправить кракозябры в тегах (без --apply только отчёт)
  artists-suggest    найти варианты написания одного исполнителя (inxs/INXS, ё/е) -> черновик соответствий
  artists-preview    показать, что изменит artist-aliases.tsv
  artists-apply      применить artist-aliases.tsv
  organize           переложить файлы по новым тегам (сначала предпросмотр, затем вопрос)
  mb-test <артист>   интерактивное перетегирование одного исполнителя через MusicBrainz (VPN)
  mb-all [запрос]    перетегирование всей библиотеки в фоне (прогресс в веб-панели)
  mb-stop            остановить перетегирование
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
    docker rm -f beets beets-panel beets-mb 2>/dev/null || true
    ;;
  status)
    docker ps -a --filter name=beets --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    echo
    docker exec beets beet stats 2>/dev/null || echo "(beets не отвечает)"
    ;;
  tags)
    if busy; then exit 1; fi
    # shellcheck disable=SC2046
    beet_run import -A -q -l /config/import-all.log "/downloads/${1:-}"
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
    { beet_run move -p | head -n 40; } || true
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
    for c in beets beets-panel beets-mb; do
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
