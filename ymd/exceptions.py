"""
Иерархия исключений для приложения.
"""


class YandexMusicError(Exception):
    """Базовое исключение для всех ошибок приложения."""
    pass


# Ошибки API
class APIError(YandexMusicError):
    """Ошибка при работе с API Яндекс.Музыки."""
    pass


class AuthenticationError(APIError):
    """Ошибка авторизации."""
    pass


class RateLimitError(APIError):
    """Превышение лимита запросов."""
    pass


# Ошибки разрешения
class ResolveError(YandexMusicError):
    """Ошибка при разрешении ссылки или ID."""
    pass


class TrackNotFoundError(ResolveError):
    """Трек не найден."""
    pass


class AlbumNotFoundError(ResolveError):
    """Альбом не найден."""
    pass


class PlaylistNotFoundError(ResolveError):
    """Плейлист не найден."""
    pass


class ArtistNotFoundError(ResolveError):
    """Исполнитель не найден."""
    pass


# Ошибки загрузки
class DownloadError(YandexMusicError):
    """Ошибка при загрузке трека."""
    pass


class QualityError(DownloadError):
    """Ошибка при выборе качества."""
    pass


class ConversionError(DownloadError):
    """Ошибка при конвертации аудио."""
    pass


# Ошибки метаданных
class MetadataError(YandexMusicError):
    """Ошибка при работе с метаданными."""
    pass


class CoverError(MetadataError):
    """Ошибка при загрузке обложки."""
    pass


class LyricsError(MetadataError):
    """Ошибка при загрузке текста."""
    pass


class TagError(MetadataError):
    """Ошибка при записи тегов."""
    pass


# Ошибки файловой системы
class FileSystemError(YandexMusicError):
    """Ошибка при работе с файловой системой."""
    pass


class PathError(FileSystemError):
    """Ошибка при формировании пути."""
    pass