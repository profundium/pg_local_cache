---
layout: doc
lang: ru
translation_key: QUICKSTART
title: Запустите pg_local_cache локально
seo_title: "Локальный запуск кэша строк PostgreSQL | pg_local_cache"
description: Запустите pg_local_cache 2.0 во временном PostgreSQL, прочитайте демонстрационные строки, проверьте попадания в кэш, протестируйте обновления и удалите демонстрацию, не меняя существующую базу данных.
section: Быстрый старт
permalink: /ru/docs/QUICKSTART.html
last_modified_at: "2026-09-16"
---

# Запустите pg_local_cache локально {#try-pg_local_cache-locally}

Эта демонстрация собирает pg_local_cache из вашего рабочего дерева в отдельном
сервере PostgreSQL 16. Она не устанавливает расширение в существующий сервер
PostgreSQL.

Вам нужны Git, Docker и Docker Compose с поддержкой `up --wait`. Образ
собирается из исходного кода.

## Запустите базу данных {#start-the-database}

```bash
git clone https://github.com/profundium/pg_local_cache.git
cd pg_local_cache
docker compose -f examples/compose.yaml up --build --wait
```

Демонстрация привязывает PostgreSQL к `127.0.0.1:55432`, не запускает RESP-
слушатель или постоянный том и хранит данные во временной файловой системе
контейнера. При остановке контейнера его данные удаляются. `demo-only`
предназначен для этого локального цикла; в production используйте собственные
учётные данные.

Если порт 55432 занят, задайте `PGLC_DEMO_PORT` перед запуском Compose и
сохраняйте это значение при запуске примера Node.js:

```bash
export PGLC_DEMO_PORT=55433
```

## Читайте с ролью приложения {#read-as-an-application-role}

Настройка создаёт 4 096 строк в `public.items`. К кэшу подключена только эта
таблица. Роль `demo` не является суперпользователем.

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U demo -d pglc_demo <<'SQL'
SELECT unnest(local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL, 999999]::bigint[]
));
SELECT unnest(local_cache.mget(
  'public.items'::regclass,
  ARRAY[42, 7, 42, NULL, 999999]::bigint[]
));
SQL
```

Оба вызова возвращают одни и те же строки в том же порядке. Первая и третья
позиции относятся к строке 42. Последние две позиции — это SQL `NULL`: один
входной элемент равен null, а ключ 999999 не существует. В psql SQL null по
умолчанию отображается пустым значением.

Функция возвращает **`text[]`**. Приведённый выше `unnest` выводит по одному
элементу массива в каждой строке.

Проверьте счётчики от имени администратора базы данных:

```bash
docker compose -f examples/compose.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d pglc_demo \
  -c 'SELECT local_cache.health();' \
  -c 'SELECT local_cache.stats();'
```

В этой свежей демонстрации `local_cache.health()` должен сообщить `ready: true`,
а повторение чтений должно увеличить `sql_cache_hits`.
Если попадания остаются равными нулю, проверьте `sql_cache_misses`,
`sql_cache_fills` и `sql_cache_bypasses` по [руководству по инвалидации](cache-invalidation.md#inspect-the-cause-of-a-miss).

## Проверьте COMMIT и ROLLBACK {#check-commit-and-rollback}

С Node.js 20 или новее:

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

Тест открывает отдельные соединения читателя и записывающего сеанса. Он
проверяет прогретое попадание, порядок входных данных, дубликаты и отсутствующие
ключи, незакоммиченное обновление, чтение собственных изменений, откат и
зафиксированное обновление. При невыполненном утверждении он завершается с
ненулевым кодом.

См. [пошаговый SQL для двух сеансов](cache-invalidation.md) или
[объяснение запроса Node.js](node-postgres.md).

## Подключите приложение {#connect-your-application}

- [Node.js](node-postgres.md): используйте существующее соединение или пул `pg`.
- [Go](go.md): подключитесь через `pgx` и декодируйте возвращённые строки.
- [RESP](resp.md): включите необязательную конечную точку и подключитесь клиентом Redis.

Далее [сравните одинаковую нагрузку SQL и RESP](BENCHMARKS.md#run-the-same-comparison-on-every-client).
Если нужны результаты или возникли проблемы с настройкой, создайте
[отчёт о нагрузке](https://github.com/profundium/pg_local_cache/issues/new?template=workload.yml),
указав окружение и JSON бенчмарка или журнал ошибки.

## Удалите демонстрацию {#remove-the-demo}

```bash
docker compose -f examples/compose.yaml down
```

Локально собранный образ Docker останется доступен для следующего запуска. Службу
PostgreSQL на хосте не нужно перезапускать или восстанавливать.

Для существующей базы данных следуйте [руководству по установке](INSTALL_EXISTING.md).
В этом случае будут другими права доступа, конфигурация и требования к перезапуску.
