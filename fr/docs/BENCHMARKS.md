---
layout: doc
lang: fr
translation_key: BENCHMARKS
title: Benchmarks du cache PostgreSQL
description: Résultats mesurés de pg_local_cache avec Node.js, Go et RESP sur Apple M3 Max. La machine, le CPU PostgreSQL, la mémoire et la méthode sont inclus.
section: Benchmarks
permalink: /fr/docs/BENCHMARKS.html
last_modified_at: "2026-09-16"
---

# Benchmarks du cache PostgreSQL {#postgresql-cache-benchmarks}

Mesurés sur un Apple M3 Max avec PostgreSQL 16. Chaque comparaison utilise le
même client, le même jeu de données et les mêmes résultats de lignes décodés
pour les lectures mises en cache et les lectures SQL ordinaires.

## Là où le cache a aidé — et là où il n'a pas aidé {#where-the-cache-helpedand-where-it-did-not}

- **SQL à clé unique :** le SQL préparé a dépassé SQL `mget` dans les deux configurations client publiées. Avec 256 connexions Go, il a renvoyé 253,790 requêtes/s contre 186,296 pour `mget`. Ajouter un cache de lignes n'a pas amélioré cette charge SQL.
- **Lots SQL de 64 clés :** Go avec 256 connexions a renvoyé 43,647 requêtes/s via `mget` contre 27,615 pour le SQL préparé, soit environ 1.58× de débit. Node.js avec 64 connexions a montré un gain plus faible : 16,616 contre 15,577. La taille du lot et le coût du client comptent ; vérifiez aussi le CPU et la latence.
- **RESP à clé unique :** Go avec 256 connexions a atteint 839,678 requêtes/s contre la référence SQL de 253,790. Les workers RESP utilisent un rôle de base de données configuré et ne partagent ni la transaction SQL ni le snapshot de l'appelant.

Les [mesures Node.js](benchmarks-node.md) utilisent un client macOS et un
serveur Docker ; les [mesures Go et RESP](benchmarks-go.md) placent les deux
dans la VM Docker. Chaque page contient les répétitions brutes, les versions
exactes et les coûts de ressources du serveur. Ces configurations séparées ne
classent pas les langages. Pour des exemples de connexion, consultez
[Node.js](node-postgres.md), [Go](go.md) ou [RESP](resp.md).

## Lancer la même comparaison avec chaque client {#run-the-same-comparison-on-every-client}

Depuis la racine du dépôt, avec Docker, Node.js 20+ et Go 1.25+ :

```bash
./examples/benchmark.sh all > comparison.json
python3 scripts/benchmark_report.py comparison.json
```

Le runner construit un serveur PostgreSQL éphémère et exécute cette matrice
commune :

| Paramètre | Tous les clients et chemins de lecture |
|---|---|
| Clients | Node.js avec node-postgres / node-redis ; Go avec pgx / RESP2 de la bibliothèque standard |
| Chemins de lecture | SQL préparé `ANY`, SQL `mget`, RESP `MGET` |
| Clés par requête | 1, 16, 64 ; les mêmes clés fixes en commençant à 1 |
| Connexions | 4, 64, 256 ; persistantes, une requête en attente par connexion |
| Échantillons | Trois répétitions de cinq secondes par cas ; l'ordre tourne |
| Placement | Même conteneur client Linux séparé, partageant l'espace de noms réseau de PostgreSQL |
| Avant la mesure | Connexion, comparaison des lignes décodées, puis échauffement de chaque connexion |
| Contrat de résultat | Lignes JSON complètes, ordre d'entrée, doublons, valeurs nulles, clés absentes, entrée vide et entièrement nulle |
| Mesures | Requêtes/s, percentiles de latence, CPU client, CPU/mémoire serveur, compteurs du cache |

Il y a 162 échantillons par défaut, soit environ 14 minutes de travail
chronométré plus la préparation. Pour une courte vérification de correction :

```bash
CONNECTIONS=4 BATCHES=1,16,64 REPEATS=1 DURATION_SECONDS=1 \
  ./examples/benchmark.sh all > smoke.json
```

