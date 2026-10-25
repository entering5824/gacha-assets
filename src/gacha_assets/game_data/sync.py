"""Release coordinator with one remote authority and retryable immutable staging."""
from pathlib import Path

from .contract import atomic_json, now
from .errors import safe_error
from .ingest import load_release


def synchronize(path: Path, publishers: dict, *, required=("supabase", "mongodb"),
                dry_run=True, verify_only=False, activate=False, confirmed=False) -> dict:
    if not dry_run and not verify_only and not confirmed:
        raise ValueError("remote writes require --confirm")
    if activate and ("supabase" not in required or set(required) != {"supabase", "mongodb"}):
        raise ValueError("activation requires both Supabase and MongoDB verification")
    if not required or any(name not in publishers for name in required):
        raise ValueError("required publishers are not configured")
    release = load_release(path)
    report = {k: release[k] for k in ("syncId", "game", "locale", "sourceVersion", "validated", "new", "changed", "unchanged", "failed")}
    report.update({"startedAt": now(), "targets": {}, "status": "staging", "active": False})
    authority = publishers.get("supabase")
    activation_attempted = False
    try:
        # Capture expected state BEFORE staging, so simultaneous releases cannot silently supersede each other.
        expected = authority.active(release["game"], release["locale"]) if activate and not dry_run else None
        for name in required:
            try:
                report["targets"][name] = publishers[name].publish(release, path.parent, dry_run=dry_run, verify_only=verify_only)
            except Exception as exc:
                # Never include connection strings or remote response bodies in reports.
                report["targets"][name] = {"status": "failed", **safe_error(exc)}
                report["failed"] += 1
        verified = all(report["targets"][name]["status"] == "verified" for name in required)
        if verified and activate and not dry_run and not verify_only:
            activation_attempted = True
            authority.activate(release, expected, report["targets"])
            report["active"] = True
        report["status"] = "dry-run" if dry_run else ("active" if report["active"] else "verified" if verified else "failed")
    except Exception as exc:
        report.update({"status": "activation-unknown" if activation_attempted else "failed",
                       "active": None if activation_attempted else False,
                       **safe_error(exc), "failed": report["failed"] + 1})
    report["finishedAt"] = now()
    if not dry_run and not verify_only:
        if authority is not None:
            try:
                authority.api("rpc/record_game_data_sync_run", "POST", {"p_sync_id": release["syncId"], "p_report": report})
            except Exception:
                # The immutable release commit is authoritative even if reporting transport fails.
                report["remoteReportStored"] = False
            else:
                report["remoteReportStored"] = True
        atomic_json(path.parent / "sync-report.json", report)
    return report
