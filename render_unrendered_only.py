"""
Render & Download Unrendered Videos Only
========================================
Specifically designed to detect already-downloaded videos locally, skip them completely,
and only render & download the remaining missing videos (e.g. Scripts 74 to 100).

Features:
- Never restarts from video 34 or re-renders existing videos.
- Renders all missing videos at once on a warm Modal T4 GPU container.
- Downloads the finished MP4 videos one by one into 'rendered_videos/'.

Usage:
  python render_unrendered_only.py               # Renders & downloads remaining 27 videos (74 to 100)
  python render_unrendered_only.py --batch-size 5  # Optional: render & download in chunks of 5
"""

import os
import sys
import time
import argparse
import requests
import re
from pathlib import Path

# Unbuffered UTF-8 console output for Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

DEFAULT_BASE_URL = os.environ.get("MODAL_API_URL", "https://jackharbour799--atc-video-generator-api.modal.run")
ROOT_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = ROOT_DIR / "rendered_videos"


def get_local_downloaded_numbers():
    """Scans rendered_videos/ and extracts all completed script numbers."""
    if not OUTPUT_DIR.exists():
        return set()

    numbers = set()
    for f in OUTPUT_DIR.glob("*.mp4"):
        # Matches script_01, script_34, script_73, etc.
        m = re.search(r"script_(\d+)", f.name)
        if m:
            numbers.add(int(m.group(1)))
    return numbers


def download_file(url: str, dest_path: Path):
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        total_size = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        pct = (downloaded / total_size) * 100
                        print(f"\r  Downloading: {pct:.1f}% ({downloaded / (1024*1024):.1f}/{total_size / (1024*1024):.1f} MB)", end="", flush=True)
        print()


