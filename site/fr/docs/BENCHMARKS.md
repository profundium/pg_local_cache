---
layout: doc
lang: fr
translation_key: BENCHMARKS
title: Benchmarks du cache PostgreSQL, 3.1.0
description: "Résultats pg_local_cache 3.1.0 sur deux machines : lectures épinglées et complètes, écritures, lectures pendant les écritures et méthode."
section: Benchmarks
permalink: /fr/docs/BENCHMARKS.html
redirect_from:
  - /fr/docs/benchmarks-go.html
  - /fr/docs/benchmarks-node.html
  - /fr/docs/benchmarks-go/
  - /fr/docs/benchmarks-node/
last_modified_at: "2026-10-07"
---
# Benchmarks du cache PostgreSQL : 3.1.0

Ces mesures comparent pg_local_cache RESP MGET, Valkey et SQL préparé sur deux VM reliées par un réseau privé. Tous les résultats utilisent le build final 3.1.0 c431bcc. Les médianes des essais répétés sont indiquées pour les matrices de lecture et les essais d’écriture ; les deux cas épinglés à un cœur avec Valkey io-threads=1 ont deux répétitions, les autres cas épinglés en ont cinq. Les essais mixtes, les sondes de 120 secondes et le soak d’une heure sont des essais uniques. Ces essais ne garantissent pas une capacité en production.

## Résultats du build final

- Par cœur serveur épinglé, la meilleure configuration Valkey mesurée était io-threads=1 sur un cœur (180k lectures/s contre 210k pour pg_local_cache) ; sur deux cœurs, c’était io-threads=4 (266k contre 337k). Sur quatre cœurs, le débit a atteint un plateau limité par le client autour de 361k contre 357k ; pg_local_cache utilisait 5,24 contre 7,95 vCPU serveur. Valkey avec io-threads=1 utilisait le moins de CPU serveur par requête. SQL préparé était loin derrière dans les tests épinglés à clé unique.
- Avec Valkey io-threads=8, les écarts observés entre médianes MGET 16/64 allaient d’environ −2,15 % à +1,26 % ; les plages ne se chevauchaient pour aucune des quatre comparaisons à 64 clés. Ces essais ne démontrent pas une équivalence statistique. Les grands MGET étaient limités par le client et le réseau. Dans une ligne, p99 était plus élevé pour pg_local_cache avec clés aléatoires, MGET 64 et 256 clients (43,52 contre 40,37 ms).
- Le surcoût par rapport aux transactions sur table simple était de 3,6–8,2 % pour UPDATE et de 3,7–5,5 % pour INSERT. UPDATE simple suivi de Valkey DEL était inférieur de 28,9–63,7 % au débit UPDATE simple.
- Pendant les écritures, aucun contrôle n’a trouvé d’entrée périmée pg_local_cache. Après 120 secondes, Valkey cache-aside conservait 2 entrées périmées en Uniform et 1 en Zipf. Le soak d’une heure a terminé 252 M MGET et 72 M UPDATE ; la croissance RssAnon est restée sous 44 kB par worker.

## Lectures par paire de vCPU serveur épinglée

Le client Go/pgx utilisait 256 clients et une clé par MGET. Chaque cas a duré 20 secondes. Les deux cas épinglés à une paire avec Valkey io-threads=1 ont été répétés deux fois ; les autres cas épinglés, cinq fois. PostgreSQL et Valkey étaient épinglés à N paires de vCPU (2N vCPU). Valkey utilisait io-threads=2N et io-threads=1 ; SQL préparé utilisait les mêmes clés.

