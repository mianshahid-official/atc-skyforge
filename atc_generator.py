"""
ATC & Pilot Aviation Radio Video Generator
=========================================
Specialized pipeline for generating 9:16 vertical videos of Pilot and ATC Tower conversations
with realistic aviation radio acoustics (bandpass filter, mic clicks, VHF static) and
automated stock video selection (Plane for Pilot, Tower for ATC).
"""

from __future__ import annotations
import os
import re
import sys
import time
import json
import glob
import random
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import soundfile as sf
import numpy as np

# Workspace Root
ROOT_DIR = Path(__file__).parent.resolve()
MODELS_DIR = ROOT_DIR / "models"
KOKORO_MODEL = MODELS_DIR / "kokoro" / "kokoro-v0_19.onnx"
KOKORO_VOICES = MODELS_DIR / "kokoro" / "voices.bin"

PLANE_VIDEOS_DIR = ROOT_DIR / "plane_videos"
TOWER_VIDEOS_DIR = ROOT_DIR / "tower_videos"
OUTPUT_DIR = ROOT_DIR / "rendered_videos"

VALID_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


# ==============================================================================
# 1. Conversation Parser
# ==============================================================================
class DialogueTurn:
    def __init__(self, speaker: str, text: str):
        self.speaker = speaker.lower().strip()  # 'pilot' or 'tower'/'atc'
        self.text = text.strip()
        self.audio_path: Optional[str] = None
        self.duration: float = 0.0
        self.selected_video: Optional[str] = None

    def is_pilot(self) -> bool:
        return "pilot" in self.speaker or "cockpit" in self.speaker or "captain" in self.speaker or "aircraft" in self.speaker

    def is_tower(self) -> bool:
        return "tower" in self.speaker or "atc" in self.speaker or "ground" in self.speaker or "approach" in self.speaker or "center" in self.speaker or "radar" in self.speaker

    def __repr__(self) -> str:
        role = "PILOT" if self.is_pilot() else "TOWER"
        return f"[{role}] {self.text[:40]}... ({self.duration:.2f}s)"


def parse_conversation(script_text: str) -> List[DialogueTurn]:
    """
    Parses dialogue scripts in multiple standard formats:
    - Pilot: Say again altitude?
    - Tower: Maintain 5000 feet.
    - [Pilot] Roger, leaving 7000 for 5000.
    """
    lines = script_text.strip().splitlines()
    turns: List[DialogueTurn] = []

    pattern = re.compile(r"^(?:\[?([A-Za-z0-9_\s]+)\]?)\s*[:\-]\s*(.+)$")

    current_speaker = "pilot"
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        match = pattern.match(line)
        if match:
            spk, text = match.group(1).strip(), match.group(2).strip().strip('"').strip("'")
            current_speaker = spk
            turns.append(DialogueTurn(speaker=current_speaker, text=text))
        else:
            # Continuation line
            clean_l = line.strip().strip('"').strip("'")
            if turns:
                turns[-1].text += " " + clean_l
            else:
                turns.append(DialogueTurn(speaker=current_speaker, text=clean_l))

    # CRITICAL: Guarantee every turn ends with the word 'over.'
    for turn in turns:
        t = turn.text.strip().rstrip('.!?,')
        if not t.lower().endswith("over"):
            turn.text = f"{t}, over."
        else:
            turn.text = f"{t}."

    return turns


# ==============================================================================
# 2. Local TTS Engine (Kokoro-ONNX / Extensible for Qwen3TTS)
# ==============================================================================
class TTSBackend:
    def synthesize(self, text: str, voice: str, speed: float, out_wav: str) -> float:
        raise NotImplementedError


class KokoroONNXBackend(TTSBackend):
    def __init__(self, model_path: Path = KOKORO_MODEL, voices_path: Path = KOKORO_VOICES):
        self.model_path = str(model_path)
        self.voices_path = str(voices_path)
        self._kokoro = None

    def _load(self):
        if self._kokoro is None:
            from kokoro_onnx import Kokoro
            if not os.path.exists(self.model_path) or not os.path.exists(self.voices_path):
                raise FileNotFoundError(f"Kokoro model or voices missing: {self.model_path}")
            self._kokoro = Kokoro(self.model_path, self.voices_path)

    def synthesize(self, text: str, voice: str, speed: float, out_wav: str) -> float:
        self._load()
        samples, sr = self._kokoro.create(text, voice=voice, speed=speed, lang="en-us")
        sf.write(out_wav, samples, sr)
        return len(samples) / float(sr)


