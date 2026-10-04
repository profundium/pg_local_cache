---
layout: doc
lang: es
translation_key: INSTALL_EXISTING
title: Instala pg_local_cache en PostgreSQL 14–18
seo_title: Instala pg_local_cache en PostgreSQL 14–18
description: Instala un paquete Debian o RPM verificado, usa PGXN o PGXS, configura preload, reinicia PostgreSQL e inicializa, actualiza o desinstala pg_local_cache.
section: Instalación
permalink: /es/docs/INSTALL_EXISTING.html
last_modified_at: "2026-10-05"
---

# Instala pg_local_cache en un servidor PostgreSQL existente {#install-pg_local_cache-on-an-existing-postgresql-server}

Esta guía cubre Linux con PostgreSQL 14–18. La instalación y configuración requieren privilegios de superusuario de la base de datos. Añadir la extensión a <code>shared_preload_libraries</code> requiere reiniciar PostgreSQL una vez.

## 1. Requisitos {#choose-an-installation-path}

Al compilar desde el código fuente, usa el <code>pg_config</code> del servidor de destino. Planifica el reinicio con el servicio u operador que administre el clúster de PostgreSQL.

## 2. Instala un paquete {#fast-binary-install}

Descarga el paquete y <code>SHA256SUMS</code> para la versión principal de PostgreSQL y la arquitectura correspondientes desde la misma [versión de GitHub](https://github.com/profundium/pg_local_cache/releases).

### Debian y Ubuntu {#controlled-binary-install}

Descarga <code>postgresql-&lt;major&gt;-pg-local-cache_&lt;version&gt;-1_&lt;arch&gt;.deb</code>. Los paquetes se compilan en Debian 12 y requieren glibc 2.36 o posterior. El manifiesto enumera todos los archivos de la versión; usa <code>--ignore-missing</code> si solo descargas algunos.

Verifica la suma de comprobación y la procedencia de la compilación; después, instala:

```bash
sha256sum -c --ignore-missing SHA256SUMS
gh attestation verify <file>.deb --repo profundium/pg_local_cache
sudo apt install ./<file>.deb
```

### RHEL, Rocky Linux y AlmaLinux 9

Estos RPM requieren PostgreSQL de PGDG con la misma versión principal. Configura
el [repositorio Yum de PGDG](https://www.postgresql.org/download/linux/redhat/)
para tu versión de EL y la versión principal de PostgreSQL, e instala desde PGDG
`postgresql<major>-server` antes del paquete de la extensión. El RPM se instala
en `/usr/pgsql-<major>`. Los paquetes de PostgreSQL de la distribución usan
nombres, rutas y servicios distintos; para esos servidores, sigue las
[instrucciones de compilación desde el código fuente](#build-from-source).

Descarga el <code>.rpm</code> correspondiente a la versión principal de PostgreSQL y a la arquitectura. Verifícalo e instálalo:

```bash
sha256sum -c --ignore-missing SHA256SUMS
gh attestation verify <file>.rpm --repo profundium/pg_local_cache
sudo dnf install ./<file>.rpm
```

### PGXN

Instala el cliente PGXN, los encabezados de desarrollo del servidor PostgreSQL
de destino, un compilador de C y GNU Make. Indica el <code>pg_config</code> del
servidor; PGXN usa el primero que encuentra en <code>PATH</code> si no se indica.
La fase de instalación requiere privilegios de root. Consulta las
[opciones de instalación de PGXN](https://pgxn.github.io/pgxnclient/usage.html#pgxn-install).

```bash
pgxn install --pg_config /path/to/pg_config --sudo sudo pg_local_cache
```

### Compila desde el código fuente {#build-from-source}

Instala los encabezados de desarrollo de la versión de PostgreSQL de destino, un compilador de C y GNU Make. Compila e instala la extensión con el <code>pg_config</code> de esa versión:

```bash
make PG_CONFIG=/path/to/pg_config && \
  sudo make PG_CONFIG=/path/to/pg_config install
```

## 3. Configura <code>postgresql.conf</code> {#configure-before-restart}

Conserva las entradas existentes de `shared_preload_libraries` y añade
`pg_local_cache`. Sustituye `app` por el nombre de la base de datos que atenderá
la extensión.

### Configurar el listener RESP {#enable-optional-resp2}

Configura el listener RESP con un rol worker dedicado, un archivo de token
protegido y TLS nativo. La configuración siguiente activa mTLS al confiar en una
CA de cliente. TLS nativo para RESP requiere PostgreSQL compilado con soporte de
OpenSSL.

```conf
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'app'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.port = 6380
pg_local_cache.bind_address = '10.0.0.10'
pg_local_cache.auth_token_file = '/secure/path/token'
pg_local_cache.cache_entries = 16384
pg_local_cache.memory_budget_mb = 384
pg_local_cache.tls = on
pg_local_cache.tls_cert_file = '/secure/path/resp.crt'
pg_local_cache.tls_key_file = '/secure/path/resp.key'
pg_local_cache.tls_ca_file = '/secure/path/client-ca.crt'
pg_local_cache.tls_min_protocol_version = 'TLSv1.2'
pg_local_cache.allow_plaintext_network = off
```

Sustituye `10.0.0.10` por una dirección accesible para los clientes. TLS está
desactivado de forma predeterminada; para activarlo se necesitan el certificado
y la clave del servidor. Esta configuración protege con TLS un listener RESP
fuera de loopback y verifica certificados de cliente mediante la CA. Para usar
TLS con autenticación solo del servidor, deja vacío `pg_local_cache.tls_ca_file`
y omite los certificados de cliente. TLS para RESP es independiente de los
ajustes `ssl_*` de PostgreSQL. Sin TLS, un listener fuera de loopback requiere
`pg_local_cache.allow_plaintext_network = on`; limita esta opción explícita a
una red de confianza.

La clave privada debe seguir la [regla de PostgreSQL para claves de
servidor](https://www.postgresql.org/docs/current/ssl-tcp.html): modo `0600` si
pertenece al usuario del sistema operativo de PostgreSQL, o propiedad de root
con modo `0640` y lectura para el grupo del servidor.

La clave privada debe seguir la [regla de PostgreSQL para claves de servidor](https://www.postgresql.org/docs/current/ssl-tcp.html): modo `0600` si pertenece al usuario del sistema operativo de PostgreSQL, o propiedad de root con modo `0640` y lectura para el grupo del servidor.

Dimensiona conjuntamente las entradas de caché, los estados de relación, los
workers y los clientes, además de `memory_budget_mb`. Consulta las recomendaciones
de capacidad y memoria en la [referencia técnica](TECHNICAL.md#shared-memory-and-configuration).

## 4. Reinicia PostgreSQL

Usa el servicio u operador que administra el clúster. Con systemd:

```bash
# Debian and Ubuntu
sudo systemctl restart postgresql@<major>-main
# RHEL, Rocky Linux, and AlmaLinux
sudo systemctl restart postgresql-<major>
```

En Patroni, cambia la configuración del clúster y reinícialo mediante Patroni:

```bash
patronictl edit-config <cluster>
patronictl restart <cluster>
```

Para Kubernetes, crea una imagen de PostgreSQL personalizada que incluya el paquete correspondiente y despliega mediante tu operador. Las imágenes de extensión para CloudNativePG están previstas.

## 5. Inicializa {#initialize-a-source-installation}

Conéctate a la base de datos configurada como superusuario de la base de datos. Crea la extensión y un rol worker dedicado; después, concédele acceso a los metadatos:

```sql
CREATE EXTENSION IF NOT EXISTS pg_local_cache;
CREATE ROLE local_cache_worker LOGIN NOINHERIT NOSUPERUSER
    NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE app TO local_cache_worker;
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON TABLE local_cache.mapping TO local_cache_worker;
```

Mantén el rol worker separado de los propietarios de tablas de la aplicación. Asocia cada tabla permanente con una clave primaria compatible:

### Asocia una tabla {#attach-a-table}

```sql
SELECT local_cache.attach_table('public.items'::regclass);
```

### Comprueba el estado {#verify-cold-fill-and-warm-hit}

```sql
SELECT local_cache.health();
```

Confirma que la extensión está lista y que las asociaciones están actualizadas.

## 6. Actualiza {#recover-a-failed-binary-install}

Para actualizar de 2.x a 3.0, sigue primero la [guía de actualización](UPGRADING.md).
Migra las aplicaciones de SQL `mget` a RESP `MGET`, instala el paquete
3.1.0 correspondiente, reinicia PostgreSQL y actualiza la extensión en cada
base de datos donde esté instalada:

```sql
ALTER EXTENSION pg_local_cache UPDATE;
```

## 7. Desinstala {#troubleshooting}

Desasocia todas las tablas y elimina la extensión como superusuario de la base de datos. Quita <code>pg_local_cache</code> de <code>shared_preload_libraries</code>, reinicia PostgreSQL y elimina el paquete con el gestor correspondiente:

```sql
SELECT local_cache.detach_table('public.items'::regclass);
DROP EXTENSION pg_local_cache;
```

```bash
sudo apt remove postgresql-<major>-pg-local-cache
sudo dnf remove pg_local_cache_<major>
```

A continuación, consulta la [referencia técnica](TECHNICAL.md) sobre SQL, memoria, monitorización y seguridad de RESP.
