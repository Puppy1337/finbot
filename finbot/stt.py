"""Распознавание голосовых сообщений (speech-to-text).

Два варианта:
- "openai": любой OpenAI-совместимый endpoint /audio/transcriptions (OpenAI Whisper, Groq — бесплатный тариф).
- "local":  библиотека faster-whisper, работает на CPU без внешних сервисов (нужно pip install faster-whisper).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)


class STTError(Exception):
    pass


class SpeechToText:
    def __init__(self, provider: str, api_key: str = "", base_url: str = "https://api.openai.com/v1",
                 model: str = "whisper-1", transport: Optional[httpx.AsyncBaseTransport] = None):
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.client = httpx.AsyncClient(timeout=120.0, transport=transport)
        self._local_model = None

    async def close(self) -> None:
        await self.client.aclose()

    @property
    def available(self) -> bool:
        if self.provider == "local":
            try:
                import faster_whisper  # noqa: F401
                return True
            except ImportError:
                return False
        return bool(self.api_key)

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg", language: str = "ru") -> str:
        if self.provider == "local":
            return await asyncio.get_running_loop().run_in_executor(None, self._transcribe_local, audio, language)
        if not self.api_key:
            raise STTError("Не настроен ключ для распознавания речи (STT_API_KEY в .env)")
        r = await self.client.post(
            f"{self.base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            data={"model": self.model, "language": language, "response_format": "json"},
            files={"file": (filename, audio, "audio/ogg")},
        )
        if r.status_code != 200:
            raise STTError(f"STT {r.status_code}: {r.text[:300]}")
        text = (r.json().get("text") or "").strip()
        if not text:
            raise STTError("Не удалось разобрать речь")
        return text

    def _transcribe_local(self, audio: bytes, language: str) -> str:
        import tempfile
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise STTError("faster-whisper не установлен (pip install faster-whisper)") from e
        if self._local_model is None:
            self._local_model = WhisperModel(self.model if self.model != "whisper-1" else "small",
                                             device="cpu", compute_type="int8")
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=True) as f:
            f.write(audio)
            f.flush()
            segments, _ = self._local_model.transcribe(f.name, language=language, vad_filter=True)
            text = " ".join(s.text.strip() for s in segments).strip()
        if not text:
            raise STTError("Не удалось разобрать речь")
        return text
