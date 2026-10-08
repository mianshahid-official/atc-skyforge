"""
ATC SkyForge — Automated Script Deployer & Cloud Batch Renderer
==============================================================
Replaces dialogue scripts on Modal cloud volume (/scripts_emotional.json)
and renders all queued scripts in high-efficiency warm GPU batches of X,
automatically downloading finished 9:16 vertical MP4s into rendered_videos/.

Usage:
    python render_new_scripts.py scripts_201_300.json
    python render_new_scripts.py scripts_201_300.json --batch-size 10
    python render_new_scripts.py                     (Interactive mode)
"""

import os
import sys
import re
import json
import time
import shutil
import argparse
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Set, Optional

# UTF-8 unbuffered stream for Windows console
if sys.platform.startswith("win"):
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

ROOT_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = ROOT_DIR / "rendered_videos"
DEFAULT_BASE_URL = os.environ.get("MODAL_API_URL", "https://jackharbour799--atc-video-generator-api.modal.run")
MODAL_VOLUME_NAME = "atc-assets"
MODAL_REMOTE_SCRIPT_PATH = "/scripts_emotional.json"


# ==============================================================================
# Helper Utilities
# ==============================================================================
def find_available_script_files() -> List[Path]:
    """Finds candidate json script files in current directory."""
    candidates = []
    for f in ROOT_DIR.glob("*.json"):
        if f.name in {"package.json", "tsconfig.json"}:
            continue
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict) and "dialogue" in data[0]:
                    candidates.append(f)
        except Exception:
            pass
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def validate_script_file(file_path: Path) -> List[Dict[str, Any]]:
    """Loads and validates that the JSON file contains properly formatted scripts."""
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        data = data.get("scripts") or data.get("conversations") or list(data.values())[0]

    if not isinstance(data, list) or len(data) == 0:
        raise ValueError(f"{file_path.name} must contain a non-empty array of script objects.")

    for idx, s in enumerate(data, start=1):
        if not isinstance(s, dict):
            raise ValueError(f"Script #{idx} in {file_path.name} is not a valid JSON object.")
        if "title" not in s:
            raise ValueError(f"Script #{idx} is missing required 'title' field.")
        if "dialogue" not in s or not isinstance(s["dialogue"], list):
            raise ValueError(f"Script #{idx} ('{s.get('title')}') is missing 'dialogue' array.")

    return data


def upload_scripts_to_modal(local_path: Path) -> bool:
    """Uploads the scripts file to Modal persistent volume at /scripts_emotional.json."""
    print(f"\n[1/3] Uploading '{local_path.name}' to Modal volume '{MODAL_VOLUME_NAME}'...")

    # Method 1: Try modal CLI
    try:
        cmd = ["modal", "volume", "put", "-f", MODAL_VOLUME_NAME, str(local_path), MODAL_REMOTE_SCRIPT_PATH]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        print(f"  ✓ Modal CLI upload successful: {local_path.name} -> {MODAL_REMOTE_SCRIPT_PATH}")
        return True
    except Exception as e:
        print(f"  [Notice] Modal CLI upload attempt failed ({e}). Trying Modal Python SDK...")

    # Method 2: Fallback to Modal Python SDK
    try:
        import modal
        vol = modal.Volume.from_name(MODAL_VOLUME_NAME)
        with open(local_path, "rb") as f:
            content = f.read()
        vol.write_file(MODAL_REMOTE_SCRIPT_PATH, content)
        vol.commit()
        print(f"  ✓ Modal Python SDK upload successful: {len(content)} bytes committed.")
        return True
    except Exception as e2:
        print(f"  ❌ Failed to upload scripts to Modal volume: {e2}")
        return False


def get_local_downloaded_numbers() -> Set[int]:
    """Scans rendered_videos/ and extracts all completed script numbers."""
    if not OUTPUT_DIR.exists():
        return set()

    numbers = set()
    for f in OUTPUT_DIR.glob("*.mp4"):
        if f.stat().st_size < 500 * 1024:
            continue
        m = re.search(r"script_(\d+)", f.name)
        if m:
            numbers.add(int(m.group(1)))
    return numbers


