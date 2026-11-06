"""
Утилиты для работы с путями и файлами.

Здесь же — словари шаблонов по умолчанию для каждого типа источника
(см. task §3: контекстный выбор --path-pattern).
"""

import re
import unicodedata
from enum import Enum
from pathlib import Path
from typing import Optional, Union

from ymd.exceptions import FileSystemError


class SourceType(str, Enum):
    """Тип распознанного источника (определяет LinkResolver, см. api/resolver.py)."""

    TRACK = 'track'                        # одиночный трек (--track-id, /track/<id>)
    ALBUM = 'album'                        # альбом целиком (--album-id, /album/<id>)
    ARTIST_DISCOGRAPHY = 'artist_discography'  # дискография артиста (/artist/<id>)
    ARTIST_TRACKS = 'artist_tracks'        # треки артиста (/artist/<id>/tracks, ar.<uuid>)
    ARTIST_PODCASTS = 'artist_podcasts'    # подкасты артиста (/artist/<id>/podcasts)
    PLAYLIST = 'playlist'                  # любой плейлист (UUID, lk., старый формат, likes)


# Шаблоны --path-pattern по умолчанию: выбираются по типу источника,
# когда пользователь НЕ передал --path-pattern явно.
DEFAULT_PATTERNS = {
    SourceType.TRACK: "{artists} - {title}",
    SourceType.ALBUM: "{artists} - {album}/{track_number_padded} {artists} - {title}",
    SourceType.ARTIST_DISCOGRAPHY: "{artists}/{album}/{track_number_padded} {artists} - {title}",
    SourceType.ARTIST_TRACKS: "{artists}/{artists} - {title}",
    SourceType.ARTIST_PODCASTS: "{artists}/{artists} - {title}",
    SourceType.PLAYLIST: "{playlist}/{artists} - {title}",
}

# Как печатать тип источника в консоли (сообщение о выбранном шаблоне).
SOURCE_TYPE_LABELS = {
    SourceType.TRACK: 'трек',
    SourceType.ALBUM: 'альбом',
    SourceType.ARTIST_DISCOGRAPHY: 'дискография артиста',
    SourceType.ARTIST_TRACKS: 'треки артиста',
    SourceType.ARTIST_PODCASTS: 'подкасты артиста',
    SourceType.PLAYLIST: 'плейлист',
}


def select_default_pattern(source_type: Union[SourceType, str]) -> str:
    """
    Возвращает шаблон пути по умолчанию для типа источника.

    Неизвестный тип трактуется как альбом (исторически ближайший шаблон v0.0.1).
    """
    try:
        return DEFAULT_PATTERNS[SourceType(source_type)]
    except (ValueError, KeyError):
        return DEFAULT_PATTERNS[SourceType.ALBUM]


def sanitize_filename(filename: str, replacement: str = '_') -> str:
    """
    Очищает имя файла от недопустимых символов.
    
    Args:
        filename: Исходное имя файла
        replacement: Символ для замены недопустимых
        
    Returns:
        Очищенное имя файла
    """
    # Нормализуем Unicode
    filename = unicodedata.normalize('NFKC', filename)
    
    # Удаляем управляющие символы
    filename = re.sub(r'[\x00-\x1f\x7f]', '', filename)
    
    # Оставляем только допустимые символы
    # Windows: \ / : * ? " < > |
    filename = re.sub(r'[<>:"/\\|?*]', replacement, filename)
    
    # Убираем точки в конце (Windows)
    filename = filename.rstrip('.')
    
    # Убираем пробелы в начале и конце
    filename = filename.strip()
    
    # Ограничиваем длину (255 символов для Windows)
    if len(filename) > 250:
        filename = filename[:250]
    
    return filename if filename else 'untitled'


def sanitize_path(path: Path, replacement: str = '_') -> Path:
    """
    Очищает все компоненты пути.
    
    Args:
        path: Исходный путь
        replacement: Символ для замены
        
    Returns:
        Очищенный путь
    """
    parts = []
    for part in path.parts:
        clean_part = sanitize_filename(part, replacement)
        if clean_part:
            parts.append(clean_part)
    
    return Path(*parts) if parts else Path('.')


def ensure_directory(path: Path) -> None:
    """
    Создает директорию, если она не существует.
    
    Args:
        path: Путь к директории
        
    Raises:
        FileSystemError: Если не удалось создать директорию
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise FileSystemError(f"Не удалось создать директорию {path}: {e}") from e


def get_unique_filename(path: Path) -> Path:
    """
    Возвращает уникальное имя файла, добавляя число к дубликатам.
    
    Args:
        path: Исходный путь к файлу
        
    Returns:
        Уникальный путь к файлу
    """
    if not path.exists():
        return path
    
    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    
    counter = 1
    while True:
        new_path = parent / f"{stem} ({counter}){suffix}"
        if not new_path.exists():
            return new_path
        counter += 1


def get_file_size(path: Path) -> int:
    """
    Возвращает размер файла.
    
    Args:
        path: Путь к файлу
        
    Returns:
        Размер файла в байтах
        
    Raises:
        FileSystemError: Если файл не существует или не удалось получить размер
    """
    try:
        return path.stat().st_size
    except OSError as e:
        raise FileSystemError(f"Не удалось получить размер файла {path}: {e}") from e


def format_size(size_bytes: int) -> str:
    """
    Форматирует размер в человекочитаемый вид.
    
    Args:
        size_bytes: Размер в байтах
        
    Returns:
        Отформатированная строка
    """
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"