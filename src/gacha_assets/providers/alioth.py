"""Discover Genshin material icons from Alioth's published inventory cards.

Names are matched by exact normalized display text. Image URLs are read from
the matching card's image src attribute; this provider never constructs a URL
from a material name or key.
"""
from __future__ import annotations

from html.parser import HTMLParser
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SOURCE_CATALOG = "https://alioth.wiki/en/gi/item"
_ALLOWED_IMAGE_HOSTS = {"transform.alioth.wiki", "img.alioth.wiki"}
_ICON_PATH = re.compile(r"/ItemIcon/(UI_ItemIcon_[A-Za-z0-9_-]+)\.[A-Za-z0-9]+$")


def _normalized_name(value: object) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(value))
    text = text.replace("'", "").replace("\\u2018", "").replace("\\u2019", "")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _image_source(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_IMAGE_HOSTS:
        return None
    match = _ICON_PATH.search(parsed.path)
    if not match:
        return None
    return value, match.group(1)


class _InventoryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, str]] = []
        self._card_name: str | None = None
        self._image_alt: str | None = None
        self._image_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "button" and values.get("aria-label"):
            self._card_name = str(values["aria-label"])
            self._image_alt = None
            self._image_url = None
        elif tag == "img" and self._card_name and values.get("alt") and values.get("src"):
            self._image_alt = str(values["alt"])
            self._image_url = str(values["src"])

    def handle_endtag(self, tag: str) -> None:
        if tag != "button" or not self._card_name:
            return
        if (
            self._image_alt
            and _normalized_name(self._image_alt) == _normalized_name(self._card_name)
            and _image_source(self._image_url)
        ):
            self.items.append({"name": self._card_name, "imageUrl": self._image_url})
        self._card_name = None
        self._image_alt = None
        self._image_url = None


def _page_url(base_url: str, page: int) -> str:
    parsed = urlsplit(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if page > 1:
        query["page"] = str(page)
    else:
        query.pop("page", None)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _parse_page(document: str) -> list[dict[str, str]]:
    parser = _InventoryParser()
    parser.feed(document)
    return parser.items


def _fetch_page(url: str) -> list[dict[str, str]]:
    request = Request(url, headers={"User-Agent": "gacha-assets/0.1", "Accept": "text/html"})
    with urlopen(request, timeout=30) as response:
        if not 200 <= response.status < 300 or response.headers.get_content_type() != "text/html":
            raise RuntimeError("Alioth inventory catalog request failed")
        document = response.read().decode("utf-8", "replace")
    return _parse_page(document)


def discover_material_asset_candidates(
    material_rows: list[dict],
    base_url: str = SOURCE_CATALOG,
    max_pages: int = 20,
) -> dict:
    """Return source-declared exact-name candidates; downstream must decode them."""
    if not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError("max_pages must be a positive integer")

    catalog: list[dict[str, str]] = []
    seen_pages: set[tuple[tuple[str, str], ...]] = set()
    pages_fetched = 0
    for page in range(1, max_pages + 1):
        items = _fetch_page(_page_url(base_url, page))
        pages_fetched += 1
        signature = tuple((item["name"], item["imageUrl"]) for item in items)
        if not signature or signature in seen_pages:
            break
        seen_pages.add(signature)
        catalog.extend(items)
    else:
        raise RuntimeError(f"Alioth inventory exceeded the {max_pages}-page safety limit")

    by_name: dict[str, dict[str, dict[str, str]]] = {}
    for item in catalog:
        by_name.setdefault(_normalized_name(item["name"]), {})[item["imageUrl"]] = item

    candidates: list[dict[str, str]] = []
    ambiguous_keys: list[str] = []
    unmatched_keys: list[str] = []
    matched_keys: set[str] = set()
    for row in material_rows:
        key = str(row.get("key", ""))
        display_name = row.get("name") or key
        matches = by_name.get(_normalized_name(display_name), {})
        if len(matches) > 1:
            ambiguous_keys.append(key)
            continue
        if not matches:
            unmatched_keys.append(key)
            continue
        item = next(iter(matches.values()))
        source = _image_source(item["imageUrl"])
        if not source:
            unmatched_keys.append(key)
            continue
        image_url, source_id = source
        matched_keys.add(key)
        candidates.append({
            "materialKey": key,
            "provider": "alioth",
            "sourceId": source_id,
            "mappingProvider": "alioth-inventory",
            "sourceCatalog": SOURCE_CATALOG,
            "sourceName": item["name"],
            "imageUrl": image_url,
        })

    return {
        "provider": "alioth",
        "sourceCatalog": SOURCE_CATALOG,
        "pagesFetched": pages_fetched,
        "catalogItems": len(catalog),
        "matchedRecords": len(matched_keys),
        "ambiguousKeys": ambiguous_keys,
        "candidates": candidates,
        "unmatchedKeys": unmatched_keys,
    }
