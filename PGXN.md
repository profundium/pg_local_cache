---
layout: doc
title: PGXN packaging and publishing
section: PGXN
permalink: /docs/PGXN.html
---

# PGXN packaging, automatic versions, and publishing

`pg_local_cache` is packaged as a regular PGXS extension. The repository ships
PGXN 1.0 metadata, a deterministic source bundle, automatic semantic
versioning, and an authenticated release workflow.

After the one-time PGXN credentials are configured, merging a release-worthy
change to `master` is enough. The repository prepares the next version, runs the
full CI and binary release pipelines, and uploads the exact stable ZIP to PGXN.
No per-release PGXN Manager upload is required.

## One-time repository setup

1. Create and approve the maintainer account in PGXN Manager.
2. Add these GitHub Actions repository secrets:

   - `PGXN_USERNAME`
   - `PGXN_PASSWORD`

3. Allow GitHub Actions `contents: write` and `actions: write`. If `master` has a
   branch protection rule, allow the repository Actions identity to create the
   generated release commit, or replace the checkout credential with a narrowly
   scoped repository token that has that permission.

The workflow writes the credentials to a mode-`0600` temporary `.netrc` on
the ephemeral runner and sends the ZIP to PGXN Manager's authenticated
`POST /upload` endpoint as multipart field `archive`. The credentials are not
written to the distribution, GitHub release, or repository. GitHub masks
repository-secret values in workflow output.

## Automatic release sequence

A human push or merged pull request to `master` starts
`.github/workflows/auto-version.yml`.

1. `scripts/auto_version.py` finds the highest reachable stable `vX.Y.Z` tag.
2. It classifies all commits since that tag.
3. When a release is required, it updates every version-bearing source file and
   creates the PostgreSQL install and upgrade SQL for the new version.
4. The workflow commits the result as
   `chore(release): prepare vX.Y.Z [skip version]`.
5. Because GitHub does not recursively trigger workflows for a push made with
   `GITHUB_TOKEN`, the workflow explicitly dispatches `ci.yml` for the new
   version-bearing `master` commit.
6. The existing release workflow builds and verifies PostgreSQL 14–18 artifacts
   and creates immutable GitHub releases.
7. The `PGXN package` workflow rebuilds the ZIP from the exact stable release
   SHA, verifies that `vX.Y.Z` points to that SHA, and uploads it directly to
   PGXN Manager over HTTPS.

A rerun is idempotent. Before and after upload, the workflow downloads the
published PGXN archive and byte-compares it with the stable GitHub release
asset. An existing equal archive is accepted; different bytes fail closed.
PGXN versions are immutable and are never overwritten.

## Semantic version rules

The latest stable Git tag is the baseline. Conventional Commit messages select
the highest required bump:

| Change since the stable tag | Version bump |
|---|---|
| `type!:` or a `BREAKING CHANGE:` footer | major |
| `feat:` | minor |
| `fix:`, `perf:`, `refactor:`, `build:`, `revert:`, `security:` | patch |
| only `docs:`, `test:`, `ci:`, `chore:`, or `style:` | no stable release |
| an unclassified non-merge production commit | patch, as a fail-safe |

The generated release commit contains `[skip version]`, so it never asks for a
second bump. Re-running the planner before the stable tag is created also stays
idempotent: if the calculated version is already present, no files change.

Inspect the plan locally:

```bash
make version-next
python3 scripts/auto_version.py --json
```

Apply it locally for review:

```bash
make version-bump
```

Normal development does not need to run `version-bump`; the master workflow
owns the release commit.

## PostgreSQL upgrade SQL

Released install files are immutable. The version planner compares the current
install SQL with the same file from the latest stable tag.

For a C-only release, it creates:

- a new full install file, for example `pg_local_cache--1.2.0.sql`;
- an explicit no-op upgrade file, for example
  `pg_local_cache--1.1.0--1.2.0.sql`.

When a change modifies extension SQL objects, commit the final full install SQL
and also put only the incremental migration statements in:

```text
sql/pg_local_cache--unreleased.sql
```

The planner consumes that fragment into the generated `old--new` upgrade file,
removes the fragment, and restores the released old install file from its tag.
It fails closed if install SQL changed without an explicit migration, or if a
migration fragment exists while the install SQL is unchanged.

## Install from PGXN

This section owns the PGXN/source build step only. For a verified binary
package, start with the
[README](https://github.com/profundium/pg_local_cache#choose-an-install-path).
After PGXN copies the files, the existing-server guide owns preload settings,
restart orchestration, state-bound verification and rollback.

Requirements:

- PostgreSQL 14–18 server development files for the target installation;
- a C compiler and GNU Make;
- the PGXN client;
- permission to write to the target PostgreSQL library and extension
  directories.

Choose the same `pg_config` used by the PostgreSQL server:

```bash
pgxn install \
  --pg_config /usr/lib/postgresql/16/bin/pg_config \
  --sudo -- \
  pg_local_cache
```

`pgxn install` downloads, compiles, and copies the extension files. It does not
configure the server, restart PostgreSQL, or attach application tables.

`pg_local_cache` must be present in `shared_preload_libraries` before
`CREATE EXTENSION` because its shared-memory layout is allocated at postmaster
startup. Preserve every existing preload entry, configure the extension memory
and database settings, perform one controlled restart, and then run:

```sql
CREATE EXTENSION pg_local_cache;
SELECT local_cache.attach_table('public.items'::regclass);
```

Use the
[existing-server installation guide](https://profundium.github.io/pg_local_cache/docs/INSTALL_EXISTING.html)
for preflight, memory sizing, restart, verification, HA, and rollback. The PGXN
client replaces only the source download/build/copy part of that procedure.

Do not use `pgxn load` before the preload configuration and restart. Loading the
SQL objects without the preloaded module cannot initialize the shared cache.

## Validate and build the distribution

Validate the metadata and repository version contracts:

```bash
make pgxn-check
```

Build the upload archive without requiring `pg_config`:

```bash
make dist
```

The output is:

```text
dist/pg_local_cache-<version>.zip
```

The builder packages the exact committed revision with one
`pg_local_cache-<version>/` root, validates required metadata and source files,
rejects dirty or unsafe release inputs, and prints the archive SHA-256 digest.

## Recovery and manual verification

Automatic publishing intentionally stops before PGXN when any invariant is not
proven: missing credentials, a moving `master`, a stable tag pointing to another
commit, metadata drift, a failed CI/release job, or an existing GitHub asset
with different bytes.

The generated ZIP is retained as a GitHub Actions artifact and attached to the
immutable GitHub release. A maintainer can inspect it with:

```bash
VERSION="$(python3 scripts/validate_pgxn_meta.py --print-version)"
unzip -t "dist/pg_local_cache-${VERSION}.zip"
sha256sum "dist/pg_local_cache-${VERSION}.zip"
```

Never reuse an existing semantic version for a different commit. Fix the
release input and produce a higher version instead of attempting to replace an
immutable PGXN distribution.
