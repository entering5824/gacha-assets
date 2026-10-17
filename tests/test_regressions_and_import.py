import io,json
from pathlib import Path
from urllib.error import URLError
from PIL import Image
import gacha_assets.validate as validation
from gacha_assets.regressions import check_character_counter_fixtures
from gacha_assets.pipeline import run_genentering_material_import
from gacha_assets.providers import genshin_dev

def webp_bytes():
 out=io.BytesIO(); Image.new("RGB",(3,2),(1,2,3)).save(out,"WEBP"); return out.getvalue()

def test_character_counter_audit_urls_stay_unresolved_without_discovered_objects():
 fixtures=Path(__file__).parents[1]/"config"/"regression"/"character-counter.json"
 result=check_character_counter_fixtures(fixtures,None)
 assert result["total"]==4 and result["ready"]==0 and result["unresolved"]==4
 assert {row["reason"] for row in result["assets"]}=={"upstream_catalog_not_supplied"}

def test_empty_discovered_catalog_marks_each_case_no_source_asset(tmp_path):
 fixtures=Path(__file__).parents[1]/"config"/"regression"/"character-counter.json"
 empty=tmp_path/"objects.json"; empty.write_text("[]")
 result=check_character_counter_fixtures(fixtures,empty)
 assert {row["reason"] for row in result["assets"]}=={"no_source_asset"}

def test_all_character_counter_fixtures_resolve_and_decode_discovered_objects(monkeypatch,tmp_path):
 fixtures=Path(__file__).parents[1]/"config"/"regression"/"character-counter.json"
 rows=json.loads(fixtures.read_text())["assets"]
 candidates=tmp_path/"objects.json"; candidates.write_text(json.dumps([{"assetId":row["assetId"],"sourceVariant":row["variant"],"imageUrl":f"https://objects.example/{row['assetId']}.webp"} for row in rows]))
 calls=[]; payload=webp_bytes()
 class Headers:
  def get_content_type(self): return "image/webp"
 class Response:
  status=200; headers=Headers()
  def __enter__(self): return self
  def __exit__(self,*args): pass
  def read(self,*args): return payload
 def fake_urlopen(request,timeout): calls.append(request.get_method()); return Response()
 monkeypatch.setattr(validation,"urlopen",fake_urlopen)
 result=check_character_counter_fixtures(fixtures,candidates)
 assert result["ready"]==4 and result["unresolved"]==0
 assert calls==[method for _ in rows for method in ("HEAD","GET")]

def test_regression_maps_provider_character_portrait_to_canonical_icon(monkeypatch,tmp_path):
 fixtures=Path(__file__).parents[1]/"config"/"regression"/"character-counter.json"
 rows=json.loads(fixtures.read_text())["assets"]
 sigrid=next(row for row in rows if row["entityId"]=="sigrid")
 candidates=tmp_path/"objects.json"
 candidates.write_text(json.dumps([{"assetId":"zenless-zone-zero.character.sigrid.icon","sourceVariant":"portrait","imageUrl":"https://objects.example/sigrid.webp"}]))
 calls=[]; payload=webp_bytes()
 class Headers:
  def get_content_type(self): return "image/webp"
 class Response:
  status=200; headers=Headers()
  def __enter__(self): return self
  def __exit__(self,*args): pass
  def read(self,*args): return payload
 def fake_urlopen(request,timeout): calls.append(request.get_method()); return Response()
 monkeypatch.setattr(validation,"urlopen",fake_urlopen)
 result=check_character_counter_fixtures(fixtures,candidates)
 row=next(item for item in result["assets"] if item["entityId"]=="sigrid")
 assert row["status"]=="ready" and row["variant"]=="icon" and row["sourceVariant"]=="portrait"
 assert calls==["HEAD","GET"]

def test_regression_rejects_unmapped_source_variant_before_network(monkeypatch,tmp_path):
 fixtures=Path(__file__).parents[1]/"config"/"regression"/"character-counter.json"
 rows=json.loads(fixtures.read_text())["assets"]
 sigrid=next(row for row in rows if row["entityId"]=="sigrid")
 candidates=tmp_path/"objects.json"
 candidates.write_text(json.dumps([{"assetId":sigrid["assetId"],"sourceVariant":"artwork","imageUrl":"https://objects.example/sigrid.webp"}]))
 calls=[]
 monkeypatch.setattr(validation,"urlopen",lambda request,timeout: calls.append(request.get_method()))
 result=check_character_counter_fixtures(fixtures,candidates)
 row=next(item for item in result["assets"] if item["entityId"]=="sigrid")
 assert row["status"]=="unresolved" and row["reason"]=="source_variant_mismatch"
 assert row["candidateVariant"]=="artwork" and calls==[]

def test_verified_upstream_resolution_checks_head_get_mime_and_decodes(monkeypatch):
 calls=[]; data=webp_bytes()
 class Headers:
  def get_content_type(self): return "image/webp"
 class Response:
  status=200; headers=Headers()
  def __enter__(self): return self
  def __exit__(self,*args): pass
  def read(self,*args): return data
 def fake_urlopen(request,timeout): calls.append(request.get_method()); return Response()
 monkeypatch.setattr(validation,"urlopen",fake_urlopen)
 decoded=validation.decode_and_normalize(validation.fetch_verified_image("https://assets.example/icon.webp"))
 assert calls==["HEAD","GET"] and decoded

