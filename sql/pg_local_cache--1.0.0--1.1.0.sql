\echo Use "ALTER EXTENSION pg_local_cache UPDATE TO '1.1.0'" to load this file. \quit

-- Version 1.1.0 expands the tested PostgreSQL and Linux release matrix.
-- It does not change SQL objects, so existing 1.0.0 installations only need
-- the new shared library before ALTER EXTENSION records the new version.
