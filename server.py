"""
ATC SkyForge - Aviation Emergency Video Generator UI Server
============================================================
FastAPI backend providing real-time telemetry, settings adjustment,
single sample testing, batch generation, and in-browser video playback.
"""

import os
import sys

if sys.platform.startswith("win"):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import json
import time
import glob
import asyncio
import threading
from pathlib import Path
from collections import deque
from typing import Optional, Dict, Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Body
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from atc_generator import ATCPipeline, PLANE_VIDEOS_DIR, TOWER_VIDEOS_DIR, OUTPUT_DIR, load_scripts_from_json

ROOT_DIR = Path(__file__).parent.resolve()
WEB_DIR = ROOT_DIR / "web"
SCRIPTS_PATH = ROOT_DIR / "scripts_emotional.json" if (ROOT_DIR / "scripts_emotional.json").exists() else ROOT_DIR / "scripts.json"
RENDERED_DIR = ROOT_DIR / "rendered_videos"
RENDERED_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ATC SkyForge UI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared Thread-Safe Engine State
engine_lock = threading.Lock()
stop_requested = threading.Event()

current_settings: Dict[str, float] = {
    "pilot_speed": 1.26,
    "tower_speed": 1.26,
    "conversation_speed": 1.26,
    "bg_volume": 0.35,
    "voice_volume": 1.6,
    "opening_delay": 1.0,
}

engine_state: Dict[str, Any] = {
    "is_running": False,
    "mode": "idle",  # "idle" | "test" | "batch"
    "current_index": 0,
    "total_items": 0,
    "current_title": "",
    "step": "idle",
    "step_message": "Ready to launch. Click 'Test Run Sample Video' or 'Render All Videos'.",
    "video_percent": 0,
    "overall_percent": 0,
    "last_rendered": "",
    "error": "",
    "logs": deque(maxlen=250),
}


def log_message(msg: str):
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    print(entry, flush=True)
    with engine_lock:
        engine_state["logs"].append(entry)


