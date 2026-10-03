---
layout: doc
lang: ru
translation_key: go
title: Пакетное чтение строк с Go и pgx
seo_title: "Пакетное чтение строк PostgreSQL с Go и pgx"
description: Используйте pg_local_cache из Go с pgx, параметризованными ключами и декодированными строками JSON.
section: Go
permalink: /ru/docs/go.html
last_modified_at: "2026-09-16"
---

# Пакетное чтение строк с Go и pgx {#batch-row-lookups-with-go-and-pgx}

Сначала запустите [временную базу данных](QUICKSTART.md), затем выполните:

```bash
go -C examples/go-pgx run ./demo
```

Используются `127.0.0.1:55432`, база `pglc_demo` и учётные данные `demo` /
`demo-only`. Если quickstart использует другой порт, задайте
`PGLC_DEMO_PORT`.

Демонстрация передаёт ключи как параметр запроса:

```sql
SELECT local_cache.mget('public.items'::regclass, $1::bigint[]);
```

`mget` возвращает `text[]`; каждый ненулевой элемент является JSON-строкой.
Пример запрашивает `42, 7, 42, NULL, 999999` и выводит строки в порядке
входных данных. Дубликат `42` остаётся в обеих позициях; null-вход и отсутствующий
`999999` дают элементы null.

## Сравнивайте строки, а не только обмены с сервером {#compare-rows-not-just-round-trips}

Обычный запрос `WHERE id = ANY($1::bigint[])` не сохраняет запрошенные позиции.
Восстановите порядок входных данных, дубликаты и отсутствующие строки перед
сравнением с `mget`; в [руководстве по пакетному чтению](batch-primary-key-lookups.md)
показаны клиентский и SQL-подходы.

Бенчмарк использует постоянные соединения и подготовленные операторы. Подготовка
SQL не кэширует его строки результатов: см. [руководство по кэшированию PostgreSQL](postgresql-caching.md). [Общий бенчмарк](BENCHMARKS.md#run-the-same-comparison-on-every-client)
проверяет Go и Node.js с одинаковыми ключами, размерами пакетов, числом
соединений и длительностью через SQL и RESP. RESP не разделяет SQL-транзакцию
вызывающего сеанса.

См. [quickstart](QUICKSTART.md) для настройки и [проверок транзакций](cache-invalidation.md)
перед адаптацией пути чтения.
