"""
Загрузка и форматирование текстов песен.
"""

import logging
from typing import Optional

from yandex_music import Client, Track

from ymd.exceptions import LyricsError

logger = logging.getLogger(__name__)


class LyricsDownloader:
    """
    Загружает тексты песен.
    """
    
    def __init__(self, client: Client, prefer_sync: bool = False):
        self.client = client
        self.prefer_sync = prefer_sync
    
    def download_lyrics(
        self,
        track: Track,
        format_type: str = 'text',
    ) -> Optional[str]:
        """
        Скачивает текст песни.
        
        Args:
            track: Объект трека
            format_type: Формат ('text' или 'lrc')
            
        Returns:
            Текст песни или None
        """
        try:
            # Проверяем, есть ли текст
            if not track.lyrics_info:
                logger.debug(f"Нет текста для трека {track.id}")
                return None
            
            # Проверяем, доступен ли текст
            if not track.lyrics_info.has_available_text_lyrics:
                logger.debug(f"Текст недоступен для трека {track.id}")
                return None
            
            # Получаем текст через API
            lyrics = track.get_lyrics(format_=format_type.upper())
            
            if not lyrics:
                logger.debug(f"Не удалось получить текст для трека {track.id}")
                return None
            
            # Получаем содержимое
            text = lyrics.fetch_lyrics()
            
            if not text:
                logger.debug(f"Текст пуст для трека {track.id}")
                return None
            
            logger.debug(f"Текст загружен: {len(text)} символов")
            return text
            
        except Exception as e:
            logger.warning(f"Ошибка при загрузке текста: {e}")
            return None
    
    def download_lyrics_sync(self, track: Track) -> Optional[str]:
        """Скачивает синхронизированный текст (LRC)."""
        try:
            if not track.lyrics_info or not track.lyrics_info.has_available_sync_lyrics:
                return None
            
            lyrics = track.get_lyrics(format_="LRC")
            if lyrics:
                return lyrics.fetch_lyrics()
        except Exception as e:
            logger.warning(f"Ошибка при загрузке синхронизированного текста: {e}")
        return None