# ==============================================================================
# Background Worker Threads
# ==============================================================================
def run_single_test_worker(script_idx: int = 0):
    global engine_state
    pipeline = ATCPipeline()

    try:
        scripts = load_scripts_from_json(str(SCRIPTS_PATH))
    except Exception as e:
        log_message(f"Error loading scripts.json: {e}")
        with engine_lock:
            engine_state["is_running"] = False
            engine_state["error"] = str(e)
        return

    if not scripts:
        log_message("No scripts found in scripts.json!")
        with engine_lock:
            engine_state["is_running"] = False
        return

    if script_idx < 0:
        script_idx = 0
        for idx, (t, _) in enumerate(scripts):
            v_path = RENDERED_DIR / f"{t}.mp4"
            if not (v_path.exists() and v_path.stat().st_size > 500 * 1024):
                script_idx = idx
                break

    safe_idx = max(0, min(script_idx, len(scripts) - 1))
    title, script_content = scripts[safe_idx]

    with engine_lock:
        engine_state["is_running"] = True
        engine_state["mode"] = "test"
        engine_state["current_index"] = safe_idx + 1
        engine_state["total_items"] = 1
        engine_state["current_title"] = title
        engine_state["video_percent"] = 5
        engine_state["overall_percent"] = 5
        engine_state["step"] = "init"
        engine_state["step_message"] = f"Initializing test run for: {title}"
        engine_state["error"] = ""

    log_message(f"Starting TEST RUN for [{safe_idx + 1}/{len(scripts)}] '{title}'...")

    out_file = str(RENDERED_DIR / f"{title}.mp4")

    def progress_hook(info: Dict[str, Any]):
        with engine_lock:
            step = info.get("step", "")
            pct = info.get("percent", 0)
            msg = info.get("message", "")
            engine_state["step"] = step
            engine_state["video_percent"] = pct
            engine_state["overall_percent"] = pct
            engine_state["step_message"] = msg
            if msg:
                engine_state["logs"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    try:
        with engine_lock:
            active_cfg = dict(current_settings)

        out_path = pipeline.run(
            script_text=script_content,
            output_video_path=out_file,
            pilot_speed=active_cfg["pilot_speed"],
            tower_speed=active_cfg["tower_speed"],
            conversation_speed=active_cfg["conversation_speed"],
            bg_volume=active_cfg["bg_volume"],
            voice_volume=active_cfg["voice_volume"],
            opening_delay=active_cfg["opening_delay"],
            progress_callback=progress_hook,
            stop_event=stop_requested,
        )

        log_message(f"Test run complete! Video rendered: {os.path.basename(out_path)}")
        with engine_lock:
            engine_state["last_rendered"] = f"{title}.mp4"
            engine_state["step"] = "done"
            engine_state["step_message"] = f"Test Video Finished: {title}.mp4! Preview ready."
            engine_state["video_percent"] = 100
            engine_state["overall_percent"] = 100

    except InterruptedError:
        log_message(f"[CANCELLED] Test run was stopped by user. Cleaning up incomplete {title}.mp4...")
        if os.path.exists(out_file):
            try:
                os.remove(out_file)
                log_message(f"[CLEANUP] Deleted incomplete file: {title}.mp4")
            except Exception:
                pass
        with engine_lock:
            engine_state["step"] = "cancelled"
            engine_state["step_message"] = f"Test run stopped. Incomplete {title}.mp4 removed."

    except Exception as e:
        if stop_requested.is_set():
            log_message(f"[CANCELLED] Test run interrupted by user.")
            if os.path.exists(out_file):
                try:
                    os.remove(out_file)
                except Exception:
                    pass
        else:
            log_message(f"Test run failed: {e}")
            with engine_lock:
                engine_state["error"] = str(e)
                engine_state["step"] = "error"
                engine_state["step_message"] = f"Error: {e}"

    finally:
        with engine_lock:
            engine_state["is_running"] = False
            engine_state["mode"] = "idle"


def run_batch_worker():
    global engine_state
    pipeline = ATCPipeline()
    stop_requested.clear()

    try:
        scripts = load_scripts_from_json(str(SCRIPTS_PATH))
    except Exception as e:
        log_message(f"Failed to load scripts: {e}")
        with engine_lock:
            engine_state["is_running"] = False
            engine_state["error"] = str(e)
        return

    total = len(scripts)
    with engine_lock:
        engine_state["is_running"] = True
        engine_state["mode"] = "batch"
        engine_state["total_items"] = total
        engine_state["current_index"] = 0
        engine_state["overall_percent"] = 0
        engine_state["error"] = ""

    log_message(f"Starting BATCH RENDER: {total} total videos...")

    for i, (title, script_content) in enumerate(scripts, start=1):
        if stop_requested.is_set():
            log_message("Batch render paused/stopped by user.")
            break

        out_mp4 = str(RENDERED_DIR / f"{title}.mp4")

        # CHECK IF ALREADY RENDERED: Skip if valid MP4 exists (> 500 KB)
        if os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 500 * 1024:
            size_mb = round(os.path.getsize(out_mp4) / (1024 * 1024), 2)
            log_message(f"[ALREADY RENDERED] Video {i}/{total}: '{title}.mp4' ({size_mb} MB) exists -> Skipping to next script.")
            with engine_lock:
                engine_state["current_index"] = i
                engine_state["current_title"] = title
                engine_state["video_percent"] = 100
                overall = int((i / total) * 100)
                engine_state["overall_percent"] = overall
                engine_state["step"] = "skipped"
                engine_state["step_message"] = f"Skipping {title}.mp4 (Already Rendered: {size_mb} MB)"
                engine_state["last_rendered"] = f"{title}.mp4"
            continue

        with engine_lock:
            engine_state["current_index"] = i
            engine_state["current_title"] = title
            engine_state["video_percent"] = 0
            base_overall = int(((i - 1) / total) * 100)
            engine_state["overall_percent"] = base_overall
            engine_state["step"] = "starting_video"
            engine_state["step_message"] = f"Rendering Video {i}/{total}: {title}.mp4"

        log_message(f"--- [Batch {i}/{total}] {title}.mp4 ---")

        def progress_hook(info: Dict[str, Any]):
            with engine_lock:
                step = info.get("step", "")
                v_pct = info.get("percent", 0)
                msg = info.get("message", "")
                engine_state["step"] = step
                engine_state["video_percent"] = v_pct
                # Calculate smoothed overall percent
                overall = int(((i - 1 + (v_pct / 100.0)) / total) * 100)
                engine_state["overall_percent"] = min(100, overall)
                engine_state["step_message"] = msg
                if msg:
                    engine_state["logs"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")

        try:
            with engine_lock:
                active_cfg = dict(current_settings)

            out_path = pipeline.run(
                script_text=script_content,
                output_video_path=out_mp4,
                pilot_speed=active_cfg["pilot_speed"],
                tower_speed=active_cfg["tower_speed"],
                conversation_speed=active_cfg["conversation_speed"],
                bg_volume=active_cfg["bg_volume"],
                voice_volume=active_cfg["voice_volume"],
                opening_delay=active_cfg["opening_delay"],
                progress_callback=progress_hook,
                stop_event=stop_requested,
            )

            log_message(f"Successfully generated: {title}.mp4")
            with engine_lock:
                engine_state["last_rendered"] = f"{title}.mp4"

        except InterruptedError:
            log_message(f"[CANCELLED] Batch render cancelled by user. Incomplete {title}.mp4 removed.")
            if os.path.exists(out_mp4):
                try:
                    os.remove(out_mp4)
                    log_message(f"[CLEANUP] Deleted incomplete file: {title}.mp4")
                except Exception:
                    pass
            break

        except Exception as e:
            if stop_requested.is_set():
                log_message(f"[CANCELLED] Batch render interrupted by user. Cleaning up {title}.mp4...")
                if os.path.exists(out_mp4):
                    try:
                        os.remove(out_mp4)
                    except Exception:
                        pass
                break
            log_message(f"Error on {title}.mp4: {e}. Continuing to next video...")
            with engine_lock:
                engine_state["error"] = f"Error on {title}: {e}"

    with engine_lock:
        engine_state["is_running"] = False
        engine_state["mode"] = "idle"
        if stop_requested.is_set():
            engine_state["step"] = "cancelled"
            engine_state["step_message"] = "Batch render cancelled. Incomplete file removed. Adjust settings and click Render Videos again."
            log_message("BATCH RUNNER CANCELLED. Ready for adjusted settings.")
        else:
            engine_state["video_percent"] = 100
            engine_state["overall_percent"] = 100
            engine_state["step"] = "done"
            engine_state["step_message"] = "Batch render completed! All videos available in gallery."
            log_message("BATCH RUNNER FINISHED.")


# ==============================================================================
# API Endpoints
# ==============================================================================
@app.get("/api/status")
def get_status():
    with engine_lock:
        state_copy = dict(engine_state)
        state_copy["logs"] = list(engine_state["logs"])[-50:]
        return {
            "status": state_copy,
            "settings": current_settings,
        }


@app.get("/api/events")
async def sse_events(request: Request):
    """Server-Sent Events stream for instant real-time telemetry updates."""
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            with engine_lock:
                st = dict(engine_state)
                st["logs"] = list(engine_state["logs"])[-30:]
                payload = json.dumps({"status": st, "settings": current_settings})
            yield f"data: {payload}\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/settings")
def get_settings():
    with engine_lock:
        return current_settings


@app.post("/api/settings")
def update_settings(payload: Dict[str, float] = Body(...)):
    with engine_lock:
        for k in current_settings:
            if k in payload:
                current_settings[k] = float(payload[k])
    log_message(f"Updated settings: {current_settings}")
    return {"status": "ok", "settings": current_settings}


@app.post("/api/test_run")
async def start_test_run(request: Request):
    stop_requested.clear()
    script_index = 0
    try:
        body = await request.json()
        if isinstance(body, dict) and "script_index" in body:
            script_index = int(body["script_index"])
    except Exception:
        pass
    if "script_index" in request.query_params:
        try:
            script_index = int(request.query_params["script_index"])
        except Exception:
            pass

    with engine_lock:
        if engine_state["is_running"]:
            raise HTTPException(status_code=400, detail="A video generation process is already running.")
    thread = threading.Thread(target=run_single_test_worker, args=(script_index,), daemon=True)
    thread.start()
    return {"status": "started", "mode": "test", "script_index": script_index}


@app.post("/api/open_folder")
def open_folder():
    try:
        os.startfile(str(RENDERED_DIR))
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/reset_settings")
def reset_settings():
    default_vals = {
        "pilot_speed": 1.26,
        "tower_speed": 1.26,
        "conversation_speed": 1.26,
        "bg_volume": 0.35,
        "voice_volume": 1.6,
        "opening_delay": 1.0,
    }
    with engine_lock:
        current_settings.clear()
        current_settings.update(default_vals)
    log_message("Settings reset to defaults (1.26x speed).")
    return {"status": "ok", "settings": current_settings}


@app.post("/api/render_all")
def start_render_all():
    stop_requested.clear()
    with engine_lock:
        if engine_state["is_running"]:
            raise HTTPException(status_code=400, detail="A video generation process is already running.")
    thread = threading.Thread(target=run_batch_worker, daemon=True)
    thread.start()
    return {"status": "started", "mode": "batch"}


@app.post("/api/stop")
def stop_generation():
    stop_requested.set()
    log_message("User requested generation STOP.")

    with engine_lock:
        curr_title = engine_state.get("current_title", "")
        if curr_title:
            incomplete_file = RENDERED_DIR / f"{curr_title}.mp4"
            if incomplete_file.exists():
                try:
                    os.remove(str(incomplete_file))
                    log_message(f"[CLEANUP] Deleted incomplete file: {curr_title}.mp4")
                except Exception as e:
                    log_message(f"[CLEANUP] Error deleting {curr_title}.mp4: {e}")
        engine_state["is_running"] = False
        engine_state["mode"] = "idle"
        engine_state["step"] = "cancelled"
        engine_state["step_message"] = "Generation stopped by user. Incomplete files removed. Readjust settings and click Render Videos again."

    return {"status": "stop_requested"}


@app.get("/api/scripts")
def list_scripts():
    if not SCRIPTS_PATH.exists():
        return {"scripts": [], "first_unrendered_index": 0, "total_rendered": 0, "total_count": 0}
    try:
        scripts_tuples = load_scripts_from_json(str(SCRIPTS_PATH))
    except Exception:
        scripts_tuples = []

    result = []
    first_unrendered_idx = None
    rendered_count = 0

    for idx, (title, script_text) in enumerate(scripts_tuples):
        out_file = RENDERED_DIR / f"{title}.mp4"
        is_rendered = out_file.exists() and out_file.stat().st_size > 500 * 1024
        size_mb = 0.0
        if is_rendered:
            rendered_count += 1
            size_mb = round(out_file.stat().st_size / (1024 * 1024), 2)
        elif first_unrendered_idx is None:
            first_unrendered_idx = idx

        first_line = script_text.splitlines()[0] if script_text else ""
        result.append({
            "index": idx,
            "title": title,
            "preview": first_line,
            "is_rendered": is_rendered,
            "size_mb": size_mb,
            "rendered_file": f"{title}.mp4" if is_rendered else None,
        })

    if first_unrendered_idx is None:
        first_unrendered_idx = 0

    return {
        "scripts": result,
        "first_unrendered_index": first_unrendered_idx,
        "total_rendered": rendered_count,
        "total_count": len(result),
    }


@app.get("/api/videos")
def list_videos():
    vids = sorted(glob.glob(str(RENDERED_DIR / "*.mp4")), key=os.path.getmtime, reverse=True)
    result = []
    for v in vids:
        st = os.stat(v)
        result.append({
            "filename": os.path.basename(v),
            "size_mb": round(st.st_size / (1024 * 1024), 2),
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        })
    return result


@app.get("/videos/{filename}")
def stream_video(filename: str):
    video_path = RENDERED_DIR / filename
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(
        str(video_path),
        media_type="video/mp4",
        filename=filename,
    )


# Serve static web frontend
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="static")


def main():
    port = 5000
    print(f"\n==================================================================")
    print(f"  [ATC SKYFORGE] WEB UI SERVER")
    print(f"  Open in Browser: http://localhost:{port}")
    print(f"==================================================================\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
