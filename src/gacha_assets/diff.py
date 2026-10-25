def diff_manifests(before,after):
 old={x["assetId"]:x for x in before}; new={x["assetId"]:x for x in after}
 return {"added":[new[k] for k in sorted(new.keys()-old.keys())],"changed":[new[k] for k in sorted(new.keys()&old.keys()) if new[k]!=old[k]],"removed":[old[k] for k in sorted(old.keys()-new.keys())]}
