"""
STT-модуль проекта «ДЕЛУ.ВРЕМЯ».
Распознаёт русскую речь из аудиофайлов (.ogg, .wav, .mp3) с помощью Whisper-large-v3.
Автор: Николай Воронов (НВ)
"""
import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

import torch
from transformers import pipeline

log = logging.getLogger("delu.stt")


class STTProcessor:
    """
    Класс для распознавания речи с помощью OpenAI Whisper.
    Автоматически использует MPS (Apple Silicon) или CPU.

    В боте используйте transcribe_async(): распознавание идёт в отдельном фоновом
    потоке и не блокирует event loop aiogram (бот продолжает отвечать другим
    пользователям, пока Whisper работает).
    """

    # Один рабочий поток на весь процесс: pipeline не потокобезопасен, а модель
    # одна. Голосовые встают в очередь и распознаются по одному, при этом
    # общий пул asyncio.to_thread (его использует NLP) не занимается.
    _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")

    def __init__(self, model_name: str = "openai/whisper-large-v3"):
        """
        Инициализация модели. Загружается один раз при создании экземпляра.
        
        Args:
            model_name: Имя модели HuggingFace (по умолчанию whisper-large-v3)
        """
        print(f"⏳ Загрузка STT-модели: {model_name}...")
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.pipe = pipeline(
            "automatic-speech-recognition",
            model=model_name,
            device=self.device
        )
        print(f"✅ STT-модель загружена на устройстве: {self.device}")
    
    def transcribe(self, audio_path: str, language: str = "russian") -> str:
        """
        Распознаёт речь из аудиофайла.
        
        Args:
            audio_path: Путь к аудиофайлу (.ogg, .wav, .mp3 и др.)
            language: Язык распознавания (по умолчанию 'russian')
            
        Returns:
            Распознанный текст (str)
            
        Raises:
            FileNotFoundError: Если файл не найден
            ValueError: Если файл пустой
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Аудиофайл не найден: {audio_path}")
        
        if os.path.getsize(audio_path) == 0:
            raise ValueError(f"Аудиофайл пустой: {audio_path}")
        
        # Распознавание
        result = self.pipe(
            audio_path,
            return_timestamps=False,
            generate_kwargs={"language": language, "task": "transcribe"}
        )
        
        return result["text"].strip()

    async def transcribe_async(self, audio_path: str, language: str = "russian") -> str:
        """
        Асинхронная обёртка над transcribe() для aiogram/asyncio.

        Распознавание выполняется в фоновом потоке STT, event loop не блокируется.
        Исключения transcribe() (FileNotFoundError, ValueError, ошибки модели)
        пробрасываются вызывающему коду.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.transcribe, audio_path, language)
