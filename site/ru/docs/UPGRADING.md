---
layout: doc
lang: ru
translation_key: UPGRADING
title: Обновление pg_local_cache до 3.1.0
seo_title: "Обновление pg_local_cache до 3.1.0"
description: Обновление pg_local_cache с 3.0.0 или 2.x, параметры ёмкости и workers в 3.1, перезапуск и проверка расширения.
section: Установка
permalink: /ru/docs/UPGRADING.html
last_modified_at: "2026-10-06"
---

# Обновление pg_local_cache до 3.1.0 {#upgrade-pg_local_cache-from-2x-to-310}

В версии 3.0.0 удалена SQL-функция `local_cache.mget(regclass, anyarray)`.
Используйте аутентифицированный RESP `MGET` для чтения целых строк из кэша.
Рабочие процессы RESP используют настроенную роль PostgreSQL и не наследуют
SQL-права, транзакцию или снимок приложения. Оставьте SQL для проекций,
соединений, блокировок строк и чтений, которым нужна семантика сеанса
приложения. Настройка клиента и кодирование ключей описаны в
[руководстве RESP](resp.md).

## Обновление с 3.0.0 {#upgrade-from-300}

Версия 3.1.0 меняет общую библиотеку и разметку общей памяти. Установите пакет
или библиотеку 3.1.0 для используемой основной версии PostgreSQL и перезапустите
PostgreSQL до начала работы. SQL-миграция `3.0.0--3.1.0` ничего не меняет:
SQL-объекты, сопоставления подключённых таблиц и триггеры остаются прежними.
Выполните обновление расширения в каждой базе, чтобы записать версию `3.1.0`.

### Изменения параметров {#setting-changes}

| Параметр | Поведение в 3.1.0 |
|---|---|
| `pg_local_cache.cache_entries` | Диапазон — `128`–`16777216` дескрипторов. Встроенное значение по умолчанию — `262144`, рассчитано для бюджета 384 МиБ с резервом не менее половины на страницы арены. Фактическая ёмкость в байтах зависит от арены и размера строк; при превышении бюджета запуск завершается ошибкой. |
| `pg_local_cache.lock_partitions` | Значение по умолчанию — `64`; степень двойки от `16` до `256`. Для небольших кэшей используется меньше разделов. |
| `pg_local_cache.dirty_marker_entries` | Значение по умолчанию `-1` включает автоматический расчёт: `min(16384, max(1024, floor(cache_entries / 4)))`. Явный диапазон: `128`–`1048576`. |
| `pg_local_cache.dirty_marker_memory_mb` | Значение по умолчанию `-1` включает автоматический расчёт: `min(16, max(1, floor(memory_budget_mb / 25)))` МиБ. Явный диапазон: `1`–`1024` МиБ. |
| `pg_local_cache.max_clients_per_worker` | По умолчанию `64`; диапазон расширен до `1`–`4096`. `max_clients` не должен превышать `workers × max_clients_per_worker`. Мягкий лимит `RLIMIT_NOFILE` для каждого worker должен быть не ниже `min(max_clients, max_clients_per_worker) + 33`; при необходимости увеличьте `nofile` процесса или контейнера. |
| `pg_local_cache.max_deferred_misses` | Новый параметр. По умолчанию `8`; диапазон `1`–`64` отложенных запросов на worker при блокировке отношения. |

В 3.1.0 параметры из 3.0.0 не удалялись и не переименовывались. Все эти
параметры применяются после перезапуска.

