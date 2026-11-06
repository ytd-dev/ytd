"""
Yandex Music Downloader — загрузчик музыки с Яндекс.Музыки.
"""

from ymd.config import AppConfig
from ymd.exceptions import *

# Основные компоненты для удобного импорта
from ymd.core import (
    TrackDownloader,
    AudioConverter,
    DownloadQuality,
    ConversionPlan,
    FormatDetector,
    AudioFormat,
)

from ymd.metadata import (
    TagWriter,
    CoverDownloader,
    CoverData,
    LyricsDownloader,
)

from ymd.filesystem import (
    FileNamer,
    PLACEHOLDERS,
    sanitize_filename,
    sanitize_path,
    ensure_directory,
    get_unique_filename,
)

from ymd.api import (
    init_client,
    get_token_via_device_flow,
    create_client_from_config,
    LinkResolver,
)

__version__ = "0.0.2"
__author__ = "Yandex Music Downloader Team"

__all__ = [
    # Config
    "AppConfig",
    # Core
    "TrackDownloader",
    "AudioConverter",
    "DownloadQuality",
    "ConversionPlan",
    "FormatDetector",
    "AudioFormat",
    # Metadata
    "TagWriter",
    "CoverDownloader",
    "CoverData",
    "LyricsDownloader",
    # Filesystem
    "FileNamer",
    "PLACEHOLDERS",
    "sanitize_filename",
    "sanitize_path",
    "ensure_directory",
    "get_unique_filename",
    # API
    "init_client",
    "get_token_via_device_flow",
    "create_client_from_config",
    "LinkResolver",
    # Exceptions
    "YandexMusicError",
    "APIError",
    "AuthenticationError",
    "RateLimitError",
    "ResolveError",
    "TrackNotFoundError",
    "AlbumNotFoundError",
    "PlaylistNotFoundError",
    "ArtistNotFoundError",
    "DownloadError",
    "QualityError",
    "ConversionError",
    "MetadataError",
    "CoverError",
    "LyricsError",
    "TagError",
    "FileSystemError",
    "PathError",
]