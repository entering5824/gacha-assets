from .models import Asset
import re
import unicodedata

GAME_ALIASES = {
 "genshin-impact": "genshin",
 "honkai-star-rail": "hsr",
 "wuthering-waves": "wuwa",
 "zenless-zone-zero": "zzz",
}

def canonical_catalog_asset_id(value):
 parts=str(value or "").split(".")
 if len(parts)==4:
  parts[0]=GAME_ALIASES.get(parts[0],parts[0])
 return ".".join(parts)

def canonical_id(value):
 value=re.sub(r"(?<=[a-z0-9])(?=[A-Z])","-",str(value))
 value=unicodedata.normalize("NFKD",value).encode("ascii","ignore").decode().lower()
 return re.sub(r"-+","-",re.sub(r"[^a-z0-9]+","-",value)).strip("-")

def canonical_asset(row):
 g=canonical_id(row["game"]); k=canonical_id(row["entityType"]); eid=canonical_id(row["entityId"]); v=canonical_id(row["variant"])
 a=Asset(f"{g}.{k}.{eid}.{v}",g,k,eid,v,row.get("storage","public"),row.get("path",f"{g}/{k}s/{eid}/{v}.webp"),{"provider":str(row["provider"]),"sourceId":str(row["sourceId"])},None,"pending")
 a.validate(); return a

def resolve_from_catalog(asset_id,candidates):
 """Resolve only an exact canonical asset ID from a discovered upstream catalog."""
 matches=[row for row in candidates if canonical_catalog_asset_id(row.get("assetId"))==asset_id and row.get("imageUrl")]
 if len(matches)>1: raise ValueError(f"ambiguous upstream candidates for {asset_id}")
 return matches[0] if matches else None
