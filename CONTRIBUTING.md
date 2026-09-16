# Maintaining the docs and examples

Documentation and executable examples target the 2.0 SQL mget API.

## Checks before merging

Pull-request CI follows the changed files. Extension changes run source tests, sanitizers,
Docker integration and PostgreSQL compilation. Documentation changes run the
Pages checks; executable-example changes also run PostgreSQL 14–18 smoke tests.
Package inputs retain their separate archive validation. Manual workflow
dispatch remains available. The examples matrix is not repeated on a push to
`master`; direct pushes that bypass PR checks need a manual examples run.

The examples execute shell blocks from QUICKSTART.md and SQL/configuration
from INSTALL_EXISTING.md on fresh databases, including a non-default port.
The default workload benchmark, common SQL/RESP launcher smoke and Node unit
tests run once, on PostgreSQL 16;
other versions run the functional examples. Throughput is never a pass/fail
threshold. Benchmark records and browser screenshots/traces are retained for
14 days as `demo-benchmark-pg16` and `browser` artifacts.

Quick local checks:

```bash
make verify-static source-test source-sanitize
npm --prefix examples/node-postgres ci --ignore-scripts
node --test examples/node-postgres/queries.test.mjs
```

Run the documented database examples locally (Docker and Node.js 20+):

```bash
docker compose -f examples/compose.yaml up --build --wait
python3 tests/install_docs_smoke.py
python3 tests/quickstart_docs_smoke.py
docker compose -f examples/compose.yaml down
```

For PostgreSQL 14, 15, 17 or 18, export `PGLC_DEMO_PG` before starting Compose.
Use `--skip-benchmark` on the quickstart check when only testing functionality.
Always run `compose down` after a failed check too; the demo data is disposable.

After building the site with GitHub Pages' Jekyll builder, check the output:

```bash
python3 scripts/check_site.py _site
python3 -m venv /tmp/pglc-browser-tests
/tmp/pglc-browser-tests/bin/pip install -r tests/browser/requirements.txt
/tmp/pglc-browser-tests/bin/python -m playwright install --with-deps chromium webkit
/tmp/pglc-browser-tests/bin/python tests/browser/site_smoke.py _site
```

The browser suite covers all pages with Chromium and WebKit at 1440, 390 and
320 pixels, plus a mobile case without JavaScript. It checks metadata, HTTP and
console errors, overflow, navigation, table of contents, FAQ and clipboard.

## Publishing

Pages builds once, validates that output, then publishes the same artifact
on upstream `master`. Pull requests never deploy. The workflow checks the
published homepage's HTTP response; it does not repeat the browser suite.
Forks can validate pull requests but do not publish a preview automatically.

Enable **Settings → Pages → Source: GitHub Actions** before deploying.
If deployment fails after validation, rerun the failed job while its Pages
artifact exists. After its 14-day retention period, rerun the workflow.

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

Search Console and Bing verification tokens go in `google_site_verification`
and `bing_site_verification` in `_config.yml`. Empty values emit no tags.
Sitemap: `https://profundium.github.io/pg_local_cache/sitemap.xml`.

Crawlers use the host-level `https://profundium.github.io/robots.txt`, managed
in the organization site repository.

The public site uses Google Analytics (`G-MHQBYKWZ7W`); browser tests stub its
loader so local/CI visits are not recorded. The extension has no telemetry.
