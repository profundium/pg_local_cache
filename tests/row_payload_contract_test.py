#!/usr/bin/env python3
"""Wire-format and source contracts for whole-row cache payloads."""

from __future__ import annotations

from pathlib import Path
import struct
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "src" / "row_payload.c").read_text(encoding="utf-8")
HEADER = (ROOT / "src" / "row_payload.h").read_text(encoding="utf-8")
KEY_CODEC = (ROOT / "src" / "key_codec.c").read_text(encoding="utf-8")

MAGIC = 0x50474C43
VERSION = 1
HAS_JSON = 1
HEADER_SIZE = 40
CHECKSUM_OFFSET = 28
CRC32C_POLYNOMIAL = 0x82F63B78


def crc32c(payload: bytes) -> int:
    value = 0xFFFFFFFF
    for byte in payload:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (
                CRC32C_POLYNOMIAL if value & 1 else 0
            )
    return value ^ 0xFFFFFFFF


def payload_checksum(payload: bytes) -> int:
    material = bytearray(payload)
    material[CHECKSUM_OFFSET : CHECKSUM_OFFSET + 4] = b"\0" * 4
    return crc32c(bytes(material))


def sample_payload() -> bytes:
    # The tuple body is intentionally opaque here.  PostgreSQL validates the
    # native HeapTupleHeader after the checksum has authenticated its bytes.
    composite = b"composite-datum-placeholder"
    json = b'{"id":1,"value":"cached"}'
    header = struct.pack(
        ">IHHIiIIIIQ",
        MAGIC,
        VERSION,
        HAS_JSON,
        16_384,
        -1,
        2,
        len(composite),
        len(json),
        0,
        0xD4A7_7735_6073_1BC1,
    )
    payload = bytearray(header + composite + json)
    struct.pack_into(">I", payload, CHECKSUM_OFFSET, payload_checksum(payload))
    return bytes(payload)


def corrupted(payload: bytes, offset: int) -> bytes:
    changed = bytearray(payload)
    changed[offset] ^= 0x01
    return bytes(changed)


class RowPayloadWireContractTests(unittest.TestCase):
    def test_v1_header_has_exactly_forty_bytes(self) -> None:
        self.assertEqual(len(sample_payload()) - len(b"composite-datum-placeholder") - len(b'{"id":1,"value":"cached"}'), HEADER_SIZE)
        self.assertIn("#define PGLC_ROW_PAYLOAD_HEADER_SIZE 40", HEADER)

    def test_crc_rejects_header_corruption(self) -> None:
        payload = sample_payload()
        self.assertEqual(payload_checksum(payload), struct.unpack_from(">I", payload, CHECKSUM_OFFSET)[0])
        changed = corrupted(payload, 16)
        self.assertNotEqual(payload_checksum(changed), struct.unpack_from(">I", changed, CHECKSUM_OFFSET)[0])

    def test_crc_rejects_composite_corruption(self) -> None:
        payload = sample_payload()
        changed = corrupted(payload, HEADER_SIZE + 3)
        self.assertNotEqual(payload_checksum(changed), struct.unpack_from(">I", changed, CHECKSUM_OFFSET)[0])

    def test_crc_rejects_json_corruption(self) -> None:
        payload = sample_payload()
        changed = corrupted(payload, len(payload) - 3)
        self.assertNotEqual(payload_checksum(changed), struct.unpack_from(">I", changed, CHECKSUM_OFFSET)[0])


