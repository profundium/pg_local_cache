# Changelog

All notable changes to pg_local_cache are documented here. This project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.0.0] - 2026-10-04

### Added

- Add the SIGHUP `pg_local_cache.enabled` operational kill switch.
- Add an explicit opt-in for plaintext RESP listeners on non-loopback addresses.
- Add native TLS and optional mutual TLS for the RESP listener, with
  handshake metrics.
- Add an upgrade path from 2.0.4 that preserves existing trigger function OIDs.

- Debian and RPM package references and package-build CI for PGDG repositories.
- Release `.deb` and `.rpm` packages, a source tarball, `SHA256SUMS`, and build
  provenance attestations.
- pg_regress administrative coverage, package installcheck, and security policy.
- SPDX license identifiers to all C source and header files.

### Changed

- Make the extension RESP-only; RESP `MGET` replaces SQL `local_cache.mget`.
- Advance the global cache epoch when the kill switch is re-enabled.
- Require `pg_local_cache.allow_plaintext_network` for non-loopback plaintext
  listeners.
- Require TLS or explicit trusted-network opt-in for non-loopback RESP
  access.
- Move the multilingual documentation site under `site/` and keep release
  archives focused on extension source, SQL, tests, and Markdown documentation.
- Replace legacy release helpers with the tested `scripts/bump-version.sh`
  workflow and extend it to update and validate Debian changelog metadata.

### Removed

- Remove SQL `local_cache.mget(regclass, anyarray)` and its SQL-only counters.
- Remove the bespoke installer and glibc/musl binary tarballs.
- Remove duplicate historical install SQL copies, obsolete release helpers, and
  superseded contract tests; retain current install SQL and upgrade paths.

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
