"""
Helper script to sync all local stock videos to Modal Volume 'atc-assets'.
Skips files that are already on the cloud volume.
"""

import os
import sys
import subprocess
from pathlib import Path
import modal

ROOT_DIR = Path(__file__).parent.resolve()
PLANE_DIR = ROOT_DIR / "plane_videos"
TOWER_DIR = ROOT_DIR / "tower_videos"

def main():
    print("=" * 60)
    print("Connecting to Modal Volume: atc-assets")
    print("=" * 60)

    vol = modal.Volume.from_name("atc-assets")

    # Fetch existing files in volume
    remote_planes = set()
    remote_towers = set()
    try:
        for entry in vol.listdir("/plane_videos"):
            remote_planes.add(entry.path)
    except Exception:
        pass

    try:
        for entry in vol.listdir("/tower_videos"):
            remote_towers.add(entry.path)
    except Exception:
        pass

    print(f"Already on cloud: {len(remote_planes)} plane videos, {len(remote_towers)} tower videos.")

    # Find local videos
    local_planes = [f for f in PLANE_DIR.glob("*.mp4")]
    local_towers = [f for f in TOWER_DIR.glob("*.mp4")]

    to_upload_planes = [f for f in local_planes if f.name not in remote_planes]
    to_upload_towers = [f for f in local_towers if f.name not in remote_towers]

    print(f"Pending uploads: {len(to_upload_planes)} plane videos, {len(to_upload_towers)} tower videos.")

    all_uploads = [("plane_videos", f) for f in to_upload_planes] + [("tower_videos", f) for f in to_upload_towers]

    for idx, (folder, file_path) in enumerate(all_uploads, start=1):
        rel_remote = f"/{folder}/{file_path.name}"
        size_mb = file_path.stat().st_size / (1024 * 1024)
        print(f"[{idx}/{len(all_uploads)}] Uploading {file_path.name} ({size_mb:.1f} MB) -> {rel_remote}...")
        try:
            cmd = ["modal", "volume", "put", "atc-assets", str(file_path), rel_remote]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            print(f"  ✓ Uploaded successfully.")
        except Exception as e:
            print(f"  ✗ Failed to upload {file_path.name}: {e}")

    print("\nAll assets sync finished!")

if __name__ == "__main__":
    main()
