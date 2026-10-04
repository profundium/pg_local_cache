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

## Клиенты TLS и mTLS {#tls-mtls-clients}

Для доступа к RESP вне loopback используйте TLS. Встроенные настройки TLS RESP
независимы от параметров PostgreSQL `ssl_*`. Примеры используют RESP2 и mTLS:
`cache.example` должен соответствовать сертификату сервера, `./ca.crt` должен
ему доверять, а сертификат клиента должен быть подписан CA, указанным в
`pg_local_cache.tls_ca_file`. Для TLS только с проверкой сервера опустите
параметры клиентского сертификата и ключа.

**redis-cli**

```bash
export REDISCLI_AUTH="$PGLC_RESP_TOKEN"
redis-cli -2 --tls --cacert ./ca.crt --cert ./client.crt --key ./client.key -h cache.example -p 6380 MGET 'CRUD:app.public.items:{"id":42}'
```

**go-redis**

```go
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"log"
	"os"

	"github.com/redis/go-redis/v9"
)

func main() {
	caPEM, err := os.ReadFile("./ca.crt")
	if err != nil {
		log.Fatal(err)
	}
	roots := x509.NewCertPool()
	if ok := roots.AppendCertsFromPEM(caPEM); !ok {
		log.Fatal("no CA certificates found")
	}
	clientCert, err := tls.LoadX509KeyPair("./client.crt", "./client.key")
	if err != nil {
		log.Fatal(err)
	}

	client := redis.NewClient(&redis.Options{
		Addr:            "cache.example:6380",
		Password:        os.Getenv("PGLC_RESP_TOKEN"),
		Protocol:        2,
		DisableIdentity: true,
		TLSConfig: &tls.Config{
			RootCAs:      roots,
			MinVersion:   tls.VersionTLS12,
			ServerName:   "cache.example",
			Certificates: []tls.Certificate{clientCert},
		},
	})
	defer client.Close()

	rows, err := client.MGet(context.Background(), `CRUD:app.public.items:{"id":42}`).Result()
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("%v", rows)
}
```

**node-redis**

```js
import { readFileSync } from 'node:fs';
import { createClient } from '@redis/client';

const client = createClient({
  socket: {
    host: 'cache.example',
    port: 6380,
    tls: true,
    servername: 'cache.example',
    ca: readFileSync('./ca.crt'),
    cert: readFileSync('./client.crt'),
    key: readFileSync('./client.key'),
  },
  password: process.env.PGLC_RESP_TOKEN,
  RESP: 2,
  disableClientInfo: true,
});
client.on('error', console.error);
await client.connect();

try {
  const values = await client.mGet(['CRUD:app.public.items:{"id":42}']);
  const rows = values.map(value => value === null ? null : JSON.parse(value));
  console.log(rows);
} finally {
  await client.close();
}
```

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

По умолчанию RESP listener привязывается к `127.0.0.1`. Для подключений вне
loopback предпочтителен TLS; параметры TLS RESP независимы от `ssl_*`
PostgreSQL. При отключённом TLS plaintext-listener вне loopback требует явного
разрешения `pg_local_cache.allow_plaintext_network=on`; используйте его только в
доверенной сети. Демонстрация включает его только внутри своей сети контейнеров.
См. [клиенты TLS и mTLS](#tls-mtls-clients). `pg_local_cache.enabled` —
аварийный выключатель SIGHUP. Каждый RESP-воркер асинхронно применяет
перезагрузку на следующей границе команд, после завершения выполняемой команды.
Поле `cache_enabled` в `local_cache.health()` показывает значение, видимое
SQL-сеансу, который вызвал функцию; оно не подтверждает применение настройки
всеми воркерами. Чтобы отключить чтение кэша без перезапуска, выполните `ALTER SYSTEM SET pg_local_cache.enabled = off;` и `SELECT pg_reload_conf();`. Пока
настройка выключена, RESP читает каждую запись напрямую из исходной таблицы.

## Сравнение с SQL {#compare-with-sql}

[Общий бенчмарк](BENCHMARKS.md#run-the-same-comparison-on-every-client) запускает
RESP `MGET` и подготовленный SQL с одинаковыми ключами и декодированными
результатами в Node.js и Go. Опубликованные измерения SQL `mget` из 2.x —
исторические; этот вариант теста удалён в 3.0.0.
Для более широкого кэширования приложения прочитайте [руководство по cache-aside для PostgreSQL и Redis](postgresql-redis-cache.md).

## Остановите демонстрацию {#stop-the-demo}

```bash
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```
