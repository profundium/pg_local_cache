---
layout: doc
lang: ru
translation_key: QUICKSTART
title: Запустите pg_local_cache локально
seo_title: "Локальный запуск кэша строк PostgreSQL | pg_local_cache"
description: "Запустите pg_local_cache 3.1.0 во временном PostgreSQL, читайте демонстрационные строки через RESP, проверяйте попадания в кэш, тестируйте обновления и удаляйте демонстрацию без изменений существующей базы."
section: Быстрый старт
permalink: /ru/docs/QUICKSTART.html
last_modified_at: "2026-09-16"
---

# Запустите pg_local_cache локально {#try-pg_local_cache-locally}

Эта демонстрация собирает pg_local_cache из текущего checkout в отдельном сервере PostgreSQL 16. Расширение не устанавливается в существующий сервер. Пример чтения использует listener RESP, настроенный слоем Compose.

Вам нужны Git, Docker и Docker Compose с поддержкой `up --wait`. Образ
собирается из исходного кода.

## Запустите базу данных {#start-the-database}

```bash
git clone https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
```

Демонстрация привязывает PostgreSQL и RESP к loopback-портам `55432` и `56379`, не использует постоянный том и хранит данные во временной файловой системе контейнера. Интерфейс RESP принимает соединения внутри сети контейнера и явно включает plaintext-режим для демонстрации. При остановке контейнера данные удаляются. `demo-only` и открытый RESP-токен предназначены только для этой loopback-демонстрации; в production используйте собственные учётные данные.

Если порт 55432 занят, задайте `PGLC_DEMO_PORT` перед запуском Compose и
сохраняйте это значение при запуске примера Node.js:

```bash
export PGLC_DEMO_PORT=55433
```

## Чтение через RESP {#read-as-an-application-role}

Настройка создаёт 4 096 строк в `public.items`. К кэшу подключена только эта таблица. Роль `demo` не является суперпользователем.

```bash
export REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789
redis-cli -2 -p 56379 MGET \
  'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":7}' \
  'CRUD:pglc_demo.public.items:{"id":42}' \
  'CRUD:pglc_demo.public.items:{"id":999999}'
```

Ответ сохраняет порядок ключей и дубликаты. Первая и третья позиции относятся к строке 42; последняя позиция — RESP null, поскольку ключа 999999 нет. RESP-запрос не передаёт входные ключи со значением null; клиентские помощники могут восстановить эти позиции при необходимости.

Проверьте счётчики от имени администратора базы данных:

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d pglc_demo \
  -c 'SELECT local_cache.health();' \
  -c 'SELECT local_cache.stats();'
```

В этой свежей демонстрации `local_cache.health()` должен сообщить `ready: true`, а повторные чтения должны увеличить `cache_hits`. При необходимости проверьте `cache_misses`, `database_reads` и `cache_enabled` в `local_cache.stats()` и `local_cache.health()`.

## Проверка COMMIT и ROLLBACK {#check-commit-and-rollback}

С Node.js 20 или новее:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

Тест использует RESP для чтения и PostgreSQL для записи. Он проверяет прогретое попадание, порядок входных данных, дубликаты и отсутствующие ключи, инвалидацию кэша и видимость подтверждённых обновлений. RESP-воркеры используют настроенную роль PostgreSQL и не разделяют SQL-транзакцию или снимок приложения.

См. [руководство по инвалидации кэша](cache-invalidation.md) или [объяснение запросов Node.js](resp.md#nodejs).

## Подключите приложение {#connect-your-application}

- [Node.js](resp.md#nodejs): используйте RESP для чтения из кэша и `pg` для SQL-записи.
- [Go](resp.md#go): используйте RESP для чтения из кэша и `pgx` для SQL-записи.
- [RESP](resp.md): подключайтесь клиентом Redis.

Далее [сравните одинаковую нагрузку подготовленного SQL и RESP](BENCHMARKS.md#run-the-same-comparison-on-every-client). Для сообщения о результатах или проблемах настройки создайте [отчёт о нагрузке](https://github.com/profundium/pg_local_cache/issues/new?template=workload.yml), приложив окружение, JSON бенчмарка или журнал ошибок.

## Удалите демонстрацию {#remove-the-demo}

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```

Локально собранный образ Docker останется доступен для следующего запуска. Службу
PostgreSQL на хосте не нужно перезапускать или восстанавливать.

Для существующей базы данных следуйте [руководству по установке](INSTALL_EXISTING.md).
В этом случае будут другими права доступа, конфигурация и требования к перезапуску.
