---
layout: doc
lang: ru
translation_key: resp
title: Клиенты RESP
description: Подключайте совместимые с Redis клиенты для чтения строк PostgreSQL через RESP2 MGET, аутентификацию по токену и встроенный TLS.
section: RESP
permalink: /ru/docs/resp.html
redirect_from:
  - /ru/docs/go.html
  - /ru/docs/node-postgres.html
last_modified_at: "2026-10-04"
---

# Клиенты RESP {#resp-clients}

Подключайте совместимые с Redis клиенты для чтения целых строк PostgreSQL по первичному ключу. На этой странице описаны кодирование ключей, аутентификация, формат ответов, ошибки и TLS.

<a id="connect-over-resp"></a>

## Формат ключей и ответов {#key-and-response-contract}

Ключи RESP указывают на подключённую таблицу и объект первичного ключа:

```text
CRUD:<db>.<schema>.<table>:<json pk>
CRUD:app.public.items:{"id":42}
CRUD:app.public.orders:{"tenant_id":7,"id":42}
```

Команда `MGET key [key ...]` возвращает массив RESP2 в порядке запроса. Дубликаты ключей сохраняют свои позиции. Для существующих строк возвращаются bulk-строки JSON, для отсутствующих — элементы nil.

| Ограничение | Поведение |
|---|---|
| 1 024 ключа в команде | Большие пакеты возвращают `ERR MGET accepts at most 1024 keys`. |
| 65 536 байт на строку JSON | Строку большего размера нельзя вернуть через RESP. |
| 66 560 байт на закодированный ответ MGET | Ответы большего размера возвращают `ERR response exceeds limit`. |

Размер пакета ограничен и числом ключей, и размером ответа в байтах. Разбивайте пакеты, если крупная строка может приблизиться к лимиту ответа.

## AUTH {#auth}

Перед остальными командами отправьте `AUTH <token>`. Клиенты также могут отправить `AUTH <username> <token>`; имя пользователя должно совпадать с `pg_local_cache.role`. Токен общий для RESP-клиентов и не выбирает привилегии PostgreSQL отдельно для каждого клиента. Храните его в `pg_local_cache.auth_token_file` с режимом `0400` или `0600`; встроенный `auth_token` используйте только при разработке. Для listener вне loopback требуется токен длиной не менее 32 байт.

## TLS {#tls}

Для встроенного TLS в RESP используются параметры `pg_local_cache.tls_*`; они не связаны с SQL TLS в PostgreSQL. Требуются сборка PostgreSQL с поддержкой OpenSSL, сертификат сервера и закрытый ключ. Параметр `tls_ca_file` также делает сертификаты клиентов обязательными и проверяет их (mTLS). Для TLS с аутентификацией только сервера не задавайте параметры сертификата клиента. Минимальная версия по умолчанию — TLS 1.2.

