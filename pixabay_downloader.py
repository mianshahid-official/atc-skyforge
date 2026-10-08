"""
Pixabay Video Downloader for Aviation & ATC Tower Videos (Strict 9:16 Vertical)
All saved videos are guaranteed to be 9:16 vertical format (1080x1920).
"""

import os
import sys
import time
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import List, Dict, Any

import httpx

ROOT_DIR = Path(__file__).parent.resolve()


def _get_pixabay_key() -> str:
    key = os.environ.get("PIXABAY_API_KEY", "")
    if not key and (ROOT_DIR / "api_keys.txt").exists():
        try:
            for line in (ROOT_DIR / "api_keys.txt").read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("PIXABAY_API_KEY="):
                    return line.split("=", 1)[1].strip()
        except Exception:
            pass
    return key


PIXABAY_API_KEY = _get_pixabay_key()
PLANE_DIR = ROOT_DIR / "plane_videos"
TOWER_DIR = ROOT_DIR / "tower_videos"

PLANE_DIR.mkdir(parents=True, exist_ok=True)
TOWER_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG_CMD = shutil.which("ffmpeg") or "ffmpeg"


def search_pixabay_videos(query: str, per_page: int = 50) -> List[Dict[str, Any]]:
    url = "https://pixabay.com/api/videos/"
    params = {
        "key": PIXABAY_API_KEY,
        "q": query,
        "per_page": per_page,
    }
    try:
        resp = httpx.get(url, params=params, timeout=20.0)
        if resp.status_code == 200:
            hits = resp.json().get("hits", [])
            # Sort: Prioritize native vertical (height > width) first, then duration <= 20s
            def sort_key(h):
                v = h.get("videos", {}).get("medium") or h.get("videos", {}).get("large") or {}
                w, height = v.get("width", 1), v.get("height", 1)
                is_vert = height > w
                dur = h.get("duration", 30)
                dur_penalty = abs(dur - 15) if dur <= 25 else 50
                return (not is_vert, dur_penalty)

            hits.sort(key=sort_key)
            return hits
        else:
            print(f"[Pixabay] Search error {resp.status_code}: {resp.text}", flush=True)
    except Exception as e:
        print(f"[Pixabay] Request exception: {e}", flush=True)
    return []


def download_and_format_9_16(hit: Dict[str, Any], output_path: Path) -> bool:
    """
    Downloads the video from Pixabay, then processes it with FFmpeg to guarantee
    a strict 9:16 vertical video (1080x1920) without black bars.
    """
    videos = hit.get("videos", {})
    vid_meta = videos.get("large") or videos.get("medium") or videos.get("small")
    if not vid_meta or not vid_meta.get("url"):
        return False

    download_url = vid_meta["url"]
    dur = hit.get("duration", 0)
    w_src = vid_meta.get("width", 0)
    h_src = vid_meta.get("height", 0)

    print(f"Downloading ID {hit['id']} ({dur}s, source {w_src}x{h_src})...", flush=True)

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_file:
        tmp_path = Path(tmp_file.name)

    try:
        with httpx.stream("GET", download_url, timeout=60.0) as r:
            if r.status_code != 200:
                print(f"[Error] Failed to download: HTTP {r.status_code}", flush=True)
                return False
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=65536):
                    f.write(chunk)

        # Convert to strict 9:16 (1080x1920) using smart center crop & scale
        print(f"Formatting to strict 9:16 (1080x1920) -> {output_path.name}...", flush=True)
        vf = (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920:(in_w-1080)/2:(in_h-1920)/2,"
            "setsar=1,fps=30"
        )
        cmd = [
            FFMPEG_CMD, "-y",
            "-i", str(tmp_path),
            "-vf", vf,
            "-an",  # strip original video audio
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            str(output_path)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"[Error] FFmpeg format to 9:16 failed: {res.stderr[-200:]}", flush=True)
            if output_path.exists():
                output_path.unlink()
            return False

        print(f"[Success] Saved 9:16 video {output_path.name} ({output_path.stat().st_size / (1024*1024):.2f} MB)", flush=True)
        return True

    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def get_existing_ids(folder: Path, prefix: str) -> set:
    ids = set()
    for f in folder.glob("*.mp4"):
        name = f.stem
        if name.startswith(prefix):
            vid_id = name.replace(prefix, "")
            ids.add(vid_id)
    return ids


