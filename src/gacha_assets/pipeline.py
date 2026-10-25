import hashlib,json,os,tempfile
from pathlib import Path
from urllib.error import HTTPError,URLError
from .resolve import canonical_asset,resolve_from_catalog
from .validate import decode_and_normalize,fetch_verified_image
from .diff import diff_manifests
from .models import Asset
def _read(row):
 if row.get("sourceFile"): return Path(row["sourceFile"]).read_bytes()
 url=row.get("imageUrl")
 if not url: raise ValueError("sourceFile or resolved HTTPS imageUrl required")
 return fetch_verified_image(url)

def run_genentering_material_import(game_data_path,assets_root,candidate_catalog,manifest_path,report_path):
 """Build a Genshin material contract manifest from exact keys and verified local/upstream candidates."""
 from .resolve import canonical_id
 from .models import Asset
 from pathlib import Path
 import hashlib
 data=json.loads(Path(game_data_path).read_text("utf8")); material_rows=data["materials"]
 materials={row["key"]:row for row in material_rows}
 if not candidate_catalog or not Path(candidate_catalog).is_file():
  raise ValueError("verified upstream candidate catalog is required; run discover-gen-materials first")
 candidate_document=json.loads(Path(candidate_catalog).read_text("utf8"))
 catalog=candidate_document.get("candidates",candidate_document.get("assets",candidate_document.get("objects",candidate_document))) if isinstance(candidate_document,dict) else candidate_document
 existing_path=Path(assets_root)/"manifest.json"
 existing=json.loads(existing_path.read_text("utf8")) if existing_path.is_file() else {"materials":{}}
 material_map=existing.get("materials",{}); records=[]; issues=[]; counts={"mapped":0,"missing":0,"pending":0,"failed":0}; publish_files={}; material_updates={}
 for key,definition in sorted(materials.items()):
  entity_id=canonical_id(key); asset_id=f"genshin.material.{entity_id}.icon"; rel=material_map.get(key)
  if isinstance(rel,str) and rel:
   local=(Path(assets_root)/rel).resolve()
   if local.is_relative_to(Path(assets_root).resolve()) and local.is_file():
    try:
     source_bytes=local.read_bytes(); decode_and_normalize(source_bytes); digest=hashlib.sha256(source_bytes).hexdigest()
     record=Asset(asset_id,"genshin","material",entity_id,"icon","bundled",Path(rel).as_posix(),{"provider":"genentering-bundled-manifest","sourceId":f"materials/{rel}"},digest,"ready").to_dict(); counts["mapped"]+=1
     records.append(record); continue
    except Exception as exc: issues.append({"materialKey":key,"status":"failed","reason":f"local_asset_decode_failed: {exc}"}); counts["failed"]+=1
  matches=[x for x in catalog if (x.get("materialKey")==key or x.get("key")==key) and x.get("imageUrl")]
  if len(matches)>1: issues.append({"materialKey":key,"status":"failed","reason":"ambiguous_upstream_candidates"}); counts["failed"]+=1; continue
  if matches:
   candidate=matches[0]
   try:
    raw=fetch_verified_image(candidate["imageUrl"]); normalized=decode_and_normalize(raw); digest=hashlib.sha256(normalized).hexdigest()
    source_id=str(candidate.get("sourceId") or candidate.get("materialKey") or key)
    rel=f"materials/imported/{entity_id}.webp"; publish_files[rel]=normalized; material_updates[key]=rel
    record=Asset(asset_id,"genshin","material",entity_id,"icon","bundled",rel,{"provider":str(candidate["provider"]),"sourceId":source_id},digest,"ready").to_dict()
    records.append(record); counts["mapped"]+=1
   except HTTPError as exc:
    if exc.code==404:
     issues.append({"materialKey":key,"assetId":asset_id,"status":"missing","reason":"no_source_asset"})
     records.append(Asset(asset_id,"genshin","material",entity_id,"icon","bundled",f"materials/{entity_id}.webp",{"provider":"canonical-resolver","sourceId":key},None,"missing").to_dict())
     counts["missing"]+=1
    else:
     issues.append({"materialKey":key,"assetId":asset_id,"status":"pending","reason":f"upstream_http_{exc.code}"})
     records.append(Asset(asset_id,"genshin","material",entity_id,"icon","bundled",f"materials/imported/{entity_id}.webp",{"provider":str(candidate.get("provider") or "unknown"),"sourceId":str(candidate.get("sourceId") or key)},None,"pending").to_dict())
     counts["pending"]+=1; counts["failed"]+=1
   except URLError as exc:
    issues.append({"materialKey":key,"assetId":asset_id,"status":"pending","reason":f"upstream_unavailable: {exc.reason}"})
    records.append(Asset(asset_id,"genshin","material",entity_id,"icon","bundled",f"materials/imported/{entity_id}.webp",{"provider":str(candidate.get("provider") or "unknown"),"sourceId":str(candidate.get("sourceId") or key)},None,"pending").to_dict())
    counts["pending"]+=1; counts["failed"]+=1
   except Exception as exc:
    issues.append({"materialKey":key,"assetId":asset_id,"status":"failed","reason":f"upstream_candidate_rejected: {exc}"})
    records.append(Asset(asset_id,"genshin","material",entity_id,"icon","bundled",f"materials/imported/{entity_id}.webp",{"provider":str(candidate.get("provider") or "unknown"),"sourceId":str(candidate.get("sourceId") or key)},None,"failed").to_dict())
    counts["failed"]+=1
  else:
   records.append(Asset(asset_id,"genshin","material",entity_id,"icon","bundled",f"materials/{entity_id}.webp",{"provider":"canonical-resolver","sourceId":key},None,"missing").to_dict())
   issues.append({"materialKey":key,"assetId":asset_id,"status":"missing","reason":"no_source_asset"}); counts["missing"]+=1
 published=counts["failed"]==0
 if not published:
  records=[dict(row,status="pending") if row["path"] in publish_files and row["status"]=="ready" else row for row in records]
  counts["pending"]=sum(1 for row in records if row["status"]=="pending")
  counts["mapped"]=sum(1 for row in records if row["status"]=="ready")
 if published and publish_files:
  root=Path(assets_root)
  with tempfile.TemporaryDirectory(dir=root) as temp_dir:
   stage=Path(temp_dir)
   for rel,content in publish_files.items():
    staged=stage/rel;staged.parent.mkdir(parents=True,exist_ok=True);staged.write_bytes(content)
   for rel in sorted(publish_files):
    target=root/rel;target.parent.mkdir(parents=True,exist_ok=True);os.replace(stage/rel,target)
  existing.setdefault("materials",{}).update(material_updates)
  fd,name=tempfile.mkstemp(dir=root,suffix=".manifest.tmp")
  try:
   with os.fdopen(fd,"w",encoding="utf8") as stream:json.dump(existing,stream,indent=2,ensure_ascii=False);stream.write("\n")
   os.replace(name,existing_path)
  finally:
   if os.path.exists(name):os.unlink(name)
 manifest={"schemaVersion":1,"assets":records}
 report={"game":"genshin","entityType":"material","rulesMaterials":len(materials),"mapped":counts["mapped"],"missing":counts["missing"],"pending":counts["pending"],"failed":counts["failed"],"upstreamSource":candidate_document.get("sourceCatalog",candidate_document.get("provider")) if isinstance(candidate_document,dict) else "provided-catalog","upstreamCatalogItems":candidate_document.get("catalogItems") if isinstance(candidate_document,dict) else None,"upstreamMatchedRecords":candidate_document.get("matchedRecords") if isinstance(candidate_document,dict) else None,"upstreamMissingIconVariants":candidate_document.get("missingIconVariants",[]) if isinstance(candidate_document,dict) else [],"upstreamUnmatchedKeys":candidate_document.get("unmatchedKeys",[]) if isinstance(candidate_document,dict) else [],"upstreamCandidates":len(catalog),"published":published,"issues":issues}
 Path(manifest_path).parent.mkdir(parents=True,exist_ok=True); Path(report_path).parent.mkdir(parents=True,exist_ok=True)
 Path(manifest_path).write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
 Path(report_path).write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
 return report
