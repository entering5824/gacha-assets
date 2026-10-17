from io import BytesIO
from PIL import Image,ImageOps
from urllib.request import Request,urlopen
class InvalidImage(ValueError): pass

def fetch_verified_image(url,timeout=30):
 if not isinstance(url,str) or not url.startswith("https://"): raise InvalidImage("candidate image URL must use HTTPS")
 # HEAD confirms an actual image object; GET is still required for byte decode.
 with urlopen(Request(url,method="HEAD",headers={"User-Agent":"gacha-assets/0.1"}),timeout=timeout) as response:
  content_type=response.headers.get_content_type()
  if not 200 <= response.status < 300 or not content_type.startswith("image/"):
   raise InvalidImage(f"HEAD did not return image/* ({response.status}, {content_type})")
 with urlopen(Request(url,headers={"User-Agent":"gacha-assets/0.1"}),timeout=timeout) as response:
  content_type=response.headers.get_content_type(); data=response.read(20000001)
  if not 200 <= response.status < 300 or not content_type.startswith("image/"):
   raise InvalidImage(f"GET did not return image/* ({response.status}, {content_type})")
 return data
def decode_and_normalize(data,max_bytes=20000000,max_pixels=40000000):
 if not data: raise InvalidImage("empty image (zero bytes)")
 if len(data)>max_bytes: raise InvalidImage("image exceeds byte limit")
 try:
  with Image.open(BytesIO(data)) as im:
   if im.width<1 or im.height<1 or im.width*im.height>max_pixels: raise InvalidImage("invalid/excessive dimensions")
   im.verify()
  with Image.open(BytesIO(data)) as im:
   im.load(); im=ImageOps.exif_transpose(im)
   if im.mode not in ("RGB","RGBA"): im=im.convert("RGBA" if "transparency" in im.info else "RGB")
   out=BytesIO(); im.save(out,"WEBP",lossless=True,method=4); result=out.getvalue()
  with Image.open(BytesIO(result)) as check: check.verify()
  if not result: raise InvalidImage("normalizer returned empty bytes")
  return result
 except InvalidImage: raise
 except Exception as exc: raise InvalidImage(f"image decode/normalize failed: {exc}") from exc
