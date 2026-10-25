# gacha-assets

Canonical asset metadata and a validation-first image pipeline for **Character Counter**, **Zenlentering**, and **GenEntering**. Each consumer can retain its existing storage and runtime architecture.

## Versioned game data

The existing image pipeline is extended with a separate JSON pipeline, **Game
Data Contract v1**, a Nanoka ZZZ provider, validated raw/image caches, incremental
diffs, Supabase/PostgreSQL and MongoDB revision publishers, and coordinated release
activation. Existing image manifests and CLI commands remain supported.

```powershell
python -m pip install -e ".[mongo,test]"
gacha-assets data version
gacha-assets data ingest --dry-run --report reports/zzz-preflight.json
gacha-assets data ingest --report reports/zzz-ingest.json
gacha-assets data diff
gacha-assets data sync --dry-run
```

Remote writes require explicit `--confirm`. Numeric-to-consumer mappings start
unresolved until verified; source API versions and image filenames are never
guessed. [Complete setup, mapping, publishing, verification, recovery and consumer
integration guide](docs/game-data.md). The independently deployable database
migration is [migrations/001_game_data.sql](migrations/001_game_data.sql).

## Overview

The pipeline follows these stages:

**Discover → Resolve → Download → Validate → Decode → Normalize → Hash → Diff → Publish**

An asset must never be published or marked `ready` unless all of the following conditions are met:

- The image bytes are non-empty.
- Pillow successfully verifies and fully decodes the image.
- Image dimensions are within the configured limits.
- Image normalization succeeds.

File extensions and MIME types are advisory; they do not establish image validity.

A rejected item aborts the batch. The manifest is replaced only after the batch succeeds, preventing a failed run from leaving a partially updated manifest.

## Quick start

Install the Python package in your environment, then run:

```powershell
gacha-assets run --input discovery.json --manifest manifests/genshin.json --public-root cache
```

The command reads or downloads the supplied source bytes, validates and normalizes every image, calculates its hash, writes the verified image bytes locally, and updates the manifest.

The output manifest follows **Asset Contract v1**.

### Manifest and storage guarantees

- Partial discovery batches merge records by `assetId` and preserve assets that were not discovered in the current batch.
- Conflicting hashes for a shared storage path abort the batch.
- Legacy JSON-list manifests are accepted and upgraded to Asset Contract v1.
- Verified image bytes are stored in the Git-ignored `cache/` directory, using each contract record's relative `path`, such as `cache/assets/games/...`.

Use `--public-root cache` when ingesting assets and `--assets-root cache` when publishing or synchronizing a consumer.

This keeps the canonical image source independent of each consumer's runtime bundle. A web consumer can remove duplicated local images only after remote GET, hash, and image-decode coverage reaches 100% and the remote manifest has been verified.

### Supported discovery adapters

The pipeline provides adapters for:

- GitHub Trees
- Cloudinary Admin API
- Explicitly configured Enka JSON catalogs

No undocumented Enka endpoint is assumed.

## Character Counter regression checks

Run the historical regression checks against discovered source objects:

```powershell
gacha-assets check-cc-regressions --candidates discovered-objects.json --output reports/character-counter-regressions.json
```

The command tests four historical Character Counter failures against candidates matched by exact `assetId`.

The resolver never synthesizes URLs. Each candidate must pass all of these checks:

1. HTTPS HEAD with an `image/*` content type.
2. GET with an `image/*` content type.
3. Pillow image decoding.
4. Image normalization.

Missing candidates are reported as `unresolved`. The reason `no_source_asset` is used only after a discovered catalog has been supplied.

## GenEntering material discovery and import

### 1. Discover upstream candidates

The `discover-gen-materials` command identifies missing or invalid local mappings and checks actual upstream icon variants through the documented genshin.dev API.

In PowerShell, set the path to your local GenEntering project first:

```powershell
$GenEnteringRoot = "C:\path\to\GenEntering"
```

Replace the example path with your actual project directory.

Run discovery:

```powershell
gacha-assets discover-gen-materials `
  --game-data "$GenEnteringRoot\genentering\data\game_data.json" `
  --assets-root "$GenEnteringRoot\genentering\assets" `
  --output reports/genentering-upstream-candidates.json
```

Matching uses exact keys and names after canonical normalization. The resolver does not turn guessed icon URLs into candidates.

### 2. Import and validate candidates

```powershell
gacha-assets import-gen-materials `
  --game-data "$GenEnteringRoot\genentering\data\game_data.json" `
  --assets-root "$GenEnteringRoot\genentering\assets" `
  --candidates reports/genentering-upstream-candidates.json `
  --manifest manifests/genentering-genshin-materials.json `
  --report reports/genentering-material-import.json
```

Unmatched keys are reported as `missing` with `reason: no_source_asset`. Ambiguous or invalid candidates are reported as failed and are never marked `ready`.

### 3. Discover materials through GenshinDB

For current Genshin material names that are absent from genshin.dev, `discover-gen-materials-gdb` matches exact English material names against the GenshinDB catalog and reads the source-declared `filename_icon`.

It emits Alioth image candidates. These candidates remain untrusted until `import-gen-materials` successfully downloads and decodes each image.

A catalog match alone never marks an asset `ready`.

### 4. Discover materials through Alioth inventory pages

