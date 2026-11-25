"""Optional local-only voice transcription; model and audio are not uploaded here."""
from __future__ import annotations
import io
from pathlib import Path

MAX_VOICE_BYTES = 5 * 1024 * 1024
MAX_VOICE_SECONDS = 120


class VoiceUnavailable(RuntimeError):
    pass


def transcribe(audio: bytes, model_path: Path) -> str:
    if not audio or len(audio) > MAX_VOICE_BYTES:
        raise VoiceUnavailable('Voice note is empty or exceeds 5 MB.')
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise VoiceUnavailable('Voice support is not installed. See README for optional local setup.') from exc
    if not model_path.is_dir():
        raise VoiceUnavailable('The local Whisper model is missing. See README for optional setup.')
    try:
        model = WhisperModel(str(model_path), device='cpu', compute_type='int8', local_files_only=True)
        segments, _info = model.transcribe(io.BytesIO(audio), beam_size=1, vad_filter=True)
        text = ' '.join(segment.text.strip() for segment in segments).strip()
    except Exception as exc:
        raise VoiceUnavailable('Local transcription failed; check the local model and audio.') from exc
    if not text:
        raise VoiceUnavailable('No speech was recognized. Please try again or type your request.')
    if len(text) > 4000:
        raise VoiceUnavailable('Transcript is too long; please send a shorter voice note.')
    return text
