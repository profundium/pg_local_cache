# Maintaining the docs and examples

Public documentation describes the 2.0 SQL mget contract. Keep historical
benchmarks and migration narratives out of these pages.

## Checks before merging

Every pull request runs the source tests, package validation, documentation
examples, and Pages artifact checks. Check the latest commit, not an older
successful run. No benchmark throughput threshold is used as a correctness test.

The examples workflow runs PostgreSQL 14–18 on fresh Linux amd64 runners. It
executes shell blocks taken directly from QUICKSTART.md, including the default
benchmark, and SQL/configuration extracted from INSTALL_EXISTING.md. The latter
uses a separate container without demo initialization or external networking.
The non-default demo port is tested too. Node dependencies come from the
committed lockfile via `npm ci --ignore-scripts`.

The Pages workflow builds production and fork-preview configurations. Preview
pages are marked `noindex`; production pages must remain indexable. It packages
the site with upload-pages-artifact, downloads that archive, unpacks it, checks
its bytes, and serves it under `/pg_local_cache/` for Playwright. Chromium and
WebKit each test all pages at 1440, 390 and 320 pixels, plus a JavaScript-disabled
mobile case. Checks cover metadata, HTTP errors, console errors, horizontal
overflow, the mobile menu, table of contents, FAQ, and clipboard writes.

Artifacts are kept for 14 days: `pages-production`, `pages-preview`,
`browser-production`, `browser-preview`, and `demo-benchmark-pg14` through
`demo-benchmark-pg18`. Browser artifacts contain screenshots, traces, and a
machine-readable result with the tested source commit. For a PR this is GitHub's
temporary merge commit, not just its head. Benchmark records identify the
extension and harness revisions separately.

For a local check after downloading and unpacking a Pages artifact:

```bash
python3 -m pip install -r tests/browser/requirements.txt
python3 -m playwright install --with-deps chromium webkit
python3 tests/browser/site_smoke.py /path/to/unpacked/site
```

Quick tests do not need Docker or browsers:

```bash
make verify-static source-test
node --test examples/node-postgres/queries.test.mjs
```

## Publishing and checking the live site

Only a push to upstream `master`, or the explicit review branch in the testing
fork, can publish. Pull requests have read-only permissions and never deploy.
Publication waits for the artifact's browser checks and uploads no rebuilt copy.
After deployment, every HTTP resource is checked against the artifact's SHA-256
manifest and the browser suite runs against the live URL. A stale deployment,
missing asset, or failed interaction fails the workflow rather than reporting
that a successful build was a successful publication.

GitHub Pages must already be enabled with **Settings → Pages → Source: GitHub
Actions**. The workflow token can deploy but cannot grant itself the
administration permission needed to enable Pages. A disabled site fails at
`Check Pages configuration`; it is not skipped or treated as a pass. After the
owner enables Pages, rerun the failed job while its artifact still exists. If
artifacts have expired, rerun all jobs.

Before merging upstream, require successful CI, package, examples and Pages
artifact checks on the current commit. To claim a completed preview deployment,
also require the non-PR `Publish verified Pages artifact` job to pass, including
its live HTTP and browser steps. Browser engines on Linux do not replace tests
on physical iOS devices. These tests do not exercise a production restart under
systemd or Patroni, or guarantee search ranking.

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
