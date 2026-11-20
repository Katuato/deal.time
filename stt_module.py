"""
STT-модуль проекта «ДЕЛУ.ВРЕМЯ».
Распознаёт русскую речь из аудиофайлов (.ogg, .wav, .mp3) с помощью Whisper-large-v3.
Автор: Николай Воронов (НВ)
"""
import os
import torch
from transformers import pipeline

class STTProcessor:
    """
    Класс для распознавания речи с помощью OpenAI Whisper.
    Автоматически использует MPS (Apple Silicon) или CPU.
    """
    
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


# ==========================================================
# Пример использования (для тестирования)
# ==========================================================
if __name__ == "__main__":
    print("=" * 60)
    print("STT-модуль проекта Делу.Время")
    print("=" * 60)
    
    # 1. Создаем экземпляр (модель загрузится в память)
    stt = STTProcessor()
    
    print("\n💡 Пример использования:")
    print("  from stt_module import STTProcessor")
    print("  stt = STTProcessor()")
    print("  text = stt.transcribe('audio.ogg')")
    print("\n✅ Модуль готов к работе!")
