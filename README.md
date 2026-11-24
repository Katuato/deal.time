# ДЕЛУ.ВРЕМЯ

Команды выполняются на VPS из каталога с `compose.yaml` и `.env`.

## Подготовка

- `docker login ghcr.io --username GITHUB_LOGIN` — вход в GHCR для закрытого образа. В поле пароля введи токен с правом `read:packages`.
- `export COMPOSE_FILE=compose.yaml:deploy/compose.vps.yaml COMPOSE_PROJECT_NAME=delu-backend` — выбирает конфигурацию VPS и имя проекта.
- `export DEAL_TIME_IMAGE=ghcr.io/katuato/deal.time:COMMIT_SHA` — выбирает образ по полному SHA опубликованного коммита из `for_merge`.

## Обновление

1. `docker compose pull bot` — скачивает выбранный образ.
2. `docker compose --profile tools run --rm --no-deps migrate` — применяет миграции БД.
3. `docker compose up -d --no-build --wait --wait-timeout 600 bot` — запускает новую версию и ждёт готовности. Выполняй после успешной миграции.

## Статус и логи

- `docker compose ps bot` — показывает состояние бота.
- `docker compose logs --tail=100 -f bot` — показывает последние 100 строк и следит за новыми.

## Откат

1. `export DEAL_TIME_IMAGE=ghcr.io/katuato/deal.time:PREVIOUS_COMMIT_SHA` — выбирает предыдущий образ.
2. `docker compose pull bot && docker compose up -d --no-build --wait --wait-timeout 600 bot` — запускает предыдущий образ при совместимой схеме БД.
