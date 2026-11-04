"""Parse command-line arguments for the Yandex Music downloader.

A single :func:`create_parser` defines every CLI key; :func:`parse_args`
validates values and converts the namespace into a typed
:class:`~ymd.config.AppConfig`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional, Sequence, Tuple

from ymd import __version__
from ymd.config import (
    DEFAULT_PATH_PATTERN,
    VALID_LYRICS_FORMATS,
    VALID_QUALITIES,
    AppConfig,
)

# Values accepted by --compatibility-level (1 disables FLAC requests).
COMPATIBILITY_LEVELS = (0, 1)


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not an integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"'{value}' must be at least 1")
    return number


def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not an integer") from None
    if number < 0:
        raise argparse.ArgumentTypeError(f"'{value}' must not be negative")
    return number


def _non_negative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a number") from None
    if number < 0:
        raise argparse.ArgumentTypeError(f"'{value}' must not be negative")
    return number


def _quality_arg(value: str) -> int:
    """Parse one of the accepted quality keys: 0, 1, 2, 192, 256 or 320."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"quality must be one of {', '.join(map(str, VALID_QUALITIES))} (got '{value}')"
        ) from None
    if number not in VALID_QUALITIES:
        raise argparse.ArgumentTypeError(
            f"quality must be one of {', '.join(map(str, VALID_QUALITIES))} (got '{value}')"
        )
    return number


def _cover_resolution_arg(value: str) -> str:
    """Parse --cover-resolution: 'original' or a number of at least 100 pixels."""
    raw = str(value).strip()
    if raw.lower() in ('original', 'orig'):
        return 'original'
    try:
        number = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "cover resolution must be 'original' or a number not smaller "
            f"than 100 (got '{value}')"
        ) from None
    if number < 100:
        raise argparse.ArgumentTypeError(
            f"cover resolution must not be smaller than 100 (got '{value}')"
        )
    return str(number)


def _lyrics_format_arg(value: str) -> str:
    """Parse --lyrics-format case-insensitively: NONE, TEXT or LRC."""
    fmt = str(value).strip().upper()
    if fmt not in VALID_LYRICS_FORMATS:
        raise argparse.ArgumentTypeError(
            f"lyrics format must be one of {', '.join(VALID_LYRICS_FORMATS)} (got '{value}')"
        )
    return fmt


def _compatibility_level_arg(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"compatibility level must be one of {COMPATIBILITY_LEVELS} (got '{value}')"
        ) from None
    if number not in COMPATIBILITY_LEVELS:
        raise argparse.ArgumentTypeError(
            f"compatibility level must be one of {COMPATIBILITY_LEVELS} (got '{value}')"
        )
    return number