def test_genentering_import_reports_exact_missing_keys_without_guessing(tmp_path):
 data={"materials":[{"key":"KnownKey","name":"Known"},{"key":"NoSource","name":"No source"}]}
 root=tmp_path/"assets"; (root/"materials").mkdir(parents=True)
 source=root/"materials"/"known.webp"; source.write_bytes(webp_bytes())
 (root/"manifest.json").write_text(json.dumps({"materials":{"KnownKey":"materials/known.webp"}}))
 game_data=tmp_path/"game-data.json"; game_data.write_text(json.dumps(data)); candidates=tmp_path/"candidates.json"; candidates.write_text("[]")
 manifest=tmp_path/"manifest-out.json"; report=tmp_path/"report.json"
 result=run_genentering_material_import(game_data,root,candidates,manifest,report)
 assert result["rulesMaterials"]==2 and result["mapped"]==1 and result["missing"]==1 and result["failed"]==0
 output=json.loads(manifest.read_text()); missing=next(row for row in output["assets"] if row["entityId"]=="no-source")
 assert missing["status"]=="missing"
 assert json.loads(report.read_text())["issues"][0]["reason"]=="no_source_asset"

def test_imported_candidate_is_head_get_decoded_then_published(monkeypatch,tmp_path):
 data={"materials":[{"key":"KnownKey","name":"Known"}]}; root=tmp_path/"assets"; (root/"materials").mkdir(parents=True)
 (root/"manifest.json").write_text(json.dumps({"materials":{}}))
 game_data=tmp_path/"game-data.json"; game_data.write_text(json.dumps(data)); candidates=tmp_path/"candidates.json"
 candidates.write_text(json.dumps([{"materialKey":"KnownKey","provider":"test-catalog","sourceId":"known","imageUrl":"https://assets.example/icon.png"}]))
 payload=webp_bytes(); calls=[]
 class Headers:
  def get_content_type(self): return "image/webp"
 class Response:
  status=200; headers=Headers()
  def __enter__(self): return self
  def __exit__(self,*args): pass
  def read(self,*args): return payload
 def fake_urlopen(request,timeout): calls.append(request.get_method()); return Response()
 monkeypatch.setattr(validation,"urlopen",fake_urlopen)
 manifest=tmp_path/"contract.json"; report=tmp_path/"report.json"
 result=run_genentering_material_import(game_data,root,candidates,manifest,report)
 row=json.loads(manifest.read_text())["assets"][0]
 assert result["mapped"]==1 and result["published"] is True and calls==["HEAD","GET"]
 assert row["status"]=="ready" and (root/row["path"]).is_file()
 assert json.loads((root/"manifest.json").read_text())["materials"]["KnownKey"]==row["path"]

def test_unavailable_upstream_candidate_is_pending_and_never_ready(monkeypatch,tmp_path):
 data={"materials":[{"key":"KnownKey","name":"Known"}]}
 root=tmp_path/"assets"; (root/"materials").mkdir(parents=True)
 (root/"manifest.json").write_text(json.dumps({"materials":{}}))
 game_data=tmp_path/"game-data.json"; game_data.write_text(json.dumps(data))
 candidates=tmp_path/"candidates.json"; candidates.write_text(json.dumps([{"materialKey":"KnownKey","provider":"test","sourceId":"known","imageUrl":"https://assets.example/icon.png"}]))
 monkeypatch.setattr(validation,"urlopen",lambda *args,**kwargs: (_ for _ in ()).throw(URLError("DNS unavailable")))
 manifest=tmp_path/"contract.json"; report=tmp_path/"report.json"

 result=run_genentering_material_import(game_data,root,candidates,manifest,report)

 row=json.loads(manifest.read_text())["assets"][0]
 assert result["mapped"]==0 and result["pending"]==1 and result["failed"]==1 and result["published"] is False
 assert row["status"]=="pending" and row["sha256"] is None
 assert not (root/"materials"/"imported"/"known-key.webp").exists()

def test_gone_upstream_candidate_is_missing_with_no_source_reason(monkeypatch,tmp_path):
 from urllib.error import HTTPError
 data={"materials":[{"key":"KnownKey","name":"Known"}]}
 root=tmp_path/"assets"; (root/"materials").mkdir(parents=True)
 (root/"manifest.json").write_text(json.dumps({"materials":{}}))
 game_data=tmp_path/"game-data.json"; game_data.write_text(json.dumps(data))
 candidates=tmp_path/"candidates.json"; candidates.write_text(json.dumps([{"materialKey":"KnownKey","provider":"test","sourceId":"known","imageUrl":"https://assets.example/icon.png"}]))
 monkeypatch.setattr(validation,"urlopen",lambda *args,**kwargs: (_ for _ in ()).throw(HTTPError("https://assets.example/icon.png",404,"Not Found",{},None)))
 manifest=tmp_path/"contract.json"; report=tmp_path/"report.json"

 result=run_genentering_material_import(game_data,root,candidates,manifest,report)

 row=json.loads(manifest.read_text())["assets"][0]
 assert result["mapped"]==0 and result["missing"]==1 and result["failed"]==0
 assert row["status"]=="missing" and json.loads(report.read_text())["issues"][0]["reason"]=="no_source_asset"

def test_genshin_dev_discovery_requires_catalog_match_and_listed_icon(monkeypatch):
 def catalog(url):
  if url.endswith("/materials/all?lang=en"):
   return {"known-key":{"name":"Known Material"},"another":{"name":"Other"}}
  if url.endswith("/materials/known-key/list"): return ["icon", "gacha-splash"]
  raise AssertionError(f"unexpected candidate request {url}")
 monkeypatch.setattr(genshin_dev,"_json",catalog)
 result=genshin_dev.discover_material_candidates([{"key":"KnownKey","name":"Known Material"},{"key":"Missing","name":"Missing"}],"https://catalog.example")
 assert result["catalogItems"]==2 and result["matchedRecords"]==1 and result["unmatchedKeys"]==["Missing"]
 assert result["candidates"]==[{"materialKey":"KnownKey","provider":"genshin.dev","sourceId":"known-key","imageUrl":"https://catalog.example/materials/known-key/icon"}]
