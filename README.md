# Enactus OS

Технический каркас модульного монолита для управления организациями. Фаза 1 включает глобальные аккаунты, вход по сессии, Django Ninja и отдельную PostgreSQL 16.

## Локальный запуск

Из корня репозитория с Python 3 и Docker Compose с поддержкой `up --wait`:

```bash
python3 scripts/init-env.py
docker compose config --quiet
docker compose up --build --wait
docker compose exec web python manage.py createsuperuser
```

Первую команду выполняют один раз. Она создаёт локальный `.env` из `.env.example` со случайными ключом Django и паролем PostgreSQL. Если файл уже есть, переходите к проверке Compose.

Откройте страницу входа и введите данные созданного пользователя. При запуске веб-сервис сам применяет миграции. PostgreSQL принимает соединения внутри сети Compose и хранит данные в именованном томе проекта.

- [Страница входа](http://127.0.0.1:8002/accounts/login/)
- [Django Admin](http://127.0.0.1:8002/admin/)
- [Healthcheck API](http://127.0.0.1:8002/api/v1/health/)
- [OpenAPI в режиме разработки](http://127.0.0.1:8002/api/v1/openapi.json)

Если порт 8002 занят, измените `WEB_PORT` в `.env`. Сохраните `127.0.0.1` в `DJANGO_ALLOWED_HOSTS` для проверки здоровья веб-сервиса. Compose предназначен для локальной разработки. После инициализации PostgreSQL изменение пароля в `.env` требует отдельной смены пароля роли в самой БД.

## Проверки

```bash
docker compose exec web python manage.py check
docker compose exec web python manage.py makemigrations --check --dry-run
docker compose exec web python manage.py test
curl --fail http://127.0.0.1:8002/api/v1/health/
```

Тестовый runner Django создаёт отдельную базу с префиксом `test_` и удаляет свою временную БД после прогона. Для остановки проекта с сохранением данных используйте `docker compose stop`.

## Резервная копия PostgreSQL

Сохраните дамп локальной БД в защищённом каталоге. Для проверки восстановления команды создают отдельную временную базу с уникальным именем.

```bash
umask 077
backup_file="$(mktemp "${TMPDIR:-/tmp}/enactus-backup.XXXXXXXX.dump")"
docker compose exec -T db sh -c 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$backup_file"
```

После успешного создания дампа:

```bash
restore_db="enactus_restore_$(date +%Y%m%d_%H%M%S)"
docker compose exec -T db sh -c 'createdb -U "$POSTGRES_USER" "$1"' sh "$restore_db" &&
  docker compose exec -T db sh -c 'pg_restore --exit-on-error -U "$POSTGRES_USER" -d "$1"' sh "$restore_db" < "$backup_file"
```

## Структура

- `config/` содержит настройки Django, маршруты и Ninja API.
- `apps/accounts/` содержит модель глобального пользователя и её миграцию.
- `templates/` содержит HTML входа и защищённой главной страницы.
- `tests/` проверяет API и сессионную аутентификацию.
- `docs/` содержит [ТЗ](docs/TECHNICAL_SPEC.md), [решения](docs/DECISIONS.md), [API](docs/API.md), [схему данных](docs/ERD.md) и [статус](docs/STATUS.md).
