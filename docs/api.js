/* Authenticated API client for the Telegram Mini App. */
(() => {
  const config = window.DEALENDER_CONFIG || {};
  const baseUrl = String(config.apiBaseUrl || '/api/v1').replace(/\/+$/, '');

  async function request(path, options = {}) {
    const initData = window.Telegram?.WebApp?.initData;
    if (!initData) throw new Error('Откройте приложение из Telegram, чтобы подключиться к аккаунту.');

    const headers = new Headers(options.headers || {});
    headers.set('Authorization', `tma ${initData}`);
    if (options.body !== undefined) headers.set('Content-Type', 'application/json');

    let response;
    try {
      response = await fetch(`${baseUrl}${path}`, { ...options, headers, credentials: 'same-origin' });
    } catch (_) {
      throw new Error('Сервер Mini App недоступен. Проверьте адрес API и подключение к сети.');
    }

    const payload = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) {
      const message = payload?.error?.message || payload?.message;
      if (response.status === 401) throw new Error('Не удалось подтвердить Telegram-сессию. Закройте и снова откройте Mini App.');
      throw new Error(message || `Ошибка сервера (${response.status}).`);
    }
    if (response.status !== 204 && (!payload || typeof payload !== 'object')) {
      throw new Error('Сервер вернул данные в неожиданном формате.');
    }
    return payload;
  }

  const json = value => JSON.stringify(value);
  window.DealenderApi = Object.freeze({
    request,
    me: () => request('/me'),
    async allEvents() {
      const events = new Map();
      const cursors = new Set();
      let cursor = '';
      do {
        const query = new URLSearchParams({limit: '100'});
        if (cursor) query.set('cursor', cursor);
        const page = await request('/events?' + query);
        if (!Array.isArray(page.events)) throw new Error('Не удалось прочитать список событий.');
        for (const event of page.events) events.set(String(event.id), event);
        cursor = page.next_cursor;
        if (cursor != null && (typeof cursor !== 'string' || !cursor || cursors.has(cursor))) {
          throw new Error('Сервер вернул некорректную страницу истории.');
        }
        cursors.add(cursor);
      } while (cursor);
      return [...events.values()];
    },
    chats: () => request('/chats'),
    parseEvent: message => request('/events/parse', { method: 'POST', body: json({ message }) }),
    updateEvent: (eventId, patch) => request(`/events/${encodeURIComponent(eventId)}`, { method: 'PATCH', body: json(patch) }),
    deleteEvent: eventId => request(`/events/${encodeURIComponent(eventId)}`, { method: 'DELETE' }),
    cancelSeries: eventId => request(`/events/${encodeURIComponent(eventId)}/series`, { method: 'DELETE' }),
    tasks: () => request('/tasks'),
    createTask: task => request('/tasks', { method: 'POST', body: json(task) }),
    updateTask: (taskId, patch) => request(`/tasks/${encodeURIComponent(taskId)}`, { method: 'PATCH', body: json(patch) }),
    deleteTask: taskId => request(`/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' }),
    notifications: () => request('/notifications'),
    markNotificationRead: notificationId => request(`/notifications/${encodeURIComponent(notificationId)}`, { method: 'PATCH', body: json({ unread: false }) }),
    notificationSettings: enabled => request('/notifications/settings', { method: 'PUT', body: json({ enabled }) }),
    googleStatus: () => request('/google/status'),
    googleConnect: () => request('/google/connect', { method: 'POST' }),
    googleDisconnect: () => request('/google/connection', { method: 'DELETE' })
  });
})();
