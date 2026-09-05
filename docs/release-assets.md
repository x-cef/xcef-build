# Updating release assets

Each manual build requires one `source_tag` and `cef_packages`, a JSON array of
1-32 unique stable standard CEF SDK archive filenames. XCef Console provides a
release dropdown and four searchable multi-select lists: Windows-x64, macOS-arm64,
macOS-x64, and Linux-x64. One request can include different targets and versions.

Example workflow inputs:

```json
{
  "source_tag": "v1.0.0",
  "cef_packages": "[\"cef_binary_151.3.24+g2384915+chromium-151.0.7922.174_windows64.tar.bz2\",\"cef_binary_151.3.24+g2384915+chromium-151.0.7922.174_linux64.tar.bz2\"]"
}
```

The workflow derives each version, platform, and architecture from the package
name and validates the exact filename against the live stable-channel SDK index.
Beta, minimal, unsupported, duplicate, and unavailable packages are rejected before
builds start. The configured target pairs are Windows/x64, Linux/x64, macOS/arm64, and macOS/x64.
macOS/x64 uses the Intel `macos-15-intel` runner and the `macosx64` CEF SDK.
New targets must first be added to `config/builds.json` with the correct runner.

There is no mode parameter. Missing SDK assets are added automatically; existing
assets with exactly matching filenames are replaced. Other assets remain intact.
Different full CEF versions produce different XCef filenames, including different
versions within the same CEF major. Each package is published as soon as its own build and verification succeed.
A failed package does not block successful siblings.

Existing release updates require a mutable release with a valid `manifest.json`
and the same XCef source repository, tag, and version. The source commit may change:
selected assets are overwritten with the new build, while retained assets preserve
their own source SHA, build SHA, and checksum. The catalog-level source SHA identifies
the latest publisher; consumers should use each asset record for provenance. Preparation is
read-only for every run; it never creates a draft or deletes assets. Finalization validates package metadata and checksums, uploads packages,
and merges `build-plan.json`, `manifest.json`, and `SHA256SUMS`. Retained assets keep
their original source commit, build commit, and checksum. Published releases retain their notes and
publication state. Existing drafts with valid catalogs are automatically published
only after package and catalog uploads succeed. Incomplete legacy drafts without a
valid manifest must be reconciled before reuse. Replacing an SDK changes its checksum and can invalidate cached downloads.

One dispatch remains one workflow run. Each matrix row calls `build-package.yml`,
which contains an independent build job and a publish job. Publish jobs use a
per-tag concurrency group with `queue: max`, so waiting publishers are queued
instead of replacing each other. The outer workflow retains its separate per-tag
queue to prevent overlapping runs for the same release. Up to 32 packages in one
run fit within GitHub's 100-pending-job queue limit.

Inside the publish lock, finalization queries the release again and merges the
latest catalog. The first successful package creates the formal release; later
packages reuse it. Rerunning a successful publisher replaces the same asset without
duplicating catalog entries. Build and publish failures remain visible in the run.
GitHub uploads are separate operations, not an atomic transaction: interrupted
uploads can require reconciling assets and catalogs before retrying. Avoid manual
asset edits during publication. There is no draft or manual publish step.

The existing
`repository_dispatch` source-tag event may omit `cef_packages` to build the configured
matrix: CEF `151.3.24+g2384915+chromium-151.0.7922.174` across all four targets.
It may instead supply the package array to select other stable SDKs. Manual dispatch always
requires an explicit nonempty package array. Every path validates stable SDK availability.

## Package identity

Every uploaded SDK ZIP contains a UTF-8 `.package` file in its runtime directory.
Its content is the complete SDK filename without `.zip`, with no BOM or newline.
For example: `xcef-1.0.0-151.3.24+g2384915+chromium-151.0.7922.174-windows-x64`.

- Windows: `XCef/bin/Win64/.package`
- Linux (both architectures): `XCef/bin/Linux/.package`
- macOS ARM64: `XCef/bin/Mac/arm64/.package`
- macOS x64: `XCef/bin/Mac/x86_64/.package` (or `Mac/x64` for older layouts)

The build repository adds the record to the staged ZIP, including builds from older
XCef tags. Existing compressed entries and symlink metadata are preserved. SHA-256
and size are calculated after the record is added. Conflicting or duplicate records
and missing or ambiguous runtime directories are rejected before release upload.

Preparation needs `contents: write` to list draft releases, although it performs
no mutations. This matches publish visibility before compilation. Moving a source tag is allowed
for incremental updates; existing package source commits do not block replacement.
