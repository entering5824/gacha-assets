"""Discover Genshin material icons through the documented genshin.dev API routes."""
import json,re,unicodedata
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from urllib.request import Request,urlopen
from urllib.error import HTTPError

def _key(value):
 value=re.sub(r"(?<=[a-z0-9])(?=[A-Z])","-",str(value))
 value=unicodedata.normalize("NFKD",value).encode("ascii","ignore").decode().lower()
 return re.sub(r"[^a-z0-9]+","-",value).strip("-")

def _json(url):
 with urlopen(Request(url,headers={"User-Agent":"gacha-assets/0.1","Accept":"application/json"}),timeout=30) as response:
  if not 200 <= response.status < 300 or not response.headers.get_content_type().endswith("json"):
   raise RuntimeError(f"catalog request failed: {response.status} {response.headers.get_content_type()}")
  return json.load(response)

def _flatten_materials(document):
 entries=[]
 def visit(value,fallback=""):
  if isinstance(value,list):
   for row in value: visit(row,fallback)
  elif isinstance(value,dict):
   if isinstance(value.get("name"),str):
    entries.append((str(value.get("id") or fallback),value)); return
   for key,child in value.items():
    if key!="id": visit(child,str(key))
 visit(document)
 return [(item_id,detail) for item_id,detail in entries if item_id and _key(item_id)!="id"]

def _resolve_material_candidate(row,entries,base):
 key=str(row["key"]); target_id=_key(key); target_name=_key(row.get("name",key))
 matches=[source_id for source_id,_ in entries if _key(source_id)==target_id]
 if not matches:
  matches=[source_id for source_id,detail in entries if _key(detail.get("name", ""))==target_name]
 if not matches: return "unmatched",key,None
 if len(matches)!=1: return "ambiguous",key,None
 item=matches[0]
 try: available=_json(f"{base}/materials/{quote(item,safe='')}/list")
 except HTTPError as exc:
  if exc.code==404: return "missing_icon",key,None
  raise
 if isinstance(available,dict): available=available.get("availableImages",available.get("images",[]))
 if not isinstance(available,list): return "missing_icon",key,None
 variants={str(value).removesuffix(".png").removesuffix(".webp").removesuffix(".jpg") for value in available}
 if "icon" not in variants: return "missing_icon",key,None
 return "candidate",key,{"materialKey":key,"provider":"genshin.dev","sourceId":item,"imageUrl":f"{base}/materials/{quote(item,safe='')}/icon"}

def discover_material_candidates(material_rows,base_url="https://genshin.jmp.blue"):
 """Return only unique key/name matches whose upstream image list explicitly contains icon."""
 base=base_url.rstrip("/"); details=_json(f"{base}/materials/all?lang=en")
 entries=_flatten_materials(details)
 if not entries: raise RuntimeError("upstream materials catalog contained no named material entries")
 # The API requires one image-list lookup per material. Bound concurrency to keep
 # discovery practical without flooding the upstream provider.
 with ThreadPoolExecutor(max_workers=min(8,max(1,len(material_rows)))) as pool:
  outcomes=list(pool.map(lambda row:_resolve_material_candidate(row,entries,base),material_rows))
 result=[asset for status,_,asset in outcomes if status=="candidate"]
 matched_keys={key for status,key,_ in outcomes if status in {"candidate","missing_icon"}}
 missing_icon_variants=[key for status,key,_ in outcomes if status=="missing_icon"]
 ambiguous_keys=[key for status,key,_ in outcomes if status=="ambiguous"]
 return {"provider":"genshin.dev","sourceCatalog":"https://github.com/genshindev/api","catalogItems":len(entries),"matchedRecords":len(matched_keys),"missingIconVariants":missing_icon_variants,"ambiguousKeys":ambiguous_keys,"candidates":result,"unmatchedKeys":[str(row["key"]) for row in material_rows if str(row["key"]) not in matched_keys and str(row["key"]) not in ambiguous_keys]}
