CREATE TABLE IF NOT EXISTS public.pg_local_cache_demo (
    id bigint PRIMARY KEY,
    value text NOT NULL
);

INSERT INTO public.pg_local_cache_demo (id, value) VALUES
    (1, 'served through ordinary PostgreSQL SQL'),
    (2, 'kept coherent by pg_local_cache')
ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value;

GRANT SELECT ON public.pg_local_cache_demo TO local_cache_worker;
SELECT local_cache.attach_table('public.pg_local_cache_demo'::regclass);
