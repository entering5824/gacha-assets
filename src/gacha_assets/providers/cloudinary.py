import base64,json,os
from urllib.parse import urlencode
from urllib.request import Request,urlopen
def list_images(cloud_name=None,api_key=None,api_secret=None,prefix=None):
 cloud=cloud_name or os.environ["CLOUDINARY_CLOUD_NAME"];key=api_key or os.environ["CLOUDINARY_API_KEY"];secret=api_secret or os.environ["CLOUDINARY_API_SECRET"]
 auth=base64.b64encode(f"{key}:{secret}".encode()).decode();cursor=None;items=[]
 while True:
  q={"max_results":500}
  if prefix:q["prefix"]=prefix
  if cursor:q["next_cursor"]=cursor
  u=f"https://api.cloudinary.com/v1_1/{cloud}/resources/image/upload?{urlencode(q)}"
  with urlopen(Request(u,headers={"Authorization":"Basic "+auth,"User-Agent":"gacha-assets/0.1"}),timeout=30) as r:d=json.load(r)
  items.extend(d.get("resources",[]));cursor=d.get("next_cursor")
  if not cursor:return items
