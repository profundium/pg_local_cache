# Maintaining the docs and examples

Public documentation describes the 2.0 SQL mget contract. Keep historical
benchmarks and migration narratives out of these pages.

Run `make verify-static source-test`, plus:

```bash
node --test examples/node-postgres/queries.test.mjs
python3 scripts/check_site.py _site
```

The second command needs a built Jekyll site. The Pages workflow runs it before
deployment. The Documentation examples workflow builds the pinned demo, checks
concurrent transactions, and archives benchmark JSON and the dependency lockfile.
Shared-runner timings are not a reference performance claim.

## Publishing a result

Keep the extension revision separate from the benchmark harness revision.
Record the environment, exact commands, all repetitions, cache counters, and
client-side processing. Do not average p99 values or reuse measurements from a
different API. Publish the raw results alongside any table on the site.

## Search indexing

The project site is `https://profundium.github.io/pg_local_cache/`. Its sitemap
is generated from the public pages; adding a document does not require a
second URL list. Set `last_modified_at` only after a substantive content edit.
Do not replace it with the build date.

A property owner must verify that URL-prefix property in Google Search Console.
Add the real HTML verification token to `google_site_verification` in
`_config.yml`, deploy, then submit
`https://profundium.github.io/pg_local_cache/sitemap.xml`. Inspect the home and
guide URLs and review non-brand impressions and clicks. An empty token emits
no tag. `bing_site_verification` works the same way for Bing Webmaster Tools.
A PR cannot verify ownership or submit a sitemap from an unconnected account.

The project's `/pg_local_cache/robots.txt` does not control the host. A crawler
uses `https://profundium.github.io/robots.txt`; manage that in the organization
site repository if needed. The absence of a robots file does not disallow
crawling. See Google's [robots guidance](https://developers.google.com/search/docs/crawling-indexing/robots/create-robots-txt)
and [sitemap guidance](https://developers.google.com/search/docs/crawling-indexing/sitemaps/build-sitemap).

Repository description and topics are settings, not files changed by merging
a PR. A repository administrator can apply:

```bash
gh repo edit profundium/pg_local_cache \
  --description 'PostgreSQL extension for shared-memory primary-key row caching with explicit SQL mget and transaction-aware invalidation.' \
  --homepage https://profundium.github.io/pg_local_cache/ \
  --add-topic postgresql --add-topic postgresql-extension \
  --add-topic caching --add-topic cache-invalidation \
  --add-topic shared-memory --add-topic performance
```

There is no added visitor tracking or extension telemetry. Use Search Console
and GitHub traffic to see discovery; use workload reports to establish actual
trials. Downloads and copied commands are not confirmed installations.
