import re
from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

def parsem(text: str):
    # AndroidManifest.xml is binary xml. Try to parse as XML after stripping bogus bytes.
    import html
    text = html.unescape(text)
    # decode binary xml entity-encoded chars: &#...; and &#x...;
    text = re.sub(r'&#x([0-9a-fA-F]+);', lambda m: chr(int(m.group(1),16)), text)
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)
    return text

apk_path = Path("platforms/android/app/build/outputs/apk/debug/app-debug.apk")
apk = zipfile.ZipFile(apk_path)
data = apk.read("AndroidManifest.xml")
text = data.decode("utf-8", errors="replace")
text = parsem(text)
print("=== parsed manifest (first 1000 chars) ===")
print(text[:1000])
print("=== parsed manifest (last 1000 chars) ===")
print(text[-1000:])
names = apk.namelist()
print("\n=== python bundle: governor/personality files ===")
for n in sorted(names):
    if "governor" in n or "personality" in n or "model.py" in n or "growth.py" in n:
        print("  ", n)
print("\n=== python bundle count ===")
print("total python files:", sum(1 for n in names if n.startswith("python/")))
apk.close()
apk.close()
