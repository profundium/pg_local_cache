---
layout: doc
lang: en
translation_key: batch-primary-key-lookups
title: Batch PostgreSQL primary-key lookups
seo_title: "Batch PostgreSQL Primary-Key Lookups with RESP MGET"
description: Avoid N+1 row reads with ordered RESP MGET batches and bounded response sizes.
section: Guides
permalink: /docs/batch-primary-key-lookups.html
last_modified_at: "2026-10-04"
---

# Batch PostgreSQL primary-key lookups {#batch-postgresql-primary-key-lookups}

This guide shows how RESP `MGET` batches avoid one database request per key while preserving the requested result positions.

## Prevent N+1 reads {#graphql-dataloader-and-n1-reads}

An application that fetches one row per ID makes N source reads after its initial query. Build one `MGET` from the known primary keys to make a bounded batch request. This is useful for repeated whole-row lookups; it does not cache arbitrary SQL or replace joins and projections.

## Know the result contract {#know-the-result-contract}

`MGET key [key ...]` returns one array element per input key, in input order. Duplicate keys remain duplicated. Missing rows produce nil elements. Each key uses `CRUD:<db>.<schema>.<table>:<json pk>`; see [RESP clients](resp.md#key-and-response-contract) for encoding and runnable examples.

## When MGET fits {#when-mget-is-the-right-alternative}

One command accepts up to 1,024 keys. The encoded reply limit is 66,560 bytes, so large rows may require smaller batches even when the key count is low. Split on both count and expected payload size; an oversized reply returns an error rather than a partial array.

Ordinary SQL remains a better fit for filters, joins, row locks, projections, or reads that must share a transaction. Compare source-query and RESP behavior in the [cache invalidation guide](cache-invalidation.md) and [technical reference](TECHNICAL.md#transaction-consistency).

2.x SQL `local_cache.mget(regclass, anyarray)` was removed in 3.0.0; see the [upgrade guide](UPGRADING.md).
