---
layout: doc
lang: ru
translation_key: INSTALL_EXISTING
title: Установка pg_local_cache на PostgreSQL 14–18
seo_title: Установка pg_local_cache на PostgreSQL 14–18
description: Установите проверенный пакет Debian или RPM, используйте PGXN или PGXS, настройте preload, перезапустите PostgreSQL и создайте, обновите или удалите pg_local_cache.
section: Установка
permalink: /ru/docs/INSTALL_EXISTING.html
last_modified_at: "2026-10-05"
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

Сохраните существующие значения `shared_preload_libraries` и добавьте
`pg_local_cache`. Замените `app` на имя базы данных, обслуживаемой расширением.

### Настройте RESP listener {#enable-optional-resp2}

Настройте RESP listener с отдельной ролью worker, защищённым файлом токена и
встроенным TLS. Эта конфигурация включает mTLS, доверяя CA клиентов. Для
встроенного TLS RESP требуется сборка PostgreSQL с поддержкой OpenSSL.

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.port = 6380
pg_local_cache.bind_address = '10.0.0.10'
pg_local_cache.auth_token_file = '/secure/path/token'
pg_local_cache.cache_entries = 16384
pg_local_cache.memory_budget_mb = 384
pg_local_cache.tls = on
pg_local_cache.tls_cert_file = '/secure/path/resp.crt'
pg_local_cache.tls_key_file = '/secure/path/resp.key'
pg_local_cache.tls_ca_file = '/secure/path/client-ca.crt'
pg_local_cache.tls_min_protocol_version = 'TLSv1.2'
pg_local_cache.allow_plaintext_network = off
```

Замените `10.0.0.10` на адрес, доступный клиентам. По умолчанию TLS выключен;
для включения нужны сертификат и ключ сервера. Эта конфигурация защищает TLS
non-loopback RESP listener и проверяет сертификаты клиентов через заданный CA.
Для TLS только с проверкой сервера оставьте `pg_local_cache.tls_ca_file` пустым
и не задавайте сертификаты клиента. TLS RESP независим от параметров PostgreSQL
`ssl_*`. Без TLS listener вне loopback требует
`pg_local_cache.allow_plaintext_network = on`; используйте это явное разрешение
только в доверенной сети.

Для закрытого ключа действует [правило PostgreSQL для файлов ключей
сервера](https://www.postgresql.org/docs/current/ssl-tcp.html): режим `0600`,
если файл принадлежит системному пользователю PostgreSQL, либо владелец root,
режим `0640` и чтение для группы сервера.

Совместно рассчитывайте размер кэша, состояния отношений, число workers и
клиентов, а также `memory_budget_mb`. Рекомендации по ёмкости и памяти см. в
[технической справке](TECHNICAL.md#shared-memory-and-configuration).

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

Не используйте роль worker для владельцев таблиц приложения. Подключите каждую постоянную таблицу с поддерживаемым первичным ключом:

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

При переходе с 2.x на 3.0 сначала следуйте [руководству по обновлению](UPGRADING.md).
Замените в приложениях SQL `mget` на RESP `MGET`, установите пакет
3.1.0 для нужной версии PostgreSQL, перезапустите PostgreSQL и обновите
расширение в каждой базе данных, где оно установлено:

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
