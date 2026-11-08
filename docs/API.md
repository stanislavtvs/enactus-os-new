# API

| Метод | Маршрут | Доступ и ответ |
|---|---|---|
| GET | `/api/v1/health/` | Публичный. HTTP 200 и JSON `{"status":"ok"}`. Проверяет HTTP-процесс, доступность БД проверяет Compose. |
| GET | `/api/v1/openapi.json` | Схема API в режиме разработки. |
| GET | `/api/v1/docs` | Swagger UI в режиме разработки. |

Вход через Django `GET/POST /accounts/login/`, выход через `POST /accounts/logout/`. Формы защищены CSRF. Главная страница `/` доступна после входа.
