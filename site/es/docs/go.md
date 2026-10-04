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

La demo muestra una consulta SQL preparada normal como alternativa:

```sql
SELECT id::text AS key, row_to_json(items)::text AS row
FROM public.items
WHERE id = ANY($1::bigint[]);
```

El ejemplo solicita `42, 7, 42, NULL, 999999`, restaura en Go el orden de entrada y muestra null para la entrada nula y la clave ausente. Para lecturas en caché, usa RESP `MGET`; el endpoint usa el rol configurado para los workers y no forma parte de la transacción SQL de la aplicación.

## Compara filas, no solo viajes de ida y vuelta {#compare-rows-not-just-round-trips}

Una consulta normal `WHERE id = ANY($1::bigint[])` no conserva las posiciones solicitadas. Restaura el orden, los duplicados y las filas ausentes en los resultados SQL normales. Para leer filas completas en caché, usa RESP `MGET`; la [guía de consultas por lotes](batch-primary-key-lookups.md) compara los contratos.

El benchmark usa conexiones persistentes y sentencias preparadas. Preparar SQL no almacena en caché las filas del resultado: consulta la [guía de caché de PostgreSQL](postgresql-caching.md). El [benchmark común](BENCHMARKS.md#run-the-same-comparison-on-every-client) prueba Go y Node.js con las mismas claves, tamaños de lote, conexiones y duración mediante SQL preparado y RESP `MGET`. RESP no comparte la transacción SQL del llamador.

Consulta el [quickstart](QUICKSTART.md) para la configuración y las [comprobaciones de transacciones](cache-invalidation.md) antes de adaptar el recorrido de lectura.
