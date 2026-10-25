# Game data synchronization

The image CLI and Asset Contract v1 remain supported. The `gacha-assets data`
command adds a separate Game Data Contract v1 and this pipeline:

**Discover version → Fetch JSON → Validate → Normalize → Cache → SHA-256 → Diff → Stage → Publish → Verify → Activate**

## Install and run

Use Python 3.11 or later from the project directory:

```powershell
python -m pip install -e ".[mongo,test]"
python -m pytest -q
gacha-assets data --help
gacha-assets data version --report reports/zzz-version.json
gacha-assets data discover --report reports/zzz-discovery.json
gacha-assets data ingest --dry-run --report reports/zzz-preflight.json
gacha-assets data ingest --report reports/zzz-ingest.json
gacha-assets data diff --report reports/zzz-diff.json
```

The most recent **complete local stage** is selected automatically. It is not an
active remote release. `--release cache/game-data/zzz/en/releases/<syncId>/release.json`
pins a particular stage for publish, verify, retry or rollback.

`--dry-run` prevents remote database connections, uploads and activation. Ingest
still performs public source GETs and populates validated raw/image caches, but
does not create a release or change the staged pointer. `discover` and `version`
are source reads. Run the mock test suite to test completely offline.

## Source and cache

`config/sources/zzz-data.json` configures the HTTPS base, locale, index/detail
types, timeout, retry attempts and maximum concurrency (1–16, default 4). The
Nanoka adapter implements `zzz-server-tool-main/src/lib/api/zzz-api.ts` and its
agents, weapons, discs, disc sets and endgame loaders:

- Read `manifest.json` → `zzz.latest` on every ingest; no version is hardcoded.
- Read `zzz/{version}/{character,weapon,equipment,shiyu,boss,simul,monster}.json`.
- Read every required `zzz/{version}/{locale}/{type}/{id}.json` detail, except
  monster, whose index is the available source in the reference implementation.
- Extract source-declared `icon`, `image` and `monster_icon` fields, including
  nested endgame rooms. Use the reference's filename conversion to
  `assets/zzz/{filename}.webp`; never derive a filename from a name or numeric ID.
- GET and decode images with Pillow, normalize lossless WebP and hash the bytes.

Raw JSON cache identity includes source host, game, source version, locale and
endpoint. Cached JSON has its own SHA-256 and is schema/shape checked before use.
Versioned endpoints are treated as immutable by default; `--refresh` conditionally
revalidates them using ETag/Last-Modified. The manifest always uses conditional
GET. Sources without validators return a full response on refresh. Images use a
version/locale cache and ETag where available. Timeout, 429 and 5xx use bounded
exponential backoff; 429 honors Retry-After up to a 30-second wait. A 404 remains
distinct from a transport error and does not trigger retries.

No incomplete required index/detail batch becomes a stage. A mapped required
image failure also blocks staging. An image with no proven asset mapping is
downloaded and validated in cache, but remains unresolved and does not create an
Asset Contract record. A missing optional/unmapped image is reported separately.
Such unresolved references do not block metadata publication; consumers must
inspect their status. Empty index responses are rejected, avoiding accidental
mass deletion. Missing entities are reported as `missingCandidates` and retained
with their existing asset dependencies. There is no automatic deletion command.
Repeated source image URLs are fetched only once per ingest, including refresh.

## Stable identities and mappings

An entity key is `zzz.{sourceType}.source-{sourceId}`. Display names never form a
primary key. A source ID retains its original scalar type: a numeric detail `id`
stays numeric, while a JSON index object key stays a string when no typed ID is
available. Each entity stores source version/locale, names, attributes, image
references, schema version, synchronization time and content hash. Original
index/detail JSON is retained in attributes and in the independent raw cache.

Hashes use sorted-key, compact UTF-8 JSON and exclude `contentHash` and
`syncedAt`. Source version and asset references are part of the semantic content.
The source version changing therefore creates a new revision even if gameplay
fields match. Object key ordering and repeated observation timestamps alone do
not create changes.

`config/games/zzz-mapping.json` intentionally starts empty: the supplied image
manifest has slug/file identities, without proven Nanoka numeric IDs. Do not
fill it by guessing display-name similarities. After checking the actual source
and the existing consumer's identity, add explicit mappings such as:

