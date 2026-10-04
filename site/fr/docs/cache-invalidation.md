---
layout: doc
lang: fr
translation_key: cache-invalidation
title: Invalidation du cache PostgreSQL tenant compte des transactions
seo_title: "Invalidation du cache PostgreSQL : commit et rollback | pg_local_cache"
description: "Comprenez l’invalidation par trigger pour les lectures RESP de lignes, les mises à jour validées, les lectures source et la frontière des transactions SQL."
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

L'implementation clôt les clés ou relations concernées sur le chemin
d'écriture de la base. Un remplissage transporte des informations de génération
afin de pouvoir être rejeté après une invalidation. Les entrées positives du
cache transportent aussi des informations de visibilité du tuple. Une entrée
inéligible revient à une lecture de la table source. Consultez la
[référence technique](TECHNICAL.md#transaction-consistency) pour le contrat.

{% include diagrams/transaction.html id="invalidation-transaction" %}

## Vérifier l’invalidation entre SQL et RESP {#test-with-two-sessions}

Démarrez la [démo locale](QUICKSTART.md). Lisez la ligne 42 via RESP et notez sa révision. Mettez ensuite la ligne à jour dans PostgreSQL et validez la transaction :

```sql
BEGIN;
UPDATE public.items SET revision = revision + 1 WHERE id = 42;
COMMIT;
```

Le trigger de la table attachée invalide la ligne concernée au commit. La lecture RESP suivante renvoie la révision validée. Pour observer un rollback, lancez une autre mise à jour puis annulez-la ; RESP renvoie toujours la dernière révision validée.

Les workers RESP utilisent le rôle PostgreSQL configuré et ne partagent ni la transaction SQL ni le snapshot de l’application. Une vérification read-your-writes doit utiliser SQL dans la même transaction applicative, qui suit le chemin normal de la table source. Le point de terminaison RESP sert aux lectures séparées avec le rôle worker.

Le [test Node.js exécutable](https://github.com/profundium/pg_local_cache/blob/master/examples/node-postgres/demo.mjs) vérifie les lectures RESP autour des écritures PostgreSQL.

## Cas qui contournent délibérément le cache {#cases-that-deliberately-bypass-the-cache}

`REPEATABLE READ`, `SERIALIZABLE`, la récupération, l'exécution parallèle et
les transactions ayant écrit des données mappées utilisent le chemin de la
table source. Une ligne trop volumineuse peut être renvoyée avec succès sans
être mise en cache. Un taux de hits proche de zéro ne signifie pas forcément
que l'installation a échoué : vérifiez la charge et les compteurs de
contournement.

Si l’application a besoin de `SELECT ... FOR UPDATE`, utilisez l’opération PostgreSQL ordinaire. RESP `MGET` ne fournit ni verrouillage de ligne ni sémantique de session SQL.

## Inspecter la cause d'un miss {#inspect-the-cause-of-a-miss}

En tant qu’administrateur, utilisez `local_cache.stats()` et `local_cache.health()`. Comparez les compteurs avant et après un test contrôlé. Les compteurs du cache décrivent le chemin de lecture RESP. Après des changements DDL intentionnels, suivez la procédure `reconcile_table` ou `reconcile_all` documentée au lieu de supposer qu’un ancien mapping décrit encore la table modifiée.