| Paires de vCPU | Clés | pg_local_cache req/s (p99 ms, vCPU) | Valkey io-threads=2N (req/s, p99 ms, vCPU) | Valkey io-threads=1 (req/s, p99 ms, vCPU) | SQL préparé (req/s, p99 ms, vCPU) |
| --- | --- | --- | --- | --- | --- |
| 1 | Chaudes | 209,696 (2.46, 1.99) | 91,478 (3.24, 2.00) | 180,141 (2.59, 0.85) | 23,200 (24.90, 2.00) |
| 1 | 100k aléatoires | 195,950 (2.65, 2.00) | 88,672 (3.38, 2.00) | 175,853 (2.65, 0.92) | 22,433 (25.95, 2.00) |
| 2 | Chaudes | 336,691 (2.02, 3.78) | 266,340 (1.69, 4.00) | 162,230 (2.85, 0.66) | 46,386 (16.38, 4.00) |
| 2 | 100k aléatoires | 326,125 (1.98, 3.83) | 267,826 (1.72, 4.00) | 160,220 (2.92, 0.69) | 45,211 (17.56, 4.00) |
| 4 | Chaudes | 360,968 (2.33, 5.24) | 357,161 (2.20, 7.95) | 160,633 (2.85, 0.66) | 88,604 (6.75, 8.00) |
| 4 | 100k aléatoires | 353,941 (2.33, 5.27) | 351,344 (2.20, 7.95) | 159,414 (2.92, 0.68) | 86,790 (6.88, 8.00) |

Sur un cœur, Valkey mono-thread était sa meilleure configuration. Sur deux cœurs, io-threads=4 était le meilleur réglage ; p99 de Valkey était plus faible malgré un débit inférieur. Sur quatre cœurs, pg_local_cache et io-threads=8 atteignaient presque le même débit limité par le client, avec moins de CPU serveur pour pg_local_cache.

Raw runs: [Valkey io-threads=2N](../../docs/benchmarks/3.1.0/read-per-core.jsonl), [Valkey io-threads=1](../../docs/benchmarks/3.1.0/read-per-core-valkey-io-threads-1.jsonl).

## Lectures avec les 16 vCPU disponibles

Ces essais n’étaient pas épinglés et ont été répétés trois fois. Dans les cas limités par le client, le nœud de charge utilisait environ 7–13 cœurs CPU. Avec Valkey io-threads=8, les écarts observés entre médianes MGET 16/64 allaient d’environ −2,15 % à +1,26 % ; les plages ne se chevauchaient pour aucune des quatre comparaisons à 64 clés. Ces essais ne démontrent pas une équivalence statistique. Avec io-threads=1, MGET 16 aléatoire à 256 clients a mesuré 116 130 contre 97 489 req/s (19,1 % de plus pour pg_local_cache). Les grands MGET étaient limités par le client et le réseau ; aucun résultat io-threads=1 n’est disponible pour 64 clés.

| Clés | Clés MGET | Clients | pg_local_cache (req/s, p99 ms, vCPU) | Valkey io-threads=8 (req/s, p99 ms, vCPU) | Valkey io-threads=1 (req/s, p99 ms, vCPU) | SQL préparé (req/s, p99 ms, vCPU) |
| --- | --- | --- | --- | --- | --- | --- |
| Chaudes | 1 | 64 | 217,724 (0.70, 2.89) | 206,634 (0.70, 7.85) | 167,929 (0.71, 0.66) | 132,433 (0.96, 11.32) |
| Chaudes | 1 | 256 | 372,683 (2.39, 4.21) | 365,947 (2.33, 7.92) | 161,455 (2.92, 0.68) | 173,571 (4.26, 13.14) |
| Chaudes | 16 | 64 | 82,858 (3.38, 3.13) | 83,098 (3.38, 6.41) | 79,179 (3.38, 0.69) | 73,512 (2.05, 8.78) |
| Chaudes | 16 | 256 | 122,496 (9.83, 4.07) | 122,817 (9.83, 7.42) | 111,790 (8.06, 0.83) | 100,966 (6.36, 11.49) |
| Chaudes | 64 | 64 | 30,241 (8.78, 3.40) | 30,845 (9.04, 3.57) | — | 27,790 (6.88, 9.05) |
| Chaudes | 64 | 256 | 39,531 (43.52, 4.26) | 39,995 (47.71, 5.66) | — | 34,519 (30.67, 12.23) |
| 100k aléatoires | 1 | 64 | 215,310 (0.70, 2.89) | 205,081 (0.70, 7.85) | 162,778 (0.73, 0.67) | 131,563 (0.97, 11.34) |
| 100k aléatoires | 1 | 256 | 366,860 (2.39, 4.31) | 360,894 (2.33, 7.92) | 158,444 (2.98, 0.68) | 169,884 (4.26, 13.19) |
| 100k aléatoires | 16 | 64 | 79,505 (3.51, 3.45) | 79,556 (3.44, 6.68) | 74,270 (3.38, 0.79) | 69,262 (2.13, 9.12) |
| 100k aléatoires | 16 | 256 | 116,130 (10.35, 4.42) | 114,683 (9.83, 7.53) | 97,489 (7.67, 0.95) | 91,572 (7.01, 11.96) |
| 100k aléatoires | 64 | 64 | 29,000 (8.78, 3.81) | 29,464 (9.04, 4.61) | — | 26,169 (7.14, 9.39) |
| 100k aléatoires | 64 | 256 | 37,356 (43.52, 4.93) | 38,175 (40.37, 7.58) | — | 32,756 (31.20, 13.03) |

