---
layout: doc
lang: ru
translation_key: INSTALL_EXISTING
title: Установите pg_local_cache на PostgreSQL 14–18
seo_title: Установите pg_local_cache на PostgreSQL 14–18
description: Установите расширение PostgreSQL pg_local_cache с проверенными бинарными файлами Linux или через PGXS, затем настройте preload, перезапустите, проверьте и безопасно восстановите работу.
section: Установка
permalink: /ru/docs/INSTALL_EXISTING.html
last_modified_at: "2026-09-05"
---

# Установите pg_local_cache на существующий сервер PostgreSQL {#install-pg_local_cache-on-an-existing-postgresql-server}

Установите расширение из проверенного пакета для Linux или соберите его с
помощью набора инструментов PGXS PostgreSQL. Оба пути требуют одного
контролируемого перезапуска PostgreSQL перед `CREATE EXTENSION`.

> **Запланируйте окно обслуживания:** первая активация меняет
> `shared_preload_libraries`. Сохраните существующие записи и перезапускайте
> нужный кластер только после успешной предварительной проверки.

## Выберите путь установки {#choose-an-installation-path}

| Путь | Лучше всего подходит для | Кто перезапускает |
|---|---|---|
| Последний проверенный бинарный файл | Локальный кластер Linux amd64 | начальная загрузка `pg_ctl` |
| Зафиксированный проверенный бинарный файл | Production и управляемые операции | systemd, `pg_ctl` или внешний оператор |
| Сборка исходников через PGXS | Неподдерживаемая платформа или нестандартная установка PostgreSQL | ваш обычный рабочий процесс эксплуатации |

Опубликованные бинарные файлы поддерживают PostgreSQL 14–18 на Linux amd64 с
glibc или musl. В примерах для фиксированной версии ниже используется
pg_local_cache 2.0.1.

## Быстрая установка бинарного файла {#fast-binary-install}

Для локального кластера под управлением `pg_ctl`:

```bash
curl -fsSL https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh | bash -s -- app
```

Замените `app` именем базы данных. Это включает режим только SQL с
`pg_local_cache.port = 0`.

Начальная загрузка разрешает один тег релиза, проверяет `fetch-release.sh` по
его `SHA256SUMS` этого релиза, выбирает соответствующий архив PostgreSQL и libc,
проверяет его, устанавливает, перезапускает сервер, создаёт расширение и
запускает `local_cache.health()`.

Если `curl | bash` не соответствует вашей политике, сначала просмотрите скрипт:

```bash
curl -fsSLO https://github.com/profundium/pg_local_cache/releases/latest/download/install-latest.sh
less install-latest.sh
bash install-latest.sh app
```

## Контролируемая установка бинарного файла {#controlled-binary-install}

Загрузите фиксированный релиз с опубликованным помощником:

```bash
curl -fsSLO https://github.com/profundium/pg_local_cache/releases/download/v2.0.1/fetch-release.sh
bash fetch-release.sh --release-tag v2.0.1 --output-directory ./pg_local_cache-package
```

Выполните предварительную проверку, затем явно выберите владельца перезапуска:

```bash
sudo ./pg_local_cache-package/install.sh preflight --database app
sudo ./pg_local_cache-package/install.sh install \
  --database app \
  --restart-method systemd \
  --systemd-unit postgresql@16-main
```

Поддерживаются методы перезапуска `systemd`, `pg_ctl` и `none`. Используйте
`none` с Patroni, оператором Kubernetes или другим внешним контроллером.
Перезапустите через этот контроллер и затем проверьте:

```bash
sudo ./pg_local_cache-package/install.sh verify --database app
```

Установщик выводит каталог состояния. Сохраняйте его до успешной проверки:
там находится онлайн-резервная копия, необходимая для `recover`.

## Соберите из исходников {#build-from-source}

Используйте тот же `pg_config`, что и целевой сервер PostgreSQL. Сначала
установите заголовочные файлы сервера, компилятор C и GNU Make.

```bash
git clone --branch v2.0.1 --depth 1 https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
make PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config
sudo make install PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config
```

Собирайте из чистого рабочего дерева, чтобы бинарный файл записал коммит Git.
Установка из исходников копирует только файлы расширения. Продолжите настройкой
preload, перезапуском и приведённой ниже инициализацией SQL.

## Настройте перед перезапуском {#configure-before-restart}