```json
{
  "schemaVersion": 1,
  "entities": {"character:SOURCE_ID": "existing-consumer-slug"},
  "images": {"character:SOURCE_ID:detail.some.icon": "zzz.character.existing-consumer-slug.icon"}
}
```

`SOURCE_ID` and `existing-consumer-slug` are placeholders, not verified game
values. The entity mapping sets `attributes.consumerId`. It also maps the
source's `index.icon` to the corresponding existing `.icon` asset. Equipment maps
to Asset Contract `artifact`. Additional nested image fields require explicit
`images` entries. Image overrides must name existing asset IDs. New entity icon
IDs can be created through a verified entity mapping. Ambiguous mappings fail.

An unchanged image retains its asset ID, old path, hash and provenance. Changed
bytes retain the canonical asset ID and get a content-addressed image path,
leaving older release paths intact. Other existing variants are retained.
All mapped images pass through the existing `pipeline.run`, resolver, Pillow
normalizer and asset manifest validation. Existing `manifests/*.json` are never
rewritten by `data` commands.

## Environment and database setup

The original `.env` loader is reused; process environment values take precedence.
Never commit populated `.env` files. See `.env.example` for variable names.

| Variable | Purpose |
| --- | --- |
| `SUPABASE_URL` | HTTPS project URL for Storage and PostgREST |
| `SUPABASE_BUCKET` | Existing public Storage bucket; default `GachaCounter` |
| `SUPABASE_STORAGE_API_KEY` | Existing privileged Supabase key variable |
| `SUPABASE_SERVICE_ROLE_KEY` | Supported alternative |
| `SUPABASE_SECRET_KEY` | Supported alternative for opaque `sb_secret_*` keys |
| `MONGODB_URI` | MongoDB URI; never printed in CLI reports |
| `MONGODB_DATABASE` | Explicit destination database, required for MongoDB |
| `MONGODB_COLLECTION` | Explicit dedicated entity-revision collection |
| `MONGODB_RELEASE_COLLECTION` | Dedicated snapshot descriptors; defaults to `<collection>_releases` |

The Supabase key must have permission for **both** the existing bucket and the
new database tables/RPCs. A bucket-scoped key alone is insufficient. Opaque
Supabase keys use the `apikey` header rather than a JWT Bearer header.

Review and apply `migrations/001_game_data.sql` independently through your normal
database deployment process. No CLI command applies migrations or changes bucket
configuration. The migration creates:

- `game_data_entities`: immutable JSON entity revisions, keyed by
  `(game, locale, canonical_id, content_hash)`.
- `game_data_releases`: immutable snapshot descriptors, keyed by `sync_id`.
- `game_data_active`: the authoritative pointer per `(game, locale)`.
- `game_data_sync_runs`: the latest execution report for a retryable release.

RLS exposes only active metadata to `anon`/`authenticated`. Write permissions and
activation/report RPCs are restricted to `service_role`. Storage follows your
existing bucket policies: staged objects in a public bucket can be accessed by
their exact immutable URL before activation, but are never advertised as active.
Do not treat object existence as release activation.

MongoDB uses dedicated collections, not a guessed Character Counter schema. Each
revision has deterministic `_id = SHA256([canonicalId, contentHash])`, plus a
unique compound index on game, locale, canonical ID and hash. Multiple historic
revisions are intentional; one release points to exactly one revision per entity.
Bulk `$setOnInsert` upserts are idempotent even after an ambiguous timeout after
server commit. Writes use majority write concern; every batch and descriptor is
read back. No binary image data is stored in MongoDB. MongoDB document size limits
still apply; oversized source entities require a future chunking adapter.

## Publish and activate

These commands **default to dry-run** unless `--confirm` is supplied:

```powershell
gacha-assets data publish-supabase --dry-run --report reports/supabase-plan.json
gacha-assets data publish-mongodb --dry-run --report reports/mongo-plan.json
gacha-assets data sync --dry-run --report reports/zzz-sync-plan.json
```

After you have reviewed mappings, unresolved records, migration and destinations,
explicitly authorize writes for the desired invocation:

```powershell
gacha-assets data publish-supabase --confirm --report reports/supabase-stage.json
gacha-assets data publish-mongodb --confirm --report reports/mongo-stage.json
gacha-assets data verify --target both --report reports/zzz-verify.json
gacha-assets data sync --release cache/game-data/zzz/en/releases/SYNC_ID/release.json --confirm --report reports/zzz-sync.json
```

