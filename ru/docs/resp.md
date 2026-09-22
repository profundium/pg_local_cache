---
layout: doc
lang: ru
translation_key: resp
title: Подключение через RESP
description: Читайте строки PostgreSQL через RESP2 с redis-cli или Node.js. Включены аутентификация, настройки клиента, запускаемые примеры и очистка.
section: RESP
permalink: /ru/docs/resp.html
last_modified_at: "2026-09-16"
---

# Подключение через RESP {#connect-over-resp}

Используйте `MGET`, чтобы читать кэшированные строки PostgreSQL клиентом RESP2.
Сначала запустите [временную демонстрацию](QUICKSTART.md) с конфигурацией RESP:

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml up --build --wait
```

Это включает RESP на `127.0.0.1:56379`. При пересоздании демонстрации её данные
удаляются. Приведённый ниже токен является публичным и предназначен только для
этой локальной демонстрации.

## redis-cli {#redis-cli}

```bash
export REDISCLI_AUTH=DemoRespToken_0123456789abcdef0123456789
redis-cli -2 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}'
```

Ответ содержит строку 42 в формате JSON. Для отсутствующих строк возвращается
`nil`. [redis-cli](https://redis.io/docs/latest/develop/tools/cli/) читает токен
из `REDISCLI_AUTH`.

## Node.js {#nodejs}

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run resp
```

Пример использует официальный пакет `@redis/client` и проверяет порядок,
дубликаты, позиции входных null и отсутствующие строки. Его помощник исключает
null-ключи из протокола и восстанавливает их позиции после декодирования.
Для подключения из приложения:

```js
import { createClient } from '@redis/client';

const client = createClient({
  url: 'redis://127.0.0.1:56379',
  password: process.env.PGLC_RESP_TOKEN,
  RESP: 2,
  disableClientInfo: true,
});
client.on('error', console.error);
await client.connect();

try {
  const values = await client.mGet(['CRUD:pglc_demo.public.items:{"id":42}']);
  const rows = values.map(value => value === null ? null : JSON.parse(value));
  console.log(rows);
} finally {
  await client.close();
}
```

Задайте `PGLC_RESP_TOKEN` токеном вашего сервера. Не закрывайте это соединение
между запросами. Эти [настройки клиента](https://github.com/redis/node-redis/blob/master/docs/client-configuration.md)
выбирают RESP2 и пропускают специфичные для Redis команды метаданных клиента.
Используйте только токеновую аутентификацию, без имени пользователя и номера
базы данных Redis.

Воркеры RESP используют одну настроенную роль PostgreSQL для всех клиентов. Для
чтения внутри SQL-транзакции используйте [SQL Node.js](node-postgres.md) или
[SQL Go](go.md). Сведения о командах и ограничениях см. в [справочнике RESP](TECHNICAL.md#optional-resp2-endpoint).

## Сравнение с SQL {#compare-with-sql}

[Общий бенчмарк](BENCHMARKS.md#run-the-same-comparison-on-every-client) запускает
RESP `MGET`, SQL `mget` и подготовленный SQL с одинаковыми ключами и
декодированными результатами в Node.js и Go.
Для более широкого кэширования приложения прочитайте [руководство по cache-aside для PostgreSQL и Redis](postgresql-redis-cache.md).

## Остановите демонстрацию {#stop-the-demo}

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```
