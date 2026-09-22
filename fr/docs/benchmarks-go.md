---
layout: doc
lang: fr
translation_key: benchmarks-go
title: "Benchmarks Go : SQL et RESP"
description: Résultats locaux de pgx et RESP2 sur Apple M3 Max, avec CPU PostgreSQL, mémoire et montée en charge des connexions.
section: Benchmarks
permalink: /fr/docs/benchmarks-go.html
last_modified_at: "2026-09-16"
---

# Benchmarks Go : SQL et RESP {#go-benchmarks-sql-and-resp}

[Vue d'ensemble](BENCHMARKS.md) · [Node.js](benchmarks-node.md) · [Go et RESP](benchmarks-go.md)

Go 1.27.1 avec pgx 5.11.0 et un client RESP2 de la bibliothèque standard ;
`GOMAXPROCS=8`. [Machine et méthode de mesure](BENCHMARKS.md#test-environment).


Mesurés le 15 septembre 2026 avec le build d'extension `f03ed22`.
Le client Go s'exécute dans la VM Linux, dans un conteneur séparé partageant
l'espace de noms réseau de PostgreSQL. Le CPU et la mémoire du client sont
exclus des compteurs de ressources du serveur. RESP utilise huit workers, un
budget de cache/buffer de 1 Gio et une limite de 512 clients.

Médiane de **requêtes/s**, trois échantillons de cinq secondes par cas :

| Clés/requête | Connexions | SQL préparé | SQL mget | RESP MGET |
|---:|---:|---:|---:|---:|
| 1 | 64 | 277,088 | 211,251 | 722,133 |
| 1 | 256 | 253,790 | 186,296 | 839,678 |
| 64 | 64 | 26,459 | 38,122 | 38,877 |
| 64 | 256 | 27,615 | 43,647 | 52,774 |

Le SQL à 64 clés a varié de 19 à 28k requêtes/s entre les redémarrages du
serveur malgré des données, réglages et plans d'index identiques. Le tableau
utilise l'exécution répétée la plus rapide ; la cause de la variation reste
non résolue. Les [129 échantillons](../../assets/benchmarks/2026-09-15-m3-max-resp.json)
incluent les deux exécutions, les révisions source exactes, les hash des
binaires et les plans de requête.

Les échantillons du cache en lecture seule ont atteint 100 % de hits, sans
erreur ni rejet dû à la limite de connexions. Le harnais vérifie la marge de
connexions SQL et RESP. Une sonde séparée à 256 connexions et 64 clés avec 12
threads Go n'a pas amélioré RESP ; le SQL `mget` a gagné 6 % par rapport à
huit threads.

### Ressources serveur {#server-resources}

À **256 connexions**, médianes des mêmes échantillons :

| Clés/requête | Chemin | Cœurs CPU client | Cœurs CPU serveur (% de la VM) | µs serveur/requête | Pic échantillonné MiB |
|---:|---|---:|---:|---:|---:|
| 1 | SQL | 3.97 | 8.94 (63.9%) | 36.3 | 679.8 |
| 1 | SQL mget | 3.36 | 9.93 (70.9%) | 54.4 | 684.2 |
| 1 | RESP MGET | 5.98 | 6.11 (43.7%) | 7.8 | 263.7 |
| 64 | SQL | 3.72 | 9.45 (67.5%) | 348.3 | 689.8 |
| 64 | SQL mget | 5.12 | 4.99 (35.6%) | 116.0 | 707.7 |
| 64 | RESP MGET | 5.76 | 4.97 (35.5%) | 95.8 | 266.7 |

### Client Go sur macOS {#go-client-on-macos}

Via les ports publiés par Docker, à **64 connexions** ; médianes de trois
échantillons de cinq secondes, en requêtes/s :

| Clés/requête, 64 connexions | SQL préparé | SQL mget | RESP MGET |
|---:|---:|---:|---:|
| 1 | 49,194 | 48,010 | 52,426 |
| 64 | 14,324 | 15,751 | 16,240 |

Les cas VM et hôte diffèrent par le système du client et le chemin réseau. Les
résultats via le port de l'hôte ne peuvent donc pas isoler la limite de débit
de PostgreSQL. RESP a aussi un contrat de session différent : les workers
utilisent un rôle de base configuré et n'héritent ni de la transaction SQL ni
du snapshot d'un appelant. Consultez la [référence RESP](TECHNICAL.md#optional-resp2-endpoint).

## Reproduire {#reproduce}

<details markdown="1">
<summary>Lancer le benchmark Go</summary>

Depuis la racine du dépôt, avec Docker, Node.js 20+ et Go 1.25+ :

```bash
./examples/benchmark.sh go > go.json
```

Le script construit un serveur PostgreSQL éphémère et le client Go, exécute
chaque cas trois fois, enregistre les ressources serveur puis supprime ses
conteneurs. Le client s'exécute dans la VM Docker, dans un cgroup séparé de
PostgreSQL. Valeurs par défaut actuelles : 4/64/256 connexions, 1/16/64 clés
et cinq secondes par échantillon. Utilisez `all` pour exécuter Node.js et Go
contre le même serveur avec la [matrice commune](BENCHMARKS.md#run-the-same-comparison-on-every-client).
Pour reproduire l'exécution enregistrée, utilisez les révisions dans le JSON
des mesures.

Surcharges optionnelles : `CONNECTIONS`, `BATCHES`, `REPEATS`,
`DURATION_SECONDS` et `GOMAXPROCS`. Le JSON enregistre l'identifiant du build
de l'extension en cours.

</details>
