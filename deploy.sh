#!/bin/bash
# ============================================================
# 
#
#   для безопасного отката проекта.
#
# ВАЖНО:
#   .env, базы данных, data/, backups/, ключи и другие
#   чувствительные данные НЕ откатываются из GitHub.
# ============================================================

set -uo pipefail

# ------------------------------------------------------------
# Защита от чужого GIT_DIR в окружении
#
# GIT_DIR — зарезервированное имя переменной окружения самого git:
# если оно установлено, git ищет репозиторий ПО ЭТОМУ ПУТИ НАПРЯМУЮ
# (ожидая, что там лежит сама метаинформация репозитория — objects/,
# refs/, HEAD — а не рабочее дерево с подкаталогом .git), и все
# команды git внутри этого скрипта тогда ломаются с непонятной
# ошибкой "not a git repository", даже если путь абсолютно верный.
# Раз мы поддерживаем настройку через переменные окружения — снимаем
# случайно унаследованный GIT_DIR из окружения ДО того, как скрипт
# вызовет хоть одну git-команду. Свою собственную переменную для пути
# к локальному репозиторию скрипт называет GIT_REPO_DIR (см. ниже) —
# именно её и нужно задавать/переопределять, а не GIT_DIR.
# ------------------------------------------------------------
unset GIT_DIR

# ------------------------------------------------------------
# Конфигурация
#
# Приоритет источников значений (первое найденное побеждает):
#   1. Переменная окружения, заданная явно при запуске
#      (например: PROJECT_DIR=/other/path ./deploy.sh)
#   2. Файл deploy.conf рядом со скриптом (или путь в DEPLOY_CONFIG)
#   3. Значения по умолчанию ниже (изначально — для Medical Diary)
#
# Чтобы использовать этот же deploy.sh для ДРУГОГО проекта — либо
# задайте переменные окружения, либо положите рядом со скриптом свой
# deploy.conf. Если ни того ни другого нет и каталоги проекта/Git не
# найдены — при запуске появится мастер настройки, который создаст
# deploy.conf сам (см. run_setup_wizard()).
# ------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
DEPLOY_CONFIG="${DEPLOY_CONFIG:-$SCRIPT_DIR/deploy.conf}"

# Реальное имя файла самого скрипта (обычно deploy.sh, но подхватит и
# переименованный вариант) — используется ниже, чтобы он и его конфиг
# никогда не попали в коммит/push, даже если физически лежат внутри
# GIT_REPO_DIR.
SELF_NAME="$(basename -- "${BASH_SOURCE[0]}")"

if [ -f "$DEPLOY_CONFIG" ]; then
    # shellcheck source=/dev/null
    source "$DEPLOY_CONFIG"
fi

PROJECT_DIR="${PROJECT_DIR:-/s/beets}"
GIT_REPO_DIR="${GIT_REPO_DIR:-/c/Users/Azerty/GitHub/beets}"
GITHUB_REPO="${GITHUB_REPO:-ceshbox-code/muzick-beets}"
GITHUB_URL="${GITHUB_URL:-https://github.com/${GITHUB_REPO}}"
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"

# Имя проекта для заголовков — по умолчанию берётся из имени каталога
# проекта, чтобы один и тот же deploy.sh корректно подписывался для
# любого проекта без правки самого скрипта.
PROJECT_NAME="${PROJECT_NAME:-$(basename "$PROJECT_DIR")}"

# Каталог резервных копий проекта перед rollback
ROLLBACK_BACKUP_DIR="${ROLLBACK_BACKUP_DIR:-${PROJECT_DIR}/../${PROJECT_NAME}_rollback_backups}"

# ------------------------------------------------------------
# Цвета
# ------------------------------------------------------------
if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    CYAN='\033[0;36m'
    MAGENTA='\033[0;35m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    CYAN=''
    MAGENTA=''
    BOLD=''
    NC=''
fi

# ------------------------------------------------------------
# Вспомогательные функции
# ------------------------------------------------------------
log() {
    printf '%b\n' "${BLUE}[INFO]${NC} $*"
}

success() {
    printf '%b\n' "${GREEN}[OK]${NC} $*"
}

warning() {
    printf '%b\n' "${YELLOW}[WARN]${NC} $*"
}

error() {
    printf '%b\n' "${RED}[ERROR]${NC} $*" >&2
}

die() {
    error "$*"
    exit 1
}

pause() {
    echo
    printf '%b' "${CYAN}Нажмите Enter, чтобы продолжить...${NC}"
    read -r _ || true
}

ask_yes_no() {
    local prompt="$1"
    local answer

    while true; do
        printf '%b' "${YELLOW}${prompt} [y/N]: ${NC}"
        read -r answer

        case "${answer,,}" in
            y|yes)
                return 0
                ;;
            n|no|"")
                return 1
                ;;
            *)
                echo "Введите y или n."
                ;;
        esac
    done
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# ------------------------------------------------------------
# Рамки интерфейса
# ------------------------------------------------------------
FRAME_WIDTH=60

