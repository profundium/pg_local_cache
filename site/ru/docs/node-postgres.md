---
layout: doc
lang: ru
translation_key: node-postgres
title: "Пакетное чтение строк с node-postgres"
seo_title: "Пакетное чтение строк PostgreSQL с node-postgres"
description: "Используйте аутентифицированный RESP MGET в Node.js для чтения строк из кэша, а node-postgres — для SQL-записи и обычных запросов."
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

## Чтение через RESP {#send-one-parameterized-query}

С подключённым RESP-клиентом из `@redis/client`:

```js
const ids = [42, 7, 42, null, 999999];
const wireKeys = ids.filter(id => id !== null).map(id =>
  `CRUD:app.public.items:${JSON.stringify({ id })}`
);
const values = await client.mGet(wireKeys);
let position = 0;
const rows = ids.map(id => {
  if (id === null) return null;
  const value = values[position++];
  return value === null ? null : JSON.parse(value);
});
```

RESP `MGET` возвращает строки в JSON-кодировке в порядке ключей. Помощник пропускает входные ключи null и восстанавливает их позиции; отсутствующие ключи возвращаются как null.

Имя таблицы должно быть фиксированным в коде приложения. Передавайте ID как параметры запроса и не собирайте SQL конкатенацией строк. См. документацию node-postgres о [параметрах и именованных подготовленных операторах](https://node-postgres.com/features/queries).

Команда RESP принимает не более 1 024 ключей. Исполняемый помощник возвращает `[]` без запроса, если все входные значения — null. Значения PostgreSQL `bigint` и числовые поля JSON могут превышать точный числовой диапазон JavaScript; используйте JSON-парсер без потерь или явный контракт сериализации.

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

Именованный запрос node-postgres повторно использует подготовленный оператор в каждом соединении, но не кэширует возвращённые строки. RESP `MGET` использует общий кэш целых строк расширения, однако роль воркера и состояние сессии отделены от SQL-соединения приложения. См. [руководство по выбору кэша](postgresql-caching.md) и [руководство по пакетному чтению](batch-primary-key-lookups.md).

Для RESP2 см. [пример RESP для Node.js](resp.md#nodejs). [Результаты Node.js](benchmarks-node.md) включают пакетные чтения и конкурентные обновления. [Общий бенчмарк](BENCHMARKS.md#run-the-same-comparison-on-every-client) запускает Node.js и Go в одинаковых сценариях подготовленного SQL и RESP.
