"""Discover exact Genshin material icon mappings from the GenshinDB catalog."""
from __future__ import annotations

import json
import re
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _key(value: object) -> str:
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", str(value))
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def _catalog(base_url: str) -> list[dict]:
    query = urlencode({
        "query": "names",
        "matchCategories": "true",
        "verboseCategories": "true",
        "dumpResult": "true",
    })
    request = Request(
        f"{base_url.rstrip('/')}/materials?{query}",
        headers={"User-Agent": "gacha-assets/0.1", "Accept": "application/json"},
    )
    with urlopen(request, timeout=20) as response:
        if not 200 <= response.status < 300 or not response.headers.get_content_type().endswith("json"):
            raise RuntimeError("GenshinDB materials catalog request failed")
        document = json.load(response)
    records = document.get("result") if isinstance(document, dict) else document
    if not isinstance(records, list):
        raise RuntimeError("GenshinDB materials catalog has an unsupported response")
    return [record for record in records if isinstance(record, dict)]


def discover_material_asset_candidates(
    material_rows: list[dict],
    base_url: str = "https://genshin-db-api.vercel.app/api/v5",
    image_base_url: str = "https://transform.alioth.wiki/i/gi/ItemIcon",
) -> dict:
    """Resolve exact material names to source-declared icon filenames.

    This only discovers candidates. Consumers must still GET, hash, and decode
    every image before writing a manifest or publishing bytes.
    """
    records = _catalog(base_url)
    by_name: dict[str, list[dict]] = {}
    for record in records:
        name = record.get("name")
        if isinstance(name, str) and name.strip():
            by_name.setdefault(_key(name), []).append(record)

    candidates = []
    matched_keys = set()
    ambiguous_keys = []
    missing_icon_variants = []
    for row in material_rows:
        key = str(row["key"])
        matches = by_name.get(_key(row.get("name", key)), [])
        if not matches:
            matches = by_name.get(_key(key), [])
        if not matches:
            continue
        if len(matches) != 1:
            ambiguous_keys.append(key)
            continue
        matched_keys.add(key)
        record = matches[0]
        image_name = (record.get("images") or {}).get("filename_icon")
        if not isinstance(image_name, str) or not re.fullmatch(r"UI_ItemIcon_[A-Za-z0-9_-]+", image_name):
            missing_icon_variants.append(key)
            continue
        candidates.append({
            "materialKey": key,
            "provider": "alioth",
            "sourceId": f"genshin-db:{record.get('id', '')}:{image_name}",
            "mappingProvider": "genshin-db",
            "sourceCatalog": "https://github.com/theBowja/genshin-db",
            "imageUrl": f"{image_base_url.rstrip('/')}/{image_name}.png?f=auto&w=256",
        })

    return {
        "provider": "genshin-db",
        "sourceCatalog": "https://github.com/theBowja/genshin-db",
        "catalogItems": len(records),
        "matchedRecords": len(matched_keys),
        "missingIconVariants": missing_icon_variants,
        "ambiguousKeys": ambiguous_keys,
        "candidates": candidates,
        "unmatchedKeys": [
            str(row["key"])
            for row in material_rows
            if str(row["key"]) not in matched_keys and str(row["key"]) not in ambiguous_keys
        ],
    }
