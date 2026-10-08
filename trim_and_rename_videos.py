"""
Video Manager:
1. Removes duplicate files (e.g. '(1)' copies).
2. Trims all videos in plane_videos/ and tower_videos/ to a maximum of 10.0 seconds.
3. Renames newly added footage into clean sequential names:
   - plane_01.mp4, plane_02.mp4, ...
   - tower_01.mp4, tower_02.mp4, ...
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

# Unbuffered output for real-time progress
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

ROOT_DIR = Path(__file__).parent.resolve()
PLANE_DIR = ROOT_DIR / "plane_videos"
TOWER_DIR = ROOT_DIR / "tower_videos"
VALID_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
MAX_DURATION = 10.0

def find_tool(tool_name: str) -> str:
    return shutil.which(tool_name) or tool_name

def get_duration(video_path: Path) -> float:
    cmd = [
        find_tool("ffprobe"),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except Exception as e:
        print(f"Error getting duration for {video_path.name}: {e}")
        return 0.0

def trim_video(video_path: Path):
    dur = get_duration(video_path)
    if dur <= MAX_DURATION + 0.1:
        print(f"  [OK] {video_path.name} is {dur:.2f}s (<= 10s), no trim needed.")
        return

    print(f"  [TRIM] {video_path.name} is {dur:.2f}s -> trimming to 10.0s...")
    tmp_path = video_path.with_name(f"temp_trim_{video_path.name}")
    
    cmd = [
        find_tool("ffmpeg"), "-y",
        "-ss", "0",
        "-i", str(video_path),
        "-t", "10.0",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        str(tmp_path)
    ]
    
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        os.replace(tmp_path, video_path)
        new_dur = get_duration(video_path)
        print(f"  -> Successfully trimmed {video_path.name} to {new_dur:.2f}s")
    except Exception as e:
        print(f"  -> Error trimming {video_path.name}: {e}")
        if tmp_path.exists():
            tmp_path.unlink()

def process_directory(directory: Path, prefix: str):
    print("\n" + "=" * 60)
    print(f"Processing Directory: {directory.name} (Prefix: {prefix})")
    print("=" * 60)
    
    # 1. Remove duplicate '(1)' files if exact duplicate or same size exists
    for f in list(directory.glob("*(1)*")):
        print(f"Removing duplicate copy: {f.name}")
        f.unlink()

    # 2. Gather all video files
    video_files = [f for f in directory.iterdir() if f.is_file() and f.suffix.lower() in VALID_EXTS]
    print(f"Found {len(video_files)} video files.")

    # 3. Trim all videos to <= 10.0s first
    print(f"\n--- Checking and Trimming Videos to Max {MAX_DURATION}s ---")
    for f in video_files:
        trim_video(f)

    # 4. Standardize renaming: existing 'plane_XX' / 'tower_XX' vs new raw names
    print(f"\n--- Renaming into sequential format ({prefix}_XX.mp4) ---")
    existing_pattern_files = []
    new_files = []

    for f in sorted(directory.iterdir()):
        if not f.is_file() or f.suffix.lower() not in VALID_EXTS:
            continue
        stem = f.stem.lower()
        if stem.startswith(f"{prefix}_") and stem.replace(f"{prefix}_", "").isdigit():
            num = int(stem.replace(f"{prefix}_", ""))
            existing_pattern_files.append((num, f))
        else:
            new_files.append(f)

    # Sort existing by number
    existing_pattern_files.sort(key=lambda x: x[0])
    highest_num = existing_pattern_files[-1][0] if existing_pattern_files else 0

    # Rename new files starting from highest_num + 1
    current_num = highest_num + 1
    for f in new_files:
        new_name = f"{prefix}_{current_num:02d}.mp4"
        target_path = directory / new_name
        # Ensure target doesn't clash
        while target_path.exists():
            current_num += 1
            new_name = f"{prefix}_{current_num:02d}.mp4"
            target_path = directory / new_name
        
        print(f"  Renaming: {f.name} -> {new_name}")
        os.rename(f, target_path)
        current_num += 1

    final_count = len([f for f in directory.iterdir() if f.is_file() and f.suffix.lower() in VALID_EXTS])
    print(f"\nDone! Directory {directory.name} now has {final_count} standardized, trimmed videos.")

def main():
    print("=" * 60)
    print("Stock Footage Processing & Trimming Pipeline")
    print(f"Rule: No video greater than {MAX_DURATION} seconds.")
    print("=" * 60)

    process_directory(PLANE_DIR, "plane")
    process_directory(TOWER_DIR, "tower")

    print("\n" + "=" * 60)
    print("ALL VIDEOS TRIMMED & STANDARDIZED SUCCESSFULLY!")
    print("=" * 60)

if __name__ == "__main__":
    main()
