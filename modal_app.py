"""
ATC & Pilot Aviation Video Generator - Modal Cloud App
======================================================
Complete cloud-native video generation pipeline deployed on Modal:
- Kokoro-82M TTS on GPU (Pilot: am_adam @ 1.22x, Tower: am_fenrir @ 1.28x)
- Radio Acoustics: VHF bandpass filter, PTT mic clicks, tactical Roger beeps
- Authentic background radio static bed from audio_samples/04_background_radio_static.wav (volume: 0.10)
- Automatic stock footage selection (Plane for Pilot, Tower for ATC)
- 9:16 Vertical Video (1080x1920) formatting with handheld camera shake
- Persistent storage on Modal Volume 'atc-assets'
- FastAPI REST endpoints for rendering, monitoring, and streaming videos
"""

import os
import re
import json
import glob
import time
import random
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional

import modal
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# ==============================================================================
# 1. Modal App & Infrastructure Configuration
# ==============================================================================
APP_NAME = "atc-video-generator"
VOLUME_NAME = "atc-assets"
ASSETS_DIR = Path("/assets")
PLANE_VIDEOS_DIR = ASSETS_DIR / "plane_videos"
TOWER_VIDEOS_DIR = ASSETS_DIR / "tower_videos"
AUDIO_SAMPLES_DIR = ASSETS_DIR / "audio_samples"
SCRIPTS_FILE = ASSETS_DIR / "scripts_emotional.json"
RENDERED_VIDEOS_DIR = ASSETS_DIR / "rendered_videos"

VALID_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

app = modal.App(APP_NAME)
atc_assets = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

# Build optimized container image with FFmpeg, eSpeak-NG, and pre-warmed Kokoro-82M
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "espeak-ng")
    .pip_install(
        "kokoro>=0.8.4",
        "soundfile",
        "numpy",
        "scipy",
        "fastapi[standard]",
        "pydantic",
        "aiofiles"
    )
    .run_commands(
        # Pre-warm model weights into container image during build
        "python -c 'from kokoro import KPipeline; p = KPipeline(lang_code=\"a\"); p(\"Mayday\", voice=\"am_adam\", speed=1.22)'"
    )
)


# ==============================================================================
# 2. Dialogue Parser
# ==============================================================================
class DialogueTurn:
    def __init__(self, speaker: str, text: str):
        self.speaker = speaker.lower().strip()
        self.text = text.strip()
        self.duration: float = 0.0

    def is_pilot(self) -> bool:
        return any(k in self.speaker for k in ("pilot", "cockpit", "captain", "aircraft"))

    def is_tower(self) -> bool:
        return any(k in self.speaker for k in ("tower", "atc", "ground", "approach", "center", "radar"))


def clean_dialogue_text(text: str) -> str:
    """Removes commas between repeated MAYDAY words to eliminate unnatural pauses."""
    text = re.sub(r'\b(mayday)\s*,\s*(?=mayday\b)', r'\1 ', text, flags=re.IGNORECASE)
    # Guarantee turn ends with 'over.'
    t = text.strip().rstrip('.!?,')
    if not t.lower().endswith("over"):
        return f"{t}, over."
    return f"{t}."


