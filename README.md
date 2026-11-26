# ДЕЛУ.ВРЕМЯ

## Команды на VPS

- `cd /srv/delu-vremya/backend` — открывает каталог с `.env` и Compose.
- `bash deploy/backend.sh update COMMIT_SHA` — скачивает образ по полному SHA коммита из `for_merge`, применяет миграции и проверяет запуск. При сбое возвращает рабочий образ, если схема БД совместима.
- `bash deploy/backend.sh rollback` — возвращает сохранённый предыдущий образ, если версия схемы БД совпадает с его миграциями.
- `docker ps -a --filter name=delu-backend-bot-1` — показывает состояние контейнера.
- `docker logs --tail=100 -f delu-backend-bot-1` — показывает последние 100 строк и следит за новыми.

## Установка на VPS

- `scp deploy/backend.sh VPS:~/backend.sh` — копирует скрипт на VPS по SSH. Выполни из репозитория.
- `sudo install -m 0755 ~/backend.sh /srv/delu-vremya/backend/deploy/backend.sh` — устанавливает скрипт на VPS рядом с `compose.vps.yaml`.
- `docker login ghcr.io --username GITHUB_LOGIN` — вход для закрытого образа с токеном `read:packages`.
