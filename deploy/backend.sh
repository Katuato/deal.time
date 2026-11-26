#!/usr/bin/env bash
set -euo pipefail
umask 077

repository=ghcr.io/katuato/deal.time
project=${COMPOSE_PROJECT_NAME:-delu-backend}
state_dir=deploy/.state
previous_file=$state_dir/previous-image

die() {
    printf '%s\n' "$*" >&2
    exit 1
}

if [[ ${1:-} == update && $# == 2 && $2 =~ ^[a-f0-9]{40}$ ]]; then
    tag=$repository:$2
elif [[ ${1:-} != rollback || $# != 1 ]]; then
    die 'Команды: backend.sh update ПОЛНЫЙ_SHA или backend.sh rollback'
fi

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
mkdir -p -- "$state_dir"
exec 9>"$state_dir/lock"
flock -n 9 || die 'В этом каталоге уже выполняется обновление или откат.'

compose=(docker compose -p "$project" --env-file .env -f compose.yaml -f deploy/compose.vps.yaml)

with_image() {
    local image=$1
    shift
    DEAL_TIME_IMAGE="$image" "${compose[@]}" "$@"
}

pinned_image() {
    docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$1" |
        awk -v prefix="$repository@sha256:" 'index($0, prefix) == 1 && !found++ {print}'
}

migrate() {
    local image=$1
    shift
    with_image "$image" --profile tools run --rm --no-deps --pull never -T migrate "$@"
}

schema_at_head() {
    migrate "$1" python -m alembic current --check-heads
}

start_bot() {
    with_image "$1" up -d --no-build --pull never --wait --wait-timeout 600 bot
}

running_image() {
    local container
    container=$(with_image "$1" ps --status running --quiet bot) || return
    if [[ -n $container ]]; then
        docker inspect --format '{{.Image}} {{.State.Health.Status}}' "$container"
    fi
}

previous_image=
if [[ -s $previous_file ]]; then
    read -r previous_image < "$previous_file"
fi

save_previous() {
    printf '%s\n' "$1" > "$previous_file.next"
    mv -f -- "$previous_file.next" "$previous_file"
}

if [[ $1 == rollback ]]; then
    [[ $previous_image == "$repository@sha256:"* &&
        ${previous_image#*@sha256:} =~ ^[a-f0-9]{64}$ ]] ||
        die 'Предыдущий образ появится после первого обновления до другого релиза.'
    if ! docker image inspect "$previous_image" >/dev/null 2>&1; then
        docker pull --platform linux/amd64 "$previous_image"
    fi
    schema_at_head "$previous_image" ||
        die 'Откат образа остановлен. Проверьте совместимость текущей схемы БД с предыдущим образом.'
    current=$(running_image "$previous_image")
    image_id=$(docker image inspect --format '{{.Id}}' "$previous_image")
    if [[ $current == "$image_id healthy" ]]; then
        printf 'Предыдущий образ уже работает. %s\n' "$previous_image"
        exit 0
    fi
    current_image=
    if [[ $current == *' healthy' ]]; then
        current_image=$(pinned_image "${current% healthy}")
        [[ -n $current_image ]] || die 'Не удалось определить digest рабочего образа.'
    fi
    if ! start_bot "$previous_image"; then
        if [[ -n $current_image ]] && schema_at_head "$current_image" && start_bot "$current_image"; then
            die 'Откат не завершился. Исходный образ снова работает.'
        fi
        die 'Откат не завершился. Проверьте логи бота.'
    fi
    printf 'Предыдущий образ запущен. %s\n' "$previous_image"
    exit 0
fi

with_image "$tag" config --quiet
docker pull --platform linux/amd64 "$tag"
revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$tag")
[[ $revision == "$2" ]] || die 'Метка коммита в образе отличается от заданного SHA.'
target_image=$(pinned_image "$tag")
[[ -n $target_image ]] || die 'Не удалось определить digest скачанного образа.'

current=$(running_image "$target_image")
target_id=$(docker image inspect --format '{{.Id}}' "$target_image")
if [[ $current == "$target_id healthy" ]] && schema_at_head "$target_image" >/dev/null 2>&1; then
    printf 'Выбранный релиз уже работает, схема БД актуальна. %s\n' "$target_image"
    exit 0
fi

fallback=
if [[ $current == *' healthy' ]]; then
    fallback=$(pinned_image "${current% healthy}")
    [[ -n $fallback ]] || die 'Не удалось определить digest рабочего образа для отката.'
fi

migrate "$target_image" python -m alembic current
if [[ -n $fallback && $fallback != "$target_image" ]]; then
    save_previous "$fallback"
fi

restore_bot() {
    if [[ -n $fallback ]] && schema_at_head "$fallback"; then
        if [[ $(running_image "$fallback") == "$current" ]] || start_bot "$fallback"; then
            save_previous "$previous_image"
            die 'Обновление не завершилось. Прежний образ работает.'
        fi
        die 'Обновление не завершилось. Не удалось запустить прежний образ.'
    fi
    die 'Обновление не завершилось. Автоматический откат требует рабочего образа с совместимой схемой БД.'
}

# Останавливаем прежний бот перед изменением схемы. При том же head он работает до переключения.
if ! schema_at_head "$target_image" >/dev/null 2>&1; then
    with_image "$target_image" stop bot || restore_bot
fi
if ! migrate "$target_image" || ! start_bot "$target_image"; then
    restore_bot
fi
printf 'Обновление завершено. %s\n' "$target_image"
