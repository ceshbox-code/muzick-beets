#!/usr/bin/env bash
# Обновление только веб-интерфейса Muzick. Базы и музыкальные файлы не трогаются.
# Запуск на Synology: sudo bash update-ui.sh
set -euo pipefail
BASE_DIR="${BASE_DIR:-/volume1/docker/beets}"
BRANCH="fix/safe-import-and-install"
URL="https://github.com/ceshbox-code/muzick-beets/archive/refs/heads/${BRANCH}.tar.gz"
STAMP="$(date +%Y%m%d-%H%M%S)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
die(){ echo "Ошибка: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "Запустите: sudo bash update-ui.sh"
command -v docker >/dev/null 2>&1 || die "Docker не найден"
[ -d "$BASE_DIR/config" ] || die "Не найдена папка $BASE_DIR/config"
echo "Скачиваю обновление интерфейса…"
curl -fL --retry 2 "$URL" -o "$TMP/project.tar.gz"
tar -xzf "$TMP/project.tar.gz" -C "$TMP"
ROOT="$(find "$TMP" -type f -name dashboard.html -print -quit | xargs -r dirname)"
[ -n "$ROOT" ] && [ -f "$ROOT/panel.py" ] && [ -d "$ROOT/fav.ico" ] || die "В архиве отсутствуют необходимые файлы"
echo "Создаю резервные копии…"
for f in panel.py dashboard.html; do
  [ ! -f "$BASE_DIR/config/$f" ] || cp -p "$BASE_DIR/config/$f" "$BASE_DIR/config/$f.bak-$STAMP"
done
[ ! -d "$BASE_DIR/config/fav.ico" ] || cp -a "$BASE_DIR/config/fav.ico" "$BASE_DIR/config/fav.ico.bak-$STAMP"
echo "Устанавливаю новую панель…"
cp "$ROOT/panel.py" "$BASE_DIR/config/panel.py"
cp "$ROOT/dashboard.html" "$BASE_DIR/config/dashboard.html"
mkdir -p "$BASE_DIR/config/fav.ico"
cp -R "$ROOT/fav.ico/." "$BASE_DIR/config/fav.ico/"
# The project logo is maintained on the default branch; fetch it separately if needed.
curl -fsSL --retry 2 "https://raw.githubusercontent.com/ceshbox-code/muzick-beets/main/fav.ico/logo.png" -o "$BASE_DIR/config/fav.ico/logo.png" || echo "Предупреждение: логотип не удалось скачать; существующий файл сохранён."
chmod 644 "$BASE_DIR/config/panel.py" "$BASE_DIR/config/dashboard.html"
echo "Перезапускаю только контейнер beets-panel…"
docker restart beets-panel >/dev/null
sleep 3
if [ -f "$BASE_DIR/muzick.env" ]; then
  PANEL_PASS="$(bash -c '. "$1"; printf %s "${PANEL_PASS:-}"' _ "$BASE_DIR/muzick.env")"
else
  PANEL_PASS=""
fi
if [ -n "$PANEL_PASS" ]; then
  CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 -u "muzick:$PANEL_PASS" http://127.0.0.1:8338/api/settings || true)"
else
  CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:8338/api/settings || true)"
fi
[ "$CODE" = 200 ] || die "Панель не подтвердила работу (HTTP $CODE). Проверьте журнал контейнера beets-panel; резервные копии сохранены."
echo
echo "Готово. Базы и музыкальные файлы не изменялись."
echo "Откройте панель и обновите страницу без кэша (Ctrl+F5)."
echo "Резервные копии: $BASE_DIR/config/*.bak-$STAMP"
