---
layout: doc
lang: fr
translation_key: batch-primary-key-lookups
title: Recherches PostgreSQL par clé primaire en lots
seo_title: "Lecture de lignes PostgreSQL par lots avec RESP MGET"
description: "Découvrez comment éviter les lectures N+1 par clé primaire et lire des lignes complètes par lots avec RESP MGET authentifié."
section: Guides
permalink: /fr/docs/batch-primary-key-lookups.html
last_modified_at: "2026-10-04"
---

# Requêtes PostgreSQL par lots avec clé primaire {#batch-postgresql-primary-key-lookups}

Ce guide explique comment les lots RESP `MGET` évitent un appel à la base par clé tout en conservant la position de chaque résultat demandé.

## Éviter les lectures N+1 {#graphql-dataloader-and-n1-reads}

Si une application récupère une ligne par ID, elle effectue N lectures de la source après la requête initiale. Regroupez les clés primaires connues dans un seul `MGET` pour envoyer une requête par lots bornée. Cette méthode convient aux lectures répétées de lignes complètes ; elle ne met pas en cache le SQL arbitraire et ne remplace ni les jointures ni les projections.

## Contrat du résultat {#know-the-result-contract}

`MGET key [key ...]` renvoie un élément de tableau par clé d’entrée, dans l’ordre demandé. Les doublons sont conservés. Une ligne absente produit un élément `nil`. Chaque clé suit le format `CRUD:<db>.<schema>.<table>:<json pk>` ; l’encodage et les exemples exécutables figurent dans [Clients RESP](resp.md#key-and-response-contract).

## Quand utiliser MGET {#when-mget-is-the-right-alternative}

Une commande accepte jusqu’à 1 024 clés. La réponse encodée est limitée à 66 560 octets ; des lignes volumineuses peuvent donc imposer des lots plus petits, même avec peu de clés. Découpez les lots selon le nombre de clés et la taille de charge prévue. Une réponse trop volumineuse renvoie une erreur, pas un tableau partiel.

Le SQL ordinaire convient mieux aux filtres, jointures, verrous de lignes, projections et lectures qui doivent partager une transaction. Le [guide sur l’invalidation du cache](cache-invalidation.md) et la [référence technique](TECHNICAL.md#transaction-consistency) comparent le comportement de la lecture source et de RESP.

La version 3.0.0 a supprimé la fonction SQL `local_cache.mget(regclass, anyarray)` de la version 2.x ; consultez le [guide de mise à niveau](UPGRADING.md).