def download_initial_batch():
    """Downloads 2 plane videos and 2 tower videos in strict 9:16 format immediately."""
    print("==================================================================", flush=True)
    print("  Fetching Initial Batch: 2 Plane Videos & 2 Tower Videos (9:16)", flush=True)
    print("==================================================================", flush=True)

    plane_queries = ["airport airplane plane", "planes landing airport", "airplane flight sky"]
    tower_queries = ["airport control tower", "airport tower aviation", "control tower airport"]

    # 1. Plane videos
    existing_planes = get_existing_ids(PLANE_DIR, "plane_")
    downloaded_planes = 0

    for query in plane_queries:
        if downloaded_planes >= 2:
            break
        hits = search_pixabay_videos(query)
        for h in hits:
            hid = str(h["id"])
            if hid in existing_planes:
                continue
            tags = h.get("tags", "").lower()
            if any(t in tags for t in ["plane", "airplane", "flight", "aviation", "landing", "airport"]):
                target_file = PLANE_DIR / f"plane_{hid}.mp4"
                if download_and_format_9_16(h, target_file):
                    existing_planes.add(hid)
                    downloaded_planes += 1
                    time.sleep(2.0)
                    if downloaded_planes >= 2:
                        break

    # 2. Tower videos
    existing_towers = get_existing_ids(TOWER_DIR, "tower_")
    downloaded_towers = 0

    for query in tower_queries:
        if downloaded_towers >= 2:
            break
        hits = search_pixabay_videos(query)
        for h in hits:
            hid = str(h["id"])
            if hid in existing_towers:
                continue
            tags = h.get("tags", "").lower()
            if any(t in tags for t in ["tower", "airport", "aviation", "control"]):
                target_file = TOWER_DIR / f"tower_{hid}.mp4"
                if download_and_format_9_16(h, target_file):
                    existing_towers.add(hid)
                    downloaded_towers += 1
                    time.sleep(2.0)
                    if downloaded_towers >= 2:
                        break

    print(f"\n[Completed] Initial Batch Finished: {downloaded_planes} plane videos, {downloaded_towers} tower videos in 9:16 (1080x1920) format.\n", flush=True)


def run_rate_limited_crawler(target_total: int = 10, interval_seconds: int = 60):
    """
    Downloads 1 video every minute alternating between plane and tower
    until 10 plane videos and 10 tower videos are saved in strict 9:16 format.
    """
    print("==================================================================", flush=True)
    print(f"  Pixabay Rate-Limited 9:16 Video Crawler (1 video per {interval_seconds}s)", flush=True)
    print(f"  Target: {target_total} Planes & {target_total} Towers (1080x1920)", flush=True)
    print("==================================================================", flush=True)

    plane_queries = ["airport airplane plane", "planes landing airport", "airplane flight sky", "plane flying"]
    tower_queries = ["airport control tower", "airport tower aviation", "control tower airport", "airport traffic control"]

    while True:
        existing_planes = get_existing_ids(PLANE_DIR, "plane_")
        existing_towers = get_existing_ids(TOWER_DIR, "tower_")

        count_planes = len(existing_planes)
        count_towers = len(existing_towers)

        print(f"\n[Inventory Status] 9:16 Planes: {count_planes}/{target_total} | 9:16 Towers: {count_towers}/{target_total}", flush=True)

        if count_planes >= target_total and count_towers >= target_total:
            print("[Complete] Target of 10 plane videos and 10 tower videos in 9:16 reached! Exiting crawler.", flush=True)
            break

        downloaded = False

        if count_planes < target_total and (count_planes <= count_towers or count_towers >= target_total):
            print(f"[Crawler] Searching 1 new Plane video for 9:16 conversion...", flush=True)
            for q in plane_queries:
                hits = search_pixabay_videos(q)
                candidate = next(
                    (h for h in hits if str(h["id"]) not in existing_planes and any(t in h.get("tags", "").lower() for t in ["plane", "airplane", "flight", "landing", "airport"])),
                    None
                )
                if candidate:
                    target_file = PLANE_DIR / f"plane_{candidate['id']}.mp4"
                    downloaded = download_and_format_9_16(candidate, target_file)
                    break
        elif count_towers < target_total:
            print(f"[Crawler] Searching 1 new Tower video for 9:16 conversion...", flush=True)
            for q in tower_queries:
                hits = search_pixabay_videos(q)
                candidate = next(
                    (h for h in hits if str(h["id"]) not in existing_towers and any(t in h.get("tags", "").lower() for t in ["tower", "airport"])),
                    None
                )
                if candidate:
                    target_file = TOWER_DIR / f"tower_{candidate['id']}.mp4"
                    downloaded = download_and_format_9_16(candidate, target_file)
                    break

        if not downloaded:
            print("[Crawler] No candidates found in this cycle. Retrying next cycle...", flush=True)

        print(f"[Rate-Limiter] Waiting {interval_seconds} seconds before downloading the next video...", flush=True)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    if "--crawler" in sys.argv:
        run_rate_limited_crawler(target_total=10, interval_seconds=60)
    else:
        download_initial_batch()
