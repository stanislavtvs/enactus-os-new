# Эксплуатация Enactus OS

## Резервные копии PostgreSQL

Создайте резервную копию в отдельном каталоге с правами `0700`. Скрипт создаёт каталог при необходимости и печатает абсолютный путь к готовому архиву. Дамп в формате PostgreSQL custom и файл контрольной суммы сохраняются с правами `0600`. Перед публикацией обоих файлов скрипт проверяет, что `pg_restore` читает весь архив.

```bash
backup_file="$(./scripts/backup-postgres.sh --directory "$HOME/enactus-backups")"
(cd -- "$(dirname -- "$backup_file")" && sha256sum --check -- "$(basename -- "$backup_file").sha256")
```

Для production укажите `--production` и отдельный каталог. Для автоматизации используйте тот же вызов из планировщика хоста от пользователя, который имеет доступ к проекту и Docker. Храните архив и контрольную сумму вместе. Дамп содержит данные пользователей, поэтому для копии вне хоста используйте закрытое зашифрованное хранилище с ограниченным доступом.

Порядок восстановления отсутствующей рабочей БД описан в [README](../README.md#если-отсутствует-рабочая-база). До восстановления проверьте контрольную сумму, имя Compose-проекта, рабочую БД и выбранную точку восстановления.

Для репетиции восстановления запустите `bash scripts/verify-postgres-restore.sh`. Проверка поднимает отдельный временный PostgreSQL 16 с синтетическими данными, проверяет контрольную сумму, восстанавливает архив в новую временную БД и сверяет восстановленные данные. Исходная рабочая БД не участвует.

## Отдельная HTTPS-среда

Production-профиль запускают из корня репозитория с отдельным файлом `.env.production`. Замените `enactus.example` фактическим DNS-именем без схемы, порта и пути. Команда создаёт файл с правами `0600` и разными случайными секретами. Если файл уже существует, команда завершится ошибкой и сохранит его содержимое.

```bash
(
    umask 077
    set -C
    python3 - enactus.example > .env.production <<'PY'
import secrets
import sys

print(f"DJANGO_PUBLIC_HOST={sys.argv[1]}")
print(f"ENACTUS_PROD_SECRET_KEY={secrets.token_urlsafe(48)}")
print(f"ENACTUS_PROD_POSTGRES_PASSWORD={secrets.token_urlsafe(48)}")
print("PRODUCTION_WEB_PORT=8004")
PY
)
```

Зафиксируйте имя проекта в каждой команде. У него собственный том PostgreSQL. PostgreSQL и воркер доступны внутри Compose, Gunicorn опубликован только на `127.0.0.1:8004`. Отдельный сервис применяет миграции до запуска Gunicorn, WhiteNoise обслуживает статику после `collectstatic`, а воркер начинает работу после проверок PostgreSQL и веб-приложения.

```bash
prod=(docker compose --project-name enactus_os_next_prod --env-file .env.production -f compose.production.yaml)
"${prod[@]}" config --quiet
"${prod[@]}" up --build --wait
"${prod[@]}" exec web python manage.py check --deploy
```

Настройте HTTPS на обратном прокси хоста. В виртуальном хосте Nginx укажите то же имя, что в `DJANGO_PUBLIC_HOST`, и пути к сертификату и ключу.

```nginx
server {
    listen 443 ssl;
    server_name enactus.example;
    ssl_certificate /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8004;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

На HTTP-виртуальном хосте перенаправляйте запросы на HTTPS. Прокси всегда устанавливает `X-Forwarded-Proto` сам. Django использует этот заголовок для проверки HTTPS и доверяет CSRF-запросам только с указанного HTTPS-источника. Django включает HSTS для имени приложения и флаги Secure для сессионной и CSRF cookie. Проверка `check --deploy` сообщает о двух настройках HSTS, касающихся поддоменов и preload. Их включение требует отдельного решения владельца домена.

После настройки сертификата и прокси проверьте публичный HTTPS-адрес и состояние сервисов.

```bash
curl --fail https://enactus.example/api/v1/health/
"${prod[@]}" ps --all
"${prod[@]}" logs --tail=100 migrate web worker
```
