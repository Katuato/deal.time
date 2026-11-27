# Что Mini App требует от сервера

Mini App обращается к HTTPS API по адресу `/api/v1`. В каждом запросе передаёт `Authorization: tma <Telegram.WebApp.initData>`. Сервер проверяет подпись и срок действия этих данных и разрешает пользователю читать и менять только свои записи.

## Для открытия приложения

| Запрос | Что вернуть |
| --- | --- |
| `GET /me` | Профиль пользователя, например `{"user":{"id":"123","timezone":"Europe/Moscow"}}`. |
| `GET /events?limit=100` | Всю историю событий пользователя, включая созданные ботом. Формат страницы — `{"events":[…],"next_cursor":"…"}`. Следующую страницу фронт запрашивает с `&cursor=…`. В конце `next_cursor` равен `null` или отсутствует. Для пустой истории — `{"events":[]}`. |

У события есть `id`, `title`, `date` (ГГГГ-ММ-ДД), `time` и `end` (ЧЧ:ММ в часовом поясе пользователя). Для фильтров и карточки передавайте `chatId`, `chat`, `source`, `repeat`, `status`, `sync`. Если есть, добавляйте `html_link` (ссылка на событие в Google Calendar), `seriesId`, `location`, `participants`, `timezone`, `description`.

## Для действий и остальных экранов

| Запрос | Что требуется |
| --- | --- |
| `POST /events/parse` | Принять `message`, создать событие из текста и вернуть его в поле `event`. |
| `PATCH /events/{id}` | Принять `title, date, time, end, repeat`, сохранить изменения и вернуть событие в поле `event`. |
| `DELETE /events/{id}` · `DELETE /events/{id}/series` | Удалить разовое событие либо отменить всю серию. |
| `GET /tasks` | Вернуть массив `tasks`. У задачи есть `id, title, date, time, done`. |
| `POST /tasks` · `PATCH /tasks/{id}` · `DELETE /tasks/{id}` | Создать задачу по `title, date, time, done`, изменить отметку `done` или удалить задачу. При создании и изменении вернуть объект в поле `task`. |
| `GET /notifications` | Вернуть массив `notifications` и переключатель `enabled` (true/false). Поля уведомления — `id, title, text, extra, kind, unread`. Для перехода к событию или задаче нужен `eventId` или `taskId`. |
| `PATCH /notifications/{id}` · `PUT /notifications/settings` | Отметить уведомление прочитанным по `{"unread":false}` или сохранить настройку пользователя по `{"enabled":true}` либо `{"enabled":false}`. |
| `GET /google/status` · `POST /google/connect` · `DELETE /google/connection` | Вернуть `connected` (true/false), выдать HTTPS-ссылку Google в поле `authorization_url` или отключить календарь. Синхронизацию событий с Google выполняет сервер. |
| `POST /voice/transcribe` | Принять бинарное аудио WebM/Opus, Ogg или MP4/AAC с его Content-Type, до 2 МиБ и 45 секунд. Вернуть распознанную речь в поле `text`. Событие создаётся после подтверждения пользователя. |
| `GET /chats` | Вернуть `{"scope":"authorized","chats":[{"id":"19","title":"Рабочий чат"}]}`. Значения `scope`: `authorized` — доступ подтверждён, `observed` — из чата приходили сообщения, `unknown` — доступ неизвестен. |

Ответы — JSON. При успешном удалении — HTTP 204 без тела. При ошибке — соответствующий HTTP-статус и `{"error":{"message":"Причина ошибки"}}`.

## Развёртывание на VPS

1. Добавьте `https://app.delyvremya.online/oauthcallback` в список разрешённых адресов перенаправления OAuth-клиента в Google Cloud.
2. Запишите тот же адрес в `GOOGLE_REDIRECT_URI` в `/srv/delu-vremya/backend/.env`.
3. `bash deploy/backend.sh update COMMIT_SHA` — обновляет образ с фронтендом и API и применяет миграции. Перед релизом с миграциями сохраните резервную копию базы.
4. Замените блок `app.delyvremya.online` в `/srv/delu-vremya/caddy/Caddyfile` содержимым `deploy/Caddyfile.miniapp`. Сохраните верхний блок `servers` с настройками `h1 h2`.
5. `docker exec delu-caddy-caddy-1 caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile` — проверяет конфигурацию.
6. `docker exec delu-caddy-caddy-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile` — применяет конфигурацию.
7. `curl -i https://app.delyvremya.online/api/v1/me` — проверяет доступность API. Без Telegram-сессии ожидается `401`. В открытом из Telegram Mini App должны загрузиться профиль и события.

На одном домене `/` отдаёт Mini App, `/api/v1/*` обслуживает его запросы, а `/oauthcallback` принимает ответ Google. Backend остаётся в сети Docker через `delu-backend:8080`. Сессия Mini App действует один час с момента открытия.
