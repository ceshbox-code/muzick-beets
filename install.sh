#!/usr/bin/env bash
# Установка beets + веб-панели на Synology DSM одной командой:
#   sudo bash install.sh
# Параметры задаются переменными окружения, например:
#   sudo SRC_DIR=/volume1/music LIB_DIR=/volume1/music_clean VPN_CONTAINER=VPN bash install.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENV_FILE="${BASE_DIR:-/volume1/docker/beets}/muzick.env"
# При повторном запуске сохраняем настройки из предыдущей установки.
# Значения, переданные в окружении текущего запуска, имеют приоритет.
OV_SRC="${SRC_DIR-}"; OV_LIB="${LIB_DIR-}"; OV_BASE="${BASE_DIR-}"
OV_VPN_SET="${VPN_CONTAINER+x}"; OV_VPN="${VPN_CONTAINER-}"; OV_PANEL_PORT="${PANEL_PORT-}"; OV_WEB_PORT="${WEB_PORT-}"
OV_IMAGE="${IMAGE-}"; OV_TZ="${TZ_NAME-}"; OV_PASS="${PANEL_PASS-}"
if [ -f "$ENV_FILE" ]; then
  # Файл создаётся этим установщиком и имеет права 600.
  . "$ENV_FILE"
fi
SRC_DIR="${OV_SRC:-${SRC_DIR:-/volume1/music}}"              # исходная коллекция, только чтение
LIB_DIR="${OV_LIB:-${LIB_DIR:-/volume1/music_clean}}"        # чистая библиотека
BASE_DIR="${OV_BASE:-${BASE_DIR:-/volume1/docker/beets}}"
if [ "$OV_VPN_SET" = x ]; then VPN_CONTAINER="$OV_VPN"; else VPN_CONTAINER="${VPN_CONTAINER:-VPN}"; fi
PANEL_PORT="${OV_PANEL_PORT:-${PANEL_PORT:-8338}}"
WEB_PORT="${OV_WEB_PORT:-${WEB_PORT:-8337}}"
IMAGE="${OV_IMAGE:-${IMAGE:-lscr.io/linuxserver/beets:latest}}"
TZ_NAME="${OV_TZ:-${TZ_NAME:-Europe/Berlin}}"
PANEL_PASS="${OV_PASS:-${PANEL_PASS:-}}"
RUN_USER="${RUN_USER:-${SUDO_USER:-}}"
FORCE_CONFIG="${FORCE_CONFIG:-0}"

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запустите от root: sudo bash install.sh"
command -v docker >/dev/null 2>&1 || die "Docker не найден (установите Container Manager в DSM)"
[ -d "$SRC_DIR" ] || die "нет папки с музыкой: $SRC_DIR (задайте SRC_DIR=...)"
for f in muzick.sh panel.py config.yaml; do
  [ -f "$HERE/$f" ] || die "нет файла $f рядом с install.sh"
done
# fix_tags.py использовался в старых версиях, но отсутствует в текущем репозитории.
# Не блокируем базовую установку; команды fix-tags явно сообщат о недоступности.
case "$PANEL_PASS" in
  *\'*|*\"*|*\ *) die "пароль не должен содержать кавычки и пробелы" ;;
esac

# --- от чьего имени работать с файлами ---
if [ -n "$RUN_USER" ] && [ "$RUN_USER" != root ] && id "$RUN_USER" >/dev/null 2>&1; then
  PUID="$(id -u "$RUN_USER")"
  PGID="$(id -g "$RUN_USER")"
else
  PUID="$(stat -c %u "$SRC_DIR")"
  PGID="$(stat -c %g "$SRC_DIR")"
fi
echo "Пользователь для файлов: UID=$PUID GID=$PGID"

# --- каталоги ---
say "Каталоги"
mkdir -p "$LIB_DIR" "$BASE_DIR/config"
chown "$PUID:$PGID" "$LIB_DIR"
chown -R "$PUID:$PGID" "$BASE_DIR"

# --- файлы проекта ---
say "Файлы проекта"
CFG_FILE="$BASE_DIR/config/config.yaml"
if [ -f "$CFG_FILE" ] && [ "$FORCE_CONFIG" != 1 ]; then
  echo "config.yaml уже есть, оставляю как есть (FORCE_CONFIG=1 перезапишет, копия сохранится в .bak)"
else
  if [ -f "$CFG_FILE" ]; then cp "$CFG_FILE" "$CFG_FILE.bak"; fi
  cp "$HERE/config.yaml" "$CFG_FILE"
