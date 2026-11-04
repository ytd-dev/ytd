"""
Метаданные — теги, обложки и тексты песен.
"""

from ymd.metadata.tags import TagWriter
from ymd.metadata.covers import CoverDownloader, CoverData
from ymd.metadata.lyrics import LyricsDownloader

__all__ = [
    "TagWriter",
    "CoverDownloader",
    "CoverData",
    "LyricsDownloader",
]