class Qwen3TTSBackend(TTSBackend):
    """
    Placeholder adapter for future migration to Qwen3TTS.
    Easily hooked to local weights or an inference server.
    """
    def __init__(self, endpoint_url: Optional[str] = None):
        self.endpoint_url = endpoint_url

    def synthesize(self, text: str, voice: str, speed: float, out_wav: str) -> float:
        raise NotImplementedError("Qwen3TTS backend will be configured when model weights or API endpoint are set.")


class AviationTTSEngine:
    def __init__(self, backend_type: str = "kokoro"):
        if backend_type == "kokoro":
            self.backend = KokoroONNXBackend()
        elif backend_type == "qwen3tts":
            self.backend = Qwen3TTSBackend()
        else:
            raise ValueError(f"Unknown backend: {backend_type}")

        # Voices tailored for emergency dialogue: high tempo & adrenaline
        self.pilot_voice = "am_adam"      # Crisp male pilot tone
        self.tower_voice = "am_fenrir"    # Authoritative controller tone
        self.pilot_speed = 1.22           # Fast, high-energy emergency delivery (1.22x)
        self.tower_speed = 1.28           # Crisp authoritative ATC controller delivery (1.28x)

    def synthesize_turn(self, turn: DialogueTurn, out_wav: str, custom_speed: Optional[float] = None) -> float:
        if turn.is_pilot():
            voice = self.pilot_voice
            speed = custom_speed if custom_speed is not None else self.pilot_speed
        else:
            voice = self.tower_voice
            speed = custom_speed if custom_speed is not None else self.tower_speed

        clean_text = re.sub(r'\b(mayday)\s*,\s*(?=mayday\b)', r'\1 ', turn.text, flags=re.IGNORECASE)
        dur = self.backend.synthesize(clean_text, voice=voice, speed=speed, out_wav=out_wav)
        turn.duration = dur
        turn.audio_path = out_wav
        return dur


