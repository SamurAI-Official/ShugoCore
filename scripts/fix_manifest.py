#!/usr/bin/env python3
"""Patch the AndroidManifest.xml: ensure ShugoCoreService,
SensorPublisherService, and DeviceSensorBroadcastReceiver are declared.
Idempotent — does nothing if they're already present."""
import sys
from pathlib import Path

MANIFEST = Path("platforms/android/app/src/main/AndroidManifest.xml")

MANIFEST_BLOCK = """        <service
            android:name=".ShugoCoreService"
            android:exported="false"
            android:foregroundServiceType="dataSync" />

        <service
            android:name=".runtime.SensorPublisherService"
            android:exported="false"
            android:foregroundServiceType="dataSync" />

        <receiver
            android:name=".runtime.DeviceSensorBroadcastReceiver"
            android:exported="false">
            <intent-filter>
                <action android:name="com.samurai.shugocore.DEBUG_INJECT_SENSOR" />
            </intent-filter>
        </receiver>

"""

SERVICE_TAG = "<service"
RECEIVER_TAG = "<receiver"


def main():
    text = MANIFEST.read_text(encoding="utf-8")
    if SERVICE_TAG in text and RECEIVER_TAG in text:
        print("manifest already declares services+receiver; no change")
        return 0
    insert_at = text.find("</application>")
    if insert_at == -1:
        print("ERROR: </application> not found in manifest", file=sys.stderr)
        return 2
    new_text = text[:insert_at] + MANIFEST_BLOCK + text[insert_at:]
    MANIFEST.write_text(new_text, encoding="utf-8")
    print("manifest updated with services+receiver")
    return 0


if __name__ == "__main__":
    sys.exit(main())