Utilisez `node` ou `go` au lieu de `all` pour sélectionner un client avec les
mêmes valeurs par défaut. Vous pouvez remplacer `CONNECTIONS`, `BATCHES`,
`REPEATS`, `DURATION_SECONDS` et le `GOMAXPROCS` de Go. Node.js utilise un
seul thread de boucle d'événements ; Go utilise par défaut huit threads. Le
SQL mget de Node enveloppe le tableau renvoyé dans du JSON, tandis que pgx
décode le tableau texte PostgreSQL. Ces coûts client restent inclus dans la
mesure.

Des charges identiques ne rendent pas les protocoles interchangeables : les
workers RESP utilisent leur rôle de base de données configuré et ne rejoignent
ni la transaction SQL ni le snapshot d'un appelant. Consultez le
[contrat RESP](TECHNICAL.md#optional-resp2-endpoint). La comparaison commune
mesure des lectures chaudes. Les diagnostics de lectures froides, mixtes avec
écritures et de coût des écritures restent séparés dans `node-workload`.

Les résultats publiés des 14–15 septembre ci-dessous sont antérieurs à ce
lanceur commun. Leurs environnements d'origine et révisions source restent
attachés aux données ; il ne s'agit pas de nouvelles mesures issues de la
matrice unifiée.

## Environnement de test {#test-environment}


| Composant | Configuration |
|---|---|
| Hôte | MacBook Pro `Mac15,10`, Apple M3 Max : 10 cœurs performance + 4 cœurs efficacité, 36 Gio de RAM |
| Système | macOS 26.5.2, build `25F84`, arm64 |
| VM Docker | Engine 29.7.2, Linux `7.0.12-linuxkit`, 14 CPU, 7.65 Gio de RAM ; aucun quota CPU ou RAM de conteneur |
| PostgreSQL | 16.15, Debian bookworm ; 300 connexions, 128 Mio de shared buffers, 256 Mio de `/dev/shm` |
| Données | 4 096 lignes, valeurs de 128 octets ; 1 024 entrées de cache ; données et WAL sur tmpfs |

Le client et le serveur partagent les CPU du Mac avec dix autres conteneurs de
développement. Tous les clients encodent les requêtes et décodent les lignes
JSON complètes, en conservant l'ordre d'entrée, les doublons et les positions
manquantes. Le SQL utilise des instructions préparées ; les connexions,
l'authentification et l'échauffement sont exclus de la mesure. Il n'y a ni TLS
ni pipelining.

## Méthode de mesure {#measurement-method}


Chaque connexion attend sa réponse avant d'envoyer une autre requête : une
charge **en boucle fermée**, sans correction de l'omission coordonnée. L'ordre
des requêtes tourne entre les répétitions ; les requêtes en cours se terminent
avant l'arrêt de la mesure. Les comparaisons en lecture seule utilisent des
clés fixes déjà présentes dans le cache. Les plans SQL enregistrés utilisent
`items_pkey`, avec zéro lecture de blocs partagés.

Le CPU serveur provient des compteurs cgroup du conteneur PostgreSQL. Un cœur
signifie une seconde CPU par seconde écoulée ; les pourcentages de capacité
divisent par 14. Les µs CPU/requête divisent le temps CPU serveur par le
nombre de requêtes terminées. La fenêtre d'échantillonnage inclut le
monitoring et le bref intervalle de rapport du client.

La mémoire est `memory.current` du cgroup, échantillonnée toutes les 500 ms et
aux points de terminaison. Les tableaux indiquent la médiane du pic
échantillonné de chaque répétition, y compris mémoire partagée, tmpfs et cache
de pages ; il ne s'agit pas du RSS du processus. Les fichiers JSON contiennent
aussi les E/S de blocs, le throttling, les événements mémoire et les snapshots
d'état/attente SQL. Les compteurs réseau excluent la boucle locale et omettent
donc le trafic du client de la VM.

Ces courtes mesures à cache chaud sur un portable partagé ne sont pas des
estimations de capacité de production. Les données et le WAL utilisent tmpfs
avec `fsync`, `full_page_writes` et `synchronous_commit` activés ; les
performances disque ne sont pas testées.

Une exécution échouée se termine avec un code non nul et conserve les
échantillons terminés ; le rendu Markdown rejette les résultats partiels. Pour
le code enregistré exact, utilisez le `harness_ref` et l'`extension_ref` de
chaque JSON. Les commandes de reproduction se trouvent sur les pages client ;
les résultats sont écrits dans des fichiers JSON tels que `benchmark.json`.