Replace `SYNC_ID` with the exact ID from ingest. `data sync --confirm` without
`--release` first ingests the latest complete source and then publishes it.
Individual publish commands stage and verify only their own destination; they
never activate a release. `verify`/`--verify-only` use remote reads only and cannot
activate or create indexes. Dry-run takes precedence over confirmation/verify.

Supabase reuses unchanged Storage objects, uploads verified image bytes and
entity JSON, batch upserts metadata, then verifies actual remote contents.
The release descriptor is uploaded last. Existing variants with no local file
must already exist remotely and pass GET/hash/Pillow checks; otherwise publish
fails. Remote object conflicts are rejected, never overwritten with other bytes.

## Release consistency and recovery

A content-derived sync ID identifies the immutable snapshot across both systems.
Repeated ingestion of identical source data yields the same ID. Every execution
report includes counts, times, target statuses and overall state. Retries reuse
the same release and verify both targets again; successful immutable revisions
are reused and failed/missing operations can be retried safely.

Activation requires **both** publishers to report verified. The PostgreSQL RPC
atomically compares the active pointer captured before staging, then commits the
new pointer. A competing release causes a conflict and must be reverified; there
is no cross-database transaction. Consumers must use this **single Supabase
authority** to decide which MongoDB snapshot is active. There is deliberately no
second independently updated active pointer in MongoDB or Storage.

If a destination fails, the status is `failed`, the command exits 2, and activation
is skipped. Earlier snapshots remain intact. Rerun the exact `--release` command
after fixing connectivity/permissions; do not delete successful revisions.
If activation's response/readback is lost, status is `activation-unknown` and
`active` is null: query `game_data_active` before claiming success or rollback.
Retrying the same release is safe. The CLI does not claim the old pointer stayed
unchanged when a commit may have succeeded.

Reports are saved locally as `sync-report.json` and, when possible, through the
Supabase report RPC. A reporting transport failure is exposed as
`remoteReportStored: false`; it cannot undo a verified activation. Failed ingests
never replace `staged.json`. After a process crash, inspect the local
`zzz.ingest-lock`/`.stage-*` directory before manually removing an abandoned lock;
the CLI will not guess that another ingest process is dead.

To roll back, pin a previously complete `--release` and run the same verified sync
flow with `--confirm`. This revalidates both destinations before moving the
authority pointer back. Historical local stages and immutable remote revisions
are intentionally retained; no automatic garbage collection is included.

## Consumer integration

Zenlentering, GenEntering and Character Counter should:

1. Read `game_data_active` for game/locale and pin its `sync_id` for the request.
2. Load that descriptor from `game_data_releases` or Storage path
   `game-data/{game}/releases/{syncId}.json`.
3. Load entity revisions by `canonicalId` plus `contentHash` from PostgreSQL,
   MongoDB or `game-data/{game}/entities/{contentHash}.json` in Storage.
4. For MongoDB, select the descriptor with `_id == syncId`; resolve revision IDs
   using SHA-256 of the compact JSON array `[canonicalId, contentHash]`.
5. Adapt `attributes.consumerId` to existing consumer slug keys. Check unresolved
   references before rendering image URLs; use the descriptor's Asset Contract v1
   records to resolve Storage paths. Existing image manifests remain compatible.

Do not read all MongoDB revisions as the current dataset. Do not select the newest
revision by timestamp. Consumers never need to call Nanoka. No source-specific
business logic or private server functionality is imported into consumers.

## Adding games

The contracts, revision publishers, coordinator and cache are game-independent.
Implement a provider with the same `config`, `client`, `version`, `discover`,
`records` and `image_url` interface, add its source configuration and explicit
consumer mappings, then register it in `game_data/cli.py`. Tests should use the
same mock transport and verify the game's actual documented source shapes.
Genshin, HSR and WuWa image workflows remain available. Their **JSON** adapters
are extension points, not implemented API integrations.

## Validation evidence and limitations

`tests/test_game_data.py` uses source HTTP fixtures and database mocks, including
timeouts after a simulated server commit. Existing provider/image/CLI regressions
remain in the suite. These fixtures encode the source shapes demonstrated in the
reference repository, not a captured production data dump. Source endpoints can
evolve; run `data version`, `discover` and a full ingest dry-run with actual
network access before production use. Live Nanoka access and actual database
migration/publication must be verified in the deployment environment.
