import io
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
# The manifest is binary XML, so the decoded text can contain bytes a cp1252 console
# cannot encode: printing it raw raises UnicodeEncodeError and the check fails before
# it reports anything at all.
print(text[:1000].encode("ascii", "replace").decode("ascii"))
print("=== parsed manifest (last 1000 chars) ===")
print(text[-1000:].encode("ascii", "replace").decode("ascii"))
names = apk.namelist()
print("\n=== python bundle: governor/personality files ===")
# Chaquopy ships the app's Python as assets/chaquopy/app.imy, so looking only for a
# "python/" prefix (the layout from years ago) lists nothing and reports a good APK as
# empty. Read the archive it is actually in.
bundle_names = []
for candidate in ("assets/chaquopy/app.imy", "assets/chaquopy/app.zip"):
    if candidate in names:
        try:
            with zipfile.ZipFile(io.BytesIO(apk.read(candidate))) as bundle:
                bundle_names = bundle.namelist()
            print(f"  ({candidate}: {len(bundle_names)} entries)")
        except Exception as exc:
            print(f"  ({candidate} unreadable: {exc})")
        break
for n in sorted(bundle_names):
    if "governor" in n or "personality" in n or "model.py" in n or "growth.py" in n:
        print("  ", n)
print("  legacy python/ entries:", sum(1 for n in names if n.startswith("python/")))
print("\n=== python bundle count ===")
print("total python files:", len(bundle_names)
      + sum(1 for n in names if n.startswith("python/")))
apk.close()
apk.close()
