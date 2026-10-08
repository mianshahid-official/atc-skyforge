from __future__ import annotations
import os
from enum import Enum
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from pydantic import BaseModel, Field


class AspectRatio(str, Enum):
    RATIO_16_9 = "16:9"   # Landscape / YouTube Standard (1920x1080 / 3840x2160)
    RATIO_9_16 = "9:16"   # Portrait / Shorts / Reels (1080x1920)
    RATIO_1_1 = "1:1"     # Square (1080x1080)
    RATIO_4_5 = "4:5"     # Social Feed (1080x1350)


class MotionEffect(str, Enum):
    KEN_BURNS = "ken_burns"       # Cinematic gentle slow pan & zoom
    ZOOM_IN = "zoom_in"           # Smooth zoom in to center
    ZOOM_OUT = "zoom_out"         # Smooth zoom out from center
    PAN_LEFT = "pan_left"         # Smooth horizontal pan left
    PAN_RIGHT = "pan_right"       # Smooth horizontal pan right
    PULSE = "pulse"               # Subtle breathing pulse
    STATIC = "static"             # No motion


class SubtitlePosition(str, Enum):
    BOTTOM_SAFE = "bottom_safe"
    LOWER_MIDDLE = "lower_middle"
    MIDDLE = "middle"
    UPPER_MIDDLE = "upper_middle"
    TOP = "top"


class SubtitleWordMode(str, Enum):
    ONE = "1"
    TWO = "2"
    THREE = "3"
    FOUR = "4"
    AUTO = "auto"


class WordTimestamp(BaseModel):
    word: str
    start: float
    end: float
    score: Optional[float] = 1.0


class NarrationData(BaseModel):
    text: str = ""
    audio_path: str = ""
    duration_seconds: float = 0.0
    words: List[WordTimestamp] = Field(default_factory=list)


class TrackLayer(str, Enum):
    BACKDROP = "backdrop"
    TRACK_1 = "track_1"   # Base / Primary
    TRACK_2 = "track_2"   # Secondary / Mid-layer
    TRACK_3 = "track_3"   # Top Overlay / Inset


class CanvasTransform(BaseModel):
    x: float = 0.0          # 0.0 to 1.0 (relative canvas coordinate)
    y: float = 0.0          # 0.0 to 1.0
    w: float = 1.0          # 0.0 to 1.0 (relative width)
    h: float = 1.0          # 0.0 to 1.0 (relative height)
    scale: float = 1.0
    rotation: float = 0.0
    opacity: float = 1.0
    blur_px: float = 0.0
    drop_shadow: bool = False
    easing: str = "easeInOutCubic"   # easeInOutCubic, smoothstep, easeOutBack, linear


class ImageClip(BaseModel):
    index: int
    image_path: str
    filename: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    duration: float = 0.0
    motion: MotionEffect = MotionEffect.KEN_BURNS
    assigned_text: Optional[str] = ""
    is_video: bool = False
    media_type: str = "image"   # "image" or "video"
    speed: float = 1.0
    scale: float = 1.0
    position_x: float = 0.0
    position_y: float = 0.0
    rotation: float = 0.0
    opacity: float = 1.0
    # Multi-Track Canvas & Story-Driven Staging attributes
    track_layer: TrackLayer = TrackLayer.TRACK_1
    role: str = "primary"  # primary, secondary, inset, backdrop, map, document
    initial_transform: Optional[CanvasTransform] = None
    target_transform: Optional[CanvasTransform] = None
    animation_start_time: Optional[float] = None
    animation_duration: float = 1.0
    backdrop_blur_px: float = 18.0
    backdrop_darken: float = 0.55


class SoundEffect(BaseModel):
    id: str
    name: str = "SFX"
    file_path: str
    start_time: float = 0.0
    duration: Optional[float] = None
    volume: float = 0.8
    fade_in: float = 0.0
    fade_out: float = 0.0


class MusicTrack(BaseModel):
    enabled: bool = False
    file_path: Optional[str] = None
    volume: float = 0.15
    ducking_volume: float = 0.04
    ducking_attack_ms: int = 20
    ducking_release_ms: int = 300
    fade_in: float = 1.5
    fade_out: float = 2.0


class VideoOverlay(BaseModel):
    enabled: bool = True
    file_path: str
    name: str = "Overlay"
    opacity: float = 0.4
    speed: float = 1.0
    blend_mode: str = "screen"   # screen, overlay, lighten, addition, normal
    loop: bool = True
    start_time: float = 0.0
    end_time: Optional[float] = None


class SubtitleConfig(BaseModel):
    enabled: bool = True
    font_family: str = "Arial Black"
    font_size: int = 54
    fill_color: str = "#FFFFFF"
    highlight_color: str = "#FFD700"  # Vibrant Gold
    outline_color: str = "#000000"
    outline_thickness: int = 3
    shadow_offset: int = 2
    bold: bool = True
    italic: bool = False
    background_color: Optional[str] = None
    words_per_caption: SubtitleWordMode = SubtitleWordMode.TWO
    position: SubtitlePosition = SubtitlePosition.BOTTOM_SAFE
    safe_zone_padding_px: int = 140


class DynamicSceneBeat(BaseModel):
    scene_id: int
    narration_text: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    duration: float = 0.0
    narrative_intent: str = "establishing"  # contrast, evidence, scale, establishing, climax, transition
    enable_backdrop: bool = True
    backdrop_asset: Optional[str] = None
    track_1_clip: Optional[ImageClip] = None
    track_2_clip: Optional[ImageClip] = None
    track_3_clip: Optional[ImageClip] = None


class ProjectConfig(BaseModel):
    project_id: str
    project_name: str
    aspect_ratio: AspectRatio = AspectRatio.RATIO_16_9
    fps: int = 30
    resolution: Tuple[int, int] = (1920, 1080)
    narration: NarrationData = Field(default_factory=NarrationData)
    images: List[ImageClip] = Field(default_factory=list)
    multi_track_beats: List[DynamicSceneBeat] = Field(default_factory=list)
    sfx_tracks: List[SoundEffect] = Field(default_factory=list)
    music: MusicTrack = Field(default_factory=MusicTrack)
    overlays: List[VideoOverlay] = Field(default_factory=list)
    subtitles: SubtitleConfig = Field(default_factory=SubtitleConfig)
    total_duration_sec: float = 0.0
    output_video_path: Optional[str] = None
    status: str = "Draft"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
