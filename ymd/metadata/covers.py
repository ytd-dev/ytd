"""
Загрузка и кэширование обложек альбомов.
"""

import hashlib
import logging
from pathlib import Path
from typing import Optional, Dict, List
from dataclasses import dataclass

from yandex_music import Client, Track

from ymd.exceptions import CoverError

logger = logging.getLogger(__name__)

# Размер обложки по умолчанию: 600x600 — 95 КБ против 250 КБ у 1000x1000 и
# 2,1 МБ у оригинала, при этом для тегов и обложек в плеерах качества хватает.
DEFAULT_COVER_SIZE = '600x600'

# Яндекс отдаёт только фиксированные квадратные превью (плюс 'orig'). Если
# запрошенного размера нет (например 500x500), API отвечает 404, а не отдаёт
# промежуточную картинку, поэтому после запрошенного размера перебираем эти.
COVER_FALLBACK_SIZES = ('1000x1000', '600x600', '300x300', '200x200', '100x100')


@dataclass
class CoverData:
    """Данные обложки."""
    data: bytes
    mime_type: str
    size: int
    hash: str

    @classmethod
    def from_bytes(cls, data: bytes, mime_type: str = 'image/jpeg'):
        """Создает объект CoverData из байтов."""
        return cls(
            data=data,
            mime_type=mime_type,
            size=len(data),
            hash=hashlib.md5(data).hexdigest()[:8],
        )


def cover_candidates(size: Optional[str]) -> List[str]:
    """
    Возвращает порядок попыток: сначала запрошенный размер, затем запасные.

    Args:
        size: запрошенный размер ('600x600', 'orig', '1000x1000', None = default)

    Returns:
        Список размеров без повторов; запрошенный — первым.
    """
    requested = (size or DEFAULT_COVER_SIZE).lower()
    candidates = [requested]
    candidates.extend(s for s in COVER_FALLBACK_SIZES if s != requested)
    return candidates


class CoverDownloader:
    """
    Загружает и кэширует обложки альбомов.

    Кэш считается по cover_uri (а не по ID трека): треки одного альбома имеют
    одну обложку, поэтому для альбома из 20 треков обложка скачивается один раз.
    """

    def __init__(self, client: Client, cache_dir: Optional[Path] = None):
        self.client = client
        self.cache_dir = cache_dir or Path.home() / '.ymd' / 'covers'
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, CoverData] = {}  # in-memory cache

    def download_cover(
        self,
        track: Track,
        size: str = DEFAULT_COVER_SIZE,
        use_cache: bool = True,
    ) -> Optional[CoverData]:
        """
        Загружает обложку для трека.

        Args:
            track: Объект трека
            size: размер обложки ('200x200', '600x600', '1000x1000', 'orig').
                Если такого превью у Яндекса нет, берутся запасные размеры
                (см. cover_candidates), чтобы обложка всё равно была.
            use_cache: Использовать кэш

        Returns:
            CoverData или None, если обложка не найдена
        """
        try:
            cover_uri = getattr(track, 'cover_uri', None)
            if not cover_uri:
                logger.debug(f"Нет обложки для трека {track.id}")
                return None

            cache_key = self._cache_key(cover_uri, size)

            # Проверяем in-memory кэш
            if use_cache and cache_key in self._cache:
                logger.debug(f"Обложка найдена в кэше: {track.id}")
                return self._cache[cache_key]

            # Проверяем файловый кэш
            cache_file = self.cache_dir / f"{cache_key}.cache"
            if use_cache and cache_file.exists():
                with open(cache_file, 'rb') as f:
                    data = f.read()
                if data:
                    cover_data = CoverData.from_bytes(data, self._detect_mime_type(data))
                    self._cache[cache_key] = cover_data
                    logger.debug(f"Обложка загружена из файлового кэша: {cache_file}")
                    return cover_data

            logger.debug(f"Скачиваем обложку для трека {track.id}, размер {size}")
            for candidate in cover_candidates(size):
                cover_data = self._fetch_one(track, candidate)
                if cover_data is None:
                    continue
                if candidate != size.lower():
                    logger.debug(f"Размер {size} недоступен, взято превью {candidate}")
                if use_cache:
                    self._cache[cache_key] = cover_data
                    try:
                        with open(cache_file, 'wb') as f:
                            f.write(cover_data.data)
                    except OSError as e:
                        logger.debug(f"Не удалось сохранить кэш обложки: {e}")
                logger.debug(f"Обложка скачана: {len(cover_data.data)} байт ({candidate})")
                return cover_data

            logger.warning(f"Не удалось скачать обложку для трека {track.id}")
            return None

        except Exception as e:
            logger.error(f"Ошибка при загрузке обложки: {e}")
            return None

    def _fetch_one(self, track: Track, size: str) -> Optional[CoverData]:
        """
        Пробует скачать одно превью заданного размера.

        Returns:
            CoverData или None, если такого превью нет (404) либо запрос не прошёл.
        """
        try:
            # Используем встроенный метод библиотеки
            cover_bytes = track.download_cover_bytes(size=size)
        except Exception as e:
            # Яндекс отвечает 404 на несуществующие превью — это штатная ситуация,
            # переходим к следующему размеру.
            logger.debug(f"Превью {size} недоступно для трека {track.id}: {e}")
            return None

        if not cover_bytes:
            return None
        return CoverData.from_bytes(cover_bytes, self._detect_mime_type(cover_bytes))

    @staticmethod
    def _cache_key(cover_uri: str, size: Optional[str]) -> str:
        """
        Ключ кэша: обложка + запрошенный размер.

        Берётся основа cover_uri (без '%%'), поэтому все треки альбома делят одну
        запись, а разные запрошенные размеры не перекрывают друг друга.
        """
        base = cover_uri.split('%%')[0].rstrip('/')
        digest = hashlib.md5(f'{base}|{str(size or DEFAULT_COVER_SIZE).lower()}'.encode('utf-8'))
        return digest.hexdigest()[:16]

    def _detect_mime_type(self, data: bytes) -> str:
        """Определяет MIME тип по магическим байтам."""
        if data.startswith(b'\xFF\xD8\xFF'):
            return 'image/jpeg'
        if data.startswith(b'\x89PNG'):
            return 'image/png'
        if data.startswith(b'GIF'):
            return 'image/gif'
        if data.startswith(b'RIFF') and len(data) > 12 and data[8:12] == b'WEBP':
            return 'image/webp'
        return 'image/jpeg'  # fallback

    def clear_cache(self):
        """Очищает кэш обложек."""
        self._cache.clear()
        for file in self.cache_dir.glob('*.cache'):
            file.unlink()
        logger.debug("Кэш обложек очищен")