Pour des clés aléatoires, MGET 64 et 256 clients, p99 de pg_local_cache était de 43,52 ms contre 40,37 ms pour Valkey. SQL préparé avait un débit nettement inférieur dans les essais épinglés à clé unique ; les résultats varient selon la taille du lot et la latence.

Raw runs: [Valkey io-threads=8](../../docs/benchmarks/3.1.0/read-8-cores.jsonl), [Valkey io-threads=1](../../docs/benchmarks/3.1.0/read-8-cores-valkey-io-threads-1.jsonl).

## Surcoût des écritures

Trois répétitions de 15 secondes comparent les transactions sur table simple, celles via pg_local_cache et les transactions simples suivies de Valkey DEL. Les pourcentages comparent au cas correspondant sur table simple.

| Opération | synchronous_commit | Clients | Table simple tx/s (DB µs/tx) | pg_local_cache tx/s (DB µs/tx, écart) | Table simple + Valkey DEL tx/s (DB µs/tx, écart) |
| --- | --- | --- | --- | --- | --- |
| Update | on | 32 | 46,747 (109) | 45,065 (119, −3.6%) | 33,234 (204, −28.9%) |
| Update | on | 64 | 63,479 (108) | 61,027 (121, −3.9%) | 37,659 (238, −40.7%) |
| Update | off | 32 | 119,156 (75) | 110,987 (87, −6.9%) | 53,374 (158, −55.2%) |
| Update | off | 64 | 148,358 (73) | 136,194 (83, −8.2%) | 53,853 (189, −63.7%) |
| Insert | on | 32 | 48,831 (97) | 47,020 (105, −3.7%) | 34,863 (184, −28.6%) |
| Insert | on | 64 | 65,688 (97) | 63,011 (108, −4.1%) | 39,642 (218, −39.7%) |
| Insert | off | 32 | 123,679 (63) | 117,868 (72, −4.7%) | 58,168 (135, −53.0%) |
| Insert | off | 64 | 138,454 (68) | 130,805 (77, −5.5%) | 58,361 (165, −57.8%) |

Pour UPDATE, pg_local_cache était 3,6–8,2 % sous les écritures simples ; le surcoût INSERT était de 3,7–5,5 %. UPDATE simple plus Valkey DEL était inférieur de 28,9–63,7 % au débit UPDATE simple ; pour INSERT, l’écart était de 28,6–57,8 %.

Raw runs: [write overhead](../../docs/benchmarks/3.1.0/write-overhead.jsonl).

## Lectures pendant les écritures

