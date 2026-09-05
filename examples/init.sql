\set ON_ERROR_STOP on
CREATE EXTENSION pg_local_cache;
-- Mapping registration validates this role even with the RESP listener disabled.
CREATE ROLE local_cache_worker LOGIN NOSUPERUSER NOINHERIT NOCREATEDB
    NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE pglc_demo TO local_cache_worker;
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON local_cache.mapping TO local_cache_worker;
CREATE ROLE demo LOGIN PASSWORD 'demo-only';
CREATE TABLE public.items (
    id bigint PRIMARY KEY,
    value text NOT NULL,
    revision integer NOT NULL DEFAULT 0
);
INSERT INTO public.items (id, value)
SELECT id, repeat(md5(id::text), 4) FROM generate_series(1, 4096) AS id;
CREATE TABLE public.direct_items (LIKE public.items INCLUDING ALL);
INSERT INTO public.direct_items SELECT * FROM public.items;
COMMENT ON TABLE public.items IS 'pg_local_cache disposable demo';
COMMENT ON TABLE public.direct_items IS 'pg_local_cache disposable demo';
GRANT SELECT, UPDATE ON public.items, public.direct_items TO demo;
GRANT USAGE ON SCHEMA local_cache TO demo;
GRANT EXECUTE ON FUNCTION local_cache.mget(regclass, anyarray) TO demo;
SELECT local_cache.attach_table('public.items'::regclass);
ANALYZE public.items;
ANALYZE public.direct_items;
