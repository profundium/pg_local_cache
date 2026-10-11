---
layout: post
lang: fr
translation_key: blog-row-cache-inside-postgresql
title: "Cache-aside sans course : le cache de lignes dans PostgreSQL à l’épreuve des mesures"
description: pg_local_cache 3.1.0 garde les lignes fréquentes en mémoire partagée PostgreSQL et les sert via RESP. Comparaison avec Valkey et SQL préparé, sans lignes obsolètes.
permalink: /fr/blog/row-cache-inside-postgresql/
date: "2026-10-12"
last_modified_at: "2026-10-12"
topic: performance
---

# Cache-aside sans course {#cache-aside-without-the-race}

Quand les lectures par clé primaire deviennent fréquentes, on place souvent
Redis ou Valkey à côté de PostgreSQL et on utilise le cache-aside : lire le
cache, interroger PostgreSQL et remplir le cache en cas de miss, puis supprimer
la clé après chaque écriture. Cela fonctionne jusqu’au chevauchement d’une
lecture et d’une écriture. Un lecteur qui a manqué le cache peut récupérer la
ligne juste avant le commit de l’écrivain ; le `DEL` de l’écrivain arrive en
premier, puis le remplissage du lecteur, et l’ancienne ligne reste dans le cache
jusqu’à ce qu’une autre opération la supprime. Nous avons déjà décrit cette
[course au remplissage tardif](cache-aside-late-fill.md).

pg_local_cache place le cache directement dans PostgreSQL : le cache et les
écritures qu’il doit suivre relèvent du même système transactionnel.

## Comment la cohérence est maintenue {#how-it-stays-correct}

- Les lignes d’une table attachée sont conservées dans une mémoire partagée
  bornée, indexées par clé primaire et servies par des processus d’arrière-plan
  via RESP2. Tout client Redis peut les lire avec `MGET` ; la clé suit le format
  `CRUD:<db>.<schema>.<table>:<json primary key>`.
- Les triggers de ligne publient une barrière d’invalidation pour chaque clé
  écrite avant que la transaction devienne visible. Les changements portant sur
  toute la relation, comme `TRUNCATE`, publient une barrière pour toute la table.
- En cas de miss, le lecteur prend d’abord un jeton de lecture. Le résultat
  n’est stocké que si aucune barrière visant la clé, la table ou le cache entier
  n’a été publiée entre-temps. Un remplissage qui chevauche une écriture est
  écarté.
- Les misses simultanés sur une même clé sont regroupés : un lecteur charge la
  ligne et les autres utilisent son résultat.
- PostgreSQL reste la source de vérité. Le cache est temporaire et peut être
  supprimé à tout moment.

## Coût et avantages {#results}

Ces chiffres proviennent de la version 3.1.0 sur deux VM de 16 vCPU (Xeon Gold
6354, 10 GbE), avec PostgreSQL 16 et un client Go/pgx. La [page des benchmarks](../docs/BENCHMARKS.md)
présente tous les tableaux, les fichiers JSONL bruts et les étapes de
reproduction.

**Lectures pendant les écritures.** 64 lecteurs, une clé par lecture,
répartition uniforme sur 60k lignes, 30,000 `UPDATE`/s :

| Système | Lectures/s | Taux de hits | Entrées obsolètes |
| --- | --- | --- | --- |
| pg_local_cache | 161,919 | 0.837 | 0 |
| Valkey cache-aside | 42,294 | 0.577 | 0 |
| SQL préparé | 106,949 | — | — |

Avec un nombre illimité d’écrivains, Valkey cache-aside a terminé avec des
entrées obsolètes (1 lors du test mixte ; 2 et 1 pendant les sondes de
120 secondes). pg_local_cache a terminé chaque test sans entrée obsolète, y
compris un test d’une heure avec 252M `MGET` et 72M `UPDATE`.

**Surcoût en écriture.** Par rapport aux écritures directes dans la table, le
débit des `UPDATE` via pg_local_cache était inférieur de 3.6–8.2% ; celui des
`UPDATE` suivis d’un `DEL` sur Valkey, de 28.9–63.7%.

**Débit en lecture.** Avec le serveur limité à une paire de vCPU, pg_local_cache
a servi 210k lectures/s contre 180k pour Valkey mono-thread ; avec deux paires,
337k contre 266k pour Valkey avec threads d’E/S. À quatre paires, les deux
atteignaient un plafond limité par le client, autour de 360k ; pg_local_cache
utilisait 5.2 vCPU serveur, Valkey 8.0. Valkey mono-thread consommait le moins
de CPU par requête. SQL préparé était très en retrait dans les tests à clé
unique.

## Quand ce n’est pas le bon outil {#when-not-to-use-it}

- Ce n’est pas un remplacement de Redis : pas de TTL, de pub/sub ni de
  structures de données, uniquement la lecture de lignes entières par clé
  primaire.
- L’ensemble chaud doit tenir dans la mémoire configurée ; une ligne est
  limitée à 64 KiB de JSON via RESP.
- Il dessert une seule base sur un seul serveur primaire inscriptible.
- Il nécessite `shared_preload_libraries` et un redémarrage.

## Essayer {#try-it}

pg_local_cache prend en charge PostgreSQL 14–18 et est distribué sous licence
MIT. Installez-le depuis les [versions GitHub](https://github.com/profundium/pg_local_cache/releases)
(deb et rpm), [PGXN](https://pgxn.org/dist/pg_local_cache/) ou les dépôts Pigsty,
puis suivez le [guide de démarrage rapide](../docs/QUICKSTART.md).
