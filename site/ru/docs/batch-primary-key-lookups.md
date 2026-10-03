---
layout: doc
lang: ru
translation_key: batch-primary-key-lookups
title: Пакетное чтение по первичному ключу PostgreSQL
seo_title: "Пакетное чтение по первичному ключу PostgreSQL с ANY и mget"
description: Замените N+1 чтений по первичному ключу одним параметризованным запросом PostgreSQL, при необходимости сохраните входные позиции и сравните явный путь pg_local_cache mget.
section: Руководства
permalink: /ru/docs/batch-primary-key-lookups.html
last_modified_at: "2026-09-16"
---

# Пакетное чтение по первичному ключу PostgreSQL {#batch-postgresql-primary-key-lookups}

Если код приложения отправляет по одному запросу на каждый ID, сетевые обмены с
сервером и накладные расходы запроса могут доминировать в небольшом чтении
строки. Сначала попробуйте один параметризованный оператор:

```sql
SELECT id, value, revision
FROM public.items
WHERE id = ANY($1::bigint[]);
```

Передавайте ID как параметр-массив. Таблицу и столбцы оставляйте фиксированными
в операторе; не собирайте SQL из строк ID. PostgreSQL вычисляет `ANY`, сравнивая
левое выражение с элементами массива, как описано в [документации о сравнении строк и массивов](https://www.postgresql.org/docs/18/functions-comparisons.html#FUNCTIONS-COMPARISONS-ANY-SOME).

## Знайте контракт результата {#know-the-result-contract}

Приведённый выше запрос возвращает набор. Он не обещает порядок входных данных,
а дублирующийся ID обычно сопоставляется с одной строкой таблицы один раз.
Отсутствующие ID не дают строк. Входной `NULL` не сопоставляется с ненулевым
первичным ключом; массив null и элементы null также следуют трёхзначной логике
`ANY` PostgreSQL. Пустой массив не возвращает строк.

Если вызывающей стороне нужен один результат для каждой запрошенной позиции,
сохраните позиции явно:

```sql
WITH requested AS (
  SELECT key, position
  FROM unnest($1::bigint[]) WITH ORDINALITY AS input(key, position)
)
SELECT requested.position,
       requested.key,
       CASE WHEN items.id IS NULL THEN NULL
            ELSE row_to_json(items)::text END AS row
FROM requested
LEFT JOIN public.items AS items ON items.id = requested.key
ORDER BY requested.position;
```

`WITH ORDINALITY` сохраняет дубликаты и позиции `NULL`; левое соединение
возвращает null в `row` для отсутствующего ключа. Это полезная базовая линия для
клиента, которому нужно явное выравнивание. См. [пример node-postgres](node-postgres.md),
где показано клиентское восстановление того же контракта.

## Когда mget — подходящая альтернатива {#when-mget-is-the-right-alternative}

Для целых строк по первичному ключу `pg_local_cache` предлагает явный
ограниченный API пакетного чтения:

```sql
SELECT local_cache.mget(
  'public.items'::regclass,
  $1::bigint[]
) AS rows;
```

Возвращаемый `text[]` сохраняет порядок входных данных и дубликаты. Входной
`NULL` и отсутствующие строки дают выровненные элементы `NULL`. Вызов принимает
не более 1 024 ключей, а кэш может быть обойдён или не использован согласно
правилам транзакции, снимка, сопоставления и размера строки; в таком случае он
переходит к PostgreSQL, не меняя контракт результата. Возвращаются целые
сериализованные строки, поэтому для проекции, соединений, фильтров помимо ключа
или неограниченного пакета используйте `ANY` или запрос с ordinality.

## GraphQL, DataLoader и чтение N+1 {#graphql-dataloader-and-n1-reads}

[DataLoader](https://github.com/graphql/dataloader#batching) объединяет отдельные
загрузки в пакет. Его пакетная функция должна возвращать по одному значению на
каждый входной ключ в том же порядке; приведённое выше восстановление даёт такую
форму даже для отсутствующих строк.

[Мемоизация DataLoader на запрос](https://github.com/graphql/dataloader#caching-per-request)
отдельна от общей памяти кэша строк PostgreSQL. Создавайте загрузчики для каждого
запроса и очищайте затронутые записи загрузчика после мутаций в этом запросе.
Инвалидация PostgreSQL не может очистить значения, уже сохранённые в загрузчике
JavaScript. Сохраняйте проверки авторизации приложения; `pg_local_cache` не
поддерживает таблицы RLS.

Запустите [quickstart](QUICKSTART.md), затем сравните оба пути чтения в
[бенчмарках](BENCHMARKS.md). [Технический справочник](TECHNICAL.md#sql-mget-api)
определяет API; [руководство по транзакциям](cache-invalidation.md) посвящено
записям.
