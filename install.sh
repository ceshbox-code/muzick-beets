#!/usr/bin/env bash
# Установка beets + веб-панели на Synology DSM одной командой:
#   sudo bash install.sh
# Параметры задаются переменными окружения, например:
#   sudo SRC_DIR=/volume1/music LIB_DIR=/volume1/music_clean VPN_CONTAINER=VPN bash install.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Сначала читаем существующие настройки, чтобы повторная установка их не сбросила.
BASE_DIR="${BASE_DIR:-/volume1/docker/beets}"
if [ -f "$BASE_DIR/.env" ]; then
  set -a; . "$BASE_DIR/.env"; set +a
elif [ -f "$BASE_DIR/muzick.env" ]; then
  set -a; . "$BASE_DIR/muzick.env"; set +a
fi

SRC_DIR="${SRC_DIR:-/volume1/music}"              # исходная папка (для сценария загрузок укажите DOWNLOADS)
LIB_DIR="${LIB_DIR:-/volume1/music_clean}"        # конечная музыкальная библиотека
BASE_DIR="${BASE_DIR:-/volume1/docker/beets}"     # конфиг, база beets, панель
VPN_CONTAINER="${VPN_CONTAINER-VPN}"              # имя VPN-контейнера; пусто = без VPN
PANEL_PORT="${PANEL_PORT:-8338}"
WEB_PORT="${WEB_PORT:-8337}"
IMAGE="${IMAGE:-lscr.io/linuxserver/beets:latest}"
TZ_NAME="${TZ_NAME:-Europe/Berlin}"
PANEL_PASS="${PANEL_PASS:-}"
RUN_USER="${RUN_USER:-${SUDO_USER:-}}"
FORCE_CONFIG="${FORCE_CONFIG:-0}"
DOCKER_ACCESS="${DOCKER_ACCESS:-1}"               # 1 = панель сама запускает задачи через VPN (нужен доступ к Docker)
PANEL_FILE="${PANEL_FILE:-panel.py}"              # panel_classic.py = прежний вид панели
WORK_DIR="${WORK_DIR:-$SRC_DIR}"                  # папка, где разрешена обработка «на месте» (панель, direct-*)

cpf() { [ "$1" -ef "$2" ] || cp "$1" "$2"; }
say() { printf '\n==> %s\n' "$*"; }
die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запустите от root: sudo bash install.sh"
command -v docker >/dev/null 2>&1 || die "Docker не найден (установите Container Manager в DSM)"
[ -d "$SRC_DIR" ] || die "нет папки с музыкой: $SRC_DIR (задайте SRC_DIR=...)"
for f in muzick.sh panel.py fix_tags.py lockrun.py diagnose_duplicates.py config.yaml config-direct.yaml ui/index.html ui/app.css ui/app.js; do
  [ -f "$HERE/$f" ] || die "нет файла $f рядом с install.sh"
done
case "$PANEL_PASS" in
  *\'*|*\"*|*\ *) die "пароль не должен содержать кавычки и пробелы" ;;
esac

# --- от чьего имени работать с файлами ---
if [ -n "${FILE_UID:-}" ] && [ -n "${FILE_GID:-}" ]; then
  PUID="$FILE_UID"
  PGID="$FILE_GID"
elif [ -n "$RUN_USER" ] && [ "$RUN_USER" != root ] && id "$RUN_USER" >/dev/null 2>&1; then
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
  cpf "$HERE/config.yaml" "$CFG_FILE"
fi
cpf "$HERE/panel.py" "$BASE_DIR/config/panel.py"
[ -f "$HERE/panel_classic.py" ] && cpf "$HERE/panel_classic.py" "$BASE_DIR/config/panel_classic.py"
[ "$HERE/ui" -ef "$BASE_DIR/config/ui" ] || { rm -rf "$BASE_DIR/config/ui" && cp -r "$HERE/ui" "$BASE_DIR/config/ui"; }
if [ -d "$HERE/fav.ico" ] && [ "$HERE" != "$BASE_DIR" ]; then
  mkdir -p "$BASE_DIR/fav.ico" && cp -r "$HERE/fav.ico/." "$BASE_DIR/fav.ico/"
