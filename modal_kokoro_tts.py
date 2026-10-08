"""
Modal deployment for Kokoro TTS (82M) with full feature set:
- Ultra-low latency synthesis (sub-second per sentence)
- Native speech speed control (0.5x - 2.0x)
- Extensive built-in voices (am_adam, am_fenrir, am_michael, am_echo, af_heart, etc.)
- Automatic MAYDAY comma-pause removal for natural panic delivery
- Single turn endpoint, Batch endpoint, and Full conversation with VHF radio acoustic filtering
"""

import io
import re
import shutil
import tempfile
import subprocess
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
import modal

# 1. Initialize Modal App
app = modal.App("atc-kokoro-tts")

# 2. Build Container Image with pre-cached model & espeak-ng
def cache_kokoro():
    """Bake Kokoro model weights and voices into the container image for 1-second cold start."""
    from kokoro import KPipeline
    print("Baking Kokoro-82M model and voices into container image...")
    pipeline = KPipeline(lang_code='a')
    # Warm up pilot and tower voices
    for v in ["am_adam", "am_fenrir", "am_michael", "am_echo"]:
        try:
            gen = pipeline("Ready for departure.", voice=v, speed=1.35)
            for _ in gen:
                pass
        except Exception as e:
            print(f"Warmup notice for {v}: {e}")
    print("Kokoro baked into image cache successfully!")

kokoro_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "libsndfile1", "espeak-ng", "ffmpeg")
    .pip_install(
        "torch>=2.4.0",
        "soundfile",
        "kokoro>=0.9.2",
        "fastapi[standard]",
        "pydantic",
        "numpy",
    )
    .run_function(cache_kokoro)
)

# 3. Default Voice & Speed Profiles
DEFAULT_PILOT_VOICE = "am_adam"     # Crisp, dynamic, high-adrenaline male voice
DEFAULT_TOWER_VOICE = "am_fenrir"   # Authoritative, dry, masculine controller voice

DEFAULT_PILOT_SPEED = 1.38          # Fast, urgent, life-or-death panic pace
DEFAULT_TOWER_SPEED = 1.28          # Crisp, steady, witty controller pace


