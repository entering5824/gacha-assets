import json
from urllib.request import Request,urlopen
def load_catalog(url):
 if not url.startswith("https://"):raise ValueError("catalog URL must use HTTPS")
 with urlopen(Request(url,headers={"User-Agent":"gacha-assets/0.1"}),timeout=30) as r:d=json.load(r)
 if isinstance(d,list):return d
 if isinstance(d,dict) and isinstance(d.get("assets"),list):return d["assets"]
 raise ValueError("expected a JSON list or assets list")