def create_parser() -> argparse.ArgumentParser:
    """Create the one and only argument parser of the CLI."""
    parser = argparse.ArgumentParser(
        prog="ymd",
        description=f"YMD (v{__version__}) — Download tracks, albums, artists and playlists from Yandex Music.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "qualities: 0 = AAC 64kbps (.m4a), 1 = AAC 192kbps (.m4a),\n"
            "2 = maximum available (FLAC when possible, MP3 otherwise),\n"
            "192/256/320 = MP3 at that bitrate, never above the bitrate the API\n"
            "reports for the track.\n"
            "\n"
            "if --token is absent the YANDEX_MUSIC_TOKEN environment variable\n"
            "is used, then YANDEX_MUSIC_TOKEN from a local .env file or from\n"
            "~/.ymd/.env; when all of that is absent too the interactive Yandex\n"
            "device flow (OAuth) is started (the received token is then stored\n"
            "in ~/.ymd/.env automatically).\n"
            "\n"
            "MP3 conversion requires FFmpeg (ffprobe for re-checking bitrates)."
        ),
    )

    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="show program's version number and exit",
    )

    group = parser.add_argument_group("source", "what to download")
    sources = group.add_mutually_exclusive_group()
    sources.add_argument(
        "--url", help="link to a track, album, artist or playlist on music.yandex.ru"
    )
    sources.add_argument("--track-id", dest="track_id", help="track ID to download")
    sources.add_argument("--album-id", dest="album_id", help="album ID to download")
    sources.add_argument(
        "--artist-id",
        dest="artist_id",
        help="all discography of the artist with this ID",
    )
    sources.add_argument(
        "--playlist-id",
        dest="playlist_id",
        metavar="PLAYLIST",
        help="playlist ID ('owner/kind' or UUID), or 'likes' for the 'My Music' playlist",
    )
    group.add_argument(
        "--recheck",
        action="store_true",
        help=(
            "scan --dir for MP3 files and write a report of their real bitrates "
            "(ffprobe); can be used on its own, without a source"
        ),
    )

    group = parser.add_argument_group("download options")
    group.add_argument(
        "-q",
        "--quality",
        dest="quality",
        type=_quality_arg,
        default=0,
        metavar="{0,1,2,192,256,320}",
        help=(
            "download/conversion quality: 0, 1, 2, 192, 256 or 320 "
            "(default: 0 = AAC 64kbps)"
        ),
    )
    group.add_argument(
        "--force-mp3",
        action="store_true",
        help="with quality 0 or 1, convert the AAC stream to MP3 (default: keep .m4a)",
    )
    group.add_argument(
        "--dir",
        dest="dir",
        default=".",
        metavar="PATH",
        help="root directory for downloaded files (default: current directory)",
    )
    group.add_argument(
        "--path-pattern",
        dest="path_pattern",
        default=DEFAULT_PATH_PATTERN,
        metavar="PATTERN",
        help=(
            "where files land inside --dir, placeholders: {id}, {title}, "
            "{artists}, {artist}, {album}, {album_artist}, {track_number}, "
            "{track_number_padded}, {real_track_number}, "
            "{real_track_number_padded}, {disc_number}, {year}, {genre}, "
            "{duration}, {bitrate}, {extension}, {extension_no_dot} "
            f"(default: {DEFAULT_PATH_PATTERN})"
        ),
    )
    group.add_argument(
        "--compatibility-level",
        dest="compatibility_level",
        type=_compatibility_level_arg,
        default=0,
        metavar="{0,1}",
        help="level 1: never request FLAC, quality 2 becomes maximum MP3/AAC (default: 0)",
    )
    group.add_argument(
        "--overwrite",
        action="store_true",
        help="download a file even when the target path already exists",
    )
    group.add_argument(
        "--skip-existing",
        dest="skip_existing",
        action="store_true",
        help=(
            "scan the output directory before downloading and skip every track "
            "whose file name is already there"
        ),
    )
    group.add_argument(
        "--unsafe-path",
        dest="unsafe_path",
        action="store_true",
        help=(
            "disable the filename sanitizer that removes characters forbidden "
            "by Windows and POSIX (for file servers)"
        ),
    )
    group.add_argument(
        "--list-only",
        dest="list_only",
        action="store_true",
        help="save the names of all found tracks to a file without downloading anything",
    )
    group.add_argument(
        "--save-unavailable",
        dest="save_unavailable",
        action="store_true",
        help="write 'unavailable_tracks_TIMESTAMP.txt' listing the skipped tracks",
    )
    group.add_argument(
        "--stick-to-artist",
        dest="stick_to_artist",
        action="store_true",
        help="with an artist source, skip albums and tracks where the artist is not primary",
    )
    group.add_argument(
        "--only-music",
        dest="only_music",
        action="store_true",
        help="with an artist source, skip podcasts, audioplays and other non-music releases",
    )

    group = parser.add_argument_group("metadata options")
    group.add_argument(
        "--embed-cover",
        dest="embed_cover",
        action="store_true",
        help="embed the album cover into the tags of the downloaded files (default)",
    )
    group.add_argument(
        "--no-embed-cover",
        dest="embed_cover",
        action="store_false",
        help=(
            "do not embed the cover; save it next to the track as cover.jpg "
            "instead (embedding is on by default)"
        ),
    )
    # Встраивание обложки включено по умолчанию (как в PR8 по умолчанию качало
    # обложку, но здесь она ещё и пишется в теги). --no-embed-cover отключает.
    parser.set_defaults(embed_cover=True)
    group.add_argument(
        "--cover-resolution",
        dest="cover_resolution",
        type=_cover_resolution_arg,
        default="600",
        metavar="original|N",
        help=(
            "cover preview to download: 'original' or a number of pixels not "
            "smaller than 100. Yandex serves square previews 100/200/300/600/1000; "
            "a size it does not have (for example 500) falls back to the available "
            "ones (default: 600)"
        ),
    )
    group.add_argument(
        "--lyrics-format",
        dest="lyrics_format",
        type=_lyrics_format_arg,
        default="NONE",
        metavar="{NONE,TEXT,LRC}",
        help=(
            "lyrics behaviour: NONE = do not fetch (default), "
            "TEXT = plain text embedded in the tags, "
            "LRC = .lrc file alongside the track (nothing embedded); "
            "when no synced lyrics exist, LRC embeds plain text"
        ),
    )

    group = parser.add_argument_group("connection options")
    group.add_argument(
        "-t",
        "--token",
        dest="token",
        help=(
            "Yandex Music OAuth token; when absent the YANDEX_MUSIC_TOKEN "
            "environment variable is read, then a local .env file or "
            "~/.ymd/.env, and without all of that the interactive device "
            "flow (OAuth) is started"
        ),
    )
    group.add_argument(
        "--tries",
        type=_positive_int,
        default=20,
        metavar="N",
        help="how many times a failed network request is retried (default: 20)",
    )
    group.add_argument(
        "--retry-delay",
        dest="retry_delay",
        type=_non_negative_int,
        default=5,
        metavar="SEC",
        help="pause between failed network requests in seconds (default: 5)",
    )
    group.add_argument(
        "--delay",
        type=_non_negative_float,
        default=0.0,
        metavar="SEC",
        help="pause between downloaded tracks (default: 0)",
    )
    group.add_argument(
        "--timeout",
        type=_positive_int,
        default=20,
        metavar="SEC",
        help="single request/download timeout in seconds, at least 1 (default: 20)",
    )
    group.add_argument(
        "--proxy",
        dest="proxy",
        help="proxy URL, for example socks5://127.0.0.1:1080",
    )
    group.add_argument(
        "--no-check-certificate",
        dest="no_check_certificate",
        action="store_true",
        help="disable TLS certificate verification",
    )

    group = parser.add_argument_group("logging options")
    group.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="print the full Python traceback of any error",
    )

    return parser


