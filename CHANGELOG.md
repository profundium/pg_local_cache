# Changelog

All notable changes to pg_local_cache are documented here. This project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [3.0.0] - 2026-10-04

3.0.0 makes pg_local_cache RESP-only and ready for distribution packages. Read
[UPGRADING.md](https://github.com/profundium/pg_local_cache/blob/v3.0.0/docs/UPGRADING.md) before upgrading from 2.x.

### Added

- Native TLS and optional mutual TLS for the RESP listener
  (`pg_local_cache.tls`, `tls_cert_file`, `tls_key_file`, `tls_ca_file`,
  `tls_min_protocol_version`), with handshake metrics and `tls_enabled` in
  `health()`.
- `pg_local_cache.enabled`, a kill switch applied on reload without a restart.
- `.deb` packages (PostgreSQL 14-18, amd64/arm64) and EL9 RPMs (x86_64/aarch64)
  in releases, with a source tarball, `SHA256SUMS` and build provenance
  attestations; Debian and RPM packaging in the repository.
- pg_regress coverage (`make installcheck`), package installchecks, an upgrade
  test from 2.0.4 on a populated database, continuous fuzzing of the RESP
  parser, and a concurrent stale-read stress test over plaintext and TLS.
- `SECURITY.md`, `UPGRADING.md` and SPDX license identifiers.

### Changed

- RESP `MGET` is the read API; SQL `local_cache.mget` is removed.
- A non-loopback listener requires `tls = on` or an explicit
  `allow_plaintext_network = on`.
- `module_pathname` is the bare library name, as PostgreSQL 18
  `extension_control_path` and CloudNativePG image-volume extensions require.
  The upgrade script re-points existing functions.
- Releases are cut from `v*` tags after the full CI; versions are prepared with
  `scripts/bump-version.sh`.
- The documentation site moved under `site/`; release archives contain only
  the extension source, SQL, tests and Markdown documentation.

### Fixed

- `health()` no longer reports ready before a RESP worker accepts
  connections.
- A request split by TCP right after a length sign is no longer rejected.
- Source builds from archives without `.git` no longer fail on a missing build
  id.

### Removed

- SQL `local_cache.mget(regclass, anyarray)` and its `sql_cache_*` counters.
- The bespoke installer scripts and the glibc/musl binary tarballs.
- Duplicate historical install SQL copies and obsolete release helpers.

## [2.0.4] - 2026-09-16

### Changed

- Clarify supported workloads and align SQL, RESP, and client benchmark reporting.
- Remove editorial filler from documentation and restore analytics on docs pages.

## [2.0.3] - 2026-09-15

### Added

- Compare decoded SQL and RESP reads across Go and Node clients, including
  connection limits and server resource costs.

### Changed

- Canonicalize RESP MGET keys once per command and publish reproducible client
  benchmark results.

### Fixed

- Release retained MGET plans with function context and preserve completed
  benchmark samples after failures.

## [2.0.2] - 2026-09-13

### Added

- Add a reproducible PostgreSQL demo and task-focused adoption guides.

### Changed

- Expand and verify multilingual documentation, Pages artifacts, and benchmark
  reporting.

### Fixed

- Initialize the mapping role in the SQL-only demo and stage Pages manifests
  with their generated site output.

## [2.0.1] - 2026-08-16

### Changed

- Remove PGXN-specific documentation and improve adoption-site SEO.

### Fixed

- Quote the homepage title correctly.

## [2.0.0] - 2026-08-16

### Added

- Provide a one-command binary installation path.

### Changed

- Make explicit `mget` the only read command and document the 2.0 migration.
- Trim obsolete repository and release infrastructure.

## [1.3.0] - 2026-08-10

### Added

- Add bounded composite-key MGET support to SQL and RESP APIs.

### Fixed

- Correct release version handling.

## [1.2.1] - 2026-08-09

### Fixed

- Correct the PGXN release workflow.

## [1.2.0] - 2026-08-09

### Added

- Accelerate ordinary `SELECT ... IN` lookups and document measured behavior.
- Add package release automation and PGXN distribution support.

### Fixed

- Preserve SQL snapshots for negative cache entries and correct version bumping.

## [1.1.0] - 2026-08-07

### Added

- Add analytics and extend CI coverage to more PostgreSQL versions.

### Fixed

- Correct release dispatch and release workflow behavior.

## [1.0.0] - 2026-08-03

### Fixed

- Avoid a release-tag propagation race.