box_line() {
    local text="$1"
    local plain="${2:-$1}"
    local len
    local pad

    len=${#plain}
    pad=$((FRAME_WIDTH - len))

    # Если ширина не совпадает из-за локали/кодировки,
    # не ломаем интерфейс, а просто не добавляем отступ.
    if (( pad < 0 )); then
        pad=0
    fi

    printf '%b' "${CYAN}│${NC}"
    printf '%b' "$text"
    printf '%*s' "$pad" ''
    printf '%b\n' "${CYAN}│${NC}"
}

section_title() {
    local title="$1"

    echo
    printf '%b\n' "${BOLD}${CYAN}┌────────────────────────────────────────────────────────────┐${NC}"
    box_line " ${BOLD}${title}${NC}" " ${title}"
    printf '%b\n' "${BOLD}${CYAN}└────────────────────────────────────────────────────────────┘${NC}"
}

show_paths_info() {
    echo
    printf '%b\n' "${CYAN}┌────────────────────────────────────────────────────────────┐${NC}"
    box_line " ${BOLD}${MAGENTA}ОКРУЖЕНИЕ${NC}" " ОКРУЖЕНИЕ"
    printf '%b\n' "${CYAN}├────────────────────────────────────────────────────────────┤${NC}"
    box_line " ${MAGENTA}Проект :${NC} ${PROJECT_DIR}" " Проект : ${PROJECT_DIR}"
    box_line " ${MAGENTA}Git    :${NC} ${GIT_REPO_DIR}" " Git    : ${GIT_REPO_DIR}"
    box_line " ${MAGENTA}GitHub :${NC} ${GITHUB_URL}" " GitHub : ${GITHUB_URL}"
    box_line " ${MAGENTA}Ветка  :${NC} ${GITHUB_BRANCH}" " Ветка  : ${GITHUB_BRANCH}"
    printf '%b\n' "${CYAN}└────────────────────────────────────────────────────────────┘${NC}"
    echo
}

# ------------------------------------------------------------
# Мастер первичной настройки для нового проекта
# ------------------------------------------------------------
run_setup_wizard() {
    local in_project_dir
    local in_git_dir
    local in_repo
    local in_branch

    section_title "НАСТРОЙКА DEPLOY.SH ДЛЯ ЭТОГО ПРОЕКТА"

    echo "Ответьте на несколько вопросов один раз — ответы будут"
    echo "сохранены в:"
    echo "  $DEPLOY_CONFIG"
    echo "и подхватятся автоматически при следующих запусках."
    echo

    printf '%b' "${CYAN}Рабочий каталог проекта (PROJECT_DIR): ${NC}"
    read -r in_project_dir
    printf '%b' "${CYAN}Локальный клон Git-репозитория (GIT_REPO_DIR): ${NC}"
    read -r in_git_dir
    printf '%b' "${CYAN}GitHub-репозиторий, вида owner/repo (GITHUB_REPO): ${NC}"
    read -r in_repo
    printf '%b' "${CYAN}Ветка [main]: ${NC}"
    read -r in_branch
    in_branch="${in_branch:-main}"

    if [ -z "$in_project_dir" ] || [ -z "$in_git_dir" ] || [ -z "$in_repo" ]; then
        warning "Не все поля заполнены — конфигурация не сохранена."
        return 1
    fi

    PROJECT_DIR="$in_project_dir"
    GIT_REPO_DIR="$in_git_dir"
    GITHUB_REPO="$in_repo"
    GITHUB_URL="https://github.com/${GITHUB_REPO}"
    GITHUB_BRANCH="$in_branch"
    PROJECT_NAME="$(basename "$PROJECT_DIR")"
    ROLLBACK_BACKUP_DIR="${PROJECT_DIR}/../${PROJECT_NAME}_rollback_backups"

    cat > "$DEPLOY_CONFIG" <<EOF
# Конфигурация deploy.sh для проекта «${PROJECT_NAME}».
# Создано мастером настройки $(date '+%Y-%m-%d %H:%M:%S').
#
# Явная переменная окружения при запуске всё равно имеет приоритет
# над значениями из этого файла — за счёт \${VAR:-...} ниже.
PROJECT_DIR="\${PROJECT_DIR:-${PROJECT_DIR}}"
GIT_REPO_DIR="\${GIT_REPO_DIR:-${GIT_REPO_DIR}}"
GITHUB_REPO="\${GITHUB_REPO:-${GITHUB_REPO}}"
GITHUB_BRANCH="\${GITHUB_BRANCH:-${GITHUB_BRANCH}}"
EOF

    success "Конфигурация сохранена: $DEPLOY_CONFIG"
    echo "Дальнейшие запуски deploy.sh из этого каталога подхватят её сами."
    echo
}

# ------------------------------------------------------------
# Проверка окружения
# ------------------------------------------------------------
check_dependencies() {
    command_exists git || die "Git не найден."
    command_exists tar || die "tar не найден."

    if { [ ! -d "$PROJECT_DIR" ] || [ ! -d "$GIT_REPO_DIR" ]; } && [ ! -f "$DEPLOY_CONFIG" ]; then
        echo
        warning "Каталоги проекта и/или Git не найдены, а файл конфигурации отсутствует:"
        echo "  $DEPLOY_CONFIG"
        echo "Похоже, deploy.sh запущен для этого проекта впервые."
        echo

        if ask_yes_no "Настроить deploy.sh для этого проекта сейчас?"; then
            run_setup_wizard || true
        fi
    fi

    [ -d "$PROJECT_DIR" ] || die "Каталог проекта не найден: $PROJECT_DIR"
    [ -d "$GIT_REPO_DIR" ] || die "Каталог Git-репозитория не найден: $GIT_REPO_DIR"

    git -C "$GIT_REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        || die "Каталог не является Git-репозиторием: $GIT_REPO_DIR"

    git -C "$GIT_REPO_DIR" config core.fileMode false >/dev/null 2>&1 || true
}

# ------------------------------------------------------------
# Защищённые файлы и каталоги
# ------------------------------------------------------------
is_protected_path() {
    local path="$1"

    case "$path" in
        .env|.env.*)
            return 0
            ;;
        data|data/*)
            return 0
            ;;
        backups|backups/*)
            return 0
            ;;
        keys|keys/*)
            return 0
            ;;
        secrets|secrets/*)
            return 0
            ;;
        *.db|*.sqlite|*.sqlite3)
            return 0
            ;;
        *.pem|*.key|*.crt|*.p12|*.pfx)
            return 0
            ;;
        @eaDir|@eaDir/*)
            return 0
            ;;
        Thumbs.db|desktop.ini)
            return 0
            ;;
        "$SELF_NAME"|deploy.conf)
            return 0
            ;;
        .git|.git/*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# ------------------------------------------------------------
# Защита самого deploy.sh (и deploy.conf) от попадания в GitHub
#
# is_protected_path() выше защищает только сравнение
# ПРОЕКТ -> ЛОКАЛЬНЫЙ GIT (Deploy). Но если deploy.sh уже физически
# лежит внутри GIT_REPO_DIR (например, был закоммичен когда-то раньше
# или помещён туда вручную), ни это, ни тем более "Git -> GitHub"
# (который коммитит git add -A всё, что реально лежит в git-каталоге)
# сами по себе его не остановят. Эти функции чинят именно это — их
# нужно вызывать в НАЧАЛЕ deploy() и git_to_github(), до commit/push.
# ------------------------------------------------------------
ensure_gitignore_has_self() {
    local gitignore="$GIT_REPO_DIR/.gitignore"
    local entry

    for entry in "$SELF_NAME" "deploy.conf"; do
        if [ -f "$gitignore" ] && grep -qxF "$entry" "$gitignore" 2>/dev/null; then
            continue
        fi

        printf '%s\n' "$entry" >> "$gitignore" \
            || die "Не удалось обновить .gitignore: $gitignore"

        log "Добавлено в .gitignore: $entry"
    done
}

untrack_self_from_git() {
    local tracked

    tracked="$(git -C "$GIT_REPO_DIR" ls-files -- "$SELF_NAME" "deploy.conf" 2>/dev/null || true)"

    [ -n "$tracked" ] || return 0

    echo
    warning "В локальном Git-репозитории отслеживаются файлы самого deploy-инструмента:"
    echo "$tracked" | sed 's/^/    /'
    echo
    echo "Эти файлы не должны попадать в GitHub — они относятся к вашей"
    echo "локальной машине, а не к самому проекту."
    echo

    if ! ask_yes_no "Убрать их из отслеживания git сейчас? (файлы на диске останутся)"; then
        die "Отменено: $SELF_NAME/deploy.conf всё ещё отслеживаются в git. Уберите их вручную (git rm --cached) и повторите."
    fi

    # --ignore-unmatch: не падать, если один из двух файлов не отслеживался
    git -C "$GIT_REPO_DIR" rm --cached --ignore-unmatch -- "$SELF_NAME" "deploy.conf" >/dev/null \
        || die "Не удалось убрать файлы из отслеживания git."

    success "Файлы deploy-инструмента убраны из отслеживания git (на диске они остались)."
}

protect_self_from_git() {
    ensure_gitignore_has_self
    untrack_self_from_git
}

# ------------------------------------------------------------
# Сравнение проекта и Git-репозитория
# ------------------------------------------------------------
collect_changes() {
    local project_file
    local relative
    local git_file

    CHANGES=()

    printf '%b\n' "${CYAN}Сканирование проекта и локального Git...${NC}"
    printf '%b\n' "${YELLOW}Защищённые каталоги (data/, backups/, keys/, secrets/) исключены из сканирования.${NC}"

    while IFS= read -r -d '' project_file; do
        relative="${project_file#"$PROJECT_DIR"/}"

        if is_protected_path "$relative"; then
            continue
        fi

        git_file="$GIT_REPO_DIR/$relative"

        if [ ! -e "$git_file" ]; then
            CHANGES+=("NEW|$relative")
        elif ! cmp -s "$project_file" "$git_file"; then
            CHANGES+=("MODIFIED|$relative")
        fi
    done < <(
        find "$PROJECT_DIR" \
            -type d \
            \( \
                -path "$PROJECT_DIR/.git" -o \
                -path "$PROJECT_DIR/data" -o \
                -path "$PROJECT_DIR/backups" -o \
                -path "$PROJECT_DIR/keys" -o \
                -path "$PROJECT_DIR/secrets" \
            \) -prune -o \
            -type f -print0
    )

    while IFS= read -r -d '' git_file; do
        relative="${git_file#"$GIT_REPO_DIR"/}"

        if is_protected_path "$relative"; then
            continue
        fi

        project_file="$PROJECT_DIR/$relative"

        if [ ! -e "$project_file" ]; then
            CHANGES+=("DELETED|$relative")
        fi
    done < <(
        find "$GIT_REPO_DIR" \
            -type d -path "$GIT_REPO_DIR/.git" -prune -o \
            -type f -print0
    )
}

print_changes() {
    local item
    local type
    local path

    if [ "${#CHANGES[@]}" -eq 0 ]; then
        success "Изменений между проектом и локальным Git-репозиторием нет."
        return 1
    fi

    section_title "ИЗМЕНЕНИЯ: ПРОЕКТ -> ЛОКАЛЬНЫЙ GIT"

    printf '%b\n' "${CYAN} Тип         Файл${NC}"
    printf '%b\n' "${CYAN}────────────────────────────────────────────────────────────${NC}"

    for item in "${CHANGES[@]}"; do
        type="${item%%|*}"
        path="${item#*|}"

        case "$type" in
            NEW)
                printf '%b\n' "${GREEN}[NEW]${NC}      $path"
                ;;
            MODIFIED)
                printf '%b\n' "${YELLOW}[MODIFIED]${NC} $path"
                ;;
            DELETED)
                printf '%b\n' "${RED}[DELETED]${NC}  $path"
                ;;
        esac
    done

    printf '%b\n' "${CYAN}────────────────────────────────────────────────────────────${NC}"
    printf '%b\n' "${BOLD}Всего изменений: ${#CHANGES[@]}${NC}"
    echo

    return 0
}

# ------------------------------------------------------------
# Выбор файлов для deploy
# ------------------------------------------------------------
select_changes() {
    SELECTED_FILES=()

    local item
    local type
    local path
    local answer
    local index=1

    section_title "ВЫБОР ФАЙЛОВ ДЛЯ DEPLOY"

    printf '%b\n' "${CYAN}Введите номера файлов через пробел или ${BOLD}all${NC}${CYAN}.${NC}"
    echo

    for item in "${CHANGES[@]}"; do
        type="${item%%|*}"
        path="${item#*|}"

        case "$type" in
            NEW)
                printf '%3d. %b %s\n' "$index" "${GREEN}[NEW]${NC}" "$path"
                ;;
            MODIFIED)
                printf '%3d. %b %s\n' "$index" "${YELLOW}[MODIFIED]${NC}" "$path"
                ;;
            DELETED)
                printf '%3d. %b %s\n' "$index" "${RED}[DELETED]${NC}" "$path"
                ;;
        esac

        index=$((index + 1))
    done

    echo
    echo "Можно:"
    echo "  all       — выбрать всё"
    echo "  номера    — например: 1 3 5"
    echo "  Enter     — отменить"
    echo

    printf '%b' "${CYAN}Ваш выбор: ${NC}"
    read -r answer

    [ -n "$answer" ] || return 1

    if [ "$answer" = "all" ]; then
        for item in "${CHANGES[@]}"; do
            SELECTED_FILES+=("${item#*|}")
        done

        return 0
    fi

    local -a numbers=()
    read -r -a numbers <<< "$answer"

    local number

    for number in "${numbers[@]}"; do
        if ! [[ "$number" =~ ^[0-9]+$ ]]; then
            warning "Некорректный номер: $number"
            return 1
        fi

        if [ "$number" -lt 1 ] || [ "$number" -gt "${#CHANGES[@]}" ]; then
            warning "Номер вне диапазона: $number"
            return 1
        fi

        item="${CHANGES[$((number - 1))]}"
        SELECTED_FILES+=("${item#*|}")
    done

    [ "${#SELECTED_FILES[@]}" -gt 0 ]
}

# ------------------------------------------------------------
# Копирование PROJECT -> GIT
# ------------------------------------------------------------
copy_selected_to_git() {
    local relative
    local source
    local target
    local target_dir

    for relative in "${SELECTED_FILES[@]}"; do
        if is_protected_path "$relative"; then
            warning "Пропущен защищённый файл: $relative"
            continue
        fi

        source="$PROJECT_DIR/$relative"
        target="$GIT_REPO_DIR/$relative"

        if [ -f "$source" ]; then
            target_dir="$(dirname "$target")"
            mkdir -p "$target_dir"

            cp -f "$source" "$target" \
                || die "Не удалось скопировать: $relative"
        elif [ ! -e "$source" ]; then
            if [ -e "$target" ]; then
                rm -f "$target" \
                    || die "Не удалось удалить: $relative"

                target_dir="$(dirname "$target")"

                while [ "$target_dir" != "$GIT_REPO_DIR" ] &&
                      [ "$target_dir" != "/" ]; do
                    if [ -d "$target_dir" ] &&
                       [ -z "$(find "$target_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
                        rmdir "$target_dir" 2>/dev/null || true
                    else
                        break
                    fi

                    target_dir="$(dirname "$target_dir")"
                done
            fi
        fi
    done
}

# ------------------------------------------------------------
# Git status
# ------------------------------------------------------------
show_git_status() {
    echo
    section_title "СОСТОЯНИЕ ЛОКАЛЬНОГО GIT"
    git -C "$GIT_REPO_DIR" status --short
    echo
}

# ------------------------------------------------------------
# GitHub remote
# ------------------------------------------------------------
ensure_origin() {
    local current_url

    current_url="$(git -C "$GIT_REPO_DIR" remote get-url origin 2>/dev/null || true)"

    if [ -z "$current_url" ]; then
        log "Remote origin отсутствует. Добавляю $GITHUB_URL"
        git -C "$GIT_REPO_DIR" remote add origin "$GITHUB_URL" \
            || die "Не удалось добавить origin."
    elif [ "$current_url" != "$GITHUB_URL" ]; then
        warning "Текущий origin: $current_url"
        warning "Ожидаемый origin: $GITHUB_URL"

        if ask_yes_no "Изменить origin на $GITHUB_URL?"; then
            git -C "$GIT_REPO_DIR" remote set-url origin "$GITHUB_URL" \
                || die "Не удалось изменить origin."
        else
            die "Deploy отменён: origin не соответствует GitHub."
        fi
    fi
}

# ------------------------------------------------------------
# Получение состояния GitHub
# ------------------------------------------------------------
fetch_origin() {
    log "Получаю актуальное состояние GitHub..."
    git -C "$GIT_REPO_DIR" fetch origin "$GITHUB_BRANCH" \
        || die "Не удалось получить данные с GitHub."
}

get_remote_sha() {
    git -C "$GIT_REPO_DIR" rev-parse "origin/$GITHUB_BRANCH" 2>/dev/null || true
}

get_local_sha() {
    git -C "$GIT_REPO_DIR" rev-parse HEAD 2>/dev/null || true
}

# ------------------------------------------------------------
# Проверка расхождения истории
# ------------------------------------------------------------
check_history() {
    local local_sha
    local remote_sha
    local counts
    local behind
    local ahead

    local_sha="$(get_local_sha)"
    remote_sha="$(get_remote_sha)"

    if [ -z "$remote_sha" ]; then
        log "Удалённая ветка origin/$GITHUB_BRANCH пока не существует."
        return 0
    fi

    if [ -z "$local_sha" ]; then
        die "Локальная ветка не содержит commit."
    fi

    counts="$(git -C "$GIT_REPO_DIR" rev-list \
        --left-right \
        --count \
        "HEAD...origin/$GITHUB_BRANCH" 2>/dev/null || true)"

    if [ -z "$counts" ]; then
        die "Не удалось определить расхождение истории."
    fi

    read -r ahead behind <<< "$counts"

    echo
    echo "Локальный HEAD : $local_sha"
    echo "GitHub origin  : $remote_sha"
    echo "Ahead          : $ahead"
    echo "Behind         : $behind"
    echo

    if [ "$behind" -gt 0 ] && [ "$ahead" -gt 0 ]; then
        warning "Локальная история и GitHub имеют разные ветки."
        return 1
    fi

    if [ "$behind" -gt 0 ]; then
        warning "Локальный репозиторий отстаёт от GitHub."
        return 2
    fi

    return 0
}

# ------------------------------------------------------------
# Синхронизация local Git <-> GitHub перед push
# Теперь автоматически делает stash перед rebase
# ------------------------------------------------------------
sync_with_github_before_push() {
    local status
    local has_changes=false
    local stash_created=false

    check_history
    status=$?

    case "$status" in
        0)
            return 0
            ;;
        1|2)
            if [ -n "$(git -C "$GIT_REPO_DIR" status --porcelain --untracked-files=normal 2>/dev/null)" ]; then
                has_changes=true
            fi

            if [ "$has_changes" = true ]; then
                echo
                warning "В локальном репозитории есть незакоммиченные изменения."
                echo "Автоматически сохраняю их через git stash перед синхронизацией..."
                echo

                if ! git -C "$GIT_REPO_DIR" stash push --include-untracked -m "Auto-stash before deploy sync"; then
                    error "Не удалось создать stash."
                    echo
                    echo "Необходимо вручную обработать незакоммиченные изменения:"
                    echo "  cd \"$GIT_REPO_DIR\""
                    echo "  git status"
                    echo "  git add <files>"
                    echo "  git commit -m \"WIP: unstaged changes\""
                    echo
                    die "Deploy остановлен."
                fi

                stash_created=true
                success "Изменения сохранены в stash."
            fi

            echo

            if [ "$status" -eq 1 ]; then
                warning "Локальная история и GitHub разошлись."
                echo "Попытаюсь синхронизировать с помощью git pull --rebase..."
            else
                warning "Локальный репозиторий отстаёт от GitHub."
                echo "Подтягиваю изменения с GitHub..."
            fi

            echo

            if ! git -C "$GIT_REPO_DIR" pull --rebase origin "$GITHUB_BRANCH"; then
                error "git pull --rebase завершился с конфликтом."

                if [ "$stash_created" = true ]; then
                    echo
                    warning "Изменения остались в stash. Восстановите их после разрешения конфликтов:"
                    echo "  cd \"$GIT_REPO_DIR\""
                    echo "  git stash pop"
                fi

                echo
                echo "Необходимо вручную разрешить конфликты:"
                echo "  cd \"$GIT_REPO_DIR\""
                echo "  git status"
                echo "  # отредактировать файлы"
                echo "  git add <files>"
                echo "  git rebase --continue"
                echo
                echo "Или отменить rebase:"
                echo "  git rebase --abort"
                echo
                die "Deploy остановлен из-за конфликтов."
            fi

            success "Локальная история синхронизирована с GitHub."

            if [ "$stash_created" = true ]; then
                echo
                log "Восстанавливаю изменения из stash..."

                if ! git -C "$GIT_REPO_DIR" stash pop; then
                    error "Не удалось автоматически восстановить изменения из stash."
                    echo
                    echo "Изменения остались в stash. Восстановите их вручную:"
                    echo "  cd \"$GIT_REPO_DIR\""
                    echo "  git stash list"
                    echo "  git stash pop"
                    echo
                    die "Deploy остановлен."
                fi

                success "Изменения восстановлены из stash."
            fi
            ;;
    esac
}

# ------------------------------------------------------------
# Commit
# ------------------------------------------------------------

create_commit() {
    local commit_message
    echo
    printf '%b' "${CYAN}Введите сообщение commit: ${NC}"
    read -r commit_message
    if [ -z "$commit_message" ]; then
        commit_message="Update ${PROJECT_NAME}"
    fi
    
    # Добавляем все изменения
    git -C "$GIT_REPO_DIR" add -A \
        || die "git add завершился ошибкой."
        
    # ЖЁСТКАЯ ЗАЩИТА: принудительно убираем deploy.sh и deploy.conf из индекса.
    # Даже если .gitignore не сработал (например, из-за CRLF/LF в Windows) 
    # или git add -A добавил их по какой-то причине, эта команда гарантирует, 
    # что они НЕ попадут в коммит.
    git -C "$GIT_REPO_DIR" rm --cached --ignore-unmatch -- "$SELF_NAME" "deploy.conf" >/dev/null 2>&1 || true
    
    if git -C "$GIT_REPO_DIR" diff --cached --quiet; then
        success "После переноса изменений для commit нет."
        return 1
    fi
    echo
    printf '%b\n' "${BOLD}Будет создан commit:${NC}"
    echo "  $commit_message"
    echo
    git -C "$GIT_REPO_DIR" diff --cached --stat
    echo
    if ! ask_yes_no "Создать этот commit?"; then
        git -C "$GIT_REPO_DIR" reset >/dev/null 2>&1 || true
        warning "Создание commit отменено."
        return 1
    fi
    git -C "$GIT_REPO_DIR" commit -m "$commit_message" \
        || die "git commit завершился ошибкой."
    success "Commit создан."
    return 0
}

# ------------------------------------------------------------
# Push + обязательная проверка SHA
# ------------------------------------------------------------
push_to_github() {
    local local_sha
    local remote_sha
    local leaked

    # Последний рубеж защиты: даже если протокол выше (protect_self_from_git)
    # был пропущен или отменён, никогда не отправлять commit, в котором
    # реально закоммичены deploy.sh/deploy.conf.
    leaked="$(git -C "$GIT_REPO_DIR" ls-tree -r --name-only HEAD -- "$SELF_NAME" "deploy.conf" 2>/dev/null || true)"
    if [ -n "$leaked" ]; then
        error "БЛОКИРОВКА PUSH: в текущем commit присутствуют файлы deploy-инструмента:"
        echo "$leaked" | sed 's/^/    /' >&2
        die "Push остановлен. Выполните: git rm --cached -- $SELF_NAME deploy.conf, закоммитьте и повторите."
    fi

    echo
    printf '%b\n' "${BOLD}Подготовка push:${NC}"

    local_sha="$(get_local_sha)"

    echo
    echo "Local HEAD:"
    echo "$local_sha"
    echo

    if ! ask_yes_no "Отправить commit в GitHub ($GITHUB_BRANCH)?"; then
        warning "Push отменён."
        return 1
    fi

    git -C "$GIT_REPO_DIR" push origin "$GITHUB_BRANCH" \
        || die "git push завершился ошибкой."

    success "git push завершён."

    log "Проверяю SHA ветки GitHub..."

    remote_sha="$(
        git -C "$GIT_REPO_DIR" ls-remote "origin" "refs/heads/$GITHUB_BRANCH" \
            | awk '{print $1}'
    )"

    if [ -z "$remote_sha" ]; then
        die "GitHub не вернул SHA ветки $GITHUB_BRANCH."
    fi

    local_sha="$(get_local_sha)"

    echo
    echo "Local HEAD : $local_sha"
    echo "GitHub SHA : $remote_sha"
    echo

    if [ "$local_sha" != "$remote_sha" ]; then
        die "КРИТИЧЕСКАЯ ОШИБКА: SHA локального HEAD и GitHub не совпадают."
    fi

    success "PUSH VERIFIED: GitHub содержит именно локальный HEAD."
}

# ------------------------------------------------------------
# GIT -> GITHUB (без переноса файлов из проекта)
#
# В отличие от deploy() эта операция НЕ трогает $PROJECT_DIR и
# не делает copy_selected_to_git — она коммитит и отправляет в
# GitHub только то, что уже лежит в локальном git-репозитории
# $GIT_REPO_DIR (например, если файлы туда были изменены вручную,
# не через пункт Deploy).
# ------------------------------------------------------------
git_to_github() {
    local local_sha
    local remote_sha
    local has_uncommitted=false

    echo
    printf '%b\n' "${BOLD}${CYAN}============================================================${NC}"
    printf '%b\n' "${BOLD}${CYAN} ${PROJECT_NAME} — GIT -> GITHUB${NC}"
    printf '%b\n' "${BOLD}${CYAN}============================================================${NC}"

    show_paths_info

    printf '%b\n' "${YELLOW}Файлы проекта ($PROJECT_DIR) в этой операции НЕ участвуют.${NC}"
    printf '%b\n' "${YELLOW}Будет закоммичено и отправлено то, что уже есть в локальном${NC}"
    printf '%b\n' "${YELLOW}Git-репозитории: $GIT_REPO_DIR${NC}"

    ensure_origin
    fetch_origin
    sync_with_github_before_push

    # protect_self_from_git — ПОСЛЕ синхронизации с GitHub, а не до неё.
    # Если делать это раньше, её незакоммиченная правка .gitignore и
    # git rm --cached превращают deploy.sh в "untracked" файл ровно в
    # момент, когда sync_with_github_before_push может понадобиться
    # git stash --include-untracked + rebase — а rebase на историю,
    # где deploy.sh ещё отслеживается (пока push с untrack не прошёл),
    # заново материализует его в рабочей копии и ломает stash pop
    # ("could not restore untracked files from stash"). Выполняя это
    # после sync, мы либо не проходим через stash вовсе (чаще всего),
    # либо делаем это до появления такой правки.
    protect_self_from_git

    show_git_status

    if [ -n "$(git -C "$GIT_REPO_DIR" status --porcelain --untracked-files=normal 2>/dev/null)" ]; then
        has_uncommitted=true
    fi

    if [ "$has_uncommitted" = true ]; then
        if ! ask_yes_no "В локальном Git есть незакоммиченные изменения. Создать commit?"; then
            warning "Commit пропущен по вашему выбору."
        elif ! create_commit; then
            warning "Git -> GitHub остановлен: commit не создан."
            return 0
        fi
    else
        success "Незакоммиченных изменений в локальном Git нет."
    fi

    local_sha="$(get_local_sha)"
    remote_sha="$(get_remote_sha)"

    if [ -n "$remote_sha" ] && [ "$local_sha" = "$remote_sha" ]; then
        success "Локальный Git уже синхронизирован с GitHub — отправлять нечего."
        return 0
    fi

    push_to_github

    echo
    printf '%b\n' "${BOLD}${GREEN}============================================================${NC}"
    printf '%b\n' "${BOLD}${GREEN} GIT -> GITHUB УСПЕШНО ЗАВЕРШЁН${NC}"
    printf '%b\n' "${BOLD}${GREEN}============================================================${NC}"
    echo
}

# ------------------------------------------------------------
# Основной DEPLOY
# ------------------------------------------------------------
deploy() {
    echo
    printf '%b\n' "${BOLD}${CYAN}============================================================${NC}"
    printf '%b\n' "${BOLD}${CYAN} ${PROJECT_NAME} — DEPLOY${NC}"
    printf '%b\n' "${BOLD}${CYAN}============================================================${NC}"

    show_paths_info

    ensure_origin
    fetch_origin
    sync_with_github_before_push

    collect_changes

    if ! print_changes; then
        return 0
    fi

    if ! ask_yes_no "Продолжить с этими изменениями?"; then
        warning "Deploy отменён."
        return 0
    fi

    if ! select_changes; then
        warning "Изменения не выбраны. Deploy отменён."
        return 0
    fi

    echo
    printf '%b\n' "${BOLD}Выбрано:${NC}"

    local relative

    for relative in "${SELECTED_FILES[@]}"; do
        echo "  $relative"
    done

    echo

    if ! ask_yes_no "Перенести выбранные изменения в локальный Git-репозиторий?"; then
        warning "Перенос отменён."
        return 0
    fi

    copy_selected_to_git

    success "Изменения перенесены в локальный Git-репозиторий."

    # protect_self_from_git — ПОСЛЕ copy_selected_to_git, а не до него.
    # copy_selected_to_git синхронизирует .gitignore и другие файлы
    # проекта В git-каталог; если чистить/дописывать .gitignore ДО
    # этого шага, только что добавленные строки deploy.sh/deploy.conf
    # тут же перезаписываются старой версией .gitignore из проекта —
    # защита откатывается в рамках того же запуска, ничего не попадает
    # в commit, и deploy.sh остаётся отслеживаемым.
    protect_self_from_git

    show_git_status

    if ! create_commit; then
        return 0
    fi

    fetch_origin
    sync_with_github_before_push

    push_to_github

    echo
    printf '%b\n' "${BOLD}${GREEN}============================================================${NC}"
    printf '%b\n' "${BOLD}${GREEN} DEPLOY УСПЕШНО ЗАВЕРШЁН${NC}"
    printf '%b\n' "${BOLD}${GREEN}============================================================${NC}"
    echo
}

# ============================================================
# ROLLBACK
# ============================================================

list_github_commits_numbered() {
    ROLLBACK_COMMITS=()

    local sha
    local i=1

    while IFS= read -r sha; do
        [ -n "$sha" ] && ROLLBACK_COMMITS+=("$sha")
    done < <(
        git -C "$GIT_REPO_DIR" --no-pager log \
            "origin/$GITHUB_BRANCH" \
            --pretty=format:%H \
            -20 2>/dev/null
    )

    if [ "${#ROLLBACK_COMMITS[@]}" -eq 0 ]; then
        warning "Коммиты в origin/$GITHUB_BRANCH не найдены."
        return 1
    fi

    section_title "ПОСЛЕДНИЕ COMMITS GITHUB"

    local short
    local date
    local author
    local subject

    for sha in "${ROLLBACK_COMMITS[@]}"; do
        short="$(git -C "$GIT_REPO_DIR" --no-pager show -s --format='%h' "$sha" 2>/dev/null || echo '?')"
        date="$(git -C "$GIT_REPO_DIR" --no-pager show -s --format='%ad' --date=iso "$sha" 2>/dev/null || echo '?')"
        author="$(git -C "$GIT_REPO_DIR" --no-pager show -s --format='%an' "$sha" 2>/dev/null || echo '?')"
        subject="$(git -C "$GIT_REPO_DIR" --no-pager show -s --format='%s' "$sha" 2>/dev/null || echo '?')"

        printf '%3d) %s | %s | %s | %s\n' "$i" "$short" "$date" "$author" "$subject"

        i=$((i + 1))
    done

    echo "------------------------------------------------------------"
    echo
}

choose_rollback_commit() {
    local choice
    local full

    list_github_commits_numbered || return 1

    echo "Введите номер коммита из списка или SHA."
    echo "Enter — отмена."
    echo

    printf '%b' "${CYAN}Выбор: ${NC}"
    read -r choice

    if [ -z "$choice" ]; then
        return 1
    fi

    if [[ "$choice" =~ ^[0-9]+$ ]] &&
       [ "$choice" -ge 1 ] &&
       [ "$choice" -le "${#ROLLBACK_COMMITS[@]}" ]; then
        full="${ROLLBACK_COMMITS[$((choice - 1))]}"
    else
        if ! full="$(git -C "$GIT_REPO_DIR" rev-parse --verify "${choice}^{commit}" 2>/dev/null)"; then
            error "Указанный коммит не найден."
            return 1
        fi
    fi

    if ! git -C "$GIT_REPO_DIR" merge-base --is-ancestor "$full" "origin/$GITHUB_BRANCH" 2>/dev/null; then
        error "Коммит $full не найден в origin/$GITHUB_BRANCH."
        return 1
    fi

    ROLLBACK_COMMIT="$full"
}

show_rollback_preview() {
    local commit="$1"

    echo
    printf '%b\n' "${BOLD}${CYAN}Выбранный commit:${NC}"
    echo "------------------------------------------------------------"

    git -C "$GIT_REPO_DIR" --no-pager show \
        -s \
        --format='Commit : %H%nDate   : %ad%nAuthor : %an%nMessage: %s' \
        --date=iso \
        "$commit"

    echo
    echo "Изменения относительно предыдущего commit:"
    echo "------------------------------------------------------------"

    git -C "$GIT_REPO_DIR" --no-pager diff-tree \
        --no-commit-id \
        --name-status \
        -r \
        "$commit"

    echo "------------------------------------------------------------"
    echo
}

create_rollback_backup() {
    local timestamp
    local backup_path

    timestamp="$(date '+%Y%m%d_%H%M%S')"
    backup_path="$ROLLBACK_BACKUP_DIR/project_before_rollback_${timestamp}.tar.gz"

    mkdir -p "$ROLLBACK_BACKUP_DIR" \
        || die "Не удалось создать каталог резервных копий."

    log "Создаю резервную копию текущего проекта..."

    tar \
        --exclude='./.git' \
        --exclude='./__pycache__' \
        --exclude='./*.pyc' \
        -czf "$backup_path" \
        -C "$PROJECT_DIR" \
        . \
        || die "Не удалось создать резервную копию проекта."

    success "Резервная копия создана:"
    echo "  $backup_path"

    ROLLBACK_BACKUP_PATH="$backup_path"
}

rollback_project_to_commit() {
    local commit="$1"
    local temp_dir
    local relative
    local source
    local target
    local target_dir

    temp_dir="$(mktemp -d)" \
        || die "Не удалось создать временный каталог."

    trap 'rm -rf "$temp_dir"' EXIT

    log "Извлекаю выбранный commit во временный каталог..."

    git -C "$GIT_REPO_DIR" archive "$commit" \
        | tar -x -C "$temp_dir" \
        || die "Не удалось извлечь commit."

    log "Восстанавливаю файлы проекта..."

    while IFS= read -r -d '' target; do
        relative="${target#"$PROJECT_DIR"/}"

        if is_protected_path "$relative"; then
            continue
        fi

        if [ ! -f "$temp_dir/$relative" ]; then
            rm -f "$target" \
                || die "Не удалось удалить старый файл: $relative"
        fi
    done < <(
        find "$PROJECT_DIR" \
            -type f \
            -not -path "$PROJECT_DIR/.git/*" \
            -print0
    )

    while IFS= read -r -d '' source; do
        relative="${source#"$temp_dir"/}"

        if is_protected_path "$relative"; then
            continue
        fi

        target="$PROJECT_DIR/$relative"
        target_dir="$(dirname "$target")"

        mkdir -p "$target_dir"

        cp -f "$source" "$target" \
            || die "Не удалось восстановить: $relative"
    done < <(
        find "$temp_dir" \
            -type f \
            -print0
    )

    find "$PROJECT_DIR" \
        -type d \
        -empty \
        -not -path "$PROJECT_DIR" \
        -not -path "$PROJECT_DIR/data" \
        -not -path "$PROJECT_DIR/backups" \
        -not -path "$PROJECT_DIR/keys" \
        -not -path "$PROJECT_DIR/secrets" \
        -delete 2>/dev/null || true

    trap - EXIT
    rm -rf "$temp_dir"

    success "Проект восстановлен из commit $commit."
}

rollback() {
    local full_commit

    echo
    printf '%b\n' "${BOLD}${RED}============================================================${NC}"
    printf '%b\n' "${BOLD}${RED} ${PROJECT_NAME} — ROLLBACK FROM GITHUB${NC}"
    printf '%b\n' "${BOLD}${RED}============================================================${NC}"

    show_paths_info

    printf '%b\n' "${YELLOW}ВНИМАНИЕ:${NC}"
    echo "Эта операция изменит файлы:"
    echo "  $PROJECT_DIR"
    echo
    echo "GitHub изменён НЕ будет."
    echo
    echo "Защищённые данные (.env, БД, data/, backups/, keys/ и т.д.)"
    echo "из GitHub восстанавливаться НЕ будут."
    echo

    ensure_origin
    fetch_origin

    if ! choose_rollback_commit; then
        warning "Rollback отменён."
        return 0
    fi

    full_commit="$ROLLBACK_COMMIT"

    show_rollback_preview "$full_commit"

    # --------------------------------------------------------
    # ПЕРВОЕ ПОДТВЕРЖДЕНИЕ
    # --------------------------------------------------------
    printf '%b\n' "${BOLD}${YELLOW}ПЕРВОЕ ПОДТВЕРЖДЕНИЕ${NC}"
    echo
    echo "Будет восстановлен commit:"
    echo "  $full_commit"
    echo
    echo "Целевой каталог:"
    echo "  $PROJECT_DIR"
    echo

    if ! ask_yes_no "Вы действительно хотите подготовить rollback?"; then
        warning "Rollback отменён."
        return 0
    fi

    create_rollback_backup

    echo
    printf '%b\n' "${BOLD}${YELLOW}Резервная копия готова.${NC}"
    echo

    # --------------------------------------------------------
    # ВТОРОЕ ПОДТВЕРЖДЕНИЕ
    # --------------------------------------------------------
    printf '%b\n' "${BOLD}${RED}ВТОРОЕ ПОДТВЕРЖДЕНИЕ${NC}"
    echo
    echo "Для продолжения необходимо ввести точно:"
    echo
    printf '%b\n' "${BOLD}ROLLBACK${NC}"
    echo

    local confirmation

    printf '%b' "${RED}Введите ROLLBACK: ${NC}"
    read -r confirmation

    if [ "$confirmation" != "ROLLBACK" ]; then
        warning "Подтверждение не совпало."
        warning "Rollback отменён."
        echo
        echo "Резервная копия сохранена:"
        echo "  $ROLLBACK_BACKUP_PATH"
        return 0
    fi

    echo
    printf '%b\n' "${RED}Последнее предупреждение:${NC}"
    echo
    echo "Сейчас файлы проекта будут заменены содержимым commit:"
    echo "$full_commit"
    echo
    echo "GitHub при этом не изменится."
    echo

    if ! ask_yes_no "Выполнить восстановление прямо сейчас?"; then
        warning "Rollback отменён."
        echo
        echo "Резервная копия сохранена:"
        echo "  $ROLLBACK_BACKUP_PATH"
        return 0
    fi

    rollback_project_to_commit "$full_commit"

    echo
    printf '%b\n' "${BOLD}${GREEN}============================================================${NC}"
    printf '%b\n' "${BOLD}${GREEN} ROLLBACK УСПЕШНО ЗАВЕРШЁН${NC}"
    printf '%b\n' "${BOLD}${GREEN}============================================================${NC}"
    echo
    echo "Восстановлен commit:"
    echo "  $full_commit"
    echo
    echo "Резервная копия проекта:"
    echo "  $ROLLBACK_BACKUP_PATH"
    echo

    warning "GitHub НЕ изменялся."
    echo
}

# ------------------------------------------------------------
# History
# ------------------------------------------------------------
show_github_commits() {
    ensure_origin
    fetch_origin

    if [ -z "$(get_remote_sha)" ]; then
        warning "Ветка origin/$GITHUB_BRANCH не найдена или пуста."
        return 0
    fi

    section_title "ИСТОРИЯ GITHUB"

    git -C "$GIT_REPO_DIR" --no-pager log \
        "origin/$GITHUB_BRANCH" \
        --pretty=format:'%h | %ad | %an | %s' \
        --date=iso \
        -20 \
        || warning "Не удалось получить историю GitHub."

    echo
}

# ============================================================
# MENU
# ============================================================
show_menu() {
    local menu_title
    local menu_pad
    local menu_spaces

    menu_title="${PROJECT_NAME^^} - DEPLOY TOOL"
    menu_pad=$(( (FRAME_WIDTH - ${#menu_title}) / 2 ))
    if (( menu_pad < 0 )); then
        menu_pad=0
    fi
    menu_spaces="$(printf '%*s' "$menu_pad" '')"

    echo
    printf '%b\n' "${BOLD}${CYAN}╔════════════════════════════════════════════════════════════╗${NC}"
    box_line "${menu_spaces}${BOLD}${MAGENTA}${menu_title}${NC}" "${menu_spaces}${menu_title}"
    printf '%b\n' "${BOLD}${CYAN}╠════════════════════════════════════════════════════════════╣${NC}"
    box_line "  ${GREEN}1${NC}  ${BOLD}Deploy${NC}    ${BLUE}Проект -> Git -> GitHub${NC}" "  1  Deploy    Проект -> Git -> GitHub"
    box_line "  ${CYAN}2${NC}  ${BOLD}Git Push${NC}  ${BLUE}Git -> GitHub${NC}" "  2  Git Push  Git -> GitHub"
    box_line "  ${YELLOW}3${NC}  ${BOLD}Rollback${NC}  ${BLUE}GitHub -> Проект${NC}" "  3  Rollback  GitHub -> Проект"
    box_line "  ${MAGENTA}4${NC}  ${BOLD}History${NC}   ${BLUE}История GitHub${NC}" "  4  History   История GitHub"
    box_line "  ${RED}5${NC}  ${BOLD}Exit${NC}      ${BLUE}Выход${NC}" "  5  Exit      Выход"
    printf '%b\n' "${BOLD}${CYAN}╚════════════════════════════════════════════════════════════╝${NC}"

    show_paths_info
}

main() {
    check_dependencies

    local choice

    if [ "${1:-}" = "deploy" ]; then
        deploy
        exit $?
    fi

    if [ "${1:-}" = "git-push" ]; then
        git_to_github
        exit $?
    fi

    if [ "${1:-}" = "rollback" ]; then
        rollback
        exit $?
    fi

    if [ "${1:-}" = "history" ]; then
        show_github_commits
        exit $?
    fi

    while true; do
        show_menu

        printf '%b' "${CYAN}Выберите действие [1-5]: ${NC}"
        read -r choice || exit 0

        case "$choice" in
            1)
                deploy
                pause
                ;;
            2)
                git_to_github
                pause
                ;;
            3)
                rollback
                pause
                ;;
            4)
                show_github_commits
                pause
                ;;
            5)
                echo "Выход."
                exit 0
                ;;
            *)
                warning "Неверный выбор."
                pause
                ;;
        esac
    done
}

main "$@"