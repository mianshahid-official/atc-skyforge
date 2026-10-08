"""
Render & Download All Scripts from Modal Cloud (Optimized 2-Phase Batch Workflow)
================================================================================
Phase 1: Renders all queued videos consecutively on a warm Modal T4 GPU container.
         (Eliminates container cold-starts, sleeps, and timeout costs).
Phase 2: Downloads all rendered MP4 videos one by one to local 'rendered_videos/'.
         (Free lightweight download phase, GPU does not stay awake).

Usage:
  python render_and_download_all.py                    # Renders all queued scripts (34 to 100) & downloads
  python render_and_download_all.py --batch-size 5     # Renders in chunks of 5
  python render_and_download_all.py --script 34        # Renders only Script 34
  python render_and_download_all.py --download-only    # Downloads any finished videos without triggering renders
"""

import os
import sys
import time
import argparse
import requests
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
    parser = argparse.ArgumentParser(description="Render and download ATC videos from Modal in 2 phases")
    parser.add_argument("--start", type=int, default=0, help="Starting script index (0-based, default: 0)")
    parser.add_argument("--end", type=int, default=None, help="Ending script index (exclusive, default: all)")
    parser.add_argument("--script", type=int, default=None, help="Render only a specific script number or index (e.g. 34)")
    parser.add_argument("--batch-size", type=int, default=10, help="Number of scripts to render per warm GPU batch (default: 10)")
    parser.add_argument("--pilot-speed", type=float, default=1.22, help="Pilot speech speed (default: 1.22)")
    parser.add_argument("--tower-speed", type=float, default=1.28, help="Tower speech speed (default: 1.28)")
    parser.add_argument("--skip-existing", action="store_true", default=True, help="Skip scripts already downloaded locally (default: True)")
    parser.add_argument("--no-skip", dest="skip_existing", action="store_false", help="Force re-render even if local file exists")
    parser.add_argument("--url", type=str, default=DEFAULT_BASE_URL, help="Modal Web API base URL")
    parser.add_argument("--download-only", action="store_true", default=False, help="Only download already rendered videos without rendering")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("✈️  ATC Video Generator — High-Efficiency 2-Phase Batch Workflow")
    print(f"Modal Endpoint:   {base_url}")
    print(f"Output Directory: {OUTPUT_DIR}")
    print(f"Speech Speeds:    Pilot={args.pilot_speed}x | Tower={args.tower_speed}x")
    print("=" * 70)

    # 1. Fetch available scripts from Modal
    try:
        r = requests.get(f"{base_url}/api/scripts", timeout=30)
        r.raise_for_status()
        scripts = r.json()
    except Exception as e:
        print(f"Error fetching scripts list from Modal: {e}")
        return

    total_scripts = len(scripts)
    print(f"\nFound {total_scripts} scripts on cloud volume (Scripts: #{scripts[0].get('script_number', 1)} to #{scripts[-1].get('script_number', total_scripts)}).")

    # Map target indices
    if args.script is not None:
        target_indices = []
        for s in scripts:
            if s.get("script_number") == args.script or s.get("index") == args.script:
                target_indices.append(s["index"])
        if not target_indices:
            print(f"Script #{args.script} not found in available scripts.")
            return
    else:
        end_idx = args.end if args.end is not None else total_scripts
        target_indices = list(range(args.start, min(end_idx, total_scripts)))

    # Filter out scripts that already exist locally
    pending_indices = []
    for idx in target_indices:
        s_info = scripts[idx]
        s_num = s_info.get("script_number", idx + 1)
        matching_local = list(OUTPUT_DIR.glob(f"script_{s_num:02d}_*.mp4")) or list(OUTPUT_DIR.glob(f"script_{s_num}_*.mp4"))
        if args.skip_existing and matching_local:
            print(f"  ⏭️ Already exists locally: {matching_local[0].name} — Skipping.")
        else:
            pending_indices.append(idx)

    print(f"\nScripts queued for GPU rendering: {len(pending_indices)} out of {len(target_indices)} requested.\n")

    rendered_files_to_download = []

    # =========================================================================
    # PHASE 1: BATCH CLOUD RENDERING ON WARM GPU
    # =========================================================================
    if not args.download_only and pending_indices:
        print("=" * 70)
        print("🔥 PHASE 1: Rendering All Queued Videos on Warm Modal T4 GPU")
        print("   (GPU stays warm continuously — no idle spin-downs or delays)")
        print("=" * 70)

        # Chunk into warm batches
        batch_size = max(1, args.batch_size)
        chunks = [pending_indices[i:i + batch_size] for i in range(0, len(pending_indices), batch_size)]

        phase1_start = time.time()
        for c_idx, chunk in enumerate(chunks, start=1):
            chunk_nums = [scripts[i].get("script_number", i + 1) for i in chunk]
            print(f"\n[Batch {c_idx}/{len(chunks)}] Dispatching {len(chunk)} scripts on Modal GPU: Scripts {chunk_nums}...")

            payload = {
                "script_indices": chunk,
                "pilot_speed": args.pilot_speed,
                "tower_speed": args.tower_speed
            }

            try:
                t0 = time.time()
                res = requests.post(f"{base_url}/api/render_batch", json=payload, timeout=3600)
                res.raise_for_status()
                data = res.json()
                batch_sec = time.time() - t0

                results = data.get("results", [])
                print(f"  ✓ Batch {c_idx} finished in {batch_sec:.1f}s ({batch_sec / max(1, len(results)):.1f}s per video):")
                for r_item in results:
                    if r_item.get("success"):
                        fname = r_item.get("filename")
                        s_num = r_item.get("script_number")
                        dur = r_item.get("video_duration_sec")
                        sz = r_item.get("file_size_mb")
                        print(f"    • Script #{s_num}: {fname} ({dur:.1f}s, {sz:.1f} MB)")
                        rendered_files_to_download.append(fname)
                    else:
                        print(f"    ✗ Script index {r_item.get('script_index')}: {r_item.get('error')}")

            except Exception as e:
                print(f"  ❌ Error in batch {c_idx}: {e}")

        phase1_time = time.time() - phase1_start
        print(f"\n✓ Phase 1 Complete! {len(rendered_files_to_download)} videos rendered on Modal in {phase1_time/60:.2f} minutes.")

    # =========================================================================
    # PHASE 2: BATCH DOWNLOAD (LIGHTWEIGHT CPU / VOLUME STREAMING)
    # =========================================================================
    print("\n" + "=" * 70)
    print("📥 PHASE 2: Downloading Rendered Videos to Local Folder")
    print(f"   Destination: {OUTPUT_DIR}")
    print("=" * 70)

    # Fetch all rendered videos on volume
    try:
        r_vids = requests.get(f"{base_url}/api/videos", timeout=30).json()
    except Exception as e:
        print(f"Error listing rendered videos: {e}")
        r_vids = []

    cloud_video_names = {v["filename"]: v for v in r_vids}

    # Determine files to download
    files_to_get = []
    if rendered_files_to_download:
        files_to_get = rendered_files_to_download
    else:
        # If download-only or fallback, check target scripts
        for idx in target_indices:
            s_num = scripts[idx].get("script_number", idx + 1)
            for fname in cloud_video_names.keys():
                if fname.startswith(f"script_{s_num:02d}_") or fname.startswith(f"script_{s_num}_"):
                    if fname not in files_to_get:
                        files_to_get.append(fname)

    downloaded_count = 0
    phase2_start = time.time()

    for idx, fname in enumerate(files_to_get, start=1):
        dest_file = OUTPUT_DIR / fname
        if dest_file.exists() and args.skip_existing:
            print(f"[{idx}/{len(files_to_get)}] Already downloaded: {fname}")
            downloaded_count += 1
            continue

        dl_url = f"{base_url}/api/download/{fname}"
        print(f"[{idx}/{len(files_to_get)}] Downloading {fname}...")
        try:
            download_file(dl_url, dest_file)
            print(f"  ✓ Saved: {dest_file.name}")
            downloaded_count += 1
        except Exception as e:
            print(f"  ✗ Failed to download {fname}: {e}")

    phase2_time = time.time() - phase2_start
    print("\n" + "=" * 70)
    print("🎉 ALL TASKS FINISHED!")
    print(f"  Videos Downloaded: {downloaded_count}/{len(files_to_get)}")
    print(f"  Total Download Time: {phase2_time:.1f}s")
    print(f"  Local Folder: {OUTPUT_DIR}")
    print("=" * 70)


if __name__ == "__main__":
    main()
