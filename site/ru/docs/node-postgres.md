---
layout: doc
lang: ru
translation_key: node-postgres
title: Пакетное чтение строк с node-postgres
seo_title: "Пакетное чтение строк PostgreSQL с node-postgres"
description: Используйте pg_local_cache 2.0 из Node.js с параметризованным массивом bigint и передачей JSON. Сохраняйте порядок и null, затем сравните результат с подготовленным запросом ANY.
section: Node.js
permalink: /ru/docs/node-postgres.html
last_modified_at: "2026-09-16"
---

# Пакетное чтение строк с node-postgres {#batch-row-lookups-with-node-postgres}

Читайте строки по первичному ключу через существующее соединение или пул
node-postgres.

Сначала запустите [демонстрацию](QUICKSTART.md), установите зависимости и
выполните её интеграционные проверки:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

## Отправьте один параметризованный запрос {#send-one-parameterized-query}

Для подключённого клиента или пула node-postgres:

```js
const result = await client.query({
  name: 'items-mget',
  text: "SELECT array_to_json(local_cache.mget('public.items'::regclass, $1::bigint[])) AS rows",
  values: [[42, 7, 42, null, 999999]],
});
const rows = result.rows[0].rows.map(row =>
  row === null ? null : JSON.parse(row)
);
```

`mget` возвращает `text[]`. `array_to_json` отправляет внешний массив как JSON,
поэтому node-postgres применяет декодер JSON. Каждый ненулевой элемент является
сериализованной строкой и требует `JSON.parse`; позиции соответствуют входным,
а отсутствующие ключи или null-вход дают `null`.

Имя таблицы храните фиксированным в коде приложения. Передавайте ID как
параметры запроса, а не собирайте SQL из строк. См. документацию node-postgres
о [параметрах и именованных подготовленных операторах](https://node-postgres.com/features/queries).

Запускаемый помощник отклоняет пакеты более чем из 1 024 ключей и возвращает `[]`
без запроса для пустого пакета. Он использует демонстрационные ID в безопасном
целочисленном диапазоне. Поля PostgreSQL `bigint` и `numeric` в JSON могут выйти
за пределы точного числового диапазона JavaScript; используйте JSON-парсер без
потерь или явно определённый контракт сериализации таких значений.

## Сравните с существующим пакетным запросом {#compare-with-the-existing-batch-query}

Базовый вариант использует:

```sql
SELECT id::text AS key, row_to_json(i)::text AS row
FROM public.items AS i
WHERE id = ANY($1::bigint[]);
```

`ANY` не сохраняет порядок входных данных и повторяющиеся запрошенные позиции.
Пример восстанавливает их на клиенте и подставляет null для отсутствующих строк,
прежде чем сравнить результаты.

Запускаемая реализация находится в
[examples/node-postgres](https://github.com/profundium/pg_local_cache/tree/master/examples/node-postgres).
Помощник принимает существующий клиент, а не создаёт пул при каждом вызове.

## Транзакции и границы приложения {#transactions-and-application-boundaries}

На протяжении транзакции используйте один полученный клиент. Чтения после
записей в той же транзакции используют путь исходной таблицы PostgreSQL.
Демонстрация проверяет это отдельными соединениями читателя и записывающего
сеанса; см. [инвалидацию кэша](cache-invalidation.md).

## Подготовленные операторы и кэширование результатов {#prepared-statements-and-result-caching}

Именованный запрос node-postgres повторно использует подготовленный оператор на
каждом соединении. Он не кэширует возвращённые строки. `local_cache.mget`
добавляет отдельный общий кэш целых строк внутри PostgreSQL; клиент по-прежнему
отправляет запрос и декодирует его результат. См. [руководство по выбору кэширования](postgresql-caching.md), где сравниваются эти уровни, и [руководство по пакетному чтению](batch-primary-key-lookups.md) с SQL-альтернативой, которая
сохраняет запрошенные позиции.

Для RESP2 используйте [пример RESP для Node.js](resp.md#nodejs).
[Записанные результаты Node.js](benchmarks-node.md) включают пакетное чтение и
конкурентные обновления. [Общий бенчмарк](BENCHMARKS.md#run-the-same-comparison-on-every-client)
прогоняет Node.js и Go через одинаковые сценарии SQL и RESP.
