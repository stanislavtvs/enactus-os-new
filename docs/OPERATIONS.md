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

## Письма для аккаунтов

Локально без `SMTP_HOST` Django сохраняет письма в `/tmp/enactus-mail` внутри веб-контейнера. Каталог доступен только пользователю приложения. Содержимое писем включает одноразовые ссылки и хранится отдельно от журнала. Для просмотра списка файлов используйте `docker compose exec web ls -l /tmp/enactus-mail`. При пересоздании контейнера локальные письма теряются.

Локальная ссылка использует `http://127.0.0.1` и порт `WEB_PORT`. Для другого адреса задайте полный `ACCOUNT_PUBLIC_ORIGIN` без пути. В production ссылки всегда используют `https://` и `DJANGO_PUBLIC_HOST`. Ссылки сброса пароля действуют до одного часа.

Для отправки укажите `SMTP_HOST` и `SMTP_FROM_EMAIL` в файле окружения нужного контура. `SMTP_USER` и `SMTP_PASSWORD` задаются парой для SMTP с аутентификацией. По умолчанию включён STARTTLS на порту 587. Для SMTP с TLS с момента соединения задайте `SMTP_USE_SSL=true`. В этом режиме STARTTLS по умолчанию отключён и используется порт 465. `SMTP_PORT` и `SMTP_TIMEOUT` позволяют явно задать порт и таймаут в секундах.

В production без `SMTP_HOST` доставка отключена. Чтобы заработали подтверждение адреса и восстановление доступа, настройте SMTP с TLS или SSL и явным адресом отправителя в `.env.production`.

## Обратный прокси

Настройте HTTPS на обратном прокси хоста. В виртуальном хосте Nginx укажите то же имя, что в `DJANGO_PUBLIC_HOST`, и пути к сертификату и ключу.

```nginx
# Place the log format in the http context.
log_format enactus_safe '$request_method $status $body_bytes_sent $request_time';

server {
    listen 443 ssl;
    server_name enactus.example;
    ssl_certificate /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;
    access_log /var/log/nginx/enactus-access.log enactus_safe;
    error_log /var/log/nginx/enactus-error.log crit;

    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;

    location ~ ^/accounts/(verify-email|reset)/ {
        access_log off;
        error_log /dev/null emerg;
        proxy_pass http://127.0.0.1:8004;
    }

    location / {
        proxy_pass http://127.0.0.1:8004;
    }
}
```

На HTTP-виртуальном хосте перенаправляйте запросы на HTTPS. Прокси всегда устанавливает `X-Forwarded-Proto` сам. Django использует этот заголовок для проверки HTTPS и доверяет CSRF-запросам только с указанного HTTPS-источника. Django включает HSTS для имени приложения и флаги Secure для сессионной и CSRF cookie. Проверка `check --deploy` сообщает о двух настройках HSTS, касающихся поддоменов и preload. Их включение требует отдельного решения владельца домена.

Прокси перезаписывает `X-Real-IP` адресом клиента. Чтобы лимиты запросов различали клиентов за прокси, укажите в `ACCOUNT_TRUSTED_PROXY_NETWORKS` только CIDR непосредственных доверенных прокси, как их видит контейнер Django. Для одного адреса укажите `/32` или `/128`. Без списка Django использует адрес самого соединения. Примените безопасный формат доступа и к HTTP-виртуальному хосту. Исключайте пути, запросы и заголовок Referer с одноразовыми ссылками также из внешних систем мониторинга и журналов.

После настройки сертификата и прокси проверьте публичный HTTPS-адрес и состояние сервисов.

```bash
curl --fail https://enactus.example/api/v1/health/
"${prod[@]}" ps --all
"${prod[@]}" logs --tail=100 migrate web worker
```