fi
cp "$HERE/panel.py" "$BASE_DIR/config/panel.py"
cp "$HERE/lockrun.py" "$BASE_DIR/config/lockrun.py"
if [ -f "$HERE/fix_tags.py" ]; then
  cp "$HERE/fix_tags.py" "$BASE_DIR/config/fix_tags.py"
fi
cp "$HERE/muzick.sh" "$BASE_DIR/muzick.sh"
chmod 755 "$BASE_DIR/muzick.sh"
chown -R "$PUID:$PGID" "$BASE_DIR/config" "$BASE_DIR/muzick.sh"

# --- пароль панели ---
ENV_FILE="$BASE_DIR/muzick.env"
GENERATED=0
if [ -z "$PANEL_PASS" ] && [ -f "$ENV_FILE" ]; then
  PANEL_PASS="$(bash -c '. "$1"; printf %s "${PANEL_PASS:-}"' _ "$ENV_FILE")"
fi
if [ -z "$PANEL_PASS" ]; then
  PANEL_PASS="$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 12)"
  GENERATED=1
fi

{
  printf 'SRC_DIR=%q\n' "$SRC_DIR"
  printf 'LIB_DIR=%q\n' "$LIB_DIR"
  printf 'BASE_DIR=%q\n' "$BASE_DIR"
  printf 'VPN_CONTAINER=%q\n' "$VPN_CONTAINER"
  printf 'PANEL_PORT=%q\n' "$PANEL_PORT"
  printf 'WEB_PORT=%q\n' "$WEB_PORT"
  printf 'IMAGE=%q\n' "$IMAGE"
  printf 'TZ_NAME=%q\n' "$TZ_NAME"
  printf 'PUID=%q\n' "$PUID"
  printf 'PGID=%q\n' "$PGID"
  printf 'PANEL_PASS=%q\n' "$PANEL_PASS"
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"

# --- контейнеры ---
say "Скачиваю образ $IMAGE (первый раз может занять несколько минут)"
docker pull "$IMAGE" >/dev/null || die "не удалось скачать образ"

say "Запускаю контейнеры"
bash "$BASE_DIR/muzick.sh" up

printf 'Жду, пока панель поднимется'
ok=0
for _ in $(seq 1 20); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -u "muzick:$PANEL_PASS" "http://127.0.0.1:$PANEL_PORT/api/status" 2>/dev/null || true)"
  if [ "$code" = 200 ]; then ok=1; break; fi
  printf '.'
  sleep 1
done
echo
if [ "$ok" = 1 ]; then echo "Панель отвечает."; else echo "Панель пока не ответила, смотрите: docker logs beets-panel"; fi

# --- доступ к MusicBrainz ---
say "Проверка доступа к MusicBrainz"
direct="$(curl -4 -s -o /dev/null -w '%{http_code}' --max-time 8 https://musicbrainz.org/ws/2/ 2>/dev/null || true)"
echo "Напрямую с NAS: ${direct:-000} (200 = доступен)"
if [ -n "$VPN_CONTAINER" ] && docker ps --format '{{.Names}}' | grep -qx "$VPN_CONTAINER"; then
  via="$(docker run --rm --network "container:$VPN_CONTAINER" --entrypoint python3 "$IMAGE" \
    -c "import urllib.request as u; print(u.urlopen('https://musicbrainz.org/ws/2/', timeout=15).status)" 2>/dev/null || echo fail)"
  echo "Через контейнер $VPN_CONTAINER: $via"
elif [ -n "$VPN_CONTAINER" ]; then
  echo "VPN-контейнер '$VPN_CONTAINER' не запущен. Если MusicBrainz напрямую недоступен, поднимите VPN или задайте VPN_CONTAINER=имя."
fi

IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") print $(i + 1)}' | head -n1 || true)"
IP="${IP:-IP_NAS}"

cat <<EOF

============================================================
Готово.
  Веб-панель:        http://$IP:$PANEL_PORT   (логин любой, пароль: $PANEL_PASS)
  Просмотр (beets):  http://$IP:$WEB_PORT
  Управление:        sudo $BASE_DIR/muzick.sh help
  Чистая библиотека: $LIB_DIR
  Исходники (только чтение): $SRC_DIR
============================================================
EOF
if [ "$GENERATED" = 1 ]; then
  echo "Пароль панели сгенерирован автоматически, он сохранён в $ENV_FILE."
fi
