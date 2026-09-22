---
layout: doc
lang: fr
translation_key: benchmarks-node
title: Benchmarks Node.js
description: "Résultats locaux de node-postgres sur Apple M3 Max : lectures par lots, mises à jour concurrentes, CPU et mémoire PostgreSQL."
section: Benchmarks
permalink: /fr/docs/benchmarks-node.html
last_modified_at: "2026-09-16"
---

# Benchmarks Node.js {#nodejs-benchmarks}

[Vue d'ensemble](BENCHMARKS.md) · [Node.js](benchmarks-node.md) · [Go et RESP](benchmarks-go.md)

Node.js 24.18.0 avec node-postgres 8.16.3.
[Machine et méthode de mesure](BENCHMARKS.md#test-environment).


Mesurés le 14 septembre 2026 avec le build d'extension `67e5754`, un budget
de cache de 384 Mio et des clients macOS via le port SQL publié par Docker.

À **64 clés par requête**, médiane de requêtes/s sur trois échantillons de
10 secondes :

| Connexions | SQL préparé | SQL mget |
|---:|---:|---:|
| 4 | 7,023 | 7,528 |
| 64 | 15,577 | 16,616 |
| 256 | 15,459 | 16,574 |

À 64 connexions, `mget` a utilisé **170 µs de CPU serveur par requête**, contre
286 µs pour le SQL. Le CPU serveur a atteint en moyenne 2.80 contre 4.43 cœurs ;
le pic mémoire échantillonné était de 215.0 contre 205.5 Mio. Le CPU client
était de 0.84 contre 0.87 cœur.

Pour **une clé** à 64 connexions, le SQL était plus rapide : 55,409 contre
53,646 requêtes/s. Le résultat par lots ne s'applique pas aux lectures à clé
unique.

Les [mesures brutes](../../assets/benchmarks/2026-09-14-m3-max-clients.json)
incluent les percentiles de latence, les échantillons de ressources et les
révisions source.

### Lectures mélangées aux écritures {#reads-mixed-with-writes}

Le runner applicatif Node.js utilise **64 connexions**, **50 000 requêtes par
échantillon** et trois répétitions. Les lectures parcourent cycliquement 128
lignes chaudes ; 5 % des opérations de la charge mixte mettent à jour des
lignes.

| Charge | Clés/requête | Requêtes/s SQL préparé | Requêtes/s mget JSON |
|---|---:|---:|---:|
| Lectures chaudes | 1 | 56,104 (52,246–56,364) | 52,842 (52,494–53,399) |
| Lectures chaudes | 16 | 37,391 (36,996–37,938) | 38,425 (38,340–38,617) |
| Lectures chaudes | 64 | 15,178 (15,109–15,488) | 16,294 (16,131–16,368) |
| 5 % de mises à jour | 1 | 55,768 (54,273–56,527) | 52,621 (51,432–53,216) |
| 5 % de mises à jour | 16 | 38,669 (38,429–38,718) | 37,604 (37,481–37,916) |
| 5 % de mises à jour | 64 | 16,049 (16,008–16,063) | 16,814 (16,771–17,229) |

Les valeurs sont des médianes de requêtes/s avec minimum–maximum entre
parenthèses. Les échantillons mixtes comptent ensemble lectures et écritures.
Le `application_run` du JSON inclut les cas de remplissage à froid et de coût
des écritures. Le remplissage à froid au lot 64 ne compte que 64 observations
de latence, trop peu pour une estimation p99 utile.

## Configuration de requête {#query-setup}

```sql
SELECT array_to_json(local_cache.mget('public.items'::regclass, $1::bigint[])) AS rows;
```

Les connexions et les instructions préparées nommées sont réutilisées. Le
décodage JSON et la restauration des positions d'entrée sont inclus dans le
temps de requête. Consultez [l'exemple Node.js](node-postgres.md).

## Reproduire {#reproduce}

<details markdown="1">
<summary>Lancer les benchmarks Node.js</summary>

Depuis la racine du dépôt, avec Docker et Node.js 20+ :

```bash
./examples/benchmark.sh node > node.json
```

Valeurs par défaut actuelles : 4/64/256 connexions, 1/16/64 clés, trois
échantillons de cinq secondes par cas. Node.js exécute désormais les trois
chemins : SQL préparé, SQL `mget` et `MGET` RESP, dans la VM Docker. Le script
crée un serveur éphémère et un conteneur client séparé, enregistre les
ressources puis supprime les deux.
Surcharges optionnelles : `CONNECTIONS`, `BATCHES`, `REPEATS`,
`DURATION_SECONDS`. Utilisez `all` pour inclure Go dans la [même matrice](BENCHMARKS.md#run-the-same-comparison-on-every-client).
Pour la configuration enregistrée sur l'hôte, utilisez les révisions dans le
JSON des mesures.

Pour les lectures mélangées aux écritures :

```bash
BATCHES=1,16,64 ./examples/benchmark.sh node-workload > benchmark.json
python3 scripts/benchmark_report.py benchmark.json
```

Valeurs par défaut : 64 connexions, 50 000 requêtes par échantillon et trois
répétitions. Le runner réinitialise ses tables de démo entre les échantillons.
`CLIENTS`, `REQUESTS`, `BATCHES` et `REPEATS` sont des surcharges optionnelles.

</details>
