FROM gcr.io/oss-fuzz-base/base-builder

COPY . $SRC/pg_local_cache
COPY .clusterfuzzlite/build.sh $SRC/
WORKDIR $SRC/pg_local_cache
