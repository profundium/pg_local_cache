---
layout: doc
lang: es
translation_key: cache-invalidation
title: Invalidación de caché consciente de las transacciones en PostgreSQL
seo_title: "Invalidación de caché en PostgreSQL: commit y rollback | pg_local_cache"
description: Cómo los triggers de PostgreSQL protegen las lecturas de filas RESP frente a commits, rollbacks y cargas de caché simultáneas.
section: Invalidación de caché
permalink: /es/docs/cache-invalidation.html
last_modified_at: "2026-10-04"
---

# Invalidación de caché consciente de las transacciones en PostgreSQL {#transaction-aware-cache-invalidation-in-postgresql}

Esta guía explica cómo los triggers de las tablas adjuntas impiden que un acierto RESP obsoleto siga a una escritura confirmada en PostgreSQL.

![Invalidación de escrituras: las actualizaciones confirmadas publican una barrera; un rollback anterior a su publicación conserva válida la entrada.](../../docs/diagrams/write-invalidation.svg)

Un trigger registra las claves modificadas o una relación modificada dentro de la transacción de escritura. Al confirmar, la extensión publica barreras de invalidación e incrementa las generaciones. Una carga que empezó antes de la barrera no puede publicar datos obsoletos. Un rollback antes de publicar la barrera descarta el estado modificado de la transacción y mantiene válidas las entradas anteriores. Si la transacción se aborta después de publicar la barrera, la invalidación no se revierte y las entradas afectadas siguen siendo inválidas.

Consulte la [referencia técnica de coherencia](TECHNICAL.md#transaction-consistency) para conocer el contrato completo de la ruta de lectura.

## Comprobar la invalidación entre SQL y RESP {#test-with-two-sessions}

Inicie la [demostración local](QUICKSTART.md) y lea la misma clave desde RESP y PostgreSQL:

```text
MGET CRUD:pglc_demo.public.items:{"id":42}
```

En otra sesión SQL, actualice y confirme:

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

El siguiente comando RESP devuelve la revisión confirmada. Si el escritor revierte la transacción, RESP sigue devolviendo la última revisión confirmada.

RESP usa el rol de PostgreSQL configurado en una transacción breve e independiente. No comparte el rol, la transacción ni la instantánea de la aplicación. Para leer las escrituras propias y usar `SELECT ... FOR UPDATE`, ejecute SQL en la transacción de la aplicación.

## Casos que omiten la caché deliberadamente {#cases-that-deliberately-bypass-the-cache}

Con `pg_local_cache.enabled` desactivado, RESP `MGET` omite la consulta y la carga de caché y lee la tabla de origen. Una barrera de escritura activa para una clave, una relación o de forma global también bloquea las lecturas desde caché y las nuevas cargas, por lo que se lee la tabla de origen. Los workers RESP se inician después de que termina la recuperación; esta no es una condición independiente para omitir la caché. Una fila que no cabe en una entrada puede devolverse desde PostgreSQL si su JSON cabe en el límite de valor RESP, pero no se almacena.

## Inspeccionar la causa de un fallo de caché {#inspect-the-cause-of-a-miss}

Compare `local_cache.stats()` y `local_cache.health()` antes y después de una carga de trabajo controlada. Revise los contadores de bypass, fallos de caché, invalidaciones y recarga de asignaciones. Consulte la [lista técnica de métricas](TECHNICAL.md#health-and-monitoring).
