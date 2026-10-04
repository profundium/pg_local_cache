---
layout: doc
lang: es
translation_key: UPGRADING
title: Actualizar pg_local_cache de 2.x a 3.1.0
seo_title: "Actualizar pg_local_cache de 2.x a 3.1.0"
description: Migra las aplicaciones de SQL mget a RESP MGET, instala y actualiza la extensión de forma segura y aprende a resolver errores de dependencias y volver a 2.0.4.
section: Instalación
permalink: /es/docs/UPGRADING.html
last_modified_at: "2026-10-05"
---

# Actualizar pg_local_cache de 2.x a 3.1.0 {#upgrade-pg_local_cache-from-2x-to-310}

La versión 3.0.0 elimina la función SQL `local_cache.mget(regclass, anyarray)`.
Usa RESP `MGET` autenticado para leer filas completas almacenadas en caché:

```text
MGET CRUD:app.public.items:{"id":42} CRUD:app.public.items:{"id":7}
```

Los workers RESP usan el rol de PostgreSQL configurado. No heredan los permisos
SQL, la transacción ni el snapshot de la aplicación. Usa SQL para proyecciones,
uniones, bloqueos de filas y lecturas que requieran la semántica de la sesión de
la aplicación. La [guía RESP](resp.md) explica la configuración del cliente y
la codificación de claves.

## Orden de actualización {#upgrade-order}

1. Cambia las aplicaciones para que dejen de llamar a SQL `mget`. Comprueba que
   RESP `MGET` usa un rol worker dedicado y cumple el modelo de autorización
   requerido.
2. Instala el paquete o la biblioteca 3.1.0 correspondiente a la versión
   principal de PostgreSQL en ejecución.
3. Si el listener 2.x usaba una dirección de `pg_local_cache.bind_address`
   fuera de loopback, configura TLS nativo para RESP y proporciona el
   certificado y la clave antes de reiniciar. Establece
   `pg_local_cache.tls_ca_file` para exigir certificados de cliente (mTLS).
   TLS para RESP es independiente de los ajustes `ssl_*` de PostgreSQL. Si se
   necesita tráfico en claro, establece explícitamente
   `pg_local_cache.allow_plaintext_network = on` y solo en una red de confianza.
   Sin TLS ni esa opción explícita para tráfico en claro, los workers RESP no
   arrancarán.
4. Reinicia PostgreSQL para que cargue la nueva biblioteca compartida.
5. Verifica el listener: `SELECT local_cache.health();` debe mostrar
   `workers_running = workers_configured`. Envía RESP `PING` y espera `PONG`.
6. Comprueba la versión de la biblioteca cargada:

   ```sql
   SELECT current_setting('pg_local_cache.binary_version');
   ```

   El resultado debe ser `3.1.0`.
7. En cada base de datos que tenga instalada la extensión, conéctate como
   superusuario de la base y ejecuta:

   ```sql
   ALTER EXTENSION pg_local_cache UPDATE;
   ```

La migración elimina y vuelve a crear `local_cache.metrics()` porque cambia
el tipo de resultado tabular cambió al retirar los contadores de la API SQL de lectura.
Se conservan las concesiones personalizadas de `local_cache.metrics()`. PostgreSQL no permite eliminarla mientras dependa de ella un objeto de usuario.
La migración no usa `CASCADE` de forma deliberada. Elimina o modifica las
vistas y funciones dependientes y luego vuelve a actualizar la extensión. Las
demás funciones C que se conservan se reemplazan en el mismo sitio, por lo que
sus OID, permisos y triggers de las tablas asociadas siguen siendo válidos.

## Volver a 2.0.4 {#rollback-to-204}

No hay un script de degradación. Para volver a 2.0.4, reinstala su paquete,
reinicia PostgreSQL para que cargue la biblioteca antigua, desasocia todas las
tablas mapeadas y después vuelve a crear la extensión en cada base de datos y
asocia las tablas otra vez:

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
CREATE EXTENSION pg_local_cache VERSION '2.0.4';
SELECT local_cache.attach_table('public.items'::regclass);
```

Repite `detach_table` y `attach_table` para cada tabla mapeada. Guarda la
lista de tablas asociadas y cualquier concesión personalizada de privilegios
sobre la extensión antes de revertir para poder restaurarlas. Los objetos de
usuario que dependan de funciones de la extensión pueden impedir su eliminación
con el error de dependencias habitual de PostgreSQL; resuelve esas dependencias
de forma explícita.

## Ruta de búsqueda de bibliotecas {#library-lookup-path}

El archivo de control usa el nombre de biblioteca sin ruta `pg_local_cache`
en `module_pathname`. PostgreSQL lo resuelve mediante
`dynamic_library_path` (que por defecto incluye `$libdir`). Si el servidor
sobrescribe ese ajuste, incluye antes del reinicio el directorio donde se
instaló `pg_local_cache`.

Documentación de 2.x: https://github.com/profundium/pg_local_cache/tree/v2.0.4/docs