def _load_env_token() -> Optional[str]:
    """Search YANDEX_MUSIC_TOKEN in a local .env or in the global ~/.ymd/.env.

    Files are read with the standard library only (no python-dotenv):
    plain ``KEY=value`` lines, ``#`` comments, optional single/double
    quotes around the value. The local ``./.env`` has priority over
    ``~/.ymd/.env``.
    """
    candidates = [
        Path.cwd() / ".env",
        Path.home() / ".ymd" / ".env",
    ]
    for env_path in candidates:
        if env_path.is_file():
            try:
                # utf-8-sig: файл, созданный Блокнотом или PowerShell, может быть
                # с BOM — без его снятия первая строка не распознаётся как ключ
                for line in env_path.read_text(encoding="utf-8-sig").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("YANDEX_MUSIC_TOKEN="):
                        val = line.split("=", 1)[1].strip()
                        val = val.strip("'\"")
                        if val:
                            return val
            except OSError:
                pass
    return None


def parse_args(
    argv: Optional[Sequence[str]] = None,
) -> Tuple[argparse.ArgumentParser, AppConfig]:
    """Parse ``argv`` and return the parser together with an :class:`AppConfig`.

    The source arguments stay optional here so that ``--recheck`` alone and
    ``--help`` keep working; the caller (``main``) reports the missing source
    through ``parser.error`` and exits with code 2.
    """
    if argv is None:
        argv = sys.argv[1:]

    parser = create_parser()
    args = parser.parse_args(argv)

    # Иерархия токена: флаг --token > переменная окружения YANDEX_MUSIC_TOKEN >
    # YANDEX_MUSIC_TOKEN из .env (локальный ./.env, затем ~/.ymd/.env).
    token = args.token or os.environ.get("YANDEX_MUSIC_TOKEN") or _load_env_token()

    config = AppConfig(
        url=args.url,
        track_id=args.track_id,
        album_id=args.album_id,
        playlist_id=args.playlist_id,
        artist_id=args.artist_id,
        token=token,
        output_dir=args.dir,
        path_pattern=args.path_pattern,
        unsafe_path=args.unsafe_path,
        quality=args.quality,
        force_mp3=args.force_mp3,
        compatibility_level=args.compatibility_level,
        stick_to_artist=args.stick_to_artist,
        only_music=args.only_music,
        skip_existing=args.skip_existing,
        overwrite=args.overwrite,
        save_unavailable=args.save_unavailable,
        list_only=args.list_only,
        recheck=args.recheck,
        embed_cover=args.embed_cover,
        cover_resolution=args.cover_resolution,
        lyrics_format=args.lyrics_format,
        tries=args.tries,
        retry_delay=args.retry_delay,
        delay=args.delay,
        timeout=args.timeout,
        proxy_url=args.proxy,
        no_check_certificate=args.no_check_certificate,
        debug=args.debug,
    )
    return parser, config
