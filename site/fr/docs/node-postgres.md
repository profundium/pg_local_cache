---
layout: doc
lang: fr
translation_key: node-postgres
title: "Recherches de lignes par lots avec node-postgres"
seo_title: "Recherches de lignes PostgreSQL par lots avec node-postgres"
description: "Utilisez RESP MGET authentifié depuis Node.js pour les lectures de lignes en cache, et node-postgres pour les écritures SQL et les requêtes ordinaires."
section: Node.js
permalink: /fr/docs/node-postgres.html
last_modified_at: "2026-09-16"
---

# Recherches de lignes par lots avec node-postgres {#batch-row-lookups-with-node-postgres}

Lisez les lignes par clé primaire avec votre connexion ou votre pool
node-postgres existant.

Démarrez la [démo](QUICKSTART.md), installez les dépendances et exécutez ses
assertions d'intégration :

```bash
npm --prefix examples/node-postgres ci --ignore-scripts
npm --prefix examples/node-postgres run demo
```

## Lire via RESP {#send-one-parameterized-query}

Avec un client RESP connecté de `@redis/client` :

```js
const ids = [42, 7, 42, null, 999999];
const wireKeys = ids.filter(id => id !== null).map(id =>
  `CRUD:app.public.items:${JSON.stringify({ id })}`
);
const values = await client.mGet(wireKeys);
let position = 0;
const rows = ids.map(id => {
  if (id === null) return null;
  const value = values[position++];
  return value === null ? null : JSON.parse(value);
});
```

RESP `MGET` renvoie des lignes JSON dans l’ordre des clés. Le helper omet les clés d’entrée nulles et rétablit leurs positions ; les clés absentes renvoient null.

Gardez le nom de table fixe dans le code applicatif. Passez les identifiants comme paramètres de requête ; ne construisez pas le SQL par concaténation de chaînes. Voir la documentation node-postgres sur les [paramètres et instructions préparées nommées](https://node-postgres.com/features/queries).

La commande RESP accepte au plus 1 024 clés. Le helper exécutable renvoie `[]` sans requête si toutes les entrées sont nulles. Les champs PostgreSQL `bigint` et les nombres JSON peuvent dépasser la précision exacte de JavaScript ; utilisez un parseur JSON sans perte ou un contrat de sérialisation explicite.

## Comparer avec la requête par lots existante {#compare-with-the-existing-batch-query}

La référence utilise :

```sql
SELECT id::text AS key, row_to_json(i)::text AS row
FROM public.items AS i
WHERE id = ANY($1::bigint[]);
```

`ANY` ne conserve ni l'ordre d'entrée ni les positions demandées en double.
L'exemple les restaure côté client et fournit une valeur nulle pour les lignes
absentes avant de comparer les résultats.

L'implémentation exécutable se trouve dans
[examples/node-postgres](https://github.com/profundium/pg_local_cache/tree/master/examples/node-postgres).
Le helper reçoit un client existant au lieu de créer un pool par appel.

## Transactions et limites applicatives {#transactions-and-application-boundaries}

Utilisez un même client acquis pendant toute une transaction. Les lectures
après des écritures dans la même transaction utilisent le chemin de la table
source PostgreSQL. La démo le vérifie avec des connexions de lecture et
d'écriture séparées ; consultez [l'invalidation du cache](cache-invalidation.md).

## Instructions préparées et cache des résultats {#prepared-statements-and-result-caching}

Une requête node-postgres nommée réutilise une instruction préparée sur chaque connexion. Elle ne met pas en cache les lignes renvoyées. RESP `MGET` utilise le cache partagé de lignes complètes de l’extension, mais son rôle worker et son état de session sont séparés de la connexion SQL de l’application. Voir le [guide de décision sur le cache](postgresql-caching.md) et le [guide de recherches par lots](batch-primary-key-lookups.md).

Pour RESP2, utilisez l’[exemple RESP Node.js](resp.md#nodejs). Les [résultats Node.js enregistrés](benchmarks-node.md) incluent des lectures par lots et des mises à jour concurrentes. Le [benchmark commun](BENCHMARKS.md#run-the-same-comparison-on-every-client) exécute Node.js et Go avec les mêmes scénarios SQL préparé et RESP.
