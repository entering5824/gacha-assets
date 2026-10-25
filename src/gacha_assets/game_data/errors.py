"""Actionable error codes without exposing driver messages or credentials."""
import re

_KNOWN = {
    "immutable storage object conflict", "storage upload failed", "storage readback verification failed",
    "Supabase metadata readback failed", "Supabase release readback failed", "active release readback failed",
    "Mongo immutable revision conflict", "Mongo readback verification failed", "Mongo release verification failed",
    "local image checksum mismatch", "retained asset unavailable or checksum mismatch", "release checksum mismatch",
    "only complete staged releases can be published", "mapping must use schemaVersion 1", "ambiguous entity mapping",
    "invalid explicit entity mapping", "explicit image mapping must reference an existing asset ID",
    "MONGODB_URI, MONGODB_DATABASE and MONGODB_COLLECTION required", "Supabase HTTPS URL and bucket required",
    "invalid source configuration", "detail identity mismatch", "empty required detail", "entity content hash mismatch",
    "another ingest is running; inspect lock before manual recovery", "cached image checksum mismatch",
    "source transport unavailable", "MongoDB entity and release collections must be distinct",
}


def safe_error(error):
    message = str(error)
    result = {"errorType": type(error).__name__}
    if message in _KNOWN:
        result["reason"] = re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")
    elif re.fullmatch(r"(?:source|Supabase|storage transient) HTTP [0-9]{1,3}", message):
        result["reason"] = message.lower().replace(" ", "_")
    elif isinstance(error, FileNotFoundError):
        result["reason"] = "required_local_file_missing"
    elif isinstance(error, TimeoutError):
        result["reason"] = "transport_timeout"
    return result