`local_cache.stats()` добавляет `fast_path_hits`, `fast_path_fallbacks` и четыре
`fast_path_fallback_key_form`, `fast_path_fallback_mapping_shape`, `fast_path_fallback_multi_key` и `fast_path_fallback_cache_state`; `cache_memory_capacity_bytes`,
`cache_memory_used_bytes`, `cache_fragmentation_bytes`,
`arena_admission_rejections_total`; `dirty_marker_capacity`,
`dirty_marker_entries`, `dirty_marker_highwater`,
`dirty_marker_fallbacks_total`, `dirty_marker_entries_effective`,
`dirty_marker_memory_mb_effective`, `dirty_marker_memory_capacity_bytes`,
`dirty_key_limit_fallbacks`, а также `lock_partitions`,
`max_clients_per_worker` и `client_slots`. RESP `STAT` добавляет локальные для
worker поля `deferred_misses_total`, `deferred_misses_current`,
`deferred_timeouts_total` и `deferred_rejections_total`.

Порядок обновления:

1. Установите пакет или библиотеку 3.1.0 для используемой основной версии
   PostgreSQL.
2. Проверьте описанные выше параметры. При увеличении числа клиентских слотов
   поднимите мягкий лимит `nofile` процесса/контейнера согласно формуле.
3. Перезапустите PostgreSQL, чтобы загрузить библиотеку и выделить память по
   новой схеме.
4. В каждой базе с установленным расширением подключитесь как суперпользователь
   базы и выполните:

   ```sql
   ALTER EXTENSION pg_local_cache UPDATE;
   ```

5. Проверьте библиотеку и workers:

   ```sql
   SELECT current_setting('pg_local_cache.binary_version');
   SELECT local_cache.health();
   ```

   Версия библиотеки должна быть `3.1.0`; health должен показывать
   `workers_running = workers_configured`.

## Обновление с 2.x {#upgrade-from-2x}

До обновления переведите приложения с SQL `mget` на RESP `MGET`, отдельную роль
worker и требуемую модель авторизации. Если listener 2.x использует адрес вне
loopback, до перезапуска настройте встроенный TLS RESP и укажите сертификат и
ключ. Параметр `pg_local_cache.tls_ca_file` включает обязательную проверку
сертификатов клиентов (mTLS). TLS RESP независим от `ssl_*` PostgreSQL. Если
нужен plaintext, явно задайте `pg_local_cache.allow_plaintext_network = on` и
используйте его только в доверенной сети. Без TLS или этого разрешения RESP
workers вне loopback не запускаются.

Миграция SQL с 2.x на 3.0 удаляет SQL API чтения и его счётчики, меняя тип
результата `local_cache.metrics()`. Пользовательские права сохраняются.
Пользовательский объект, зависящий от `local_cache.metrics()`, может помешать
миграции: `CASCADE` не используется, поэтому исправьте или удалите такую
зависимость и повторите обновление. Эти изменения относятся к прежней миграции
3.0, а не к пустой SQL-миграции 3.1. Затем выполните описанные выше шаги установки, перезапуска, обновления расширения и проверки. PostgreSQL последовательно применит прежнюю миграцию 2.x→3.0 и пустую миграцию 3.0.0→3.1.0.

## Откат на 2.0.4 {#rollback-to-204}

Скрипта понижения версии нет. Чтобы вернуться на 2.0.4, переустановите его
пакет, перезапустите PostgreSQL для загрузки старой библиотеки, отсоедините все
сопоставленные таблицы, затем заново создайте расширение в каждой базе и снова
подключите таблицы:

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
CREATE EXTENSION pg_local_cache VERSION '2.0.4';
SELECT local_cache.attach_table('public.items'::regclass);
```

Повторите `detach_table` и `attach_table` для каждой таблицы. До отката
сохраните список подключённых таблиц и особые права на расширение. Зависимые
от функций расширения пользовательские объекты могут помешать удалению;
обработайте такие зависимости отдельно.

## Путь поиска библиотеки {#library-lookup-path}

Control-файл указывает короткое имя `pg_local_cache` в `module_pathname`.
PostgreSQL разрешает его через `dynamic_library_path` (по умолчанию там есть
`$libdir`). Если сервер переопределяет параметр, до перезапуска добавьте путь,
куда пакет установил библиотеку `pg_local_cache`.

Документация для 2.x: https://github.com/profundium/pg_local_cache/tree/v2.0.4/docs
