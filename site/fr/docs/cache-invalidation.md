---
layout: doc
lang: fr
translation_key: cache-invalidation
title: Invalidation du cache PostgreSQL tenant compte des transactions
seo_title: "Invalidation du cache PostgreSQL : commit et rollback | pg_local_cache"
description: Testez l'invalidation pg_local_cache 2.0 avec des sessions PostgreSQL concurrentes. Vérifiez les mises à jour non validées, la lecture de ses propres écritures, le rollback, les lectures validées et les règles de repli.
section: Invalidation du cache
permalink: /fr/docs/cache-invalidation.html
last_modified_at: "2026-09-16"
---

# Invalidation du cache PostgreSQL tenant compte des transactions {#transaction-aware-cache-invalidation-in-postgresql}

Supprimer une entrée du cache ne suffit pas si une lecture antérieure peut la
remplir à nouveau après la suppression. Supposons qu'un lecteur commence à
charger une ancienne ligne, qu'un écrivain valide une nouvelle valeur et
invalide la clé, puis que cet ancien chargeur publie son résultat. Le cache
doit aussi rejeter cette publication tardive.

L'implémentation 2.0 clôt les clés ou relations concernées sur le chemin
d'écriture de la base. Un remplissage transporte des informations de génération
afin de pouvoir être rejeté après une invalidation. Les entrées positives du
cache transportent aussi des informations de visibilité du tuple. Une entrée
inéligible revient à une lecture de la table source. Consultez la
[référence technique](TECHNICAL.md#transaction-consistency) pour le contrat.

{% include diagrams/transaction.html id="invalidation-transaction" %}

## Tester avec deux sessions {#test-with-two-sessions}

Démarrez la [démo locale](QUICKSTART.md). Ouvrez cette commande dans deux
terminaux :

```bash
docker compose -f examples/compose.yaml exec postgres \
  psql -X -v ON_ERROR_STOP=1 -U demo -d pglc_demo
```

Dans la session A, lisez la ligne 42 et notez sa révision, puis relisez-la pour
la réchauffer :

```sql
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
```

Dans la session B, mettez à jour la ligne mais laissez la transaction ouverte :

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
SELECT (local_cache.mget('public.items'::regclass, ARRAY[42]::bigint[]))[1]::jsonb ->> 'revision';
```

B voit sa propre incrémentation. Cette lecture contourne le cache. Répétez la
requête de A pendant que B reste ouverte : A doit toujours voir la révision
validée, et non la valeur non validée de B. Dans B, exécutez `ROLLBACK` ; une
autre requête dans A doit encore renvoyer la révision originale.

Exécutez maintenant dans B :

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

Une requête lancée dans A après ce commit doit renvoyer la révision incrémentée.
C'est la limite importante : une instruction déjà en cours n'a pas à basculer
vers un snapshot pris après son démarrage. PostgreSQL documente ce comportement
pour [Read Committed](https://www.postgresql.org/docs/16/transaction-iso.html#XACT-READ-COMMITTED).

Le [test Node.js exécutable](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/demo.mjs)
vérifie ces observations avec des connexions séparées.

## Cas qui contournent délibérément le cache {#cases-that-deliberately-bypass-the-cache}

`REPEATABLE READ`, `SERIALIZABLE`, la récupération, l'exécution parallèle et
les transactions ayant écrit des données mappées utilisent le chemin de la
table source. Une ligne trop volumineuse peut être renvoyée avec succès sans
être mise en cache. Un taux de hits proche de zéro ne signifie pas forcément
que l'installation a échoué : vérifiez la charge et les compteurs de
contournement.

Lorsque l'application a besoin de `SELECT ... FOR UPDATE`, utilisez
l'opération PostgreSQL ordinaire ; `mget` ne remplace pas le verrouillage de
lignes.

## Inspecter la cause d'un miss {#inspect-the-cause-of-a-miss}

Utilisez `local_cache.stats()` et `local_cache.health()` en tant
qu'administrateur. Comparez les snapshots de compteurs avant et après un test
contrôlé. Gardez les compteurs SQL `mget` séparés des compteurs RESP. Après un
DDL intentionnel, suivez la procédure documentée `reconcile_table` ou
`reconcile_all` au lieu de supposer qu'un mapping précédemment attaché décrit
encore la table modifiée.
