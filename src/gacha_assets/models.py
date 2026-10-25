from dataclasses import dataclass,asdict
from pathlib import PurePosixPath
import re
P=re.compile(r"^[a-z0-9][a-z0-9-]*$")
A=re.compile(r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*\.[a-z0-9][a-z0-9-]*\.[a-z][a-z0-9-]*$")
@dataclass(frozen=True)
class Asset:
 assetId:str; game:str; entityType:str; entityId:str; variant:str; storage:str; path:str; source:dict; sha256:str|None; status:str
 def validate(self):
  if not A.fullmatch(self.assetId): raise ValueError("invalid assetId")
  if not P.fullmatch(self.game) or not P.fullmatch(self.entityId) or not P.fullmatch(self.variant): raise ValueError("invalid identifier")
  if self.entityType not in {"character","weapon","artifact","material","other"}: raise ValueError("invalid entityType")
  if self.storage not in {"supabase","public","bundled"} or self.status not in {"ready","pending","missing","failed"}: raise ValueError("invalid storage/status")
  p=PurePosixPath(self.path)
  if not self.path or p.is_absolute() or ".." in p.parts or "\\" in self.path: raise ValueError("unsafe path")
  if set(self.source)!={"provider","sourceId"} or not all(self.source.values()): raise ValueError("invalid source")
  if self.sha256 is not None and not re.fullmatch(r"[a-f0-9]{64}",self.sha256): raise ValueError("invalid sha256")
  if self.status=="ready" and self.sha256 is None: raise ValueError("ready requires sha256")
 def to_dict(self): self.validate(); return asdict(self)
