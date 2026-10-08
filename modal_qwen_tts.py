"""
Modal deployment for Qwen3-TTS on NVIDIA T4 GPU.
Dedicated for ATC & Cockpit Voice Generation with separated emotion guidelines.

Fixed Voices:
- Pilot: 'Ryan' (English Male) with high adrenaline/panic emotion instruction.
- Tower (ATC): 'Serena' (English Female) with calm, authoritative controller instruction.
Guidelines are passed strictly via the `instruct` parameter, kept completely separate from dialogue text.
"""

import io
import modal
from typing import Optional, List
from pydantic import BaseModel

# 1. Initialize Modal App
app = modal.App("atc-qwen3-tts")

# 2. Build Container Image
MODEL_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
TOKENIZER_ID = "Qwen/Qwen3-TTS-Tokenizer-12Hz"

def download_model():
    """Pre-download weights and tokenizer during image build so startup is instant."""
    from huggingface_hub import snapshot_download
    print(f"Downloading model {MODEL_ID} and tokenizer {TOKENIZER_ID} to image cache...")
    snapshot_download(MODEL_ID)
    try:
        snapshot_download(TOKENIZER_ID)
    except Exception as e:
        print(f"Note on tokenizer download: {e}")
    print("Model assets cached successfully!")

tts_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "libsndfile1", "git", "sox", "libsox-fmt-all")
    .pip_install(
        "torch>=2.4.0",
        "torchaudio",
        "transformers>=4.48.0",
        "accelerate",
        "soundfile",
        "qwen-tts",
        "fastapi[standard]",
        "pydantic",
        "huggingface_hub",
    )
    .run_function(download_model)
)

# 3. Fixed Voice & Instruction Defaults
PILOT_SPEAKER = "Ryan"
PILOT_DEFAULT_INSTRUCT = (
    "SCREAM the words in sheer, unhinged panic! Terrified pilot in a life-or-death emergency, "
    "rapid-fire fast speech, speaking very fast, screaming desperately at the top of his lungs, "
    "gasping for breath, hyperventilating with extreme adrenaline, voice cracking, trembling "
    "and shouting with raw terror over blaring cockpit alarms!"
)

TOWER_SPEAKER = "Eric"
TOWER_DEFAULT_INSTRUCT = (
    "Male air traffic controller with a dry, humorous, witty, relaxed, deadpan, sarcastic tone, unimpressed and slightly amused, steady pace"
)

# 4. Request / Response Schemas for Web API
class TurnRequest(BaseModel):
    text: str
    role: str = "pilot"  # "pilot" or "tower"
    speaker: Optional[str] = None  # None -> auto-selects Ryan (pilot) or Serena (tower)
    instruct: Optional[str] = None  # None -> auto-selects panic (pilot) or calm (tower)
    language: str = "English"

class TurnItem(BaseModel):
    speaker_role: str  # "PILOT" or "ATC" / "TOWER"
    text: str
    instruct: Optional[str] = None

class BatchRequest(BaseModel):
    turns: List[TurnItem]
    language: str = "English"

