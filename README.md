# ДЕЛУ.ВРЕМЯ

Выполняй команды на VPS из каталога с `compose.yaml` и `.env` под пользователем с доступом к Docker.

**Вход в GHCR.** Для закрытого образа введи токен GitHub с правом `read:packages` в поле пароля.

```bash
docker login ghcr.io --username GITHUB_LOGIN
```

**Выбор версии.** Подставь полный SHA опубликованного коммита из ветки `for_merge`.

```bash
export COMPOSE_FILE=compose.yaml:deploy/compose.vps.yaml
export COMPOSE_PROJECT_NAME=delu-backend
export DEAL_TIME_IMAGE=ghcr.io/katuato/deal.time:COMMIT_SHA
```

**Обновление.** Скачивает образ, применяет миграции и ждёт готовности бота.

```bash
docker compose pull bot &&
  docker compose --profile tools run --rm --no-deps migrate &&
  docker compose up -d --no-build --wait --wait-timeout 600 bot
```

**Статус.** Показывает состояние контейнера.

```bash
docker compose ps bot
```

**Логи.** Показывает последние 100 строк и новые сообщения.

```bash
docker compose logs --tail=100 -f bot
```

**Откат.** Запускает предыдущий образ при совместимой схеме БД. Подставь SHA предыдущего релиза.

```bash
export DEAL_TIME_IMAGE=ghcr.io/katuato/deal.time:PREVIOUS_COMMIT_SHA
docker compose pull bot &&
  docker compose up -d --no-build --wait --wait-timeout 600 bot
```
