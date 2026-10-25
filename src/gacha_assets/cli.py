import argparse,json
import os
from pathlib import Path
from .pipeline import run,run_genentering_material_import
from .regressions import check_character_counter_fixtures
from .validate import decode_and_normalize
from .providers.genshin_dev import discover_material_candidates
from .providers.genshin_db import discover_material_asset_candidates
from .providers.alioth import discover_material_asset_candidates as discover_alioth_material_candidates
from .publish import import_contract,import_genentering_contract,merge_contracts,merge_all_contracts,publish_supabase
from .env import load_local_env,resolve_supabase_key
def main():
 load_local_env()
 p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="command",required=True)
 from .game_data.cli import register as register_data
 register_data(sub)
 s=sub.add_parser("run");s.add_argument("--input",required=True,type=Path);s.add_argument("--manifest",required=True,type=Path);s.add_argument("--public-root",required=True,type=Path);s.add_argument("--dry-run",action="store_true")
 s=sub.add_parser("check-cc-regressions");s.add_argument("--candidates",type=Path);s.add_argument("--output",type=Path)
 s=sub.add_parser("discover-gen-materials");s.add_argument("--game-data",required=True,type=Path);s.add_argument("--assets-root",type=Path);s.add_argument("--base-url",default="https://genshin.jmp.blue");s.add_argument("--output",required=True,type=Path)
 s=sub.add_parser("discover-gen-materials-gdb");s.add_argument("--game-data",required=True,type=Path);s.add_argument("--base-url",default="https://genshin-db-api.vercel.app/api/v5");s.add_argument("--image-base-url",default="https://transform.alioth.wiki/i/gi/ItemIcon");s.add_argument("--output",required=True,type=Path)
 s=sub.add_parser("discover-gen-materials-alioth");s.add_argument("--game-data",required=True,type=Path);s.add_argument("--base-url",default="https://alioth.wiki/en/gi/item");s.add_argument("--max-pages",type=int,default=20);s.add_argument("--output",required=True,type=Path)
 s=sub.add_parser("import-gen-materials");s.add_argument("--game-data",required=True,type=Path);s.add_argument("--assets-root",required=True,type=Path);s.add_argument("--candidates",type=Path);s.add_argument("--manifest",required=True,type=Path);s.add_argument("--report",required=True,type=Path)
 s=sub.add_parser("import-contract");s.add_argument("--input",required=True,type=Path);s.add_argument("--assets-root",required=True,type=Path);s.add_argument("--manifest",required=True,type=Path);s.add_argument("--storage",choices=["supabase","public","bundled"])
 s=sub.add_parser("import-genentering");s.add_argument("--input",required=True,type=Path);s.add_argument("--assets-root",required=True,type=Path);s.add_argument("--manifest",required=True,type=Path);s.add_argument("--materials-manifest",type=Path)
 s=sub.add_parser("merge-contracts");s.add_argument("--input",action="append",required=True,type=Path);s.add_argument("--game",required=True);s.add_argument("--manifest",required=True,type=Path)
 s=sub.add_parser("merge-manifests");s.add_argument("--input",action="append",required=True,type=Path);s.add_argument("--manifest",required=True,type=Path)
 s=sub.add_parser("publish-supabase");s.add_argument("--manifest",required=True,type=Path);s.add_argument("--assets-root",required=True,type=Path);s.add_argument("--url",default=os.environ.get("SUPABASE_URL"));s.add_argument("--bucket",default=os.environ.get("SUPABASE_BUCKET","GachaCounter"));s.add_argument("--key-env",default="SUPABASE_STORAGE_API_KEY");s.add_argument("--manifest-path");s.add_argument("--report",type=Path);s.add_argument("--dry-run",action="store_true");s.add_argument("--verify-only",action="store_true")
 s=sub.add_parser("validate-image");s.add_argument("path",type=Path)
 a=p.parse_args()
 if a.command=="data":
  from .game_data.cli import handle
  handle(a)
  return
 if a.command=="run": result=run(json.loads(a.input.read_text("utf8")),a.manifest,a.public_root,a.dry_run)
 elif a.command=="check-cc-regressions":
  fixture=Path(__file__).resolve().parents[2]/"config"/"regression"/"character-counter.json";result=check_character_counter_fixtures(fixture,a.candidates)
  if a.output:a.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf8")
 elif a.command=="discover-gen-materials":
  rows=json.loads(a.game_data.read_text("utf8"))["materials"]
  if a.assets_root:
   local=json.loads((a.assets_root/"manifest.json").read_text("utf8")).get("materials",{}); missing=[]
   for row in rows:
    relative=local.get(row["key"]); file=(a.assets_root/relative).resolve() if isinstance(relative,str) else None
    try:
     if file and file.is_relative_to(a.assets_root.resolve()) and file.is_file(): decode_and_normalize(file.read_bytes()); continue
    except Exception: pass
    missing.append(row)
   rows=missing
  result=discover_material_candidates(rows,a.base_url)
  a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf8")
 elif a.command=="discover-gen-materials-gdb":
  rows=json.loads(a.game_data.read_text("utf8"))["materials"]
  result=discover_material_asset_candidates(rows,a.base_url,a.image_base_url)
  a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf8")
 elif a.command=="discover-gen-materials-alioth":
  rows=json.loads(a.game_data.read_text("utf8"))["materials"]
  result=discover_alioth_material_candidates(rows,a.base_url,a.max_pages)
  a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf8")
 elif a.command=="import-gen-materials": result=run_genentering_material_import(a.game_data,a.assets_root,a.candidates,a.manifest,a.report)
 elif a.command=="import-contract": result=import_contract(a.input,a.assets_root,a.manifest,a.storage)
 elif a.command=="import-genentering": result=import_genentering_contract(a.input,a.assets_root,a.manifest,a.materials_manifest)
 elif a.command=="merge-contracts": result=merge_contracts(a.input,a.manifest,a.game)
 elif a.command=="merge-manifests": result=merge_all_contracts(a.input,a.manifest)
 elif a.command=="publish-supabase":
  result=publish_supabase(a.manifest,a.assets_root,a.url or "",a.bucket,resolve_supabase_key(a.key_env),manifest_object_path=a.manifest_path,dry_run=a.dry_run,verify_only=a.verify_only)
  if a.report:a.report.parent.mkdir(parents=True,exist_ok=True);a.report.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf8")
 else:
  data=a.path.read_bytes(); normalized=decode_and_normalize(data); result={"valid":True,"size":len(data),"normalizedSize":len(normalized)}
 print(json.dumps(result,indent=2,ensure_ascii=False))
 if a.command=="run" and result["rejected"]:raise SystemExit(2)
 if a.command=="check-cc-regressions" and result["unresolved"]:raise SystemExit(3)

if __name__ == '__main__':
 main()