class RowPayloadSourceContractTests(unittest.TestCase):
    def test_checksum_precedes_any_tuple_interpretation(self) -> None:
        decode_at = SOURCE.index("pglc_row_payload_decode_internal(")
        checksum_at = SOURCE.index("pglc_row_payload_checksum(payload", decode_at)
        allocation_at = SOURCE.index("palloc(composite_len)", decode_at)
        tuple_read_at = SOURCE.index("HeapTupleHeaderGetDatumLength", decode_at)
        self.assertLess(checksum_at, allocation_at)
        self.assertLess(checksum_at, tuple_read_at)

    def test_unaligned_input_is_copied_before_tuple_access(self) -> None:
        decode_at = SOURCE.index("pglc_row_payload_decode_internal(")
        copy_at = SOURCE.index(
            "memcpy(composite, payload + PGLC_ROW_PAYLOAD_HEADER_SIZE",
            decode_at,
        )
        tuple_read_at = SOURCE.index("HeapTupleHeaderGetDatumLength", decode_at)
        self.assertLess(copy_at, tuple_read_at)
        self.assertNotIn(
            "(HeapTupleHeader) (payload",
            SOURCE[decode_at:tuple_read_at],
        )

    def test_sql_can_decode_an_aligned_backend_buffer_without_a_copy(self) -> None:
        internal_at = SOURCE.index("pglc_row_payload_decode_internal(")
        public_at = SOURCE.index("pglc_row_payload_decode(", internal_at)
        internal = SOURCE[internal_at:public_at]
        self.assertIn("if (use_input_buffer)", internal)
        self.assertIn("MAXALIGN(composite_address)", internal)
        self.assertIn("composite = (HeapTupleHeader) composite_address", internal)
        self.assertIn("if (!use_input_buffer)", internal)
        self.assertIn("pglc_row_payload_decode_in_place", SOURCE[public_at:])

    def test_encode_flattens_external_toast_and_checks_exact_size(self) -> None:
        self.assertIn("heap_copy_tuple_as_datum(tuple, descriptor)", SOURCE)
        self.assertIn("(composite->t_infomask & HEAP_HASEXTERNAL) != 0", SOURCE)
        self.assertIn("total_len > PGLC_VALUE_MAX", SOURCE)
        self.assertIn("expected_total + json_len != payload_len", SOURCE)

    def test_tupledesc_fingerprint_avoids_raw_struct_bytes(self) -> None:
        fingerprint_at = SOURCE.index(
            "pglc_row_payload_tupledesc_fingerprint("
        )
        checksum_at = SOURCE.index("pglc_row_payload_checksum(")
        fingerprint = SOURCE[fingerprint_at:checksum_at]
        for field in (
            "tdtypeid",
            "tdtypmod",
            "natts",
            "attname",
            "atttypid",
            "atttypmod",
            "attcollation",
            "attlen",
            "attbyval",
            "attalign",
            "attstorage",
            "attcompression",
            "attisdropped",
            "atthasmissing",
            "attgenerated",
            "attidentity",
        ):
            self.assertIn(field, fingerprint)
        self.assertNotIn("sizeof(FormData_pg_attribute)", fingerprint)

    def test_decode_uses_the_prevalidated_descriptor_fingerprint(self) -> None:
        decode_at = SOURCE.index("pglc_row_payload_decode_internal(")
        public_at = SOURCE.index("pglc_row_payload_decode(", decode_at)
        decode = SOURCE[decode_at:public_at]

        self.assertIn("uint64 expected_descriptor_fingerprint", decode)
        self.assertIn("fingerprint != expected_descriptor_fingerprint", decode)
        self.assertNotIn(
            "pglc_row_payload_tupledesc_fingerprint(expected_descriptor)",
            decode,
        )
        self.assertIn(
            "state->mapping.row_descriptor_fingerprint",
            (ROOT / "src" / "pg_local_cache_sql.c").read_text(
                encoding="utf-8"
            ),
        )
        self.assertIn(
            "mapping->row_descriptor_fingerprint",
            (ROOT / "src" / "pg_local_cache_worker.c").read_text(
                encoding="utf-8"
            ),
        )

    def test_sql_safe_mode_does_not_render_json(self) -> None:
        self.assertIn("PGLC_ROW_PAYLOAD_FLAG_HAS_JSON", SOURCE)
        self.assertIn("fmgr_info(F_ROW_TO_JSON_RECORD", SOURCE)
        self.assertIn("FunctionCall1(&row_to_json_finfo", SOURCE)

    def test_composite_keys_use_length_prefixed_components(self) -> None:
        self.assertIn("pglc_canonical_key(", KEY_CODEC)
        self.assertIn("destination[used++] = ':'", KEY_CODEC)
        self.assertIn("destination[used++] = ';'", KEY_CODEC)

    def test_key_codec_writes_once_into_caller_buffer(self) -> None:
        self.assertNotIn("encoded[PGLC_KEY_MAX]", KEY_CODEC)
        self.assertNotIn("memcpy(destination, encoded", KEY_CODEC)
        self.assertIn(
            "used >= destination_capacity - part_len", KEY_CODEC
        )
        self.assertIn("if (nulls[component])", KEY_CODEC)
        self.assertIn("PGLC_MAX_KEY_COLUMNS", KEY_CODEC)

    def test_bpchar_keys_canonicalize_away_typmod_padding(self) -> None:
        self.assertIn("F_BPCHAROUT", KEY_CODEC)
        self.assertIn("rendered[rendered_len - 1] == ' '", KEY_CODEC)


if __name__ == "__main__":
    unittest.main()