Chaque cas a été exécuté une fois pendant 30 secondes, avec 64 lecteurs sur 60k lignes ; les écrivains utilisaient synchronous_commit=off. Valkey utilisait cache-aside (écriture SQL puis DEL) avec io-threads=8. « Périmées » désigne le contrôle sentinelle final ; n/a signifie qu’aucune entrée de cache n’est inspectable.

| Stack | Distribution des clés | Writer | Lectures/s | p99 lecture ms | Taux de hit | Écritures/s | Entrées périmées |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pg_local_cache | Uniform | Aucun | 214,508 | 0.71 | 1.000 | 0 | 0 |
| pg_local_cache | Uniform | Update 10,000/s | 195,384 | 0.86 | 0.948 | 10,000 | 0 |
| pg_local_cache | Uniform | Update 30,000/s | 161,919 | 1.04 | 0.837 | 29,999 | 0 |
| pg_local_cache | Uniform | Update unlimited | 82,640 | 1.69 | 0.432 | 111,013 | 0 |
| pg_local_cache | Zipf | Aucun | 216,297 | 0.70 | 1.000 | 0 | 0 |
| pg_local_cache | Zipf | Update 10,000/s | 198,454 | 0.86 | 0.964 | 10,000 | 0 |
| pg_local_cache | Zipf | Update 30,000/s | 168,875 | 1.01 | 0.923 | 30,000 | 0 |
| pg_local_cache | Zipf | Update unlimited | 96,297 | 1.46 | 0.834 | 113,505 | 0 |
| Valkey cache-aside | Uniform | Aucun | 223,537 | 0.61 | 1.000 | 0 | 0 |
| Valkey cache-aside | Uniform | Update 10,000/s | 156,983 | 1.92 | 0.936 | 10,000 | 0 |
| Valkey cache-aside | Uniform | Update 30,000/s | 42,294 | 7.14 | 0.577 | 29,998 | 0 |
| Valkey cache-aside | Uniform | Update unlimited | 29,692 | 8.78 | 0.444 | 38,481 | 1 |
| Valkey cache-aside | Zipf | Aucun | 222,575 | 0.63 | 0.999 | 0 | 0 |
| Valkey cache-aside | Zipf | Update 10,000/s | 168,911 | 1.65 | 0.960 | 10,000 | 0 |
| Valkey cache-aside | Zipf | Update 30,000/s | 75,642 | 4.78 | 0.887 | 29,999 | 0 |
| Valkey cache-aside | Zipf | Update unlimited | 48,993 | 6.88 | 0.852 | 40,735 | 0 |
| Prepared SQL | Uniform | Aucun | 129,714 | 0.97 | — | 0 | n/a |
| Prepared SQL | Uniform | Update 10,000/s | 121,567 | 1.13 | — | 10,000 | n/a |
| Prepared SQL | Uniform | Update 30,000/s | 106,949 | 1.33 | — | 30,000 | n/a |
| Prepared SQL | Uniform | Update unlimited | 77,574 | 2.02 | — | 93,955 | n/a |
| Prepared SQL | Zipf | Aucun | 129,937 | 0.97 | — | 0 | n/a |
| Prepared SQL | Zipf | Update 10,000/s | 122,182 | 1.13 | — | 10,000 | n/a |
| Prepared SQL | Zipf | Update 30,000/s | 107,852 | 1.33 | — | 29,999 | n/a |
| Prepared SQL | Zipf | Update unlimited | 78,601 | 2.08 | — | 95,199 | n/a |
| pg_local_cache | Uniform | Insert unlimited | 117,531 | 1.20 | 1.000 | 114,066 | 0 |
| Valkey cache-aside | Uniform | Insert unlimited | 70,302 | 4.92 | 1.000 | 46,622 | 0 |
| Prepared SQL | Uniform | Insert unlimited | 84,294 | 1.98 | — | 95,094 | n/a |

