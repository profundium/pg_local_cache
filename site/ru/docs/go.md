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

Демонстрация показывает обычный подготовленный SQL-запрос для резервного пути:

```sql
SELECT id::text AS key, row_to_json(items)::text AS row
FROM public.items
WHERE id = ANY($1::bigint[]);
```

Пример запрашивает `42, 7, 42, NULL, 999999`, восстанавливает в Go исходный порядок и выводит null для входного null и отсутствующего ключа. Для чтения из кэша используйте RESP `MGET`; конечная точка работает с настроенной ролью воркеров и не входит в SQL-транзакцию приложения.

## Сравнивайте строки, а не только обмены с сервером {#compare-rows-not-just-round-trips}

Обычный запрос `WHERE id = ANY($1::bigint[])` не сохраняет запрошенные позиции. Восстанавливайте порядок, дубликаты и отсутствующие строки для обычных результатов SQL. Для чтения целых строк из кэша используйте RESP `MGET`; [руководство по пакетному чтению](batch-primary-key-lookups.md) сравнивает контракты.

Бенчмарк использует постоянные соединения и подготовленные операторы. Подготовка SQL не кэширует строки результата: см. [руководство по кэшированию PostgreSQL](postgresql-caching.md). [Общий бенчмарк](BENCHMARKS.md#run-the-same-comparison-on-every-client) тестирует Go и Node.js с одинаковыми ключами, размерами пакетов, числом соединений и длительностью через подготовленный SQL и RESP `MGET`. RESP не разделяет SQL-транзакцию вызывающего приложения.

См. [quickstart](QUICKSTART.md) для настройки и [проверок транзакций](cache-invalidation.md)
перед адаптацией пути чтения.
