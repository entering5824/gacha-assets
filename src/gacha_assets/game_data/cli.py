"""Game-data subcommands on the original gacha-assets CLI."""
from __future__ import annotations

import json
import os
from contextlib import ExitStack
from pathlib import Path

from ..env import resolve_supabase_key
from .contract import atomic_json, read_json
from .errors import safe_error
from .http import JsonClient
from .ingest import ingest, load_release
from .provider import NanokaProvider, SourceConfig
from .publishers import MongoPublisher, SupabasePublisher
from .sync import synchronize


def register(sub):
    parser = sub.add_parser("data", help="Versioned JSON data and coordinated releases")
    commands = parser.add_subparsers(dest="data_command", required=True)
    for name in ("version", "discover", "ingest", "diff", "publish-supabase", "publish-mongodb", "sync", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--source", type=Path, default=Path("config/sources/zzz-data.json"))
        command.add_argument("--cache-root", type=Path, default=Path("cache/game-data"))
        command.add_argument("--asset-manifest", type=Path, default=Path("manifests/zzz.json"))
        command.add_argument("--mapping", type=Path, default=Path("config/games/zzz-mapping.json"))
        command.add_argument("--release", type=Path)
        command.add_argument("--refresh", action="store_true", help="Conditional GET cached version endpoints")
        command.add_argument("--dry-run", action="store_true", help="No remote connection, no activation; source GET/cache allowed")
        command.add_argument("--confirm", action="store_true", help="Explicitly authorize remote writes for this invocation")
        command.add_argument("--verify-only", action="store_true", help="Remote reads only")
        command.add_argument("--target", choices=("supabase", "mongodb", "both"), default="both")
        command.add_argument("--report", type=Path)


def execute(args) -> dict:
    config = SourceConfig.load(args.source)
    client = JsonClient(args.cache_root / "raw", config.base_url, timeout=config.timeout, attempts=config.attempts)
    provider = NanokaProvider(config, client)
    command = args.data_command
    if command == "version":
        return {"game": config.game, "sourceVersion": provider.version()}
    if command == "discover":
        version = provider.version()
        return {"game": config.game, "sourceVersion": version, "indexes": provider.discover(version, refresh=args.refresh)}
    if command == "ingest":
        return ingest(provider, args.cache_root, args.asset_manifest, args.mapping, refresh=args.refresh, dry_run=args.dry_run)
    release_path = args.release
    if command == "sync" and not release_path:
        result = ingest(provider, args.cache_root, args.asset_manifest, args.mapping, refresh=args.refresh, dry_run=args.dry_run)
        if result["status"] in {"failed", "dry-run"}:
            return result
        release_path = Path(result["release"])
    if release_path is None:
        pointer = read_json(args.cache_root / config.game / config.locale / "staged.json", {})
        release_path = args.cache_root / config.game / config.locale / "releases" / pointer.get("syncId", "none") / "release.json"
    release = load_release(release_path)
    if command == "diff":
        return {"syncId": release["syncId"], "diff": release["diff"], "unresolved": release["unresolved"]}
    verify_only = command == "verify" or args.verify_only
    # No --confirm means safe dry-run, including when invoking a publish command.
    dry_run = args.dry_run or (not args.confirm and not verify_only)
    required = ("supabase",) if command == "publish-supabase" else ("mongodb",) if command == "publish-mongodb" else (
        ("supabase", "mongodb") if command == "sync" or args.target == "both" else (args.target,))
    with ExitStack() as stack:
        publishers = {}
        if dry_run:
            class DryPublisher:
                def publish(self, release, directory, **kwargs):
                    return {"status": "dry-run", "entities": len(release["entities"])}
            publishers = {name: DryPublisher() for name in required}
        else:
            if "supabase" in required:
                publishers["supabase"] = SupabasePublisher(os.environ.get("SUPABASE_URL", ""),
                    os.environ.get("SUPABASE_BUCKET", "GachaCounter"), resolve_supabase_key())
            if "mongodb" in required:
                from pymongo import MongoClient
                from pymongo.write_concern import WriteConcern
                uri, database, collection = (os.environ.get(name) for name in ("MONGODB_URI", "MONGODB_DATABASE", "MONGODB_COLLECTION"))
                if not all((uri, database, collection)):
                    raise ValueError("MONGODB_URI, MONGODB_DATABASE and MONGODB_COLLECTION required")
                mongo = stack.enter_context(MongoClient(uri, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000,
                                                       socketTimeoutMS=30000, retryWrites=True))
                db = mongo[database]
                release_collection = os.environ.get("MONGODB_RELEASE_COLLECTION") or collection + "_releases"
                if release_collection == collection:
                    raise ValueError("MongoDB entity and release collections must be distinct")
                publishers["mongodb"] = MongoPublisher(db.get_collection(collection, write_concern=WriteConcern("majority")),
                    db.get_collection(release_collection, write_concern=WriteConcern("majority")))
        return synchronize(release_path, publishers, required=required, dry_run=dry_run,
                           verify_only=verify_only, activate=command == "sync", confirmed=args.confirm)


def handle(args):
    try:
        result = execute(args)
    except Exception as exc:
        result = {"status": "failed", **safe_error(exc)}
    if args.report:
        atomic_json(args.report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") in {"failed", "activation-unknown"}:
        raise SystemExit(2)