For materials absent from the supported catalogs, `discover-gen-materials-alioth` scans Alioth inventory pages and matches normalized display names exactly.

```powershell
gacha-assets discover-gen-materials-alioth `
  --game-data "$GenEnteringRoot\genentering\data\game_data.json" `
  --output reports/genentering-alioth-candidates.json
```

The command takes image URLs from matching catalog cards. It rejects ambiguous names, unsafe hosts, and non-item icon paths. It never derives URLs from material keys.

Pass the resulting candidates to `import-gen-materials`. HEAD and GET checks, content-type validation, image decoding, normalization, and hashing remain mandatory before publication. Unmatched material keys remain `missing`.

## Consumer delivery

The delivery workflow separates canonical ingestion from remote publication and consumer-specific synchronization.

### Ingest versus publish

**Ingest** writes verified local image bytes and updates a canonical manifest.

**Publish** distributes validated objects and a verified manifest to Supabase.

A successful local ingest does not mean that every consumer has synchronized.

### Import an existing contract

`import-contract` bootstraps a canonical Asset Contract v1 manifest from an existing project contract. It rechecks the digest and image decoding of every file marked `ready`, preserves provenance, and can set the target storage adapter.

Example for Zenlentering:

```powershell
$ZenlenteringRoot = "C:\path\to\Zenlentering"

gacha-assets import-contract `
  --input "$ZenlenteringRoot\public\assets\asset-contract-v1.json" `
  --assets-root cache `
  --manifest manifests/zenlentering.json `
  --storage supabase
```

Replace the example root with the actual local project directory.

### Publish to Supabase

`publish-supabase` uploads only validated manifest records. It skips objects whose public GET hash already matches, verifies every newly uploaded object through GET, hashing, and image decoding, and publishes the remote manifest last.

Publication requires a write-authorized service key. A public key cannot bypass Supabase Storage Row Level Security (RLS).

The CLI reads the ignored project `.env` file. Process environment variables take precedence over values loaded from that file.

To configure credentials:

1. Copy `.env.example` to `.env`.
2. Set a write-authorized `SUPABASE_STORAGE_API_KEY` or `SUPABASE_SERVICE_ROLE_KEY`.
3. Keep secrets out of source control and never send them through chat.

Publish the imported Zenlentering contract:

```powershell
gacha-assets publish-supabase `
  --manifest manifests/zenlentering.json `
  --assets-root cache `
  --report reports/zenlentering-publish.json
```

### Consumer-specific synchronization

Each consumer has its own final synchronization step:

| Consumer | Synchronization strategy |
|---|---|
| **GenEntering** | `import-genentering` merges its required local bundle with the exact-key material manifest. Package synchronization then copies only the paths used by the offline app. |
| **Character Counter** | Reads per-game canonical manifests and reconciles MongoDB inventory against Supabase objects. |
| **Zenlentering** | Loads `manifests/zenlentering.json`, a cross-game manifest assembled from the four per-game manifests. |

After remote coverage and manifest verification succeed, public asset bytes can be removed when they are no longer required locally. The local contract remains available as a metadata fallback, and the application resolves images from Supabase at runtime.

Manifest uploads use a short cache lifetime so consumers can receive inventory updates promptly.

## Merging manifests

### Build a per-game manifest from consumer contracts

Use `merge-contracts` to assemble a per-game master manifest from consumer contracts:

```powershell
gacha-assets merge-contracts `
  --input manifests/zenlentering.json `
  --input manifests/genentering.json `
  --game genshin `
  --manifest manifests/genshin.json
```

When duplicate `assetId` values are encountered, their content hashes must agree. The resulting record prefers:

1. A `ready` asset with a verified hash.
2. The storage adapter priority: `supabase`, `public`, then `bundled`.

### Build a cross-game consumer manifest

Use `merge-manifests` to combine the four per-game manifests into a single Zenlentering manifest:

```powershell
gacha-assets merge-manifests `
  --input manifests/genshin.json `
  --input manifests/hsr.json `
  --input manifests/zzz.json `
  --input manifests/wuwa.json `
  --manifest manifests/zenlentering.json
```

Shared paths may retain multiple canonical IDs only when their content hashes agree.

Conflicting identities or hashes abort the merge before the output manifest is replaced.

## GenEntering asset identity and variants

When importing GenEntering, its local logical character manifest is authoritative for entity identity and variants.

The importer applies the following rules:

- CamelCase keys are canonicalized. For example, `HuTao` becomes `hu-tao`.
- The consumer's `portrait` key maps to the shared `icon` variant.
- Distinct `artwork` records remain separate.
- Reused image paths retain every logical `assetId`, while the underlying image bytes are staged only once.

These rules preserve logical asset identity without duplicating identical physical files.

## Full cross-game release

The script `scripts/publish-all-games.ps1` runs the publisher gate for `genshin`, `hsr`, `zzz`, and `wuwa`, then publishes the merged `manifests/zenlentering.json` view last.

It supports three modes:

| Mode | Behavior |
|---|---|
| `dry-run` | Validates every cache file without performing network writes. |
| `verify-only` | Checks every remote object and all five manifests. |
| `publish` | Uploads missing objects and requires every object and manifest to verify before reporting success. |

The script reads the ignored local `.env` file through the CLI. A server-side key may be provided through `SUPABASE_STORAGE_API_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, or `SUPABASE_SECRET_KEY`.

