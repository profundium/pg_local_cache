---
layout: blog
lang: zh
translation_key: blog
title: PostgreSQL 缓存博客 | pg_local_cache
description: 关于 PostgreSQL 行缓存、缓存失效、批量主键读取和可复现性能测试的实践文章，附有示例与技术指南。
permalink: /zh/blog/
last_modified_at: '2026-09-22'
---

# PostgreSQL 缓存实践

从请求中重复的工作开始。这些文章把 PostgreSQL 读取路径与应用决策联系起来：应该测量什么、旧数据如何被发布，以及批量响应必须保留哪些信息。

首次使用此扩展？先运行[本地演示](../docs/QUICKSTART.md)，再借助[缓存选择指南](../docs/postgresql-caching.md)确定读取路径。