# ==============================================================================
# 3. Cloud Video Generator Service
# ==============================================================================
@app.cls(
    gpu="T4",
    image=image,
    volumes={"/assets": atc_assets},
    timeout=3600,
    scaledown_window=300
)
class ATCVideoService:
    @modal.enter()
    def setup(self):
        """Loads Kokoro pipeline and initializes audio/video constants."""
        import soundfile as sf
        import numpy as np
        from kokoro import KPipeline

        print("[Modal] Initializing Kokoro TTS Pipeline on GPU...", flush=True)
        self.pipeline = KPipeline(lang_code="a")

        # Audio settings
        self.pilot_voice = "am_adam"      # Crisp male pilot tone
        self.tower_voice = "am_fenrir"    # Authoritative ATC controller tone
        self.pilot_speed = 1.22           # 1.22x speed as requested
        self.tower_speed = 1.28           # 1.28x speed as requested
        self.sample_rate = 24000

        # Ensure rendered_videos directory exists
        RENDERED_VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
        print("[Modal] Service initialized successfully.", flush=True)

    def _synthesize_voice(self, text: str, voice: str, speed: float, out_wav: str) -> float:
        import soundfile as sf
        import numpy as np

        generator = self.pipeline(text, voice=voice, speed=speed)
        audio_segments = []
        for _, _, audio in generator:
            if hasattr(audio, "numpy"):
                audio_np = audio.numpy()
            elif hasattr(audio, "cpu"):
                audio_np = audio.cpu().numpy()
            else:
                audio_np = np.asarray(audio)
            audio_segments.append(audio_np.flatten())

        if not audio_segments:
            samples = np.zeros(self.sample_rate, dtype=np.float32)
        else:
            samples = np.concatenate(audio_segments).astype(np.float32)

        sf.write(out_wav, samples, self.sample_rate)
        return len(samples) / float(self.sample_rate)

    def _apply_radio_filter(self, in_wav: str, out_wav: str, is_pilot: bool):
        hp = 340 if is_pilot else 300
        lp = 3400 if is_pilot else 3600
        vol = 1.35 if is_pilot else 1.25

        filter_chain = (
            f"highpass=f={hp},"
            f"lowpass=f={lp},"
            f"acompressor=threshold=-18dB:ratio=4:attack=5:release=50:makeup=2dB,"
            f"volume={vol}"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", in_wav,
            "-af", filter_chain,
            "-ar", str(self.sample_rate),
            "-ac", "1",
            out_wav
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    def _get_background_static(self, duration_sec: float) -> Any:
        import soundfile as sf
        import numpy as np

        target_len = int(duration_sec * self.sample_rate)
        static_file = AUDIO_SAMPLES_DIR / "04_background_radio_static.wav"

        if static_file.exists():
            try:
                data, in_sr = sf.read(str(static_file))
                if in_sr != self.sample_rate:
                    from scipy.signal import resample
                    data = resample(data, int(len(data) * self.sample_rate / in_sr))
                if data.ndim > 1:
                    data = np.mean(data, axis=1)
                data = data.astype(np.float32)
                repeats = int(np.ceil(target_len / len(data)))
                return np.tile(data, repeats)[:target_len]
            except Exception as e:
                print(f"[Warning] Failed to read static sample: {e}", flush=True)

        # High-fidelity synthetic fallback
        t = np.linspace(0, duration_sec, target_len, endpoint=False)
        rumble = 0.022 * np.sin(2 * np.pi * 95 * t) + 0.012 * np.sin(2 * np.pi * 190 * t)
        noise = (np.random.rand(target_len) * 2 - 1) * 0.018
        whine = 0.003 * np.sin(2 * np.pi * 1020 * t)
        return (rumble + noise + whine).astype(np.float32)

    def _get_stock_videos(self, folder: Path) -> List[str]:
        if not folder.exists():
            return []
        vids = []
        for ext in VALID_VIDEO_EXTS:
            vids.extend(glob.glob(str(folder / f"*{ext}")))
            vids.extend(glob.glob(str(folder / f"*{ext.upper()}")))
        return sorted(list(set(vids)))

    def _get_video_duration(self, video_path: str) -> float:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return float(res.stdout.strip())
        except Exception:
            return 10.0

    def _cut_and_format_clip(self, video_path: str, duration: float, out_clip: str):
        src_dur = self._get_video_duration(video_path)
        start_offset = 0.0
        if src_dur > duration + 0.5:
            start_offset = random.uniform(0.0, max(0.0, src_dur - duration))

        target_w, target_h = 1080, 1920
        scale_w = int(target_w * 1.06)
        scale_h = int(target_h * 1.06)

        px = round(random.uniform(0.0, 6.28), 2)
        py = round(random.uniform(0.0, 6.28), 2)

        x_shake = f"(in_w-{target_w})/2+13*sin(t*3.2+{px})+6*cos(t*6.7+{px})+2*sin(t*12.1)"
        y_shake = f"(in_h-{target_h})/2+15*cos(t*2.8+{py})+7*sin(t*5.5+{py})+3*cos(t*10.7)"

        vf = (
            f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h}:'{x_shake}':'{y_shake}',"
            f"setsar=1,fps=30"
        )

        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-ss", f"{start_offset:.2f}",
            "-i", video_path,
            "-t", f"{duration:.3f}",
            "-vf", vf,
            "-an",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            out_clip
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    @modal.method()
    def render_script(self, script_index: int, custom_pilot_speed: Optional[float] = None, custom_tower_speed: Optional[float] = None) -> Dict[str, Any]:
        """Renders complete 9:16 vertical video for the specified script."""
        import soundfile as sf
        import numpy as np

        atc_assets.reload()

        if not SCRIPTS_FILE.exists():
            raise HTTPException(status_code=404, detail="scripts_emotional.json not found on volume.")

        with open(SCRIPTS_FILE, "r", encoding="utf-8") as f:
            scripts = json.load(f)

        if script_index < 0 or script_index >= len(scripts):
            raise HTTPException(status_code=400, detail=f"Invalid script index {script_index}. Total scripts: {len(scripts)}.")

        script_data = scripts[script_index]
        title = script_data.get("title", f"Script_{script_index+1}")
        m = re.match(r"^(\d+)", title)
        script_num = int(m.group(1)) if m else script_index + 1
        slug = re.sub(r'[^a-zA-Z0-9_]', '_', title.lower())[:30]
        output_filename = f"script_{script_num:02d}_{slug}.mp4"
        final_video_path = RENDERED_VIDEOS_DIR / output_filename

        dialogue = script_data.get("dialogue", [])
        if not dialogue:
            raise HTTPException(status_code=400, detail="Script dialogue is empty.")

        turns = [DialogueTurn(d.get("speaker", "pilot"), d.get("text", "")) for d in dialogue]

        plane_vids = self._get_stock_videos(PLANE_VIDEOS_DIR)
        tower_vids = self._get_stock_videos(TOWER_VIDEOS_DIR)

        all_available_vids = plane_vids + tower_vids
        if not all_available_vids:
            raise HTTPException(status_code=500, detail="No stock videos available in volume. Please upload videos first.")

        p_speed = custom_pilot_speed or self.pilot_speed
        t_speed = custom_tower_speed or self.tower_speed

        start_time = time.time()
        temp_dir = Path(tempfile.mkdtemp(prefix="atc_cloud_render_"))

        try:
            # 1. Synthesize audio turns & prepare audio chunks
            print(f"[Render {script_index+1}] Synthesizing {len(turns)} turns...", flush=True)
            sr = self.sample_rate

            # PTT click (0.04s)
            num_ptt = int(0.04 * sr)
            t_ptt = np.linspace(0, 0.04, num_ptt, endpoint=False)
            noise_ptt = (np.random.rand(num_ptt) * 2 - 1) * np.exp(-t_ptt * 60)
            pop_ptt = 0.5 * np.sin(2 * np.pi * 1800 * t_ptt) * np.exp(-t_ptt * 80)
            ptt_audio = ((noise_ptt + pop_ptt) * 0.30).astype(np.float32)

            # Tactical dual-tone Roger chirp (0.13s)
            dur1, dur2 = 0.06, 0.07
            n1, n2 = int(dur1 * sr), int(dur2 * sr)
            t1 = np.linspace(0, dur1, n1, endpoint=False)
            t2 = np.linspace(0, dur2, n2, endpoint=False)
            tone1 = np.sin(2 * np.pi * 2100.0 * t1) * (np.sin(np.pi * t1 / dur1) ** 2) * 0.45
            tone2 = np.sin(2 * np.pi * 1750.0 * t2) * (np.sin(np.pi * t2 / dur2) ** 2) * 0.45
            n_sq = int(0.03 * sr)
            sq = (np.random.rand(n_sq) * 2 - 1) * 0.25
            roger_beep = np.concatenate([tone1, tone2, sq]).astype(np.float32)

            pause_audio = np.zeros(int(0.10 * sr), dtype=np.float32)

            all_audio_chunks = []
            for idx, turn in enumerate(turns, start=1):
                clean_txt = clean_dialogue_text(turn.text)
                is_p = turn.is_pilot()
                voice = self.pilot_voice if is_p else self.tower_voice
                spd = p_speed if is_p else t_speed

                raw_wav = str(temp_dir / f"turn_{idx}_raw.wav")
                radio_wav = str(temp_dir / f"turn_{idx}_radio.wav")

                self._synthesize_voice(clean_txt, voice=voice, speed=spd, out_wav=raw_wav)
                self._apply_radio_filter(raw_wav, radio_wav, is_pilot=is_p)

                data, in_sr = sf.read(radio_wav)
                if data.ndim > 1:
                    data = np.mean(data, axis=1)
                data = data.astype(np.float32)

                if idx == 1:
                    lead_in = np.zeros(int(1.0 * sr), dtype=np.float32)
                    turn_chunk = np.concatenate([lead_in, ptt_audio, data, roger_beep, pause_audio])
                else:
                    turn_chunk = np.concatenate([ptt_audio, data, roger_beep, pause_audio])

                all_audio_chunks.append(turn_chunk)
                turn.duration = len(turn_chunk) / float(sr)

            # Master audio mixing with continuous background static bed (volume 0.10)
            full_speech = np.concatenate(all_audio_chunks)
            total_audio_sec = len(full_speech) / float(sr)

            bg_bed = self._get_background_static(total_audio_sec + 1.0)
            match_len = min(len(full_speech), len(bg_bed))
            full_audio = full_speech[:match_len] + (bg_bed[:match_len] * 0.10)
            full_audio = np.clip(full_audio, -1.0, 1.0)

            master_wav = str(temp_dir / "master_audio.wav")
            sf.write(master_wav, full_audio, sr)

            # 2. Select Stock Videos & Format to 9:16
            print(f"[Render {script_index+1}] Formatting video clips to 9:16...", flush=True)
            clip_paths = []
            concat_list = str(temp_dir / "concat_list.txt")

            with open(concat_list, "w", encoding="utf-8") as clist:
                for idx, turn in enumerate(turns, start=1):
                    # Pick stock video pool
                    if turn.is_pilot() and plane_vids:
                        chosen_vid = random.choice(plane_vids)
                    elif turn.is_tower() and tower_vids:
                        chosen_vid = random.choice(tower_vids)
                    else:
                        chosen_vid = random.choice(all_available_vids)

                    out_clip = str(temp_dir / f"clip_{idx:02d}.mp4")
                    self._cut_and_format_clip(chosen_vid, turn.duration, out_clip)
                    clip_paths.append(out_clip)
                    clist.write(f"file '{out_clip}'\n")

            # 3. Concatenate and Mux Master Audio
            print(f"[Render {script_index+1}] Final concatenation and audio muxing...", flush=True)
            merged_video = str(temp_dir / "merged_video.mp4")
            cmd_concat = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_list,
                "-c", "copy",
                merged_video
            ]
            subprocess.run(cmd_concat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            cmd_mux = [
                "ffmpeg", "-y",
                "-i", merged_video,
                "-i", master_wav,
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-shortest",
                "-movflags", "+faststart",
                str(final_video_path)
            ]
            subprocess.run(cmd_mux, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            # Commit to persistent volume
            atc_assets.commit()

            render_duration = time.time() - start_time
            file_size_mb = os.path.getsize(str(final_video_path)) / (1024 * 1024)

            print(f"[Render {script_index+1}] Render complete: {output_filename} ({file_size_mb:.2f} MB in {render_duration:.1f}s)", flush=True)

            return {
                "success": True,
                "script_index": script_index,
                "script_number": script_num,
                "title": title,
                "filename": output_filename,
                "video_duration_sec": round(total_audio_sec, 2),
                "file_size_mb": round(file_size_mb, 2),
                "render_duration_sec": round(render_duration, 2),
                "download_url": f"/api/download/{output_filename}"
            }

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @modal.method()
    def render_batch(self, script_indices: List[int], custom_pilot_speed: Optional[float] = None, custom_tower_speed: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        Renders a batch of scripts consecutively in a single warm GPU container.
        Eliminates container cold starts and idle spin-downs between videos.
        """
        results = []
        total = len(script_indices)
        print(f"[Modal Batch] Starting cloud render batch for {total} scripts on warm GPU...", flush=True)
        batch_start = time.time()
        for pos, s_idx in enumerate(script_indices, start=1):
            print(f"[Modal Batch] [{pos}/{total}] Rendering script index {s_idx}...", flush=True)
            try:
                res = self.render_script.local(s_idx, custom_pilot_speed, custom_tower_speed)
                results.append(res)
            except Exception as e:
                print(f"[Modal Batch] Error rendering script index {s_idx}: {e}", flush=True)
                results.append({
                    "success": False,
                    "script_index": s_idx,
                    "error": str(e)
                })
        total_time = time.time() - batch_start
        print(f"[Modal Batch] Finished batch: {len(results)} videos in {total_time:.1f}s ({total_time/60:.2f} mins)", flush=True)
        return results


# ==============================================================================
# 4. FastAPI Web Server Application
# ==============================================================================
web_app = FastAPI(
    title="ATC & Pilot Video Generator API",
    description="Cloud video generator for realistic 9:16 Pilot & ATC conversation videos with VHF radio FX.",
    version="1.0.0"
)


class RenderRequest(BaseModel):
    pilot_speed: Optional[float] = 1.22
    tower_speed: Optional[float] = 1.28


class BatchRenderRequest(BaseModel):
    script_indices: Optional[List[int]] = None
    pilot_speed: Optional[float] = 1.22
    tower_speed: Optional[float] = 1.28


@web_app.get("/")
def get_status():
    atc_assets.reload()
    plane_vids = glob.glob(str(PLANE_VIDEOS_DIR / "*.*"))
    tower_vids = glob.glob(str(TOWER_VIDEOS_DIR / "*.*"))
    rendered_vids = glob.glob(str(RENDERED_VIDEOS_DIR / "*.mp4"))

    total_scripts = 0
    if SCRIPTS_FILE.exists():
        try:
            with open(SCRIPTS_FILE, "r", encoding="utf-8") as f:
                total_scripts = len(json.load(f))
        except Exception:
            pass

    return {
        "status": "online",
        "service": "ATC & Pilot Cloud Video Generator",
        "gpu": "NVIDIA T4",
        "engine": "Kokoro-82M + FFmpeg",
        "configuration": {
            "pilot_voice": "am_adam",
            "pilot_speed": 1.22,
            "tower_voice": "am_fenrir",
            "tower_speed": 1.28,
            "aspect_ratio": "9:16 (1080x1920)",
            "camera_shake": True,
            "background_radio_static": "04_background_radio_static.wav @ 0.10"
        },
        "assets": {
            "plane_videos_count": len(plane_vids),
            "tower_videos_count": len(tower_vids),
            "total_scripts": total_scripts,
            "rendered_videos_count": len(rendered_vids)
        }
    }


@web_app.get("/api/scripts")
def list_scripts():
    atc_assets.reload()
    if not SCRIPTS_FILE.exists():
        raise HTTPException(status_code=404, detail="scripts_emotional.json not found on volume.")

    with open(SCRIPTS_FILE, "r", encoding="utf-8") as f:
        scripts = json.load(f)

    items = []
    for idx, s in enumerate(scripts):
        title = s.get("title", f"Script_{idx+1}")
        m = re.match(r"^(\d+)", title)
        s_num = int(m.group(1)) if m else idx + 1
        items.append({
            "index": idx,
            "script_number": s_num,
            "title": title,
            "turns_count": len(s.get("dialogue", []))
        })
    return items


@web_app.post("/api/render/{script_index}")
def render_video_endpoint(script_index: int, req: Optional[RenderRequest] = None):
    service = ATCVideoService()
    p_spd = req.pilot_speed if req else 1.22
    t_spd = req.tower_speed if req else 1.28
    result = service.render_script.remote(script_index, custom_pilot_speed=p_spd, custom_tower_speed=t_spd)
    return result


@web_app.post("/api/render_batch")
def render_batch_endpoint(req: Optional[BatchRenderRequest] = None):
    service = ATCVideoService()
    atc_assets.reload()
    if not SCRIPTS_FILE.exists():
        raise HTTPException(status_code=404, detail="scripts_emotional.json not found on volume.")

    with open(SCRIPTS_FILE, "r", encoding="utf-8") as f:
        scripts = json.load(f)

    if req and req.script_indices is not None:
        indices = req.script_indices
    else:
        indices = list(range(len(scripts)))

    p_spd = req.pilot_speed if req else 1.22
    t_spd = req.tower_speed if req else 1.28
    results = service.render_batch.remote(indices, custom_pilot_speed=p_spd, custom_tower_speed=t_spd)
    return {
        "success": True,
        "total_requested": len(indices),
        "total_rendered": len(results),
        "results": results
    }


@web_app.get("/api/videos")
def list_rendered_videos():
    atc_assets.reload()
    if not RENDERED_VIDEOS_DIR.exists():
        return []

    vids = sorted(glob.glob(str(RENDERED_VIDEOS_DIR / "*.mp4")), key=os.path.getmtime, reverse=True)
    results = []
    for v in vids:
        p = Path(v)
        results.append({
            "filename": p.name,
            "size_mb": round(os.path.getsize(v) / (1024 * 1024), 2),
            "modified": time.ctime(os.path.getmtime(v)),
            "download_url": f"/api/download/{p.name}"
        })
    return results


@web_app.get("/api/download/{filename}")
def download_video(filename: str):
    atc_assets.reload()
    safe_name = os.path.basename(filename)
    file_path = RENDERED_VIDEOS_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Video file {safe_name} not found.")

    return FileResponse(
        path=str(file_path),
        media_type="video/mp4",
        filename=safe_name
    )


# Expose FastAPI as Modal ASGI App
@app.function(
    image=image,
    volumes={"/assets": atc_assets},
    timeout=3600
)
@modal.asgi_app()
def api():
    return web_app