pg_local_cache a terminé chaque essai mixte listé sans entrée périmée. Valkey n’a laissé qu’une entrée sentinelle périmée dans un seul cas UPDATE : clés Uniform avec un taux de mise à jour illimité. À 30k UPDATE/s avec des clés Uniform, les débits étaient de 161 919/s pour pg_local_cache, 42 294/s pour Valkey cache-aside et 106 949/s pour SQL préparé.

Raw runs: [mixed reads and writes](../../docs/benchmarks/3.1.0/mixed.jsonl).

## Sondes de cohérence de 120 secondes

Chaque sonde a été exécutée une fois avec 64 lecteurs et 64 écrivains UPDATE sans limite, puis une vérification complète des entrées périmées. Valkey utilisait io-threads=8.

| Stack | Distribution des clés | Lectures/s | p99 lecture ms | Écritures/s | Entrées périmées | Note |
| --- | --- | --- | --- | --- | --- | --- |
| pg_local_cache | Uniform | 82,565 | 1.687552 | 108,580 | 0 | — |
| pg_local_cache | Zipf | 0 | n/a | 101,797 | 0 | Voir la note ci-dessous. |
| Valkey cache-aside | Uniform | 30,210 | 8.781824 | 36,508 | 2 | — |
| Valkey cache-aside | Zipf | 28,568 | 8.781824 | 35,789 | 1 | — |

Le lecteur Zipf de pg_local_cache a échoué au contrôle d’équivalence au démarrage pendant les écritures concurrentes (problème du harness) et a indiqué zéro lecture. Le contrôle des entrées périmées a tout de même été exécuté et en a trouvé zéro.

Raw runs: [120-second stale probes](../../docs/benchmarks/3.1.0/stale-probes-120s.jsonl).

## Soak d’une heure

Essai unique : MGET 16 sur 100k clés aléatoires, 64 clients, plus 20 000 UPDATE/s. RssAnon des workers RESP était échantillonné chaque minute.

- Lectures : 251 987 814 MGET en 3 600 secondes (69 997/s), p99 3,57 ms.
- Écritures : 71 999 993 UPDATE (20 000/s), p99 1,13 ms, aucune erreur.
- RssAnon des workers est resté stable : croissance maximale de 44 kB par worker sur 60 échantillons.

Raw runs: [one-hour soak](../../docs/benchmarks/3.1.0/soak-1h.jsonl).

## Environnement et méthode

Selon lscpu, chaque VM avait 16 vCPU en 2 sockets × 4 cœurs par socket × 2 threads par cœur (topologie visible de la VM), 62 GB et Debian 13 avec le noyau 6.12.111+deb13-amd64. La VM base exécutait PostgreSQL 16.15 (max_connections=1000, shared_buffers=1048576) et pg_local_cache 3.1.0 c431bcc (activé, memory_budget_mb=1024, workers=8). La table contenait 100 000 lignes JSON de 166 octets en moyenne. Valkey 8.1.1 utilisait io-threads=8 pendant les essais d’écriture, mixtes et de sondes de cohérence. De la VM de charge vers la VM base, iperf3 a mesuré 10,4 Gbit/s et ping a indiqué un RTT min/moy/max de 0,090/0,143/0,871 ms. Le client Go/pgx tournait sur la seconde VM. Les lectures duraient 20 secondes ; écritures 15 secondes, essais mixtes 30 secondes, sondes 120 secondes et soak 3 600 secondes.

La matrice épinglée utilisait 256 clients ; chaque cas a eu cinq exécutions sauf les deux cas épinglés à une paire avec Valkey io-threads=1, qui en ont eu deux. La matrice complète et les essais d’écriture en ont eu trois ; les essais mixtes, sondes et soak, une seule. Les interruptions NIC n’étaient pas épinglées. Tous les tableaux utilisent le build final c431bcc. Le débit est en requêtes/s sauf mention tx/s ; la latence est p99 et le CPU en vCPU serveur.