def run(rows,manifest_path,public_root,dry_run=False):
 mp=Path(manifest_path); root=Path(public_root); assets=[]; files={}; errors=[]; seen=set()
 for i,row in enumerate(rows):
  try:
   a=canonical_asset(row)
   if a.assetId in seen: raise ValueError("duplicate assetId")
   seen.add(a.assetId); data=decode_and_normalize(_read(row)); digest=hashlib.sha256(data).hexdigest()
   a=a.__class__(**{**a.to_dict(),"sha256":digest,"status":"ready"})
   previous=files.get(a.path)
   if previous is not None and hashlib.sha256(previous).hexdigest()!=digest: raise ValueError("different assets resolve to the same storage path")
   assets.append(a.to_dict()); files[a.path]=data
  except Exception as e: errors.append({"index":i,"reason":str(e)})
 old_document=json.loads(mp.read_text("utf8")) if mp.exists() else []
 # Accept the original list format during migration, but always write Asset
 # Contract v1 so the result can flow directly into import/publish commands.
 if isinstance(old_document,dict):
  if old_document.get("schemaVersion")!=1: raise ValueError("Existing manifest must use Asset Contract schemaVersion 1")
  old=old_document.get("assets")
 else: old=old_document
 if not isinstance(old,list): raise ValueError("Existing manifest must be an asset list or Asset Contract v1")
 old_by_id={}
 for record in old:
  Asset(**record).validate()
  prior=old_by_id.get(record["assetId"])
  if prior is not None and prior!=record: raise ValueError(f"Conflicting existing records for asset ID {record['assetId']}")
  old_by_id[record["assetId"]]=record
 merged_by_id={**old_by_id,**{record["assetId"]:record for record in assets}}
 merged=[merged_by_id[key] for key in sorted(merged_by_id)]
 hashes_by_path={}
 for record in merged:
  if record.get("status")!="ready": continue
  prior=hashes_by_path.setdefault(record["path"],record["sha256"])
  if prior!=record["sha256"]:
   errors.append({"assetId":record["assetId"],"reason":f"different asset hashes claim storage path {record['path']}"})
 changes=diff_manifests(old,merged)
 if not dry_run and not errors:
  root.mkdir(parents=True,exist_ok=True); mp.parent.mkdir(parents=True,exist_ok=True)
  with tempfile.TemporaryDirectory(dir=root) as td:
   stage=Path(td)
   for rel,data in files.items():
    p=stage/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
   for rel in sorted(files):
    p=root/rel;p.parent.mkdir(parents=True,exist_ok=True);os.replace(stage/rel,p)
  fd,name=tempfile.mkstemp(dir=mp.parent,suffix=".tmp")
  try:
   with os.fdopen(fd,"w",encoding="utf8") as f:json.dump({"schemaVersion":1,"assets":merged},f,indent=2,ensure_ascii=False);f.write("\n")
   os.replace(name,mp)
  finally:
   if os.path.exists(name):os.unlink(name)
 return {"assets":merged,"diff":changes,"rejected":errors,"published":not dry_run and not errors}
