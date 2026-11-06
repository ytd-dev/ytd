"""Orchestrates the command line interface of the downloader."""

from __future__ import annotations

import logging
import sys
from typing import List, Optional, Sequence

from yandex_music import Album, Artist, ArtistAlbums, Playlist, Track

from ymd import __version__
from ymd.api.client import create_client_from_config
from ymd.api.resolver import (
    AlbumsSource,
    LinkResolver,
    LikedPlaylist,
    ResolvedSource,
    TrackListSource,
)
from ymd.config import AppConfig
from ymd.core.downloader import (
    TrackDownloader,
    recheck_bitrates,
    save_tracks_list,
    write_bitrate_report,
)
from ymd.core.quality import mode_description
from ymd.exceptions import (
    APIError,
    AuthenticationError,
    ConversionError,
    DownloadError,
    FileSystemError,
    MetadataError,
    PathError,
    QualityError,
    ResolveError,
)
from ymd.filesystem.paths import (
    SOURCE_TYPE_LABELS,
    SourceType,
    select_default_pattern,
)

from ymd.cli.parser import parse_args

logger = logging.getLogger("ymd")

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_INTERRUPTED = 130


def _configure_stdio() -> None:
    """Делает stdout/stderr пригодными для кириллицы и эмодзи в консоли Windows.

    По умолчанию Python в Windows кодирует вывод в кодовой странице консоли
    (cp866/cp1251), и ``print('🚫 Недоступен')`` или ``📊 СТАТИСТИКА`` упали бы с
    UnicodeEncodeError, оборвав загрузку (особенно при перенаправлении в файл).
    UTF-8 с errors='replace' таких падений не допускает.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):  # нестандартный поток — не страшно
            pass


def _setup_logging(config: AppConfig) -> None:
    """Configure the root logging used for progress and diagnostics."""
    level = logging.DEBUG if config.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
        stream=sys.stdout,
    )


def _fail(exc: BaseException, config: AppConfig, *, prefix: str = "ОШИБКА") -> int:
    """Print ``exc`` (with a traceback under ``--debug``) and return exit code 1."""
    if config.debug:
        logger.exception(prefix)
    else:
        print(f"{prefix}: {exc}")
    return EXIT_ERROR


# --------------------------------------------------------------- source

def _source_provided(config: AppConfig) -> bool:
    """True when one of the source keys (--url/--track-id/...) is set."""
    return any(
        getattr(config, attr, None) not in (None, "")
        for attr in ("url", "track_id", "album_id", "playlist_id", "artist_id")
    )


def _source_description(config: AppConfig) -> str:
    """Human-readable description of the configured source."""
    if config.url:
        return str(config.url)
    if config.track_id is not None:
        return f"https://music.yandex.ru/track/{config.track_id}"
    if config.album_id is not None:
        return f"https://music.yandex.ru/album/{config.album_id}"
    if config.artist_id is not None:
        return f"https://music.yandex.ru/artist/{config.artist_id}"
    if config.playlist_id is not None:
        pid = str(config.playlist_id).strip()
        if pid.lower() in ("likes", "liked") or pid.lower().startswith("lk"):
            return "плейлист «Мне нравится»"
        if "/" in pid:
            return f"https://music.yandex.ru/users/{pid}"
        return f"https://music.yandex.ru/playlists/{pid}"
    return ""


def _resolve_source(config: AppConfig, client) -> ResolvedSource:
    """
    Resolve the configured source into a yandex-music model object.

    Возвращает ResolvedSource: объект источника + его тип (для контекстного
    шаблона --path-pattern) + название плейлиста (для {playlist}).
    """
    resolver = LinkResolver(client)
    if config.url:
        return resolver.resolve_source(config.url)
    if config.track_id is not None:
        return ResolvedSource(resolver.resolve_track_id(config.track_id),
                              SourceType.TRACK)
    if config.album_id is not None:
        return ResolvedSource(resolver.resolve_album_id(config.album_id),
                              SourceType.ALBUM)
    if config.artist_id is not None:
        return ResolvedSource(resolver.resolve_artist_id(config.artist_id),
                              SourceType.ARTIST_DISCOGRAPHY)
    if config.playlist_id is not None:
        return resolver.resolve_playlist_source(config.playlist_id)
    return ResolvedSource(None, SourceType.ALBUM)


def _apply_path_pattern(config: AppConfig, resolved: ResolvedSource) -> None:
    """
    Контекстный выбор шаблона сохранения (task §3).

    Если пользователь НЕ передал --path-pattern явно (config.path_pattern is
    None), шаблон берётся из filesystem.paths.DEFAULT_PATTERNS по типу
    распознанного источника. Явный шаблон пользователя не трогаем.
    """
    if config.path_pattern is None:
        config.path_pattern = select_default_pattern(resolved.source_type)
        label = SOURCE_TYPE_LABELS.get(resolved.source_type,
                                       str(resolved.source_type))
        print(f"Шаблон пути выбран по типу источника ({label}):")
    else:
        print("Шаблон пути (указан пользователем):")
    print(f"  {config.path_pattern}")


# ------------------------------------------------------------- collection

def _playlist_items(source: object) -> Optional[List]:
    """List of tracks carried by a playlist-like object (duck-typed)."""
    if not (hasattr(source, "tracks") or hasattr(source, "list")):
        return None
    items = getattr(source, "tracks", None)
    if items is None:
        items = getattr(source, "list", None)
    return [t for t in (items or []) if t is not None]


def collect_tracks(source: object, downloader) -> List:
    """Expand any resolved source into the flat list of tracks to download."""
    if source is None:
        return []

    if isinstance(source, Track):
        return [source]

    if isinstance(source, Artist):
        albums = downloader.artist_albums(source.id)
        return _tracks_of_albums(albums, downloader, artist_id=source.id)

    if isinstance(source, ArtistAlbums):
        albums = getattr(source, "albums", None) or []
        artist = getattr(source, "artist", None)
        return _tracks_of_albums(
            list(albums), downloader, artist_id=getattr(artist, "id", None)
        )

    # Подкасты артиста (AlbumsSource): тот же поток, что и дискография.
    if isinstance(source, AlbumsSource):
        return _tracks_of_albums(
            list(source.albums or []), downloader,
            artist_id=getattr(getattr(source, "artist", None), "id", None),
        )

    if isinstance(source, Album):
        return downloader.album_tracks(source, apply_filters=True)

    items = _playlist_items(source)
    if items is not None:
        expand = getattr(downloader, "expand_short_tracks", None)
        return expand(items) if expand is not None else items

    if isinstance(source, (list, tuple)):
        plain = [t for t in source if t is not None]
        shorts = [t for t in plain if not isinstance(t, Track)]
        full = [t for t in plain if isinstance(t, Track)]
        expand = getattr(downloader, "expand_short_tracks", None)
        return full + (expand(shorts) if expand is not None else shorts)

    raise ResolveError(f"Не удалось понять, что именно удалось найти: {source!r}")


def _tracks_of_albums(albums: Sequence, downloader, artist_id=None) -> List:
    """Collect tracks of several albums (artist discography flow)."""
    tracks: List = []
    for album in albums:
        tracks.extend(
            downloader.album_tracks(album, apply_filters=True, artist_id=artist_id)
        )
    return tracks


# ---------------------------------------------------------------- reports

def _run_recheck(config: AppConfig) -> int:
    """--recheck: scan --dir for MP3 files and report their real bitrates."""
    groups = recheck_bitrates(config.output_dir)
    if not groups:
        print("MP3-файлы не найдены")
        return EXIT_SUCCESS
    write_bitrate_report(groups, config.output_dir)
    return EXIT_SUCCESS


# ------------------------------------------------------------------- main

def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the downloader application. Returns the process exit code."""
    _configure_stdio()
    parser, config = parse_args(argv)
    _setup_logging(config)

    # --force-mp3 имеет смысл только для AAC-качеств 0/1: при -q 2/192/256/320
    # API и так отдаёт MP3, конвертация не нужна. parser.error() завершает
    # процесс с кодом 2 (ошибка разбора аргументов).
    if config.force_mp3 and config.quality not in (0, 1):
        parser.error(
            "--force-mp3 работает только с -q 0 или -q 1 "
            "(конвертация AAC в MP3); для -q 2/192/256/320 MP3 скачивается напрямую"
        )

    has_source = _source_provided(config)
    if not has_source and not config.recheck:
        parser.error(
            "нужно указать источник: --url, --track-id, --album-id, --artist-id "
            "или --playlist-id (либо использовать --recheck без источника)"
        )

    try:
        # --- recheck without a source: only scans local files, no API ---
        if config.recheck and not has_source:
            return _run_recheck(config)

        print("=" * 60)
        print(f"Загрузчик треков Yandex Music (ymd v{__version__})")
        print("=" * 60)
        print(f"Источник: {_source_description(config)}")
        print(f"Каталог: {config.output_dir}")
        # Качество печатается до закачки: это страховка от ситуаций, когда флаг
        # -q не дошёл до программы (например ссылка с & без кавычек в cmd/bash —
        # оболочка режет командную строку, и запуск уходит с -q 0 по умолчанию).
        quality_note = mode_description(config.quality, config.force_mp3,
                                        config.compatibility_level)
        print(f"Качество: {quality_note}")

        client = create_client_from_config(config)
        downloader = TrackDownloader(client, config)

        resolved = _resolve_source(config, client)
        _apply_path_pattern(config, resolved)
        # {playlist}: название источника-плейлиста прокидывается в загрузчик
        # (FileNamer очистит недопустимые символы при форматировании пути).
        downloader.playlist_title = resolved.playlist_title or ''

        tracks = collect_tracks(resolved.source, downloader)

        if not tracks:
            print("Не найдено треков.")
            return EXIT_SUCCESS

        print(f"Найдено треков: {len(tracks)}")

        if config.list_only:
            path = save_tracks_list(tracks, config.output_dir)
            print(f"Список сохранён: {path}")
            return EXIT_SUCCESS

        stats = downloader.download_tracks(tracks)
        stats.print_summary()

        if config.save_unavailable:
            path = downloader.save_unavailable_report()
            if path is not None:
                print(f"Список недоступных треков сохранён: {path}")

        rc = EXIT_SUCCESS if getattr(stats, "errors", 0) == 0 else EXIT_ERROR

        if config.recheck:
            recheck_rc = _run_recheck(config)
            rc = rc or recheck_rc

        return rc

    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return EXIT_INTERRUPTED
    except (
        ResolveError,
        DownloadError,
        QualityError,
        ConversionError,
        APIError,
        AuthenticationError,
        FileSystemError,
        PathError,
        MetadataError,
    ) as exc:
        return _fail(exc, config)
    except Exception as exc:  # noqa: BLE001 - unexpected failure reported cleanly
        return _fail(exc, config, prefix="Непредвиденная ошибка")


if __name__ == "__main__":
    sys.exit(main())