def main():
    parser = argparse.ArgumentParser(description="Render and download only missing/unrendered videos")
    parser.add_argument("--batch-size", type=int, default=10, help="Number of scripts to render per warm GPU batch (default: 10)")
    parser.add_argument("--limit", type=int, default=None, help="Limit total scripts to process in this run (e.g. --limit 10)")
    parser.add_argument("--pilot-speed", type=float, default=1.22, help="Pilot speech speed (default: 1.22)")
    parser.add_argument("--tower-speed", type=float, default=1.28, help="Tower speech speed (default: 1.28)")
    parser.add_argument("--url", type=str, default=DEFAULT_BASE_URL, help="Modal Web API base URL")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Only list missing scripts without triggering render")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Detect already downloaded videos locally
    downloaded_numbers = get_local_downloaded_numbers()
    num_downloaded = len(downloaded_numbers)

    # 2. Fetch available scripts from Modal cloud
    try:
        r = requests.get(f"{base_url}/api/scripts", timeout=30)
        r.raise_for_status()
        cloud_scripts = r.json()
    except Exception as e:
        print(f"❌ Error connecting to Modal endpoint: {e}")
        return

    # Filter to only the scripts that are NOT yet downloaded locally
    missing_scripts = []
    for s in cloud_scripts:
        s_num = s.get("script_number", s["index"] + 1)
        if s_num not in downloaded_numbers:
            missing_scripts.append(s)

    if args.limit is not None and args.limit > 0:
        missing_scripts = missing_scripts[:args.limit]

    num_missing = len(missing_scripts)
    missing_nums = [s.get("script_number") for s in missing_scripts]

    print("=" * 72)
    print("✈️  ATC Video Generator — Render & Download Missing Videos Only")
    print(f"Modal Endpoint:   {BASE_URL}")
    print(f"Output Directory: {OUTPUT_DIR}")
    print(f"Batch Size:       {args.batch_size} per GPU batch")
    if args.limit:
        print(f"Run Limit:        {args.limit} scripts max")
    print("-" * 72)
    if downloaded_numbers:
        print(f"✓ Already downloaded locally:  {num_downloaded} videos (Scripts #{min(downloaded_numbers)} to #{max(downloaded_numbers)})")
    else:
        print(f"✓ Already downloaded locally:  0 videos")

    if not missing_scripts:
        print(f"\n🎉 All {len(cloud_scripts)} scripts are already rendered and downloaded locally!")
        print("Nothing left to render. You're completely done!")
        print("=" * 72)
        return

    print(f"🎯 Queued for rendering in this run: {num_missing} videos (Scripts #{missing_nums[0]} to #{missing_nums[-1]})")
    print("=" * 72)

    if args.dry_run:
        print(f"\n🔍 DRY RUN PREVIEW: The following {num_missing} scripts are queued for rendering:")
        for idx, s in enumerate(missing_scripts, start=1):
            print(f"  {idx:2d}. Script #{s.get('script_number')}: \"{s.get('title')}\"")
        print("\nDry run complete. No renders were triggered.")
        return

    # Determine batch chunking
    batch_size = max(1, args.batch_size)
    chunks = [missing_scripts[i:i + batch_size] for i in range(0, num_missing, batch_size)]

    total_start = time.time()
    all_rendered_files = []

    # =========================================================================
    # PHASE 1: RENDER ALL MISSING VIDEOS ON WARM MODAL GPU
    # =========================================================================
    print(f"\n🔥 PHASE 1: Rendering All {num_missing} Missing Videos On Modal T4 GPU")
    print("   (GPU stays warm continuously — no delays, no sleeps between videos)\n")

    for c_idx, chunk in enumerate(chunks, start=1):
        chunk_indices = [s["index"] for s in chunk]
        chunk_nums = [s.get("script_number") for s in chunk]
        print(f"[GPU Render Batch {c_idx}/{len(chunks)}] Rendering {len(chunk)} scripts: Scripts #{chunk_nums[0]}..#{chunk_nums[-1]}...")

        payload = {
            "script_indices": chunk_indices,
            "pilot_speed": args.pilot_speed,
            "tower_speed": args.tower_speed
        }

        try:
            t0 = time.time()
            res = requests.post(f"{base_url}/api/render_batch", json=payload, timeout=3600)
            res.raise_for_status()
            data = res.json()
            batch_dur = time.time() - t0

            results = data.get("results", [])
            print(f"  ✓ Batch {c_idx} finished in {batch_dur:.1f}s ({batch_dur / max(1, len(results)):.1f}s per video):")
            for r_item in results:
                if r_item.get("success"):
                    fname = r_item.get("filename")
                    s_num = r_item.get("script_number")
                    v_dur = r_item.get("video_duration_sec")
                    f_mb = r_item.get("file_size_mb")
                    print(f"    • Script #{s_num}: {fname} ({v_dur:.1f}s, {f_mb:.1f} MB)")
                    all_rendered_files.append((s_num, fname))
                else:
                    print(f"    ✗ Script #{r_item.get('script_index')}: {r_item.get('error')}")

        except Exception as e:
            print(f"  ❌ Error rendering batch {c_idx}: {e}")

    render_elapsed = time.time() - total_start
    print(f"\n✓ GPU Rendering Phase Complete: {len(all_rendered_files)}/{num_missing} videos rendered in {render_elapsed / 60:.2f} mins.")

    # =========================================================================
    # PHASE 2: DOWNLOAD ALL RENDERED MP4s TO LOCAL FOLDER
    # =========================================================================
    print("\n" + "=" * 72)
    print(f"📥 PHASE 2: Downloading {len(all_rendered_files)} Rendered Videos to Local Folder")
    print(f"   Destination: {OUTPUT_DIR}")
    print("=" * 72)

    download_start = time.time()
    successful_downloads = 0

    for idx, (s_num, fname) in enumerate(all_rendered_files, start=1):
        dest_file = OUTPUT_DIR / fname
        if dest_file.exists():
            print(f"[{idx}/{len(all_rendered_files)}] Script #{s_num}: Already exists locally: {fname}")
            successful_downloads += 1
            continue

        dl_url = f"{base_url}/api/download/{fname}"
        print(f"[{idx}/{len(all_rendered_files)}] Script #{s_num}: Downloading {fname}...")
        try:
            download_file(dl_url, dest_file)
            print(f"  ✓ Saved to: {dest_file.name}")
            successful_downloads += 1
        except Exception as e:
            print(f"  ✗ Failed to download {fname}: {e}")

    download_elapsed = time.time() - download_start
    total_elapsed = time.time() - total_start

    final_local_count = len(get_local_downloaded_numbers())

    print("\n" + "=" * 72)
    print("🎉 ALL PROCESSING COMPLETED SUCCESSFULLY!")
    print(f"  Videos Rendered on Cloud:    {len(all_rendered_files)}")
    print(f"  Videos Downloaded Locally:   {successful_downloads}")
    print(f"  Total Local Video Library:   {final_local_count}/100 videos complete")
    print(f"  Total Time:                  {total_elapsed / 60:.2f} minutes")
    print(f"  Local Folder:                {OUTPUT_DIR}")
    print("=" * 72)


if __name__ == "__main__":
    main()
