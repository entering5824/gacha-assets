import json,os
from urllib.request import Request,urlopen
def list_tree(owner,repo,ref,token=None):
 h={"Accept":"application/vnd.github+json","User-Agent":"gacha-assets/0.1"};token=token or os.getenv("GITHUB_TOKEN")
 if token:h["Authorization"]="Bearer "+token
 u=f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}?recursive=1"
 with urlopen(Request(u,headers=h),timeout=30) as r:d=json.load(r)
 if d.get("truncated"):raise RuntimeError("tree truncated; traverse bounded subtrees")
 return [x for x in d.get("tree",[]) if x.get("type")=="blob"]
