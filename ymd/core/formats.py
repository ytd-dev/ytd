"""
Определение и работа с аудиоформатами.
"""

from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Optional


class AudioFormat(Enum):
    """Аудиоформаты."""
    MP3 = auto()
    FLAC = auto()
    AAC = auto()
    M4A = auto()  # AAC в MP4 контейнере
    MP4 = auto()  # FLAC в MP4 контейнере
    UNKNOWN = auto()
    
    @property
    def extension(self) -> str:
        """Стандартное расширение файла."""
        return {
            AudioFormat.MP3: ".mp3",
            AudioFormat.FLAC: ".flac",
            AudioFormat.AAC: ".aac",
            AudioFormat.M4A: ".m4a",
            AudioFormat.MP4: ".mp4",
            AudioFormat.UNKNOWN: "",
        }[self]
    
    @property
    def is_lossless(self) -> bool:
        """Является ли формат lossless."""
        return self in (AudioFormat.FLAC, AudioFormat.MP4)  # MP4 может содержать FLAC
    
    @property
    def is_lossy(self) -> bool:
        """Является ли формат lossy."""
        return self in (AudioFormat.MP3, AudioFormat.AAC, AudioFormat.M4A)
    
    @property
    def container(self) -> str:
        """Название контейнера."""
        return {
            AudioFormat.MP3: "MP3",
            AudioFormat.FLAC: "FLAC",
            AudioFormat.AAC: "ADTS",
            AudioFormat.M4A: "MP4",
            AudioFormat.MP4: "MP4",
            AudioFormat.UNKNOWN: "Unknown",
        }[self]
    
    @classmethod
    def from_suffix(cls, suffix: str) -> "AudioFormat":
        """Определяет формат по расширению файла."""
        mapping = {
            ".mp3": cls.MP3,
            ".flac": cls.FLAC,
            ".aac": cls.AAC,
            ".m4a": cls.M4A,
            ".mp4": cls.MP4,
        }
        return mapping.get(suffix.lower(), cls.UNKNOWN)
    
    @classmethod
    def from_mime_type(cls, mime_type: str) -> "AudioFormat":
        """Определяет формат по MIME типу."""
        mapping = {
            "audio/mpeg": cls.MP3,
            "audio/flac": cls.FLAC,
            "audio/aac": cls.AAC,
            "audio/mp4": cls.M4A,
        }
        return mapping.get(mime_type.lower(), cls.UNKNOWN)


@dataclass
class AudioFileInfo:
    """Информация об аудиофайле."""
    format: AudioFormat
    bitrate: Optional[int] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    duration: Optional[int] = None
    
    @property
    def is_lossless(self) -> bool:
        return self.format.is_lossless
    
    @property
    def extension(self) -> str:
        return self.format.extension


class FormatDetector:
    """
    Определяет формат аудиофайлов.
    """
    
    def __init__(self):
        self._magic_patterns = self._build_magic_patterns()
    
    def _build_magic_patterns(self) -> dict:
        """Строит словарь магических паттернов для определения форматов."""
        return {
            b'\xFF\xFB': AudioFormat.MP3,  # MP3
            b'\xFF\xF3': AudioFormat.MP3,  # MP3
            b'\xFF\xF2': AudioFormat.MP3,  # MP3
            b'\xFF\xFA': AudioFormat.MP3,  # MP3
            b'\xFF\xFE': AudioFormat.MP3,  # MP3
            b'fLaC': AudioFormat.FLAC,
            b'\xFF\xF1': AudioFormat.AAC,  # AAC
            b'\x00\x00\x00\x18': AudioFormat.M4A,  # MP4
            b'\x00\x00\x00\x1C': AudioFormat.M4A,  # MP4
            b'\x00\x00\x00\x20': AudioFormat.MP4,  # MP4 with FLAC
        }
    
    def detect_format_from_bytes(self, data: bytes) -> AudioFormat:
        """
        Определяет формат по байтовой сигнатуре.
        
        Args:
            data: Байтовые данные
            
        Returns:
            AudioFormat: Определенный формат
        """
        if not data:
            return AudioFormat.UNKNOWN
        
        # Проверяем магические паттерны
        for pattern, audio_format in self._magic_patterns.items():
            if data.startswith(pattern):
                return audio_format
        
        # Проверяем M4A/MP4 сигнатуры
        if len(data) > 8:
            if data[4:8] == b'ftyp':
                # Проверяем, какой именно MP4
                if len(data) > 16:
                    brand = data[8:12]
                    if brand in (b'mp4a', b'M4A ', b'isom'):
                        return AudioFormat.M4A
                    if brand == b'fLaC':
                        return AudioFormat.MP4
        
        return AudioFormat.UNKNOWN
    
    def detect_format_from_file(self, file_path: Path) -> AudioFormat:
        """
        Определяет формат по файлу.
        
        Args:
            file_path: Путь к файлу
            
        Returns:
            AudioFormat: Определенный формат
        """
        try:
            with open(file_path, 'rb') as f:
                # Читаем первые 64 байта для определения формата
                data = f.read(64)
                return self.detect_format_from_bytes(data)
        except Exception:
            # Если не удалось прочитать, пробуем по расширению
            return AudioFormat.from_suffix(file_path.suffix)
    
    def get_file_info(self, file_path: Path) -> AudioFileInfo:
        """
        Получает полную информацию об аудиофайле.
        
        Args:
            file_path: Путь к файлу
            
        Returns:
            AudioFileInfo: Информация о файле
        """
        format_type = self.detect_format_from_file(file_path)
        
        # Пытаемся получить дополнительную информацию
        bitrate = None
        sample_rate = None
        channels = None
        duration = None
        
        try:
            import mutagen
            audio = mutagen.File(file_path)
            if audio:
                info = audio.info
                bitrate = info.bitrate // 1000 if info.bitrate else None
                sample_rate = info.sample_rate
                channels = info.channels
                duration = int(info.length) if info.length else None
        except Exception:
            pass
        
        return AudioFileInfo(
            format=format_type,
            bitrate=bitrate,
            sample_rate=sample_rate,
            channels=channels,
            duration=duration,
        )
    
    def is_supported(self, format_type: AudioFormat) -> bool:
        """Проверяет, поддерживается ли формат."""
        return format_type not in (AudioFormat.UNKNOWN,)