# ==============================================================================
# 3. Aviation Radio Audio Effects & PTT Clicks
# ==============================================================================
class AviationRadioFX:
    @staticmethod
    def _find_ffmpeg() -> str:
        return shutil.which("ffmpeg") or "ffmpeg"

    @classmethod
    def apply_radio_effect(
        cls,
        input_wav: str,
        output_wav: str,
        is_pilot: bool = True,
        tempo: Optional[float] = None,
        volume: Optional[float] = None
    ):
        """
        Applies genuine VHF AM aircraft radio acoustic filter:
        - Snappy tempo adjustment for crisp emergency urgency
        - Highpass filter (340Hz / 300Hz) + Lowpass filter (3400Hz / 3600Hz)
        - Compression (acompressor) for loud upfront vocal clarity
        - Volume boost to rise cleanly above background noise
        """
        ffmpeg_cmd = cls._find_ffmpeg()
        hp = 340 if is_pilot else 300
        lp = 3400 if is_pilot else 3600
        if tempo is not None:
            speedup = tempo if is_pilot else max(1.0, tempo - 0.04)
        else:
            speedup = 1.10 if is_pilot else 1.06

        vol = volume if volume is not None else 1.6

        filter_chain = (
            f"atempo={speedup:.2f},"
            f"highpass=f={hp},"
            f"lowpass=f={lp},"
            f"acompressor=threshold=-12dB:ratio=3.5:attack=4:release=45,"
            f"volume={vol:.2f},"
            f"acrusher=bits=14:mode=log:samples=1"
        )

        cmd = [
            ffmpeg_cmd, "-y",
            "-i", input_wav,
            "-af", filter_chain,
            "-ar", "24000",
            output_wav
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            # Fallback if acrusher isn't supported
            cmd_fallback = [
                ffmpeg_cmd, "-y",
                "-i", input_wav,
                "-af", f"atempo={speedup:.2f},highpass=f={hp},lowpass=f={lp},volume={vol:.2f}",
                "-ar", "24000",
                output_wav
            ]
            subprocess.run(cmd_fallback, capture_output=True, check=True)

    @classmethod
    def generate_ptt_click(cls, out_wav: str, duration: float = 0.08):
        """
        Generates an authentic synthetic PTT radio squelch / mic unkey click.
        """
        sr = 24000
        num_samples = int(duration * sr)
        t = np.linspace(0, duration, num_samples, endpoint=False)
        # Short burst of noise + high pitch pop decaying quickly
        noise = (np.random.rand(num_samples) * 2 - 1) * np.exp(-t * 60)
        pop = 0.5 * np.sin(2 * np.pi * 1800 * t) * np.exp(-t * 80)
        signal = (noise + pop) * 0.4
        sf.write(out_wav, signal.astype(np.float32), sr)

    @classmethod
    def create_roger_beep(cls, duration: float = 0.13, freq1: float = 2100.0, freq2: float = 1750.0) -> np.ndarray:
        """
        Tactical dual-tone Motorola/Military roger beep (as favored in Sample 03).
        Two-step descending chirp (2100Hz -> 1750Hz) + fast analog squelch burst.
        """
        sr = 24000
        dur1, dur2 = 0.06, 0.07
        n1, n2 = int(dur1 * sr), int(dur2 * sr)
        t1 = np.linspace(0, dur1, n1, endpoint=False)
        t2 = np.linspace(0, dur2, n2, endpoint=False)
        tone1 = np.sin(2 * np.pi * freq1 * t1) * (np.sin(np.pi * t1 / dur1) ** 2) * 0.45
        tone2 = np.sin(2 * np.pi * freq2 * t2) * (np.sin(np.pi * t2 / dur2) ** 2) * 0.45
        n_sq = int(0.03 * sr)
        sq = (np.random.rand(n_sq) * 2 - 1) * 0.25
        return np.concatenate([tone1, tone2, sq]).astype(np.float32)

    @classmethod
    def create_background_radio_noise(cls, duration_sec: float, sample_path: Optional[Path] = None) -> np.ndarray:
        """
        Creates authentic aviation VHF radio background bed:
        Uses recorded background radio static sample (audio_samples/04_background_radio_static.wav) if available,
        or falls back to high-fidelity synthesized cockpit turbine rumble & VHF static.
        """
        sr = 24000
        target_len = int(duration_sec * sr)
        bg_file = sample_path or (ROOT_DIR / "audio_samples" / "04_background_radio_static.wav")
        if bg_file.exists():
            try:
                data, in_sr = sf.read(str(bg_file))
                if in_sr != sr:
                    from scipy.signal import resample
                    data = resample(data, int(len(data) * sr / in_sr))
                if data.ndim > 1:
                    data = np.mean(data, axis=1)
                data = data.astype(np.float32)
                repeats = int(np.ceil(target_len / len(data)))
                return np.tile(data, repeats)[:target_len]
            except Exception:
                pass

        t = np.linspace(0, duration_sec, target_len, endpoint=False)
        rumble = 0.022 * np.sin(2 * np.pi * 95 * t) + 0.012 * np.sin(2 * np.pi * 190 * t)
        noise = (np.random.rand(target_len) * 2 - 1) * 0.018
        whine = 0.003 * np.sin(2 * np.pi * 1020 * t)
        return (rumble + noise + whine).astype(np.float32)

    @classmethod
    def generate_silence(cls, out_wav: str, duration: float = 0.4):
        sr = 24000
        num_samples = int(duration * sr)
        signal = np.zeros(num_samples, dtype=np.float32)
        sf.write(out_wav, signal, sr)


# ==============================================================================
# 4. Video Clip Selector & 9:16 Video Assembly Engine
# ==============================================================================
class AviationVideoRenderer:
    @staticmethod
    def _find_ffmpeg() -> str:
        return shutil.which("ffmpeg") or "ffmpeg"

    @staticmethod
    def _find_ffprobe() -> str:
        return shutil.which("ffprobe") or "ffprobe"

    @classmethod
    def get_stock_videos(cls, folder: Path) -> List[str]:
        if not folder.exists():
            return []
        vids = []
        for ext in VALID_VIDEO_EXTS:
            vids.extend(glob.glob(str(folder / f"*{ext}")))
            vids.extend(glob.glob(str(folder / f"*{ext.upper()}")))
        return sorted(list(set(vids)))

    @classmethod
    def get_video_duration(cls, video_path: str) -> float:
        ffprobe_cmd = cls._find_ffprobe()
        cmd = [
            ffprobe_cmd, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return float(res.stdout.strip())
        except Exception:
            return 10.0

    @classmethod
    def cut_and_format_clip(
        cls,
        video_path: str,
        duration: float,
        output_clip_path: str,
        target_w: int = 1080,
        target_h: int = 1920,
        fps: int = 30,
        stop_event: Optional[Any] = None
    ):
        """
        Scales and crops source video to exact 9:16 vertical ratio (1080x1920)
        and exact target duration without letterboxing.
        Supports instant cancellation via stop_event.
        """
        ffmpeg_cmd = cls._find_ffmpeg()
        src_dur = cls.get_video_duration(video_path)

        start_offset = 0.0
        if src_dur > duration + 1.0:
            max_start = src_dur - duration
            start_offset = random.uniform(0.0, max(0.0, max_start))

        # Handheld Mobile Camera Shake:
        scale_w = int(target_w * 1.06)
        scale_h = int(target_h * 1.06)

        px = round(random.uniform(0.0, 6.28), 2)
        py = round(random.uniform(0.0, 6.28), 2)

        x_shake = f"(in_w-{target_w})/2+13*sin(t*3.2+{px})+6*cos(t*6.7+{px})+2*sin(t*12.1)"
        y_shake = f"(in_h-{target_h})/2+15*cos(t*2.8+{py})+7*sin(t*5.5+{py})+3*cos(t*10.7)"

        vf = (
            f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h}:'{x_shake}':'{y_shake}',"
            f"setsar=1,fps={fps}"
        )

        cmd = [
            ffmpeg_cmd, "-y",
            "-stream_loop", "-1",
            "-ss", f"{start_offset:.2f}",
            "-i", video_path,
            "-t", f"{duration:.3f}",
            "-vf", vf,
            "-an",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-threads", "4",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            output_clip_path
        ]

        if stop_event and stop_event.is_set():
            raise InterruptedError("Generation cancelled by user.")

        err_file = tempfile.TemporaryFile()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=err_file
        )
        try:
            while proc.poll() is None:
                if stop_event and stop_event.is_set():
                    try:
                        proc.terminate()
                        proc.kill()
                    except Exception:
                        pass
                    if os.path.exists(output_clip_path):
                        try:
                            os.remove(output_clip_path)
                        except Exception:
                            pass
                    raise InterruptedError("Generation cancelled by user.")
                time.sleep(0.05)
        except Exception:
            try:
                proc.terminate()
                proc.kill()
            except Exception:
                pass
            raise
        finally:
            err_file.seek(0)
            err_text = err_file.read().decode('utf-8', errors='replace')
            err_file.close()

        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg clip format failed: {err_text[-300:] if err_text else ''}")


