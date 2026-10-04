---
layout: doc
lang: zh
translation_key: batch-primary-key-lookups
title: 批量查找 PostgreSQL 主键
seo_title: "使用 RESP MGET 批量读取 PostgreSQL 行"
description: "了解如何避免主键读取中的 N+1 请求，并通过经过身份验证的 RESP MGET 批量读取完整行。"
section: 指南
permalink: /zh/docs/batch-primary-key-lookups.html
last_modified_at: '2026-10-04'
---

# PostgreSQL 主键批量查询 {#batch-postgresql-primary-key-lookups}

本指南介绍如何通过 RESP `MGET` 批量读取，避免每个键单独请求数据库，同时保留结果对应的输入位置。

## 避免 N+1 次读取 {#graphql-dataloader-and-n1-reads}

如果应用按 ID 逐行读取，初始查询之后还会对数据源执行 N 次读取。将已知主键放入一次 `MGET`，发送有界批量请求。此方式适用于重复读取完整行；它不会缓存任意 SQL，也不能替代连接查询或投影。

## 结果约定 {#know-the-result-contract}

`MGET key [key ...]` 按输入顺序为每个键返回一个数组元素。重复键仍会保留。缺失行对应 `nil` 元素。键格式为 `CRUD:<db>.<schema>.<table>:<json pk>`；编码方式和可运行示例见 [RESP 客户端](resp.md#key-and-response-contract)。

## 何时适合使用 MGET {#when-mget-is-the-right-alternative}

单条命令最多接受 1,024 个键。编码后的响应上限为 66,560 字节，因此即使键数不多，大行也可能要求拆分批次。拆分时同时考虑键数和预计载荷大小。响应超限会返回错误，不会返回不完整数组。

过滤条件、连接、行锁、投影，或需要共享同一事务的读取更适合普通 SQL。[缓存失效指南](cache-invalidation.md)和[技术参考](TECHNICAL.md#transaction-consistency)说明了源查询与 RESP 的行为差异。

3.0.0 移除了 2.x 中的 SQL 函数 `local_cache.mget(regclass, anyarray)`；参见[升级指南](UPGRADING.md)。
