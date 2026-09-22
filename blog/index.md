---
layout: blog
lang: en
translation_key: blog
title: "PostgreSQL caching blog | pg_local_cache"
description: Practical articles on PostgreSQL row caching, cache invalidation, batch primary-key reads and reproducible performance tests, with examples and technical guides.
permalink: /blog/
last_modified_at: "2026-09-22"
---

# PostgreSQL caching, in practice

Start with the work a request repeats. These articles connect PostgreSQL's read
path to application decisions: what to measure, how stale data gets published,
and what a batch response must preserve.

New to the extension? Run the [local demo](../docs/QUICKSTART.md), then use the
[caching decision guide](../docs/postgresql-caching.md) to choose a read path.