# ==============================================================================
# 5. Master Pipeline Execution
# ==============================================================================
class ATCPipeline:
    def __init__(self, tts_backend: str = "kokoro"):
        self.tts = AviationTTSEngine(backend_type=tts_backend)
        self.radio_fx = AviationRadioFX()
        self.renderer = AviationVideoRenderer()

    def run(
        self,
        script_text: str,
        output_video_path: Optional[str] = None,
        plane_folder: Path = PLANE_VIDEOS_DIR,
        tower_folder: Path = TOWER_VIDEOS_DIR,
        pilot_speed: Optional[float] = None,
        tower_speed: Optional[float] = None,
        conversation_speed: Optional[float] = None,
        bg_volume: Optional[float] = None,
        voice_volume: Optional[float] = None,
        opening_delay: Optional[float] = None,
        progress_callback: Optional[Any] = None,
        stop_event: Optional[Any] = None
    ) -> str:
        def _notify(step: str, percent: int, message: str, **extra):
            if progress_callback:
                try:
                    progress_callback({"step": step, "percent": percent, "message": message, **extra})
                except Exception:
                    pass

        if stop_event and stop_event.is_set():
            raise InterruptedError("Generation cancelled by user.")

        turns = parse_conversation(script_text)
        if not turns:
            raise ValueError("No valid dialogue turns found in conversation script.")

        plane_vids = self.renderer.get_stock_videos(plane_folder)
        tower_vids = self.renderer.get_stock_videos(tower_folder)

        print(f"[ATC Pipeline] Parsed {len(turns)} turns.")
        print(f"[ATC Pipeline] Available Plane stock videos: {len(plane_vids)}")
        print(f"[ATC Pipeline] Available Tower stock videos: {len(tower_vids)}", flush=True)

        _notify("start", 5, f"Parsed {len(turns)} dialogue turns. Loading assets...", total_turns=len(turns))

        if not plane_vids:
            print(f"[WARNING] No stock plane videos found in {plane_folder}!", flush=True)
        if not tower_vids:
            print(f"[WARNING] No stock tower videos found in {tower_folder}!", flush=True)

        ffmpeg_cmd = shutil.which("ffmpeg") or "ffmpeg"
        temp_dir = Path(tempfile.mkdtemp(prefix="atc_render_"))

        try:
            # 1. Synthesize Audio for each turn
            print("\n--- 1. Generating Speech & Aviation Radio Audio (Emergency Mode) ---", flush=True)
            turn_audio_segments = []

            # Generate PTT clicks, Roger beeps & pause arrays
            sr = 24000
            ptt_dur = 0.04
            num_ptt_samples = int(ptt_dur * sr)
            t_ptt = np.linspace(0, ptt_dur, num_ptt_samples, endpoint=False)
            noise_ptt = (np.random.rand(num_ptt_samples) * 2 - 1) * np.exp(-t_ptt * 60)
            pop_ptt = 0.5 * np.sin(2 * np.pi * 1800 * t_ptt) * np.exp(-t_ptt * 80)
            ptt_audio = ((noise_ptt + pop_ptt) * 0.30).astype(np.float32)

            # Tactical Dual-Tone Chirp (2100Hz -> 1750Hz) + radio squelch burst at end of sentence
            roger_beep = self.radio_fx.create_roger_beep()

            pause_dur = 0.10  # Fast emergency radio turnaround (zero dead air)
            pause_audio = np.zeros(int(pause_dur * sr), dtype=np.float32)

            all_audio_chunks = []

            for idx, turn in enumerate(turns, start=1):
                if stop_event and stop_event.is_set():
                    raise InterruptedError("Generation cancelled by user.")
                raw_wav = str(temp_dir / f"turn_{idx}_raw.wav")
                radio_wav = str(temp_dir / f"turn_{idx}_radio.wav")

                speaker_type = "PILOT" if turn.is_pilot() else "TOWER"
                print(f"[{idx}/{len(turns)}] Synthesizing {speaker_type}: \"{turn.text}\"", flush=True)

                _notify(
                    "speech",
                    int(5 + (idx / len(turns)) * 42),
                    f"Synthesizing {speaker_type} turn {idx}/{len(turns)}...",
                    turn=idx,
                    total_turns=len(turns),
                    speaker=speaker_type,
                    text=turn.text
                )

                custom_spd = pilot_speed if turn.is_pilot() else tower_speed
                raw_dur = self.tts.synthesize_turn(turn, raw_wav, custom_speed=custom_spd)
                self.radio_fx.apply_radio_effect(
                    raw_wav,
                    radio_wav,
                    is_pilot=turn.is_pilot(),
                    tempo=conversation_speed,
                    volume=voice_volume
                )

                # Load radio processed audio
                data, in_sr = sf.read(radio_wav)
                if in_sr != sr:
                    from scipy.signal import resample
                    target_samples = int(len(data) * sr / in_sr)
                    data = resample(data, target_samples)
                if data.ndim > 1:
                    data = np.mean(data, axis=1)
                data = data.astype(np.float32)

                # Turn structure:
                # Turn 1 includes visual atmospheric delay before pilot speaks MAYDAY
                if idx == 1:
                    lead_in_dur = opening_delay if opening_delay is not None else 1.0
                    lead_in_audio = np.zeros(int(lead_in_dur * sr), dtype=np.float32)
                    turn_chunk = np.concatenate([lead_in_audio, ptt_audio, data, roger_beep, pause_audio])
                else:
                    turn_chunk = np.concatenate([ptt_audio, data, roger_beep, pause_audio])

                all_audio_chunks.append(turn_chunk)

                # Set exact duration for this turn's video clip
                turn_total_dur = len(turn_chunk) / float(sr)
                turn.duration = turn_total_dur

            # Write master audio file mixed with continuous cockpit & VHF radio static bed
            master_audio_wav = str(temp_dir / "master_radio_audio.wav")
            full_speech_and_beeps = np.concatenate(all_audio_chunks)
            total_sec = len(full_speech_and_beeps) / float(sr)

            # Continuous VHF static & turbine hum underneath the whole conversation
            # Generate with 1.0s buffer so background bed always covers full speech regardless of rounding
            bg_bed = self.radio_fx.create_background_radio_noise(total_sec + 1.0)
            match_len = min(len(full_speech_and_beeps), len(bg_bed))
            # Mix background bed at custom or default gentle level (0.10x for authentic background static bed)
            active_bg_vol = bg_volume if bg_volume is not None else 0.10
            full_audio = full_speech_and_beeps[:match_len] + (bg_bed[:match_len] * active_bg_vol)
            full_audio = np.clip(full_audio, -1.0, 1.0)

            sf.write(master_audio_wav, full_audio, sr)
            print(f"[Audio] Master radio audio rendered ({len(full_audio)/sr:.2f}s with Roger beeps & static bed): {master_audio_wav}", flush=True)

            _notify("audio_mixing", 48, f"Master radio audio mixed ({len(full_audio)/sr:.1f}s) with static bed.")

            # 2. Select Stock Videos & Format to 9:16
            print("\n--- 2. Selecting Stock Videos & Formatting to 9:16 ---", flush=True)
            video_segments = []

            for idx, turn in enumerate(turns, start=1):
                if stop_event and stop_event.is_set():
                    raise InterruptedError("Generation cancelled by user.")

                is_pilot = turn.is_pilot()
                pool = plane_vids if is_pilot else tower_vids
                role_name = "PLANE (Pilot)" if is_pilot else "TOWER (ATC)"

                clip_out = str(temp_dir / f"clip_{idx:03d}.mp4")

                _notify(
                    "video",
                    int(50 + (idx / len(turns)) * 38),
                    f"Formatting {role_name} clip {idx}/{len(turns)} with mobile camera shake...",
                    clip=idx,
                    total_clips=len(turns),
                    role=role_name
                )

                if pool:
                    chosen_vid = random.choice(pool)
                    print(f"[{idx}/{len(turns)}] {role_name} -> {os.path.basename(chosen_vid)} ({turn.duration:.2f}s)", flush=True)
                    self.renderer.cut_and_format_clip(
                        video_path=chosen_vid,
                        duration=turn.duration,
                        output_clip_path=clip_out,
                        stop_event=stop_event
                    )
                else:
                    # Fallback placeholder if user hasn't dropped videos in yet
                    color = "0x0b192c" if is_pilot else "0x1e3e62"
                    print(f"[{idx}/{len(turns)}] {role_name} -> Fallback color canvas ({turn.duration:.2f}s)")
                    cmd_placeholder = [
                        ffmpeg_cmd, "-y",
                        "-f", "lavfi",
                        "-i", f"color=c={color}:s=1080x1920:r=30",
                        "-t", f"{turn.duration:.3f}",
                        "-c:v", "libx264",
                        "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p",
                        clip_out
                    ]
                    subprocess.run(cmd_placeholder, capture_output=True, check=True)

                video_segments.append(clip_out)

            if stop_event and stop_event.is_set():
                raise InterruptedError("Generation cancelled by user.")

            # 3. Concatenate Video Segments & Mux Radio Audio
            print("\n--- 3. Assembling Master 9:16 Video (No Subtitles) ---")
            _notify("muxing", 92, "Assembling final broadcast 9:16 MP4 video...")
            video_concat_txt = str(temp_dir / "video_concat.txt")
            with open(video_concat_txt, "w", encoding="utf-8") as f:
                for v_file in video_segments:
                    clean_p = v_file.replace("\\", "/")
                    f.write(f"file '{clean_p}'\n")

            if not output_video_path:
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                output_video_path = str(OUTPUT_DIR / f"atc_radio_video_{random.randint(1000, 9999)}.mp4")

            Path(output_video_path).parent.mkdir(parents=True, exist_ok=True)

            cmd_final = [
                ffmpeg_cmd, "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", video_concat_txt,
                "-i", master_audio_wav,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "18",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                "-pix_fmt", "yuv420p",
                output_video_path
            ]

            err_file = tempfile.TemporaryFile()
            proc = subprocess.Popen(
                cmd_final,
                stdout=subprocess.DEVNULL,
                stderr=err_file
            )
            try:
                while proc.poll() is None:
                    if stop_event and stop_event.is_set():
                        try:
                            proc.terminate()
                            proc.kill()
                        except Exception:
                            pass
                        if output_video_path and os.path.exists(output_video_path):
                            try:
                                os.remove(output_video_path)
                            except Exception:
                                pass
                        raise InterruptedError("Generation cancelled by user.")
                    time.sleep(0.05)
            except Exception:
                try:
                    proc.terminate()
                    proc.kill()
                except Exception:
                    pass
                raise
            finally:
                err_file.seek(0)
                err_text = err_file.read().decode('utf-8', errors='replace')
                err_file.close()

            if proc.returncode != 0:
                raise RuntimeError(f"Final video assembly failed: {err_text[-300:] if err_text else ''}")

            print(f"\n[SUCCESS] Master ATC Radio Video generated successfully!")
            print(f"Output Video Path: {output_video_path}")
            _notify("done", 100, f"Video generated successfully: {os.path.basename(output_video_path)}", output=output_video_path)
            return output_video_path

        except InterruptedError:
            print(f"\n[CANCELLED] Generation cancelled by user. Deleting incomplete output...", flush=True)
            if output_video_path and os.path.exists(output_video_path):
                try:
                    os.remove(output_video_path)
                    print(f"[Cleanup] Deleted incomplete output: {output_video_path}", flush=True)
                except Exception as e:
                    print(f"[Cleanup] Could not delete {output_video_path}: {e}", flush=True)
            raise
        finally:
            # Clean up temp files
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass


