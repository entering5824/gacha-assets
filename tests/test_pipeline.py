import json
from PIL import Image
from gacha_assets.pipeline import run
def png(p):Image.new("RGBA",(3,2),(20,40,60,255)).save(p,"PNG")
def row(p,e="10000089"):return {"game":"genshin","entityType":"character","entityId":e,"variant":"icon","provider":"enka","sourceId":e,"sourceFile":str(p)}
def test_valid(tmp_path):
 src=tmp_path/"in.png";png(src);m=tmp_path/"manifest.json";root=tmp_path/"public";r=run([row(src)],m,root);doc=json.loads(m.read_text());a=doc["assets"][0]
 assert doc["schemaVersion"]==1 and not r["rejected"] and a["status"]=="ready" and len(a["sha256"])==64
 with Image.open(root/a["path"]) as im:im.verify()
def test_run_upgrades_legacy_list_manifest_without_false_diff(tmp_path):
 src=tmp_path/"in.png";png(src);m=tmp_path/"manifest.json";root=tmp_path/"public"
 first=run([row(src)],m,root);legacy=json.loads(m.read_text())["assets"]
 m.write_text(json.dumps(legacy),encoding="utf8")
 second=run([row(src)],m,root);doc=json.loads(m.read_text())
 assert doc=={"schemaVersion":1,"assets":legacy}
 assert not second["diff"]["added"] and not second["diff"]["changed"] and not second["diff"]["removed"]
def test_partial_batch_preserves_unseen_assets(tmp_path):
 first=tmp_path/"first.png";second=tmp_path/"second.png";png(first);Image.new("RGBA",(2,2),(90,80,70,255)).save(second,"PNG")
 m=tmp_path/"manifest.json";root=tmp_path/"public"
 run([row(first,"10000089"),row(second,"10000090")],m,root)
 update=run([row(first,"10000089")],m,root)
 doc=json.loads(m.read_text())
 assert len(doc["assets"])==2 and len(update["diff"]["removed"])==0
 assert {asset["entityId"] for asset in doc["assets"]}=={"10000089","10000090"}
def test_zero_byte_webp(tmp_path):
 src=tmp_path/"empty.webp";src.write_bytes(b"");m=tmp_path/"m.json";root=tmp_path/"public";r=run([row(src)],m,root)
 assert "zero bytes" in r["rejected"][0]["reason"] and not m.exists() and not root.exists()
def test_garbage_named_webp(tmp_path):
 src=tmp_path/"bad.webp";src.write_bytes(b"not image");assert run([row(src)],tmp_path/"m",tmp_path/"public")["rejected"]
def test_batch_rejection_aborts(tmp_path):
 good=tmp_path/"ok.png";png(good);bad=tmp_path/"bad.webp";bad.write_bytes(b"")
 m=tmp_path/"m.json";root=tmp_path/"public";r=run([row(good),row(bad,"10000090")],m,root)
 assert len(r["assets"])==1 and len(r["rejected"])==1 and not m.exists() and not root.exists()
