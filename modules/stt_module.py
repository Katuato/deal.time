"""
STT-модуль проекта «ДЕЛУ.ВРЕМЯ».
Распознаёт русскую речь из аудиофайлов (.ogg, .wav, .mp3) с помощью Whisper large-v3-turbo.
Автор: Николай Воронов (НВ)
"""
import logging
import os
from time import perf_counter

import torch
from transformers import pipeline

logger = logging.getLogger("delu.stt")
DEFAULT_MODEL = "openai/whisper-large-v3-turbo"


class STTProcessor:
    """
    Класс для распознавания речи с помощью OpenAI Whisper.
    Автоматически использует MPS (Apple Silicon) или CPU.
    """
    
    def __init__(self, model_name: str = DEFAULT_MODEL):
        """
        Инициализация модели. Загружается один раз при создании экземпляра.
        
        Args:
            model_name: Имя модели Hugging Face.
        """
        self.model_name = model_name
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        logger.info("STT loading model=%s device=%s", self.model_name, self.device)
        started = perf_counter()
        self.pipe = pipeline(
            "automatic-speech-recognition",
            model=model_name,
            device=self.device,
            model_kwargs={"use_safetensors": True},
        )
        logger.info(
            "STT model=%s device=%s load_s=%.3f",
            self.model_name, self.device, perf_counter() - started,
        )
    
    def transcribe(self, audio_path: str | bytes, language: str = "russian") -> str:
        """
        Распознаёт речь из аудиофайла.
        
        Args:
            audio_path: Путь к аудиофайлу или байты закодированного аудио.
            language: Язык распознавания (по умолчанию 'russian')
            
        Returns:
            Распознанный текст (str)
            
        Raises:
            FileNotFoundError: Если файл не найден
            ValueError: Если файл пустой
        """
        size = len(audio_path) if isinstance(audio_path, bytes) else os.path.getsize(audio_path)
        if not size:
            raise ValueError("Аудиофайл пустой")
        
        # Распознавание. return_timestamps=True обязателен для аудио длиннее 30 секунд:
        # с False Whisper выбрасывает ValueError (long-form generation требует таймкодов).
        # Короткие записи распознаются как раньше, в результате берём только текст.
        started = perf_counter()
        result = self.pipe(
            audio_path,
            return_timestamps=True,
            generate_kwargs={"language": language, "task": "transcribe"}
        )
        logger.info(
            "STT model=%s device=%s inference_s=%.3f",
            self.model_name, self.device, perf_counter() - started,
        )

        return result["text"].strip()
