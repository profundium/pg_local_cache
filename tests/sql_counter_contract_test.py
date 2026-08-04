#!/usr/bin/env python3
"""Source contracts for contention-free, exact SQL cache counters."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
CORE = (ROOT / "src" / "pg_local_cache.c").read_text(encoding="utf-8")
HEADER = (ROOT / "src" / "pg_local_cache.h").read_text(encoding="utf-8")


def c_function(source: str, name: str) -> str:
    marker = re.search(rf"(?m)^{re.escape(name)}\s*\(", source)
    if marker is None:
        raise AssertionError(f"C function {name}() is missing")
    opening = source.find("{", marker.start())
    if opening < 0:
        raise AssertionError(f"C function {name}() has no body")
    depth = 0
    in_string = False
    in_character = False
    in_line_comment = False
    in_block_comment = False
    escaped = False
    for position in range(opening, len(source)):
        character = source[position]
        following = source[position + 1] if position + 1 < len(source) else ""
        if in_line_comment:
            if character == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if character == "*" and following == "/":
                in_block_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if in_character:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "'":
                in_character = False
            continue
        if character == "/" and following == "/":
            in_line_comment = True
            continue
        if character == "/" and following == "*":
            in_block_comment = True
            continue
        if character == '"':
            in_string = True
            continue
        if character == "'":
            in_character = True
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[marker.start() : position + 1]
    raise AssertionError(f"C function {name}() has an unterminated body")


class SqlCounterSourceTests(unittest.TestCase):
    def test_one_exact_cache_line_is_reserved_per_postgres_process_slot(
        self,
    ) -> None:
        self.assertIn("typedef union PgLocalCacheSqlCounterSlot", HEADER)
        self.assertIn("padding[PG_CACHE_LINE_SIZE]", HEADER)
        self.assertIn(
            "sizeof(PgLocalCacheSqlCounterSlot) == PG_CACHE_LINE_SIZE",
            HEADER,
        )
        for counter in ("hits", "misses", "fills", "bypasses"):
            self.assertRegex(HEADER, rf"pg_atomic_uint64\s+{counter}\s*;")

        sizing = c_function(CORE, "pglc_sql_counter_memory_bytes")
        self.assertIn("MaxBackends", sizing)
        self.assertIn("sizeof(PgLocalCacheSqlCounterSlot)", sizing)
        self.assertIn("PG_CACHE_LINE_SIZE - 1", sizing)
        shared_sizing = c_function(CORE, "pglc_shared_memory_bytes")
        self.assertIn("pglc_sql_counter_memory_bytes()", shared_sizing)
        startup_limits = c_function(CORE, "pglc_validate_startup_limits")
        self.assertIn("pglc_sql_counter_memory_bytes()", startup_limits)
        self.assertIn("MaxBackends", startup_limits)

    def test_shared_array_is_aligned_and_initialized_once(self) -> None:
        startup = c_function(CORE, "pglc_shmem_startup")
        self.assertIn('"pg_local_cache SQL counter slots"', startup)
        self.assertIn("TYPEALIGN(\n\t\tPG_CACHE_LINE_SIZE", startup)
        self.assertIn("pglc_sql_counter_slot_count = MaxBackends", startup)
        self.assertIn("if (!counter_slots_found)", startup)
        for counter in ("hits", "misses", "fills", "bypasses"):
            self.assertIn(
                f"pg_atomic_init_u64(&slot->counters.{counter}, 0)",
                startup,
            )

    def test_hot_path_selects_its_unique_proc_number_without_aliasing(self) -> None:
        resolver = c_function(CORE, "pglc_current_sql_counter_slot")
        self.assertIn("#if PG_VERSION_NUM >= 170000", resolver)
        self.assertIn("MyProc->vxid.procNumber", resolver)
        self.assertIn("#else\n\tproc_number = MyProc->pgprocno;\n#endif", resolver)
        self.assertIn("MyProc->pgprocno", resolver)
        self.assertIn("proc_number >= pglc_sql_counter_slot_count", resolver)
        self.assertIn("&pglc_sql_counter_slots[proc_number]", resolver)
        self.assertNotIn("%", resolver)

        hit = c_function(CORE, "pglc_note_sql_cache_hit")
        self.assertIn("pglc_note_sql_cache_hits(1)", hit)
        batch_hits = c_function(CORE, "pglc_note_sql_cache_hits")
        self.assertIn("pglc_current_sql_counter_slot()", batch_hits)
        self.assertIn("slot->counters.hits", batch_hits)
        self.assertRegex(HEADER, r"extern void pglc_note_sql_cache_hits\(uint64 count\);")
        self.assertRegex(HEADER, r"extern void pglc_note_sql_cache_hit\(void\);")

        for event, counter in (
            ("miss", "misses"),
            ("fill", "fills"),
            ("bypass", "bypasses"),
        ):
            with self.subTest(event=event):
                helper = c_function(CORE, f"pglc_note_sql_cache_{event}")
                self.assertIn("pglc_current_sql_counter_slot()", helper)
                self.assertIn(
                    f"pglc_increment_owned_sql_counter(&slot->counters.{counter})",
                    helper,
                )
                self.assertNotIn(
                    f"pg_atomic_fetch_add_u64(&slot->counters.{counter}",
                    helper,
                )
                self.assertIn("else if (pglc_shared != NULL)", helper)
                self.assertRegex(
                    HEADER,
                    rf"extern void pglc_note_sql_cache_{event}\(void\);",
                )

        increment = c_function(CORE, "pglc_increment_owned_sql_counter")
        self.assertIn("pg_atomic_read_u64(counter) + 1", increment)
        self.assertIn("pg_atomic_write_u64", increment)
        self.assertNotIn("pg_atomic_fetch_add_u64", increment)

    def test_postgresql_14_uses_pre_hook_shared_memory_and_guc_apis(self) -> None:
        self.assertIn("#if PG_VERSION_NUM >= 150000", CORE)
        self.assertIn("pglc_shmem_request();", CORE)
        self.assertIn('EmitWarningsOnPlaceholders("pg_local_cache")', CORE)

    def test_stats_and_metrics_sum_every_slot_and_legacy_fallbacks(self) -> None:
        snapshot = c_function(CORE, "pglc_read_sql_counter_snapshot")
        for public_name, slot_name in (
            ("sql_cache_hits", "hits"),
            ("sql_cache_misses", "misses"),
            ("sql_cache_fills", "fills"),
            ("sql_cache_bypasses", "bypasses"),
        ):
            self.assertIn(f"pglc_shared->{public_name}", snapshot)
            self.assertIn(f"slot->counters.{slot_name}", snapshot)
        self.assertIn(
            "counter_slot_index < pglc_sql_counter_slot_count", snapshot
        )

        stats = c_function(CORE, "pglc_stats_json")
        metrics = c_function(CORE, "pglc_metrics_json")
        self.assertIn("pglc_read_sql_counter_snapshot(&sql_counters)", stats)
        self.assertIn("pglc_read_sql_counter_snapshot(&sql_counters)", metrics)
        for name in ("hits", "misses", "fills", "bypasses"):
            self.assertIn(f"sql_counters.{name}", stats)
            self.assertIn(f"sql_counters.{name}", metrics)


if __name__ == "__main__":
    unittest.main()