# 5. Qwen3-TTS Service Class
@app.cls(
    image=tts_image,
    gpu="T4",
    timeout=600,
    scaledown_window=120,
)
class Qwen3TTSService:
    @modal.enter()
    def load_model(self):
        import torch
        from qwen_tts import Qwen3TTSModel
        print(f"Loading {MODEL_ID} on NVIDIA T4 GPU with bfloat16...")
        self.model = Qwen3TTSModel.from_pretrained(
            MODEL_ID,
            device_map="cuda:0",
            dtype=torch.bfloat16,
        )
        print("Model loaded successfully into GPU memory!")

    def _resolve_speaker_and_instruct(
        self,
        role: str,
        speaker: Optional[str] = None,
        instruct: Optional[str] = None,
    ):
        role_lower = (role or "pilot").strip().lower()
        if "tower" in role_lower or "atc" in role_lower or "controller" in role_lower:
            selected_speaker = speaker or TOWER_SPEAKER
            selected_instruct = instruct or TOWER_DEFAULT_INSTRUCT
        else:
            selected_speaker = speaker or PILOT_SPEAKER
            selected_instruct = instruct or PILOT_DEFAULT_INSTRUCT
        return selected_speaker, selected_instruct

    def _synthesize_single(
        self,
        text: str,
        role: str = "pilot",
        speaker: Optional[str] = None,
        instruct: Optional[str] = None,
        language: str = "English",
    ) -> bytes:
        import soundfile as sf

        selected_speaker, selected_instruct = self._resolve_speaker_and_instruct(
            role=role, speaker=speaker, instruct=instruct
        )

        print(f"Generating voice: speaker={selected_speaker}, role={role}")
        # Lock seed to 42 so the speaker's vocal timbre and pitch remain 100% consistent across turns
        import torch
        torch.manual_seed(42)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(42)

        # Clean comma pauses between repeated MAYDAY words to sound real and urgent
        import re
        clean_text = re.sub(r'\b(mayday)\s*,\s*(?=mayday\b)', r'\1 ', text, flags=re.IGNORECASE)

        # In Qwen3-TTS: text and instruct are separate parameters
        wavs, sr = self.model.generate_custom_voice(
            text=clean_text,
            language=language,
            speaker=selected_speaker,
            instruct=selected_instruct,
        )

        buf = io.BytesIO()
        sf.write(buf, wavs[0], sr, format="WAV")
        return buf.getvalue()

    @modal.method()
    def synthesize_turn(
        self,
        text: str,
        role: str = "pilot",
        speaker: Optional[str] = None,
        instruct: Optional[str] = None,
        language: str = "English",
    ) -> bytes:
        """
        Synthesize a single turn of dialogue into WAV audio bytes.
        Emotion guideline is strictly passed in `instruct`, completely separated from `text`.
        """
        return self._synthesize_single(
            text=text,
            role=role,
            speaker=speaker,
            instruct=instruct,
            language=language,
        )

    def _synthesize_batch(
        self,
        turns: list,
        language: str = "English",
    ) -> list:
        import soundfile as sf

        results = []
        for i, turn in enumerate(turns):
            role = turn.get("speaker_role") or turn.get("role") or turn.get("speaker", "pilot")
            text = turn.get("text", "").strip()
            if not text:
                continue

            speaker_override = turn.get("speaker_override")
            instruct_override = turn.get("instruct")

            selected_speaker, selected_instruct = self._resolve_speaker_and_instruct(
                role=role, speaker=speaker_override, instruct=instruct_override
            )

            import torch
            torch.manual_seed(42)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(42)

            import re
            clean_text = re.sub(r'\b(mayday)\s*,\s*(?=mayday\b)', r'\1 ', text, flags=re.IGNORECASE)

            wavs, sr = self.model.generate_custom_voice(
                text=clean_text,
                language=language,
                speaker=selected_speaker,
                instruct=selected_instruct,
            )

            buf = io.BytesIO()
            sf.write(buf, wavs[0], sr, format="WAV")

            results.append({
                "index": i,
                "role": role,
                "speaker": selected_speaker,
                "text": text,
                "wav_bytes": buf.getvalue(),
                "sample_rate": sr,
            })

        return results

    @modal.method()
    def synthesize_dialogue_batch(
        self,
        turns: list,
        language: str = "English",
    ) -> list:
        """
        Synthesize an entire list of conversation turns in one GPU session.
        Returns a list of dicts with audio bytes and metadata.
        """
        return self._synthesize_batch(turns=turns, language=language)

    @modal.fastapi_endpoint(method="POST")
    def api_synthesize(self, req: TurnRequest):
        """HTTP Web API for single turn generation returning WAV audio directly."""
        from fastapi import Response

        wav_bytes = self._synthesize_single(
            text=req.text,
            role=req.role,
            speaker=req.speaker,
            instruct=req.instruct,
            language=req.language,
        )
        return Response(content=wav_bytes, media_type="audio/wav")

    @modal.fastapi_endpoint(method="POST")
    def api_batch(self, req: BatchRequest):
        """HTTP Web API for batch turn generation returning base64 encoded audio."""
        import base64

        turns_data = [t.model_dump() for t in req.turns]
        raw_results = self._synthesize_batch(turns_data, language=req.language)

        output = []
        for item in raw_results:
            output.append({
                "index": item["index"],
                "role": item["role"],
                "speaker": item["speaker"],
                "text": item["text"],
                "sample_rate": item["sample_rate"],
                "audio_base64": base64.b64encode(item["wav_bytes"]).decode("utf-8"),
            })
        return {"turns": output}


# 6. Local CLI test entrypoint
@app.local_entrypoint()
def test_cli():
    print("Testing Qwen3-TTS Deployment on Modal (T4 GPU)...")
    service = Qwen3TTSService()

    # Test Pilot
    pilot_text = "Mayday, mayday, mayday! Radar altimeter failure, cockpit warning lights flashing red!"
    print(f"\n[1/2] Synthesizing Pilot line: '{pilot_text}'")
    pilot_wav = service.synthesize_turn.remote(text=pilot_text, role="pilot")
    with open("test_pilot_qwen3.wav", "wb") as f:
        f.write(pilot_wav)
    print("Saved test_pilot_qwen3.wav!")

    # Test Tower
    tower_text = "Aircraft calling Mayday, squawk 7700, radar contact lost, state fuel and souls on board."
    print(f"\n[2/2] Synthesizing Tower line: '{tower_text}'")
    tower_wav = service.synthesize_turn.remote(text=tower_text, role="tower")
    with open("test_tower_qwen3.wav", "wb") as f:
        f.write(tower_wav)
    print("Saved test_tower_qwen3.wav!")

    print("\nAll tests completed successfully!")