Минимальная конфигурация только SQL с ёмкостью и бюджетом памяти по умолчанию:

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.cache_entries = 16384
pg_local_cache.memory_budget_mb = 384
pg_local_cache.port = 0
```

Сохраните все существующие записи `shared_preload_libraries`. Замените `app` на
фактическое имя базы данных здесь и в приведённых ниже выдачах SQL. Бюджет памяти
относится к расширению, а не ко всему серверу PostgreSQL.

Согласованно задавайте `cache_entries`, состояния отношений, клиентов, воркеров
и `memory_budget_mb`. Предварительная проверка бинарного установщика отклоняет
несогласованные планы. Для сборок из исходников перед перезапуском требуется
такая же проверка ёмкости; не увеличивайте число записей без пересмотра бюджета
памяти.

## Инициализируйте установку из исходников {#initialize-a-source-installation}

После перезапуска подключитесь к настроенной базе данных как суперпользователь
базы данных. При первой ручной установке выполните:

```sql
CREATE EXTENSION IF NOT EXISTS pg_local_cache;
CREATE ROLE local_cache_worker LOGIN NOINHERIT NOSUPERUSER
    NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE app TO local_cache_worker;
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON TABLE local_cache.mapping TO local_cache_worker;
```

Бинарный установщик создаёт эту роль и выдачи для метаданных; пропустите этот
блок, если настройка уже выполнена. Для собственного
`pg_local_cache.role` последовательно используйте это имя. Не переиспользуйте
роль, владеющую таблицами приложения.

**Роль нужна даже при `pg_local_cache.port = 0`.** Подключение таблицы также
проверяет её в режиме только SQL. Роль должна быть отделена от владельца таблицы
и иметь указанные выше атрибуты и выдачи метаданных. `attach_table` управляет её
доступом к каждой сопоставленной таблице. Для работы только SQL пароль и сетевой
слушатель не нужны.

## Подключите таблицу {#attach-a-table}

Используйте существующую постоянную таблицу с поддерживаемым первичным ключом.
От имени суперпользователя базы данных в настроенной базе выполните:

```sql
SELECT local_cache.attach_table('public.items'::regclass);
SELECT local_cache.health();
```

Выдайте существующей роли приложения только необходимые права:

```sql
GRANT SELECT ON public.items TO app_user;
GRANT USAGE ON SCHEMA local_cache TO app_user;
GRANT EXECUTE ON FUNCTION local_cache.mget(regclass, anyarray) TO app_user;
```

Расширение не переписывает обычный `SELECT`.

## Проверьте холодное заполнение и прогретое попадание {#verify-cold-fill-and-warm-hit}

```sql
SELECT local_cache.invalidate('public.items');
SELECT local_cache.mget('public.items'::regclass, ARRAY[1]::bigint[]);
SELECT local_cache.mget('public.items'::regclass, ARRAY[1]::bigint[]);
SELECT local_cache.stats();
```

Убедитесь, что `local_cache.health()` сообщает о готовности, сопоставление
согласовано, а счётчики SQL-кэша изменяются ожидаемым образом.

## Включите необязательный RESP2 {#enable-optional-resp2}

RESP2 добавляет слушатель, процессы воркеров и один общий токен. Используется та
же выделенная роль PostgreSQL, которая требуется для подключения таблиц:

```bash
sudo ./pg_local_cache-package/install.sh preflight \
  --database app \
  --mode resp \
  --token-file /secure/path/token

sudo ./pg_local_cache-package/install.sh install \
  --database app \
  --mode resp \
  --token-file /secure/path/token \
  --restart-method systemd \
  --systemd-unit postgresql@16-main
```

Оставляйте слушатель на `127.0.0.1` или за аутентифицированным TLS. Клиенты
RESP используют общую настроенную роль воркера и не получают контекст ACL
PostgreSQL для отдельного клиента.

## Восстановите неудачную установку бинарного файла {#recover-a-failed-binary-install}

Используйте каталог состояния, выведенный установщиком:

```bash
sudo ./pg_local_cache-package/install.sh recover \
  --state-directory /path/printed/by/install
```

Не выполняйте восстановление после того, как новый postmaster начал принимать
трафик, пока не изучите записанное состояние и эксплуатационные последствия.

## Устранение неполадок {#troubleshooting}

- **Ошибка preload:** проверьте конфигурацию целевого кластера и перезапустите
  правильный postmaster.
- **Роль воркера отсутствует или отклонена:** выполните приведённую выше SQL-
  инициализацию, включая атрибуты роли и выдачи метаданных, даже в режиме только
  SQL.
- **Таблица отклонена:** используйте постоянную таблицу без секционирования и
  RLS с поддерживаемым первичным ключом.
- **Ошибка прав `mget`:** выдайте `SELECT` на исходную таблицу, `USAGE` на схему
  и `EXECUTE` на функцию.
- **Обходы кэша:** проверьте уровень изоляции, записи текущей транзакции,
  состояние восстановления, размер строки и метрики.
- **Устаревшее сопоставление после DDL:** выполните
  `local_cache.reconcile_table('public.items'::regclass)`.

Далее прочитайте [технический справочник](TECHNICAL.md) о контрактах SQL,
согласованности, расчёте памяти, мониторинге и безопасности RESP.