Environnement et JSONL bruts : [environnement](../../docs/benchmarks/3.1.0/env.txt), [preuve de l’environnement](../../docs/benchmarks/3.1.0/stand.txt), [lectures épinglées](../../docs/benchmarks/3.1.0/read-per-core.jsonl), [lectures épinglées Valkey io-threads=1](../../docs/benchmarks/3.1.0/read-per-core-valkey-io-threads-1.jsonl), [lectures tous cœurs](../../docs/benchmarks/3.1.0/read-8-cores.jsonl), [tous cœurs Valkey io-threads=1](../../docs/benchmarks/3.1.0/read-8-cores-valkey-io-threads-1.jsonl), [surcoût des écritures](../../docs/benchmarks/3.1.0/write-overhead.jsonl), [lectures pendant les écritures](../../docs/benchmarks/3.1.0/mixed.jsonl), [sondes 120 s](../../docs/benchmarks/3.1.0/stale-probes-120s.jsonl) et [soak d’une heure](../../docs/benchmarks/3.1.0/soak-1h.jsonl).

## Reproduire avec bench/

Il faut Python 3 et des alias SSH vers les VM base de données et charge. La VM base nécessite PostgreSQL 16, Valkey, mpstat, sar, ip et systemd ; la VM de charge doit avoir le client Go compilé dans /root/bench-client. Voir [bench/README.md](https://github.com/profundium/pg_local_cache/blob/c431bcc4f69d4ecd28f1695548e121e55dfdd2fa/bench/README.md) et le [client Go/pgx](https://github.com/profundium/pg_local_cache/tree/c431bcc4f69d4ecd28f1695548e121e55dfdd2fa/examples/go-pgx/).

Matrice de lecture par défaut : 20 s, trois répétitions, 1/16/64 clés, 16/64/256 clients, ensemble chaud et espace de 100k clés :

    python3 bench/run_bench.py reads.jsonl

Exemple avec une paire de vCPU épinglée, 256 clients et cinq répétitions ; remplacez 0-1 par les ID CPU logiques d’une paire de vCPU :

    PGLC_SERVER_CPUS=0-1 REPEATS=5 BATCHES=1 CLIENTS=256 KEY_SPACES=0,100000 python3 bench/run_bench.py pinned.jsonl

Le runner applique AllowedCPUs à PostgreSQL et Valkey puis retire la limite. Il enregistre CPU serveur, latence, RX/TX réseau et CPU client. Configurez io-threads de Valkey avant l’essai.

Lancer les tests d’écriture et la charge mixte de 30 s :

    REPEATS=3 WRITE_SECONDS=15 WRITE_CLIENTS=32,64 python3 bench/write_bench.py writes.jsonl
    REPEATS=1 MIXED_SECONDS=30 MIXED_RATES=none,10000,30000,unlimited MIXED_KEY_DISTS=uniform,zipf python3 bench/write_bench.py mixed.jsonl

Les scripts ajoutent des lignes JSONL et conservent les échantillons terminés en cas d’erreur. Le test d’écriture mesure le CPU base par transaction ; le test mixte enregistre aussi latence, hits/misses, débit des écritures et vérification finale des sentinelles.

## Résultats historiques 2.x sur MacBook

Ces anciens JSON sont des mesures historiques sur Apple M3 Max avec PostgreSQL 16 ; environnement et topologie client diffèrent des deux VM ci-dessus. Ils incluent l’API SQL mget de 2.x, supprimée en 3.0.0. Voir la [documentation 2.0.4](https://github.com/profundium/pg_local_cache/tree/v2.0.4/docs) pour cette API ; les lectures en cache actuelles utilisent RESP MGET.

- [Mesures Go du 14 septembre 2026](../../docs/benchmarks/2026-09-14-m3-max.json)
- [Mesures client du 14 septembre 2026](../../docs/benchmarks/2026-09-14-m3-max-clients.json)
- [Mesures Go/RESP du 15 septembre 2026](../../docs/benchmarks/2026-09-15-m3-max-resp.json)
