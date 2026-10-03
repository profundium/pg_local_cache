---
layout: doc
lang: ru
translation_key: cache-invalidation
title: Инвалидация кэша PostgreSQL с учётом транзакций
seo_title: "Инвалидация кэша PostgreSQL: COMMIT и ROLLBACK | pg_local_cache"
description: Протестируйте инвалидацию pg_local_cache 2.0 с параллельными сеансами PostgreSQL. Проверьте незакоммиченные обновления, чтение собственных изменений, откат, зафиксированные чтения и правила обхода.
section: Инвалидация кэша
permalink: /ru/docs/cache-invalidation.html
last_modified_at: "2026-09-16"
---

# Инвалидация кэша PostgreSQL с учётом транзакций {#transaction-aware-cache-invalidation-in-postgresql}

Одного удаления записи кэша недостаточно, если более раннее чтение может
заполнить её после удаления. Представьте: читатель начинает загружать старую
строку, записывающий сеанс фиксирует новое значение и инвалидирует ключ, а затем
тот ранний загрузчик публикует свой результат. Кэш должен отклонить и такую
позднюю публикацию.

Реализация 2.0 ограждает затронутые ключи или отношения на пути записи в базу
данных. Заполнение несёт сведения о поколении, поэтому после инвалидации его
можно отклонить. Положительные записи кэша также содержат сведения о видимости
кортежа. Недопустимая запись использует чтение исходной таблицы. См. [технический справочник](TECHNICAL.md#transaction-consistency) с описанием контракта.

{% include diagrams/transaction.html id="invalidation-transaction" %}

## Проверьте в двух сеансах {#test-with-two-sessions}

Запустите [локальную демонстрацию](QUICKSTART.md). Выполните эту команду в двух
терминалах:

```bash
docker compose -f examples/compose.yaml exec postgres \
  psql -X -v ON_ERROR_STOP=1 -U demo -d pglc_demo
```

В сеансе A прочитайте строку 42 и запомните её ревизию, затем прочитайте её ещё
раз для прогрева:

```sql
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
```

В сеансе B обновите строку, но оставьте транзакцию открытой:

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
```

B видит собственное увеличение. Это чтение обходит кэш. Пока B остаётся
открытым, повторите запрос A: A должен по-прежнему видеть зафиксированную
ревизию, а не незакоммиченное значение B. В B выполните `ROLLBACK`; следующий
запрос в A по-прежнему должен вернуть исходную ревизию.

Теперь выполните в B:

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

Запрос A, начатый после этого коммита, должен вернуть увеличенную ревизию. Это
важная граница: уже выполняющийся оператор не обязан переключаться на снимок,
созданный после его запуска. PostgreSQL описывает это поведение в режиме
[Read Committed](https://www.postgresql.org/docs/16/transaction-iso.html#XACT-READ-COMMITTED).

Исполняемый [тест Node.js](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/demo.mjs)
проверяет эти наблюдения через отдельные соединения.

## Случаи, намеренно обходящие кэш {#cases-that-deliberately-bypass-the-cache}

`REPEATABLE READ`, `SERIALIZABLE`, восстановление, параллельное выполнение и
транзакции, записавшие сопоставленные данные, используют путь исходной таблицы.
Слишком большая строка может быть успешно возвращена без кэширования. Частота
попаданий около нуля не обязательно означает неудачную установку: проверьте
нагрузку и счётчики обходов.

Если приложению нужен `SELECT ... FOR UPDATE`, используйте обычную операцию
PostgreSQL; `mget` не заменяет блокировку строк.

## Изучите причину промаха {#inspect-the-cause-of-a-miss}

Используйте `local_cache.stats()` и `local_cache.health()` с правами
администратора. Сравнивайте снимки счётчиков до и после контролируемого теста.
Счётчики SQL `mget` держите отдельно от счётчиков RESP. После намеренного DDL
следуйте документированной процедуре `reconcile_table` или `reconcile_all`, а
не предполагайте, что ранее подключённое сопоставление по-прежнему описывает
изменённую таблицу.
