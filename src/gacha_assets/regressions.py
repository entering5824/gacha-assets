import json
from pathlib import Path
from .resolve import resolve_from_catalog
from .validate import decode_and_normalize,fetch_verified_image

def check_character_counter_fixtures(fixture_path,candidate_catalog):
 fixtures=json.loads(Path(fixture_path).read_text("utf8"))["assets"]
 catalog_supplied=bool(candidate_catalog and Path(candidate_catalog).is_file())
 candidates=json.loads(Path(candidate_catalog).read_text("utf8")) if catalog_supplied else []
 candidates=candidates.get("candidates",candidates.get("assets",candidates.get("objects",candidates))) if isinstance(candidates,dict) else candidates
 results=[]
 for fixture in fixtures:
  try:
   resolved=resolve_from_catalog(fixture["assetId"],candidates)
   if resolved is None:
    reason="no_source_asset" if catalog_supplied else "upstream_catalog_not_supplied"
    results.append({**fixture,"status":"unresolved","reason":reason}); continue
   source_variant=resolved.get("sourceVariant")
   # Providers often call a character image a portrait while consumers use it
   # as the canonical icon. Preserve the upstream variant in the report.
   canonical_variant = "icon" if fixture.get("entityType")=="character" and source_variant=="portrait" else source_variant
   if canonical_variant != fixture.get("variant"):
    reason="source_variant_unverified" if not source_variant else "source_variant_mismatch"
    results.append({**fixture,"status":"unresolved","reason":reason,"candidateUrl":resolved["imageUrl"],"candidateVariant":source_variant}); continue
   data=fetch_verified_image(resolved["imageUrl"]); normalized=decode_and_normalize(data)
   results.append({**fixture,"status":"ready","resolvedUrl":resolved["imageUrl"],"provider":resolved.get("provider"),"sourceId":resolved.get("sourceId"),"sourcePage":resolved.get("sourcePage"),"sourceVariant":source_variant,"variant":fixture["variant"],"bytes":len(data),"decodedBytes":len(normalized)})
  except Exception as exc:
   results.append({**fixture,"status":"unresolved","reason":str(exc)})
 return {"project":"character-counter","total":len(results),"ready":sum(x["status"]=="ready" for x in results),"unresolved":sum(x["status"]=="unresolved" for x in results),"assets":results}
