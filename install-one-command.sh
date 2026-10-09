#!/usr/bin/env bash
# One-command bootstrap for Synology DSM.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ceshbox-code/muzick-beets/fix/safe-import-and-install/install-one-command.sh | sudo bash
# Optional overrides:
#   sudo env SRC_DIR=/volume1/music LIB_DIR=/volume1/music_clean WORK_DIR=/volume1/music BASE_DIR=/volume1/docker/beets bash -c 'curl -fsSL https://raw.githubusercontent.com/ceshbox-code/muzick-beets/fix/safe-import-and-install/install-one-command.sh | bash'
set -Eeuo pipefail

REPO_ARCHIVE="https://github.com/ceshbox-code/muzick-beets/archive/refs/heads/fix/safe-import-and-install.tar.gz"
SRC_DIR="${SRC_DIR:-/volume1/music}"
LIB_DIR="${LIB_DIR:-/volume1/music_clean}"
WORK_DIR="${WORK_DIR:-$SRC_DIR}"
BASE_DIR="${BASE_DIR:-/volume1/docker/beets}"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nОшибка: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запустите через sudo, например: curl -fsSL URL | sudo bash"
command -v docker >/dev/null 2>&1 || die "Docker/Container Manager не найден. Установите его в DSM и повторите."
command -v curl >/dev/null 2>&1 || die "не найден curl"
command -v tar >/dev/null 2>&1 || die "не найден tar"
[ -d "$SRC_DIR" ] || die "не найдена папка исходной музыки: $SRC_DIR. Задайте SRC_DIR=/реальный/путь"
[ -d "$WORK_DIR" ] || mkdir -p "$WORK_DIR" || die "не удалось создать WORK_DIR=$WORK_DIR"

TMP_DIR="$(mktemp -d /tmp/muzick-beets-install.XXXXXX)"
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

say "Скачиваю muzick-beets"
curl -fL --retry 3 --connect-timeout 15 "$REPO_ARCHIVE" -o "$TMP_DIR/project.tar.gz" || die "не удалось скачать архив проекта"
tar -xzf "$TMP_DIR/project.tar.gz" -C "$TMP_DIR" --strip-components=1 || die "архив проекта повреждён или не распаковывается"

[ -f "$TMP_DIR/install.sh" ] || die "в архиве не найден install.sh"
[ -f "$TMP_DIR/docker-compose.yml" ] || die "в архиве не найден docker-compose.yml"

say "Устанавливаю в $BASE_DIR"
export SRC_DIR LIB_DIR WORK_DIR BASE_DIR
cd "$TMP_DIR"
bash "$TMP_DIR/install.sh"

say "Установка завершена"
printf 'Рабочая папка: %s\n' "$WORK_DIR"
printf 'Конфигурация и база: %s/config\n' "$BASE_DIR"
printf 'Управление: %s/muzick.sh status\n' "$BASE_DIR"
printf 'Пароль панели хранится в %s/muzick.env (права 600).\n' "$BASE_DIR"