fi
cpf "$HERE/fix_tags.py" "$BASE_DIR/config/fix_tags.py"
cpf "$HERE/lockrun.py" "$BASE_DIR/config/lockrun.py"
cpf "$HERE/diagnose_duplicates.py" "$BASE_DIR/config/diagnose_duplicates.py"
[ -f "$BASE_DIR/config/config-direct.yaml" ] || cp "$HERE/config-direct.yaml" "$BASE_DIR/config/config-direct.yaml"
cpf "$HERE/muzick.sh" "$BASE_DIR/muzick.sh"
chmod 755 "$BASE_DIR/muzick.sh"
chown -R "$PUID:$PGID" "$BASE_DIR/config" "$BASE_DIR/muzick.sh"

# --- пароль панели ---
ENV_FILE="$BASE_DIR/.env"
LEGACY_ENV_FILE="$BASE_DIR/muzick.env"
GENERATED=0
# Совместимость: переносим старую конфигурацию, если .env ещё не создан.
if [ ! -f "$ENV_FILE" ] && [ -f "$LEGACY_ENV_FILE" ]; then
  cp "$LEGACY_ENV_FILE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi
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
  printf 'WORK_DIR=%q\n' "$WORK_DIR"
  printf 'DOCKER_ACCESS=%q\n' "$DOCKER_ACCESS"
  printf 'PANEL_FILE=%q\n' "$PANEL_FILE"
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
# Оставляем совместимый файл для старых скриптов/установок.
cp "$ENV_FILE" "$LEGACY_ENV_FILE"
chmod 600 "$LEGACY_ENV_FILE"

# --- контейнеры ---
say "Скачиваю образ $IMAGE (первый раз может занять несколько минут)"
docker pull "$IMAGE" >/dev/null || die "не удалось скачать образ"

say "Проверка конфига beets"
# В существующий конфиг добавляем настройки, из-за отсутствия которых повторный импорт добавляет дубли.
if ! grep -q '^ *incremental:' "$CFG_FILE" || ! grep -q '^ *duplicate_action:' "$CFG_FILE"; then
  cp "$CFG_FILE" "$CFG_FILE.pre-install.bak"
  grep -q '^ *incremental:' "$CFG_FILE" || \
    sed -i 's/^\( *\)copy: yes/\1copy: yes\n\1incremental: yes/' "$CFG_FILE"
  grep -q '^ *duplicate_action:' "$CFG_FILE" || \
    sed -i 's/^\( *\)copy: yes/\1copy: yes\n\1duplicate_action: skip/' "$CFG_FILE"
  if docker run --rm -e BEETSDIR=/config -v "$BASE_DIR/config:/config" --entrypoint /lsiopy/bin/beet \
       "$IMAGE" config >/dev/null 2>&1; then
    echo "В блок import добавлены incremental и duplicate_action (копия: config.yaml.pre-install.bak)"
  else
    cp "$CFG_FILE.pre-install.bak" "$CFG_FILE"
    echo "Не удалось безопасно изменить config.yaml, оставлен прежний. Добавьте в блок import вручную: incremental: yes и duplicate_action: skip"
  fi
else
  echo "Конфиг в порядке."
fi

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
  Папка обработки:     $SRC_DIR
============================================================
EOF
if [ "$GENERATED" = 1 ]; then
  echo "Пароль панели сгенерирован автоматически, он сохранён в $ENV_FILE."
fi
if [ "$DOCKER_ACCESS" = 1 ]; then
  echo
  echo "Панели выдан доступ к Docker, чтобы запускать задачи через VPN без терминала."
  echo "Он даёт панели широкие права на NAS, поэтому держите её только в локальной сети."
  echo "Отключить: DOCKER_ACCESS=0 sudo -E bash install.sh (шаги через VPN тогда будут недоступны)."
fi