def download_file(url: str, dest_path: Path) -> bool:
    """Downloads a file with streamed progress indication."""
    import requests
    temp_path = dest_path.with_suffix(".tmp")
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total_size = int(r.headers.get("content-length", 0))
            downloaded = 0
            with open(temp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            pct = (downloaded / total_size) * 100
                            mb_done = downloaded / (1024 * 1024)
                            mb_total = total_size / (1024 * 1024)
                            print(f"\r  Streaming MP4: {pct:.1f}% ({mb_done:.1f}/{mb_total:.1f} MB)", end="", flush=True)
            print()
        if temp_path.exists():
            temp_path.replace(dest_path)
            return True
        return False
    except Exception as e:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        raise e


# ==============================================================================
# Main Orchestrator
# ==============================================================================
def main():
    import requests

    parser = argparse.ArgumentParser(
        description="Replace Modal scripts file and render all new videos in batches of X"
    )
    parser.add_argument("scripts_file", nargs="?", default=None, help="Path to new scripts JSON file (e.g. scripts_201_300.json)")
    parser.add_argument("--batch-size", "-b", type=int, default=10, help="Number of scripts to render per warm GPU batch (default: 10)")
    parser.add_argument("--pilot-speed", type=float, default=1.22, help="Pilot speech velocity multiplier (default: 1.22)")
    parser.add_argument("--tower-speed", type=float, default=1.28, help="Tower speech velocity multiplier (default: 1.28)")
    parser.add_argument("--limit", type=int, default=None, help="Optionally limit number of scripts to process in this run")
    parser.add_argument("--url", type=str, default=DEFAULT_BASE_URL, help="Modal Web API Base URL")
    parser.add_argument("--skip-upload", action="store_true", default=False, help="Skip uploading if file is already on Modal volume")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Only validate and preview without rendering")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 76)
    print("✈️  ATC SKYFORGE — SCRIPT DEPLOYER & GPU BATCH RENDERER")
    print("=" * 76)

    # 1. Resolve Script File (CLI argument or interactive prompt)
    script_path: Optional[Path] = None
    if args.scripts_file:
        candidate = Path(args.scripts_file)
        if not candidate.is_absolute():
            candidate = ROOT_DIR / candidate
        if candidate.exists():
            script_path = candidate
        else:
            print(f"❌ Error: Specified file not found: {candidate}")
            sys.exit(1)
    else:
        # Interactive selection
        candidates = find_available_script_files()
        print("\nAvailable script files detected:")
        default_idx = 0
        preferred_candidates = [c for c in candidates if "201" in c.name or "101" in c.name or "new" in c.name]
        default_file = preferred_candidates[0] if preferred_candidates else (candidates[0] if candidates else None)

        for i, c in enumerate(candidates, start=1):
            marker = " (Recommended Default)" if c == default_file else ""
            print(f"  [{i}] {c.name}{marker}")

        prompt_default = default_file.name if default_file else "scripts_201_300.json"
        print(f"\nEnter scripts file name/path [Default: {prompt_default}]: ", end="", flush=True)
        user_input = sys.stdin.readline().strip()

        if not user_input:
            chosen_name = prompt_default
        elif user_input.isdigit() and 1 <= int(user_input) <= len(candidates):
            chosen_name = candidates[int(user_input) - 1].name
        else:
            chosen_name = user_input

        chosen_path = Path(chosen_name)
        if not chosen_path.is_absolute():
            chosen_path = ROOT_DIR / chosen_path

        if not chosen_path.exists():
            print(f"❌ Error: File not found: {chosen_path}")
            sys.exit(1)

        script_path = chosen_path

        # Interactive batch size if not specified via CLI
        print(f"Enter batch size X (scripts per warm GPU batch) [Default: {args.batch_size}]: ", end="", flush=True)
        bs_input = sys.stdin.readline().strip()
        if bs_input.isdigit() and int(bs_input) > 0:
            args.batch_size = int(bs_input)

    # 2. Validate Local File Content
    try:
        scripts_data = validate_script_file(script_path)
    except Exception as e:
        print(f"\n❌ Validation Error in '{script_path.name}': {e}")
        sys.exit(1)

    total_local_scripts = len(scripts_data)
    first_title = scripts_data[0].get("title", "Unknown")
    last_title = scripts_data[-1].get("title", "Unknown")
    m_first = re.match(r"^(\d+)", first_title)
    m_last = re.match(r"^(\d+)", last_title)
    range_str = f"#{m_first.group(1)} to #{m_last.group(1)}" if (m_first and m_last) else f"1 to {total_local_scripts}"

    print(f"\n✓ Script File Validated: {script_path.name}")
    print(f"  Total Scripts:       {total_local_scripts} scripts ({range_str})")
    print(f"  First Script:        '{first_title}'")
    print(f"  Last Script:         '{last_title}'")
    print(f"  Batch Size (X):      {args.batch_size} scripts per GPU container run")
    print(f"  Modal Target Vol:    {MODAL_VOLUME_NAME}:{MODAL_REMOTE_SCRIPT_PATH}")

    # 3. Replace File on Modal Volume
    if not args.skip_upload and not args.dry_run:
        success = upload_scripts_to_modal(script_path)
        if not success:
            print("❌ Cannot proceed without updating cloud scripts file.")
            sys.exit(1)

        # Also update local scripts_emotional.json as copy
        local_emotional = ROOT_DIR / "scripts_emotional.json"
        try:
            shutil.copyfile(script_path, local_emotional)
            print(f"  ✓ Updated local copy: {local_emotional.name}")
        except Exception:
            pass

    # 4. Verify Cloud Endpoint & Active Scripts
    if args.dry_run:
        cloud_scripts = []
        for i, s in enumerate(scripts_data):
            title = s.get("title", f"Script_{i+1}")
            m = re.match(r"^(\d+)", title)
            s_num = int(m.group(1)) if m else i + 1
            cloud_scripts.append({
                "index": i,
                "script_number": s_num,
                "title": title
            })
        print(f"  [Dry Run] Simulating with '{script_path.name}' ({len(cloud_scripts)} scripts)")
    else:
        try:
            r = requests.get(f"{base_url}/api/scripts", timeout=30)
            r.raise_for_status()
            cloud_scripts = r.json()
        except Exception as e:
            print(f"❌ Failed to communicate with Modal API: {e}")
            print("Please check that the Modal app is deployed and running.")
            sys.exit(1)

        num_cloud = len(cloud_scripts)
        cloud_first = cloud_scripts[0]["title"] if cloud_scripts else "None"
        cloud_last = cloud_scripts[-1]["title"] if cloud_scripts else "None"
        print(f"  ✓ Modal Volume Active: {num_cloud} scripts online ('{cloud_first}' ... '{cloud_last}')")

    # 5. Determine Missing / Unrendered Videos
    downloaded_numbers = get_local_downloaded_numbers()
    num_downloaded = len(downloaded_numbers)

    pending_scripts = []
    for s in cloud_scripts:
        s_num = s.get("script_number", s["index"] + 1)
        if s_num not in downloaded_numbers:
            pending_scripts.append(s)

    if args.limit is not None and args.limit > 0:
        pending_scripts = pending_scripts[:args.limit]

    total_pending = len(pending_scripts)

    print("\n" + "-" * 76)
    print(f"📊 Pipeline Status:")
    print(f"  Locally Complete:    {num_downloaded} videos in {OUTPUT_DIR.name}/")
    print(f"  Queued to Render:    {total_pending} videos")
    print(f"  Batch Size (X):      {args.batch_size} videos per GPU container batch")
    print(f"  Speech Speeds:       Pilot={args.pilot_speed}x | Tower={args.tower_speed}x")
    print("-" * 76)

    if total_pending == 0:
        print(f"\n🎉 All {num_cloud} scripts are already rendered and downloaded locally in '{OUTPUT_DIR}'!")
        print("Nothing left to render. Complete!")
        print("=" * 76)
        return

    if args.dry_run:
        print(f"\n🔍 DRY RUN: The following {total_pending} scripts are queued for rendering:")
        for idx, s in enumerate(pending_scripts, start=1):
            print(f"  {idx:2d}. Script #{s.get('script_number')}: \"{s.get('title')}\"")
        print("\nDry run finished. No GPU renders were executed.")
        return

    # 6. Chunk into Batches of X
    batch_size = max(1, args.batch_size)
    batches = [pending_scripts[i:i + batch_size] for i in range(0, total_pending, batch_size)]

    print(f"\n[3/3] Commencing Cloud Rendering: {len(batches)} Batches of {batch_size} scripts on Modal GPU...")
    print("   (Each batch runs in a warm container without spin-down cold starts)\n")

    overall_start_time = time.time()
    total_rendered_files = []

    # =========================================================================
    # PHASE 1: WARM GPU BATCH RENDERING
    # =========================================================================
    print("=" * 76)
    print(f"🔥 PHASE 1: Rendering All {total_pending} Videos on Warm Modal T4 GPU")
    print("=" * 76)

    for b_idx, batch in enumerate(batches, start=1):
        batch_indices = [s["index"] for s in batch]
        batch_nums = [s.get("script_number") for s in batch]
        num_in_batch = len(batch)
        print(f"\n🚀 [Batch {b_idx}/{len(batches)}] Rendering {num_in_batch} scripts: Scripts #{batch_nums[0]} to #{batch_nums[-1]}...")

        payload = {
            "script_indices": batch_indices,
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
            print(f"  ✓ Batch {b_idx} completed in {batch_dur:.1f}s ({batch_dur / max(1, len(results)):.1f}s/video average):")
            for item in results:
                if item.get("success"):
                    fname = item.get("filename")
                    s_num = item.get("script_number")
                    v_dur = item.get("video_duration_sec")
                    f_mb = item.get("file_size_mb")
                    print(f"    • Script #{s_num}: {fname} ({v_dur:.1f}s, {f_mb:.1f} MB)")
                    total_rendered_files.append((s_num, fname))
                else:
                    print(f"    ✗ Script index {item.get('script_index')}: {item.get('error')}")

        except Exception as e:
            print(f"  ❌ Error rendering Batch {b_idx}: {e}")

    phase1_elapsed = time.time() - overall_start_time
    print(f"\n✓ GPU Rendering Phase Complete: {len(total_rendered_files)}/{total_pending} videos rendered in {phase1_elapsed/60:.2f} minutes.")

    # =========================================================================
    # PHASE 2: BATCH DOWNLOAD TO LOCAL FOLDER
    # =========================================================================
    print("\n" + "=" * 76)
    print(f"📥 PHASE 2: Downloading {len(total_rendered_files)} Videos into '{OUTPUT_DIR.name}/'")
    print(f"   Destination: {OUTPUT_DIR}")
    print("=" * 76)

    dl_start = time.time()
    successful_downloads = 0

    for idx, (s_num, fname) in enumerate(total_rendered_files, start=1):
        dest_file = OUTPUT_DIR / fname
        if dest_file.exists() and dest_file.stat().st_size > 500 * 1024:
            print(f"[{idx}/{len(total_rendered_files)}] Script #{s_num}: Already exists locally: {fname}")
            successful_downloads += 1
            continue

        dl_url = f"{base_url}/api/download/{fname}"
        print(f"[{idx}/{len(total_rendered_files)}] Script #{s_num}: Downloading {fname}...")
        try:
            download_file(dl_url, dest_file)
            f_size_mb = dest_file.stat().st_size / (1024 * 1024)
            print(f"  ✓ Saved: {dest_file.name} ({f_size_mb:.1f} MB)")
            successful_downloads += 1
        except Exception as e:
            print(f"  ✗ Failed to download {fname}: {e}")

    dl_elapsed = time.time() - dl_start
    total_elapsed = time.time() - overall_start_time
    total_local = len(get_local_downloaded_numbers())

    print("\n" + "=" * 76)
    print("🎉 ALL PROCESSING COMPLETED SUCCESSFULLY!")
    print(f"  Source Scripts File:         {script_path.name}")
    print(f"  Videos Rendered on Modal:    {len(total_rendered_files)}/{total_pending}")
    print(f"  Videos Downloaded Locally:   {successful_downloads}")
    print(f"  Total Video Library:         {total_local} complete videos in '{OUTPUT_DIR.name}/'")
    print(f"  Total Execution Time:        {total_elapsed / 60:.2f} minutes")
    print(f"  Output Directory:            {OUTPUT_DIR}")
    print("=" * 76)


if __name__ == "__main__":
    main()
