---
layout: doc
lang: ru
translation_key: INSTALL_EXISTING
title: Установка pg_local_cache на PostgreSQL 14–18
seo_title: Установка pg_local_cache на PostgreSQL 14–18
description: Установите проверенный пакет Debian или RPM, используйте PGXN или PGXS, настройте preload, перезапустите PostgreSQL и создайте, обновите или удалите pg_local_cache.
section: Установка
permalink: /ru/docs/INSTALL_EXISTING.html
last_modified_at: "2026-10-04"
---

# Установка pg_local_cache на существующий сервер PostgreSQL {#install-pg_local_cache-on-an-existing-postgresql-server}

Это руководство предназначено для Linux и PostgreSQL 14–18. Для установки и настройки нужны права суперпользователя БД. Добавление расширения в <code>shared_preload_libraries</code> требует одного перезапуска PostgreSQL.

## 1. Требования {#choose-an-installation-path}

При сборке из исходников используйте <code>pg_config</code> целевого сервера. Запланируйте перезапуск через службу или оператор, управляющий кластером PostgreSQL.

## 2. Установка пакета {#fast-binary-install}

Скачайте пакет и файл <code>SHA256SUMS</code> для нужной версии PostgreSQL и архитектуры из одного [релиза GitHub](https://github.com/profundium/pg_local_cache/releases).

### Debian и Ubuntu {#controlled-binary-install}

Скачайте <code>postgresql-&lt;major&gt;-pg-local-cache_&lt;version&gt;-1_&lt;arch&gt;.deb</code>. Пакеты собраны на Debian 12 и требуют glibc версии 2.36 или новее. Манифест содержит все release-файлы; флаг <code>--ignore-missing</code> нужен, если скачаны не все файлы.

Проверьте контрольную сумму и происхождение сборки, затем установите пакет:

```bash
sha256sum -c --ignore-missing SHA256SUMS
gh attestation verify <file>.deb --repo profundium/pg_local_cache
sudo apt install ./<file>.deb
```

### RHEL, Rocky Linux и AlmaLinux 9

Эти RPM требуют PostgreSQL той же основной версии из репозитория PGDG.
Подключите [репозиторий PGDG](https://www.postgresql.org/download/linux/redhat/)
для своей версии EL и нужной основной версии PostgreSQL, затем сначала
установите из PGDG пакет `postgresql<major>-server`. Пакет расширения
устанавливается в `/usr/pgsql-<major>`. В дистрибутивных пакетах PostgreSQL
другие имена пакетов, пути и имена служб; для таких серверов используйте
[инструкцию по сборке из исходников](#build-from-source).

Скачайте подходящий <code>.rpm</code> для версии PostgreSQL и архитектуры. Проверьте и установите его:

```bash
sha256sum -c --ignore-missing SHA256SUMS
gh attestation verify <file>.rpm --repo profundium/pg_local_cache
sudo dnf install ./<file>.rpm
```

### PGXN

Установите клиент PGXN, заголовки разработки целевого сервера PostgreSQL,
компилятор C и GNU Make. Укажите <code>pg_config</code> целевого сервера; иначе
PGXN выберет первый найденный в <code>PATH</code>. Для установки системных
файлов нужны права root. См. [параметры команды PGXN install](https://pgxn.github.io/pgxnclient/usage.html#pgxn-install).

```bash
pgxn install --pg_config /path/to/pg_config --sudo sudo pg_local_cache
```

### Сборка из исходников {#build-from-source}

Установите заголовки разработки целевой версии PostgreSQL, компилятор C и GNU Make. Соберите и установите расширение с помощью <code>pg_config</code> этой версии:

```bash
make PG_CONFIG=/path/to/pg_config && \
  sudo make PG_CONFIG=/path/to/pg_config install
```

## 3. Настройка <code>postgresql.conf</code> {#configure-before-restart}

Сохраните существующие значения <code>shared_preload_libraries</code> и добавьте в список <code>pg_local_cache</code>. Замените <code>app</code> на имя базы данных, обслуживаемой расширением. Минимальная конфигурация SQL-only:

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.cache_entries = 16384
pg_local_cache.memory_budget_mb = 384
pg_local_cache.port = 0
```

Для RESP2 оставьте прослушивание на loopback или за аутентифицированным TLS-прокси и используйте защищённый файл токена:

### Настройки RESP2 {#enable-optional-resp2}

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.port = 6380
pg_local_cache.bind_address = '127.0.0.1'
pg_local_cache.auth_token_file = '/secure/path/token'
```

Рассчитывайте размер кэша, состояния связей, число рабочих процессов и клиентов, а также <code>memory_budget_mb</code> совместно. См. рекомендации по ёмкости и памяти в [технической справке](TECHNICAL.md#shared-memory-and-configuration).

## 4. Перезапуск PostgreSQL

Используйте службу или оператор, управляющий кластером. Для systemd:

```bash
# Debian and Ubuntu
sudo systemctl restart postgresql@<major>-main
# RHEL, Rocky Linux, and AlmaLinux
sudo systemctl restart postgresql-<major>
```

В Patroni измените конфигурацию кластера и выполните перезапуск через Patroni:

```bash
patronictl edit-config <cluster>
patronictl restart <cluster>
```

Для Kubernetes создайте собственный образ PostgreSQL с нужным пакетом и разверните его через оператор. Образы расширений для CloudNativePG запланированы.

## 5. Инициализация {#initialize-a-source-installation}

Подключитесь к настроенной базе данных с правами суперпользователя БД. Создайте расширение и отдельную роль worker, затем предоставьте ей доступ к метаданным:

```sql
CREATE EXTENSION IF NOT EXISTS pg_local_cache;
CREATE ROLE local_cache_worker LOGIN NOINHERIT NOSUPERUSER
    NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE app TO local_cache_worker;
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON TABLE local_cache.mapping TO local_cache_worker;
```

Роль worker нужна даже при <code>pg_local_cache.port = 0</code>. Не используйте роль — владельца таблиц приложения. Подключите каждую постоянную таблицу с поддерживаемым первичным ключом:

### Подключение таблицы {#attach-a-table}

```sql
SELECT local_cache.attach_table('public.items'::regclass);
```

### Проверка состояния {#verify-cold-fill-and-warm-hit}

```sql
SELECT local_cache.health();
```

Проверьте готовность расширения и актуальность связей.

## 6. Обновление {#recover-a-failed-binary-install}

Установите новый пакет и перезапустите PostgreSQL через соответствующую службу или оператор. Затем обновите расширение от имени суперпользователя БД:

```sql
ALTER EXTENSION pg_local_cache UPDATE;
```

## 7. Удаление {#troubleshooting}

Отсоедините все связанные таблицы и удалите расширение от имени суперпользователя БД. Удалите <code>pg_local_cache</code> из <code>shared_preload_libraries</code>, перезапустите PostgreSQL и удалите пакет подходящим менеджером:

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
```

```bash
sudo apt remove postgresql-<major>-pg-local-cache
sudo dnf remove pg_local_cache_<major>
```

Далее: см. [техническую справку](TECHNICAL.md) об SQL, размере памяти, мониторинге и безопасности RESP.
