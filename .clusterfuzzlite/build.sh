#!/bin/bash
set -eu

mkdir -p "$WORK"

"$CC" $CFLAGS -DPGLC_RESP_STANDALONE -Isrc \
	-c src/resp.c -o "$WORK/resp.o"

"$CC" $CFLAGS -DPGLC_KEY_CODEC_STANDALONE -Isrc \
	-c src/key_codec.c -o "$WORK/key_codec.o"
"$CC" $CFLAGS -DPGLC_RESP_STANDALONE -DPGLC_KEY_CODEC_STANDALONE -Isrc \
	-c fuzz/resp_parse_fuzzer.c -o "$WORK/resp_parse_fuzzer.o"

"$CXX" $CXXFLAGS "$WORK/resp.o" "$WORK/key_codec.o" \
	"$WORK/resp_parse_fuzzer.o" \
	$LIB_FUZZING_ENGINE -o "$OUT/resp_parse_fuzzer"

(cd fuzz/corpus/resp_parse && zip -q -r "$OUT/resp_parse_fuzzer_seed_corpus.zip" .)