По умолчанию listener привязан к loopback. Если TLS выключен, plaintext-listener вне loopback требует `pg_local_cache.allow_plaintext_network=on`. Параметры listener и безопасности описаны в [техническом справочнике](TECHNICAL.md#optional-resp2-endpoint).

## redis-cli {#redis-cli}

Задайте `PGLC_RESP_TOKEN`, присвоив ему настроенный токен. В локальном кратком руководстве также приведён демонстрационный токен.

```sh
export REDISCLI_AUTH="$PGLC_RESP_TOKEN"
redis-cli -2 -h 127.0.0.1 -p 56379 MGET 'CRUD:pglc_demo.public.items:{"id":42}'
redis-cli -2 --tls --cacert ./ca.crt --cert ./client.crt --key ./client.key -h cache.example -p 6380 MGET 'CRUD:app.public.items:{"id":42}'
```

## Go {#go}

Установите `github.com/redis/go-redis/v9`. Задайте `PGLC_RESP_TOKEN`; для TLS-варианта нужны файлы CA и сертификата клиента.

```go
package main

import (
	"context"
	"fmt"
	"os"

	"github.com/redis/go-redis/v9"
)

func main() {
	client := redis.NewClient(&redis.Options{
		Addr: "127.0.0.1:56379", Password: os.Getenv("PGLC_RESP_TOKEN"), Protocol: 2,
	})
	defer client.Close()
	rows, err := client.MGet(context.Background(), `CRUD:pglc_demo.public.items:{"id":42}`).Result()
	if err != nil {
		panic(err)
	}
	fmt.Printf("%v\n", rows)
}
```

Вариант с TLS:

```go
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"

	"github.com/redis/go-redis/v9"
)

func main() {
	ca, err := os.ReadFile("./ca.crt")
	if err != nil { panic(err) }
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) { panic("invalid CA") }
	cert, err := tls.LoadX509KeyPair("./client.crt", "./client.key")
	if err != nil { panic(err) }
	client := redis.NewClient(&redis.Options{
		Addr: "cache.example:6380", Password: os.Getenv("PGLC_RESP_TOKEN"), Protocol: 2,
		TLSConfig: &tls.Config{MinVersion: tls.VersionTLS12, ServerName: "cache.example", RootCAs: roots, Certificates: []tls.Certificate{cert}},
	})
	defer client.Close()
	rows, err := client.MGet(context.Background(), `CRUD:app.public.items:{"id":42}`).Result()
	if err != nil { panic(err) }
	fmt.Printf("%v\n", rows)
}
```

## Node.js (redis v4) {#nodejs}

Установите пакет командой `npm install redis@^4`. По умолчанию Redis v4 использует RESP2.

```js
import { createClient } from 'redis';

const client = createClient({
  socket: { host: '127.0.0.1', port: 56379 },
  password: process.env.PGLC_RESP_TOKEN,
});
client.on('error', console.error);
await client.connect();
try {
  console.log(await client.mGet(['CRUD:pglc_demo.public.items:{"id":42}']));
} finally {
  await client.quit();
}
```

Вариант с TLS:

```js
import { readFileSync } from 'node:fs';
import { createClient } from 'redis';

const client = createClient({
  socket: {
    host: 'cache.example', port: 6380, tls: true, servername: 'cache.example',
    ca: [readFileSync('./ca.crt')],
    cert: readFileSync('./client.crt'), key: readFileSync('./client.key'),
  },
  password: process.env.PGLC_RESP_TOKEN,
});
client.on('error', console.error);
await client.connect();
try {
  console.log(await client.mGet(['CRUD:app.public.items:{"id":42}']));
} finally {
  await client.quit();
}
```

## Python (redis-py) {#python}

Установите пакет командой `python -m pip install redis`.

```python
import os
import redis

client = redis.Redis(host="127.0.0.1", port=56379,
                     password=os.environ["PGLC_RESP_TOKEN"],
                     decode_responses=True, protocol=2)
print(client.execute_command('MGET', 'CRUD:pglc_demo.public.items:{"id":42}'))
client.close()
```

Вариант с TLS:

```python
import os
import redis

client = redis.Redis(host="cache.example", port=6380,
                     password=os.environ["PGLC_RESP_TOKEN"],
                     decode_responses=True, protocol=2, ssl=True,
                     ssl_ca_certs="./ca.crt", ssl_certfile="./client.crt",
                     ssl_keyfile="./client.key", ssl_cert_reqs="required",
                     ssl_check_hostname=True)
print(client.execute_command('MGET', 'CRUD:app.public.items:{"id":42}'))
client.close()
```

## Ошибки и ограничения {#errors}

| Ответ | Причина |
|---|---|
| `NOAUTH Authentication required` | Сначала аутентифицируйте соединение. |
| `WRONGPASS invalid authentication token` | Неверный токен или необязательное имя пользователя. |
| `ERR MGET accepts at most 1024 keys` | Разбейте пакет. |
| `ERR response exceeds limit` | Уменьшите размер пакета или полезной нагрузки строки. |
| `ERR MGET deadline exceeded` | Чтение из источника и ожидание по тому же ключу превысили срок выполнения команды. |
| `ERR KVik key targets a different database` | Укажите базу данных, настроенную для RESP-эндпоинта. |
| `ERR unknown KVik table mapping` | Проверьте, что в ключе указаны сопоставленные схема и таблица. |
| `ERR key must use CRUD:database.schema.table:{primary-key-json}` | Используйте полный формат CRUD-ключа. |
| `ERR KVik key must end with a primary-key JSON object` | Завершите область таблицы JSON-объектом первичного ключа. |
| `ERR invalid CRUD cache scope` | Только для `INVALIDATE`: указанная область не является поддерживаемой областью CRUD. |

RESP не зависит от SQL-соединения, роли, транзакции и снимка вызывающей стороны. Для SQL-транзакций используйте PostgreSQL напрямую; см. [руководство по инвалидации](cache-invalidation.md).

## Остановка демонстрации {#stop-the-demo}

```sh
docker compose -f examples/compose.yaml -f examples/compose.resp.yaml down
```

Функция SQL `local_cache.mget(regclass, anyarray)`, доступная в версии 2.x, удалена в версии 3.0.0; см. [руководство по обновлению](UPGRADING.md).