def clean_mayday_text(text: str) -> str:
    """Removes commas between repeated MAYDAY words to eliminate robotic pauses."""
    cleaned = re.sub(r'\b(mayday)\s*,\s*(?=mayday\b)', r'\1 ', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b(pan\s+pan)\s*,\s*(?=pan\s+pan\b)', r'\1 ', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


# 4. Request / Response Models
class SynthesizeRequest(BaseModel):
    text: str
    role: str = "pilot"           # "pilot" or "tower" / "atc"
    voice: Optional[str] = None   # Explicit voice (e.g. "am_adam", "am_fenrir")
    speed: Optional[float] = None # Explicit speed multiplier (e.g. 1.38)
    lang_code: str = "a"          # "a" for American English, "b" for British

class ConversationTurnItem(BaseModel):
    speaker: str                  # "Pilot" or "Tower"
    text: str
    voice: Optional[str] = None
    speed: Optional[float] = None

class BatchSynthesizeRequest(BaseModel):
    turns: List[ConversationTurnItem]
    default_pilot_speed: float = DEFAULT_PILOT_SPEED
    default_tower_speed: float = DEFAULT_TOWER_SPEED
    apply_radio_filter: bool = True


# 5. Kokoro TTS Service Class on Modal
@app.cls(
    image=kokoro_image,
    gpu="T4",
    timeout=300,
    scaledown_window=120,
)
class KokoroTTSService:
    @modal.enter()
    def load_pipeline(self):
        import torch
        from kokoro import KPipeline
        print("Initializing Kokoro KPipeline on GPU...")
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self.pipeline_us = KPipeline(lang_code='a', device=device)
        self.pipeline_gb = KPipeline(lang_code='b', device=device)
        print(f"Kokoro KPipeline ready on {device}!")

    def _get_pipeline(self, lang_code: str = "a"):
        return self.pipeline_gb if lang_code.lower() == "b" else self.pipeline_us

    def _resolve_voice_and_speed(
        self,
        role: str,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
    ):
        role_lower = (role or "pilot").strip().lower()
        is_tower = "tower" in role_lower or "atc" in role_lower or "controller" in role_lower
        
        selected_voice = voice or (DEFAULT_TOWER_VOICE if is_tower else DEFAULT_PILOT_VOICE)
        selected_speed = speed if speed is not None else (DEFAULT_TOWER_SPEED if is_tower else DEFAULT_PILOT_SPEED)
        return selected_voice, selected_speed, is_tower

    def _synthesize_raw(
        self,
        text: str,
        voice: str,
        speed: float,
        lang_code: str = "a",
    ) -> bytes:
        import soundfile as sf
        import numpy as np

        pipeline = self._get_pipeline(lang_code)
        clean_text = clean_mayday_text(text)

        generator = pipeline(clean_text, voice=voice, speed=speed)
        audio_segments = []
        sample_rate = 24000

        for _, _, audio in generator:
            if audio is not None and len(audio) > 0:
                audio_segments.append(audio)

        if not audio_segments:
            raise ValueError(f"Kokoro produced no audio for text: '{text}'")

        full_audio = np.concatenate(audio_segments) if len(audio_segments) > 1 else audio_segments[0]

        buf = io.BytesIO()
        sf.write(buf, full_audio, sample_rate, format="WAV")
        return buf.getvalue()

    def _apply_radio_filter(self, wav_bytes: bytes, is_pilot: bool) -> bytes:
        """Applies VHF radio acoustic bandpass filter and compressor."""
        hp = 340 if is_pilot else 300
        lp = 3400 if is_pilot else 3600
        vol = 1.35 if is_pilot else 1.25

        filter_complex = (
            f"highpass=f={hp},"
            f"lowpass=f={lp},"
            f"acompressor=threshold=-18dB:ratio=4:attack=5:release=50:makeup=2dB,"
            f"volume={vol}"
        )

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as in_f, \
             tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as out_f:
            in_path = in_f.name
            out_path = out_f.name
            in_f.write(wav_bytes)

        try:
            cmd = [
                "ffmpeg", "-y",
                "-i", in_path,
                "-af", filter_complex,
                "-ar", "24000",
                "-ac", "1",
                out_path
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            with open(out_path, "rb") as f:
                return f.read()
        finally:
            import os
            for p in (in_path, out_path):
                if os.path.exists(p):
                    os.unlink(p)

    @modal.method()
    def synthesize_turn(
        self,
        text: str,
        role: str = "pilot",
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        lang_code: str = "a",
        apply_radio: bool = False,
    ) -> bytes:
        """Modal remote method for single turn speech generation."""
        sel_voice, sel_speed, is_tower = self._resolve_voice_and_speed(role, voice, speed)
        raw_wav = self._synthesize_raw(text=text, voice=sel_voice, speed=sel_speed, lang_code=lang_code)
        if apply_radio:
            return self._apply_radio_filter(raw_wav, is_pilot=not is_tower)
        return raw_wav

    @modal.method()
    def synthesize_conversation_batch(
        self,
        turns: list,
        default_pilot_speed: float = DEFAULT_PILOT_SPEED,
        default_tower_speed: float = DEFAULT_TOWER_SPEED,
        apply_radio: bool = True,
    ) -> list:
        """Modal remote method for batch conversation synthesis."""
        results = []
        for i, t in enumerate(turns):
            role = t.get("speaker") or t.get("role", "pilot")
            text = t.get("text", "").strip()
            if not text:
                continue

            voice_override = t.get("voice")
            speed_override = t.get("speed")
            sel_voice, sel_speed, is_tower = self._resolve_voice_and_speed(
                role=role,
                voice=voice_override,
                speed=speed_override or (default_tower_speed if is_tower else default_pilot_speed),
            )

            raw_wav = self._synthesize_raw(text=text, voice=sel_voice, speed=sel_speed)
            processed_wav = self._apply_radio_filter(raw_wav, is_pilot=not is_tower) if apply_radio else raw_wav

            results.append({
                "index": i,
                "role": "tower" if is_tower else "pilot",
                "voice": sel_voice,
                "speed": sel_speed,
                "text": text,
                "wav_bytes": processed_wav,
            })
        return results

    @modal.fastapi_endpoint(method="POST")
    def api_synthesize(self, req: SynthesizeRequest):
        """HTTP Web API for single turn generation returning WAV audio directly."""
        from fastapi import Response

        sel_voice, sel_speed, is_tower = self._resolve_voice_and_speed(req.role, req.voice, req.speed)
        wav_bytes = self._synthesize_raw(
            text=req.text,
            voice=sel_voice,
            speed=sel_speed,
            lang_code=req.lang_code,
        )
        return Response(content=wav_bytes, media_type="audio/wav")

    @modal.fastapi_endpoint(method="POST")
    def api_batch(self, req: BatchSynthesizeRequest):
        """HTTP Web API for batch conversation turns with base64 encoded audio."""
        import base64

        turns_data = [t.model_dump() for t in req.turns]
        raw_results = self.synthesize_conversation_batch.local(
            self,
            turns=turns_data,
            default_pilot_speed=req.default_pilot_speed,
            default_tower_speed=req.default_tower_speed,
            apply_radio=req.apply_radio_filter,
        )

        output = []
        for item in raw_results:
            output.append({
                "index": item["index"],
                "role": item["role"],
                "voice": item["voice"],
                "speed": item["speed"],
                "text": item["text"],
                "audio_base64": base64.b64encode(item["wav_bytes"]).decode("utf-8"),
            })
        return {"turns": output, "count": len(output)}

    @modal.fastapi_endpoint(method="GET")
    def api_voices(self):
        """HTTP Web API listing all available Kokoro voices."""
        return {
            "default_pilot": {"voice": DEFAULT_PILOT_VOICE, "speed": DEFAULT_PILOT_SPEED, "style": "Panic / Urgent Male"},
            "default_tower": {"voice": DEFAULT_TOWER_VOICE, "speed": DEFAULT_TOWER_SPEED, "style": "Dry / Humorous Male ATC"},
            "available_voices": {
                "american_male": ["am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa"],
                "american_female": ["af_bella", "af_sarah", "af_nicole", "af_sky", "af_heart", "af_alloy", "af_aoede", "af_jessica", "af_kore", "af_nova", "af_river"],
                "british_male": ["bm_daniel", "bm_fable", "bm_george", "bm_lewis"],
                "british_female": ["bf_alice", "bf_emma", "bf_isabella", "bf_lily"]
            }
        }
