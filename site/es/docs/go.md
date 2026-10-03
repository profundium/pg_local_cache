---
layout: doc
lang: es
translation_key: go
title: Consultas por lotes de filas con Go y pgx
seo_title: "Consultas por lotes de filas de PostgreSQL con Go y pgx"
description: Usa pg_local_cache desde Go con pgx, claves parametrizadas y filas JSON descodificadas.
section: Go
permalink: /es/docs/go.html
last_modified_at: "2026-09-16"
---

# Consultas por lotes de filas con Go y pgx {#batch-row-lookups-with-go-and-pgx}

Inicia primero la [base de datos desechable](QUICKSTART.md) y después ejecuta:

```bash
go -C examples/go-pgx run ./demo
```

Usa `127.0.0.1:55432`, la base de datos `pglc_demo` y las credenciales `demo` / `demo-only`. Define `PGLC_DEMO_PORT` cuando quickstart use otro puerto.

La demo envía las claves como parámetro de consulta:

```sql
SELECT local_cache.mget('public.items'::regclass, $1::bigint[]);
```

`mget` devuelve `text[]`; cada elemento no nulo es una fila JSON. El ejemplo solicita `42, 7, 42, NULL, 999999` e imprime las filas en el orden de entrada. El `42` duplicado permanece en ambas posiciones; la entrada nula y `999999`, que falta, producen elementos nulos.

## Compara filas, no solo viajes de ida y vuelta {#compare-rows-not-just-round-trips}

Una consulta normal `WHERE id = ANY($1::bigint[])` no conserva las posiciones solicitadas. Restaura el orden de entrada, los duplicados y las filas ausentes antes de compararla con `mget`; la [guía de consultas por lotes](batch-primary-key-lookups.md) muestra los enfoques desde el cliente y desde SQL.

El benchmark usa conexiones persistentes y sentencias preparadas. Preparar SQL no almacena en caché sus filas de resultado: consulta la [guía de caché de PostgreSQL](postgresql-caching.md). El [benchmark común](BENCHMARKS.md#run-the-same-comparison-on-every-client) prueba Go y Node.js con las mismas claves, tamaños de lote, cantidades de conexiones y duración mediante SQL y RESP. RESP no comparte la transacción SQL de quien llama.

Consulta el [quickstart](QUICKSTART.md) para la configuración y las [comprobaciones de transacciones](cache-invalidation.md) antes de adaptar el recorrido de lectura.