# ==============================================================================
# CLI Entrypoint
# ==============================================================================
def load_scripts_from_json(json_path: str) -> List[Tuple[str, str]]:
    """
    Loads scripts from a JSON file supporting flexible structures:
    1. [{"title": "slug_name", "dialogue": [{"speaker": "Pilot", "text": "..."}, ...]}, ...]
    2. [{"title": "slug_name", "script": "Pilot: ...\nTower: ..."}, ...]
    3. {"scripts": [...]} wrapper
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        # Extract list if wrapped in 'scripts' or 'conversations'
        data = data.get("scripts") or data.get("conversations") or list(data.values())[0]

    if not isinstance(data, list):
        raise ValueError("JSON must contain an array of script objects.")

    results: List[Tuple[str, str]] = []
    for idx, item in enumerate(data, start=1):
        raw_title = item.get("title") or item.get("id") or f"atc_video_{idx:02d}"
        safe_title = re.sub(r'[^a-zA-Z0-9_\-]', '_', raw_title).strip('_')

        # Check dialogue format
        if "dialogue" in item and isinstance(item["dialogue"], list):
            script_lines = []
            for d in item["dialogue"]:
                spk = d.get("speaker", "Pilot")
                txt = d.get("text", "")
                script_lines.append(f"{spk}: {txt}")
            script_text = "\n".join(script_lines)
        elif "script" in item:
            script_text = str(item["script"])
        else:
            # Fallback if text is under another key
            script_text = item.get("text", "")

        if script_text.strip():
            results.append((safe_title, script_text))

    return results


# ==============================================================================
# CLI Entrypoint & Batch Runner
# ==============================================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Aviation ATC & Pilot Radio Video Generator")
    parser.add_argument("--script", type=str, default="", help="Path to text file containing conversation, or raw text string.")
    parser.add_argument("--batch", type=str, default="", help="Path to JSON file containing multiple scripts.")
    parser.add_argument("--output", type=str, default="", help="Output MP4 file path.")
    parser.add_argument("--plane_dir", type=str, default=str(PLANE_VIDEOS_DIR), help="Directory of stock plane videos.")
    parser.add_argument("--tower_dir", type=str, default=str(TOWER_VIDEOS_DIR), help="Directory of stock tower videos.")
    parser.add_argument("--tts", type=str, default="kokoro", choices=["kokoro", "qwen3tts"], help="TTS engine backend.")
    parser.add_argument("--overwrite", action="store_true", help="Force overwrite of existing rendered videos.")

    args = parser.parse_args()

    pipeline = ATCPipeline(tts_backend=args.tts)
    plane_path = Path(args.plane_dir)
    tower_path = Path(args.tower_dir)

    # Check if batch mode is triggered
    batch_file = args.batch
    if not batch_file and os.path.exists("scripts.json") and not args.script:
        batch_file = "scripts.json"

    if batch_file and os.path.exists(batch_file):
        scripts = load_scripts_from_json(batch_file)
        print("==================================================================", flush=True)
        print(f"  BATCH MODE: Found {len(scripts)} scripts in {batch_file}", flush=True)
        print("==================================================================\n", flush=True)

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        rendered_outputs = []

        for i, (title, script_content) in enumerate(scripts, start=1):
            out_mp4 = str(OUTPUT_DIR / f"{title}.mp4")

            # Resume support: skip if video was already rendered successfully
            if os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1000000 and not args.overwrite:
                size_mb = os.path.getsize(out_mp4) / (1024 * 1024)
                print(f"  [Batch {i}/{len(scripts)}] [ALREADY RENDERED] {title}.mp4 ({size_mb:.1f} MB) -> Skipping.", flush=True)
                rendered_outputs.append(out_mp4)
                continue

            print(f"\n>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>", flush=True)
            print(f"  [Batch {i}/{len(scripts)}] Rendering Video: {title}.mp4", flush=True)
            print(f">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>\n", flush=True)

            try:
                out_path = pipeline.run(
                    script_text=script_content,
                    output_video_path=out_mp4,
                    plane_folder=plane_path,
                    tower_folder=tower_path
                )
                rendered_outputs.append(out_path)
            except Exception as e:
                print(f"\n[ERROR] Failed rendering {title}.mp4: {e}", flush=True)
                import traceback
                traceback.print_exc()
                print(f"[CONTINUE] Continuing to next video in batch...\n", flush=True)

        print("\n==================================================================", flush=True)
        print(f"  BATCH COMPLETE: {len(rendered_outputs)} videos ready in '{OUTPUT_DIR}'!", flush=True)
        print("==================================================================", flush=True)
        for out in rendered_outputs:
            print(f"  - {out}", flush=True)

    else:
        # Single script mode
        sample_dialogue = """
        Pilot: Boston Tower, American 442 heavy with you on the visual runway 4 Right, over.
        Tower: American 442 heavy, Boston Tower, winds 040 at 12 knots, runway 4 Right, cleared to land, over.
        Pilot: Cleared to land runway 4 Right, American 442 heavy, over.
        """
        script_content = sample_dialogue
        if args.script:
            if os.path.exists(args.script):
                with open(args.script, "r", encoding="utf-8") as f:
                    script_content = f.read()
            else:
                script_content = args.script
        elif os.path.exists("conversation.txt"):
            with open("conversation.txt", "r", encoding="utf-8") as f:
                script_content = f.read()

        out_video = pipeline.run(
            script_text=script_content,
            output_video_path=args.output or None,
            plane_folder=plane_path,
            tower_folder=tower_path
        )

