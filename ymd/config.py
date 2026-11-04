"""
Конфигурация приложения: датакласс настроек и общие константы.

Значения качества (--quality):
    0          → AAC 64 кбит/с (.m4a)
    1          → AAC 192 кбит/с (.m4a)
    2          → «максимально доступное»: FLAC → .flac; AAC → MP3 (битрейт API, обычно 256);
                 MP3 → MP3 (битрейт API: 320/256/192/160/128)
    192/256/320 → MP3 с указанным битрейтом, но не выше битрейта источника
"""

from dataclasses import dataclass
from typing import Optional

# Допустимые значения --quality
VALID_QUALITIES = [0, 1, 2, 192, 256, 320]

# Стандартные битрейты MP3, которые отдаёт Яндекс API (по убыванию)
MP3_BITRATES = [320, 256, 192, 160, 128]

# Допустимые значения --lyrics-format
VALID_LYRICS_FORMATS = ['NONE', 'TEXT', 'LRC']

# Стандартный шаблон пути для сохраняемых файлов
DEFAULT_PATH_PATTERN = "{artists} - {album}/{track_number_padded} - {title}"

# Поддерживаемые плейсхолдеры --path-pattern (должны совпадать с filesystem/naming.py)
PATH_PLACEHOLDERS = (
    '{id}', '{title}', '{artists}', '{artist}', '{album}', '{album_artist}',
    '{track_number}', '{track_number_padded}',
    '{real_track_number}', '{real_track_number_padded}',
    '{disc_number}', '{year}',
    '{genre}', '{duration}', '{bitrate}', '{extension}', '{extension_no_dot}',
)


def cover_size_from_resolution(cover_resolution: str) -> str:
    """
    Преобразует значение --cover-resolution в формат размера Яндекс API.

    Args:
        cover_resolution: 'original' или число не меньше 100 (например '1000')

    Returns:
        'orig' либо строка 'NxN' (например '1000x1000')

    Note:
        Яндекс отдаёт только квадратные превью 100/200/300/600/1000 и оригинал.
        Если запрошенного размера нет, CoverDownloader пробует остальные
        (см. metadata/covers.py: COVER_FALLBACK_SIZES), поэтому '500' не ошибка.
    """
    if str(cover_resolution).lower() in ('original', 'orig'):
        return 'orig'
    n = int(cover_resolution)
    if n < 100:
        raise ValueError('Минимальное разрешение обложки: 100')
    # Яндекс отдаёт только квадратные превью (100/200/300/600/1000). Если
    # запрошенного размера нет (например 500), Яндекс ответит 404, а
    # CoverDownloader возьмёт ближайший из существующих — см. COVER_FALLBACK_SIZES.
    return f'{n}x{n}'


@dataclass
class AppConfig:
    """Настройки приложения, собираемые из аргументов командной строки."""

    # --- Источник (заполняет cli.parser) ---
    url: Optional[str] = None
    track_id: Optional[str] = None
    album_id: Optional[str] = None
    playlist_id: Optional[str] = None
    artist_id: Optional[str] = None

    # --- Аутентификация ---
    token: Optional[str] = None

    # --- Куда сохранять ---
    output_dir: str = '.'
    path_pattern: str = DEFAULT_PATH_PATTERN
    unsafe_path: bool = False

    # --- Качество и конвертация ---
    quality: int = 0
    force_mp3: bool = False
    compatibility_level: int = 0  # 0..1 — уровень совместимости формата (0 = макс. качество, 1 = максимально совместимый контейнер)

    # --- Отбор треков ---
    stick_to_artist: bool = False
    only_music: bool = False

    # --- Поведение с файлами ---
    skip_existing: bool = False      # пропускать, если файл с таким же именем (без расширения) уже есть
    overwrite: bool = False          # перезаписывать существующие файлы
    save_unavailable: bool = False   # сохранять список недоступных треков в файл
    list_only: bool = False          # только сохранить список треков, ничего не скачивать
    recheck: bool = False            # проверить реальные битрейты скачанных MP3 (mutagen)

    # --- Метаданные ---
    embed_cover: bool = True         # встраивать обложку в файл (--no-embed-cover выключает)
    cover_resolution: str = '600'    # 'original' или число; по умолчанию 600x600
    lyrics_format: str = 'NONE'      # NONE / TEXT / LRC

    # --- Сеть и надёжность ---
    tries: int = 20          # число попыток запроса (минимум 1)
    retry_delay: int = 5     # пауза между попытками, секунд
    delay: float = 0.0       # пауза между треками, секунд
    timeout: int = 20        # таймаут запроса, секунд
    proxy_url: Optional[str] = None  # прокси для запросов (socks5://... и т.п.)
    no_check_certificate: bool = False  # отключить проверку SSL-сертификатов

    # --- Прочее ---
    debug: bool = False

    def get_cover_size(self) -> str:
        """Возвращает размер обложки в формате Яндекс API ('orig' или 'NxN')."""
        return cover_size_from_resolution(self.cover_resolution)

    def get_lyrics_format(self) -> str:
        """Возвращает нормализованный формат текстов (NONE/TEXT/LRC)."""
        fmt = (self.lyrics_format or 'NONE').upper()
        return fmt if fmt in VALID_LYRICS_FORMATS else 'NONE'
