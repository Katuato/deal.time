"""Serialized access to the Whisper instance used by Telegram and the Mini App."""
import asyncio
import io
import subprocess
import wave

_lock = asyncio.Lock()
_processor = None


async def initialize_stt():
    global _processor
    async with _lock:
        if _processor is None:
            from modules.stt_module import STTProcessor
            _processor = await asyncio.to_thread(STTProcessor)
        return _processor


# Only accept containers produced by the browser recorder. Limit decoded audio
# independently of the HTTP body size before handing it to the model.
FORMATS = {"audio/webm": "matroska", "audio/ogg": "ogg", "audio/mp4": "mov",
           "video/mp4": "mov", "audio/x-m4a": "mov"}
MAX_AUDIO_BYTES = 2 * 1024 * 1024


def decode_recording(data: bytes, container: str) -> bytes:
    with io.BytesIO() as buffer:
        try:
            pcm = subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "pipe",
                "-f", container, "-i", "pipe:0", "-map", "0:a:0", "-t", "46",
                "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"
            ], input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
               check=True, timeout=12).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Не удалось прочитать аудио. Запишите ещё раз.") from exc
        if not pcm or len(pcm) > 45 * 32000:
            raise ValueError("Нужна непустая запись длительностью до 45 секунд.")
        with wave.open(buffer, "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(pcm)
        return buffer.getvalue()


class SpeechBusy(Exception):
    pass


async def transcribe_miniapp(data: bytes, container: str) -> str:
    # Avoid queueing uploads in memory behind a CPU-bound inference.
    if _lock.locked():
        raise SpeechBusy
    processor = await initialize_stt()
    async with _lock:
        def run():
            return processor.transcribe(decode_recording(data, container))
        job = asyncio.create_task(asyncio.to_thread(run))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            # A disconnected client cannot stop the worker thread.
            await job
            raise


async def transcribe_telegram(voice, bot):
    processor = await initialize_stt()
    async with _lock:
        with io.BytesIO() as audio:
            await bot.download(voice, destination=audio)
            return await asyncio.to_thread(processor.transcribe, audio.getvalue())
