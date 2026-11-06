"""
Загрузка треков: получение signed-ссылок через tracks_file_info, сохранение,
конвертация по плану, теги/обложки/тексты, статистика и отчёты.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yandex_music.exceptions
from yandex_music import Client, Track

from ymd.config import AppConfig
from ymd.core.converter import AudioConverter
from ymd.core.formats import FormatDetector
from ymd.core.quality import (
    NATIVE_EXTENSIONS,
    ConversionPlan,
    DownloadQuality,
    describe_available,
    select_download_info,
)
from ymd.exceptions import DownloadError, QualityError
from ymd.filesystem.naming import FileNamer
from ymd.filesystem.paths import (
    ensure_directory,
    get_unique_filename,
    sanitize_filename,
    select_default_pattern,
)
from ymd.metadata.covers import CoverData, CoverDownloader
from ymd.metadata.lyrics import LyricsDownloader
from ymd.metadata.tags import TagWriter

logger = logging.getLogger(__name__)

STATUS_DOWNLOADED = 'downloaded'
STATUS_SKIPPED = 'skipped'
STATUS_UNAVAILABLE = 'unavailable'
STATUS_ERROR = 'error'


@dataclass
class DownloadResult:
    """Результат обработки одного трека."""
    status: str
    track: Optional[Track] = None
    path: Optional[Path] = None
    message: str = ''


@dataclass
class DownloadStats:
    """Итоговая статистика загрузки."""
    downloaded: int = 0
    skipped: int = 0
    unavailable: int = 0
    errors: int = 0
    total: int = 0

    def add(self, result: DownloadResult) -> None:
        """Учитывает результат трека."""
        self.total += 1
        if result.status == STATUS_DOWNLOADED:
            self.downloaded += 1
        elif result.status == STATUS_SKIPPED:
            self.skipped += 1
        elif result.status == STATUS_UNAVAILABLE:
            self.unavailable += 1
        else:
            self.errors += 1

    def print_summary(self) -> None:
        """Печатает блок итоговой статистики."""
        print('\n📊 СТАТИСТИКА ЗАГРУЗКИ:')
        print(f'✅ Скачано: {self.downloaded}')
        print(f'⏭️ Пропущено (уже есть): {self.skipped}')
        print(f'🚫 Недоступно: {self.unavailable}')
        print(f'❌ Ошибки: {self.errors}')
        print(f'📝 Всего: {self.total}')


def _timestamp() -> str:
    """Метка времени для имён отчётов."""
    return datetime.now().strftime('%Y%m%d_%H%M%S')


class TrackDownloader:
    """
    Скачивает треки с учётом качества, конвертирует и оформляет файлы.

    Args:
        client: инициализированный Client Яндекс.Музыки
        config: конфигурация приложения
        converter: конвертер (создаётся при необходимости)
        tag_writer: записчик тегов
        cover_downloader: загрузчик обложек (создаётся при необходимости)
        lyrics_downloader: загрузчик текстов (создаётся при необходимости)
    """

    def __init__(
        self,
        client: Client,
        config: AppConfig,
        converter: Optional[AudioConverter] = None,
        tag_writer: Optional[TagWriter] = None,
        cover_downloader: Optional[CoverDownloader] = None,
        lyrics_downloader: Optional[LyricsDownloader] = None,
    ):
        self.client = client
        self.config = config
        self.quality = DownloadQuality(config.quality)
        self.converter = converter or AudioConverter()
        self.tag_writer = tag_writer or TagWriter()
        self.cover_downloader = cover_downloader or CoverDownloader(client)
        self.lyrics_downloader = lyrics_downloader or LyricsDownloader(client)
        self.namer = FileNamer(unsafe_path=config.unsafe_path)
        self.format_detector = FormatDetector()

        self.unavailable_tracks: List[Track] = []
        # Сквозные номера треков многодисковых релизов: {id трека: номер 1..N}.
        # Позиция trackPosition.index у Яндекса считается внутри тома, поэтому
        # без этого нумерация (и имена файлов) в двухдисковых альбомах повторяются.
        self._global_numbers: Dict[str, int] = {}
        # Пути, занятые в этом запуске: {нормализованный путь: ID трека}.
        # Нужно, чтобы разные треки не написали в один файл.
        self._taken_paths: Dict[str, str] = {}
        # ID альбомов, тома которых уже догрушались (prepare_track_numbers).
        self._fetched_album_ids: set = set()
        # Название плейлиста-источника для плейсхолдера {playlist}: задаёт
        # cli/main.py из ResolvedSource.playlist_title (см. api/resolver.py).
        # Вне плейлистов пусто — {playlist} раскрывается в пустую строку.
        self.playlist_title: str = ''

    # ------------------------------------------------------------- file info

    @property
    def path_pattern(self) -> str:
        """
        Действующий шаблон пути.

        CLI проставляет его контекстно (cli/main._apply_path_pattern) уже после
        создания загрузчика, поэтому значение читается из config в момент
        форматирования. Если шаблон не задан вовсе (программу используют как
        библиотеку), берётся запасной — тот же, что select_default_pattern
        даёт для неизвестного типа источника (шаблон альбома).
        """
        return self.config.path_pattern or select_default_pattern(None)

    def fetch_file_info(self, track: Track) -> Optional[Tuple[Any, ConversionPlan]]:
        """
        Селектор потоков трека: делегирует core.quality.select_download_info.

        Returns:
            (download_info, ConversionPlan) — выбранный поток для загрузки и
            структура решения (целевой кодек, битрейт, флаг транскодирования);
            None, если у трека нет файла.
        """
        return select_download_info(self.client, track, self.quality,
                                    compatibility_level=self.config.compatibility_level,
                                    force_mp3=self.config.force_mp3)

    # ---------------------------------------------------------------- naming

    def _build_track_info(self, track: Track, bitrate: Optional[int],
                          track_number: Optional[int] = None) -> Dict[str, Any]:
        """Собирает словарь данных трека для шаблона --path-pattern."""
        from ymd.metadata.tags import TagWriter as _TW
        if track_number is None:
            track_number = self._global_numbers.get(str(track.id))
        base = _TW()._extract_track_info(track, explicit_track_number=track_number)
        return {
            'id': track.id,
            'title': base['title'],
            'artists': base['artists'],
            'album': base['album_title'],
            'album_artists': base['album_artists'],
            'track_number': base['track_number'],
            'real_track_number': base['real_track_number'],
            'disc_number': base['disc_number'],
            'year': base['year'],
            'genre': base['genre'],
            'duration': int((track.duration_ms or 0) // 1000) or None,
            'bitrate': bitrate,
            'playlist': self.playlist_title,
        }

    def _is_already_downloaded(self, full_path: Path) -> bool:
        """
        --skip-existing: проверяем наличие файла строго в целевой директории.

        Базовое имя (имя без расширения) ищется только в full_path.parent —
        без рекурсивного обхода всего --dir: раньше трек пропускался, если
        такое же имя встречалось в любой другой папке дискографии, из-за чего
        многодисковые и однотипные релизы терялись.
        """
        parent = full_path.parent
        base_name = full_path.stem
        if not parent.is_dir():
            return False
        for candidate in parent.iterdir():
            if (candidate.is_file()
                    and candidate.suffix.lower() in ('.mp3', '.m4a', '.flac', '.mp4')
                    and candidate.stem == base_name):
                return True
        return False

    def _reserve_path(self, rel_path: str, track_id: str) -> str:
        """
        Закрепляет относительный путь за треком и защищает от коллизий.

        Яндекс нумерует треки внутри тома, поэтому в многодисковом релизе два
        разных трека могут дать одинаковый путь ('02 - Sunshine Reggae' на первом
        и на втором диске). Сквозная нумерация такие совпадения обычно снимает,
        но если путь всё же повторился — берём свободное имя, а не перезатираем
        файл другого трека молча.
        """
        def normalize(value: str) -> str:
            return str(Path(value)).replace('\\', '/').casefold()

        key = normalize(rel_path)
        owner = self._taken_paths.get(key)
        if owner is None or owner == track_id:
            # путь свободен, или это тот же трек (повторное скачивание — норма)
            self._taken_paths[key] = track_id
            return rel_path

        path = Path(rel_path)
        counter = 1
        while True:
            candidate = path.parent / f'{path.stem} ({counter}){path.suffix}'
            ckey = normalize(str(candidate))
            exists = (Path(self.config.output_dir) / candidate).exists()
            if ckey not in self._taken_paths and not exists:
                self._taken_paths[ckey] = track_id
                print(f'  ⚠️ Совпадение имён: {rel_path} уже занят другим треком, '
                      f'сохраняем как {candidate}')
                return str(candidate)
            counter += 1

    # ------------------------------------------------------------- download

    def download_track(self, track: Optional[Track], index: int = 0, total: int = 0,
                       track_number: Optional[int] = None) -> DownloadResult:
        """
        Скачивает один трек целиком: file-info → bytes → конвертация → теги.

        Args:
            track: трек (или ShortTrack из плейлиста)
            index: номер трека (1-based) для прогресс-строки
            total: всего треков в батче
            track_number: сквозной номер трека в релизе. Если не передан,
                берётся из self._global_numbers (заполняется при разборе
                альбома) или из trackPosition.index (номер внутри тома).

        Returns:
            DownloadResult со статусом downloaded/skipped/unavailable/error.
        """
        if track is None:
            return DownloadResult(STATUS_ERROR, message='Трек отсутствует (None)')

        # Треки из плейлистов могут быть TrackShort (без title/artists):
        # используем встроенный полноценный .track, иначе — одиночный запрос.
        if not hasattr(track, 'title'):
            inner = getattr(track, 'track', None)
            if inner is not None and hasattr(inner, 'title'):
                track = inner
            elif hasattr(track, 'fetch_track'):
                try:
                    track = track.fetch_track() or track
                except yandex_music.exceptions.YandexMusicError:
                    pass

        title_line = _display_title(track)

        # 1. Доступность
        if getattr(track, 'available', None) is False or getattr(track, 'error', None):
            self.unavailable_tracks.append(track)
            print(f'[{index}/{total}] 🚫 Недоступен: {title_line}')
            return DownloadResult(STATUS_UNAVAILABLE, track=track, message='Трек недоступен')

        # 2. Информация о файле + решение о сохранении (селектор потоков v0.0.2:
        # для 192/256/320 запрашивается и lossless — при наличии FLAC он станет
        # источником для MP3 запрошенного битрейта).
        # Яндекс отдаёт bitrate уже в кбит/с (64/192/256/320; 0 — lossless).
        selection = self.fetch_file_info(track)
        if selection is None:
            self.unavailable_tracks.append(track)
            print(f'[{index}/{total}] 🚫 Недоступен (API не выдал ссылку): {title_line}')
            return DownloadResult(STATUS_UNAVAILABLE, track=track, message='Нет ссылки на файл')

        info, plan = selection
        bitrate_kbps = plan.source_bitrate

        # 4. Путь и skip-existing (до скачивания, чтобы не гонять трафик)
        number = track_number
        if number is None:
            number = self._global_numbers.get(str(track.id))
        track_info = self._build_track_info(track, bitrate_kbps or plan.target_bitrate,
                                            track_number=number)
        rel_path = self.namer.format_filename(track_info, self.path_pattern,
                                              extension=plan.output_extension)
        rel_path = self._reserve_path(rel_path, str(track.id))
        full_path = Path(self.config.output_dir) / rel_path
        base_name = Path(rel_path).stem

        if self.config.skip_existing and self._is_already_downloaded(full_path):
            print(f'[{index}/{total}] ⏭️ Уже существует: {rel_path}')
            return DownloadResult(STATUS_SKIPPED, track=track, path=full_path,
                                  message='Файл уже существует')

        # 5. Прогресс (матрица v0.0.2: lossless-источник подсвечивается отдельно)
        label = describe_available(info.codec, bitrate_kbps)
        print(f'[{index}/{total}] [{label}] Загружается {rel_path}')
        print(f'  API вернул: {plan.source_description()}')
        print(f'  {plan.save_action()}: {plan.output_extension} ({plan.reason})')

        # 6. Скачивание байтов
        try:
            data = info.download_bytes(timeout=self.config.timeout)
        except yandex_music.exceptions.YandexMusicError as e:
            return DownloadResult(STATUS_ERROR, track=track,
                                  message=f'Ошибка скачивания: {e}')

        if not data:
            return DownloadResult(STATUS_ERROR, track=track, message='Пустой ответ при скачивании')

        # 7. Запись нативного файла и конвертация по плану
        ensure_directory(full_path.parent)
        native_ext = NATIVE_EXTENSIONS.get(info.codec, '.dat')
        tmp_path = full_path.parent / (base_name + native_ext)

        # Защита от перезаписи чужих файлов (если не --overwrite)
        if tmp_path.exists() and not self.config.overwrite:
            tmp_path = get_unique_filename(tmp_path)

        # Скачивание в файл-заметку '.part': недокачанный файл не выглядит
        # как аудио (его не видят --skip-existing, --recheck и плееры), а после
        # полной записи переименование в целевое имя выполняется атомарно.
        part_path = tmp_path.with_name(tmp_path.name + '.part')
        try:
            part_path.write_bytes(data)
            part_path.replace(tmp_path)  # os.replace: атомарно в пределах тома
        except OSError as e:
            try:
                part_path.unlink(missing_ok=True)
            except OSError:
                pass
            return DownloadResult(STATUS_ERROR, track=track, message=f'Ошибка записи файла: {e}')

        final_path = self.converter.convert(tmp_path, plan,
                                            overwrite=self.config.overwrite)
        if final_path != tmp_path and final_path.exists() and final_path != full_path:
            # после конвертации переносим в плановое имя
            target = full_path
            if target.exists():
                if self.config.overwrite:
                    target.unlink()
                else:
                    target = get_unique_filename(target)
            try:
                final_path.rename(target)
                final_path = target
            except OSError as e:
                logger.warning(f'Не удалось переименовать {final_path}: {e}')

        # Файл в формате, не соответствующем плану (ffmpeg недоступен и т.п.)
        if final_path.suffix != plan.output_extension:
            logger.warning(f'Файл сохранён в исходном формате: {final_path}')

        # 8. Метаданные: обложка, тексты, теги
        cover_data = self._prepare_cover(track, final_path)
        lyrics_text = self._prepare_lyrics(track, final_path)

        try:
            embed_cover = cover_data if self.config.embed_cover else None
            # _prepare_lyrics уже учёл --lyrics-format: возвращает текст только тогда,
            # когда его надо встроить (TEXT, либо LRC без синхронного текста).
            self.tag_writer.write_tags(final_path, track,
                                       cover_data=embed_cover, lyrics=lyrics_text,
                                       track_number=number,
                                       compatibility_level=self.config.compatibility_level)
        except Exception as e:
            # теги — не критично: файл скачан
            logger.warning(f'Не удалось записать теги в {final_path}: {e}')
            print(f'  ⚠️ Теги не записаны: {e}')

        return DownloadResult(STATUS_DOWNLOADED, track=track, path=final_path)

    def download_tracks(self, tracks: Iterable[Track],
                        track_numbers: Optional[Dict[str, int]] = None) -> DownloadStats:
        """
        Скачивает список треков подряд (с паузой --delay между треками).

        Args:
            tracks: итерируемые треки
            track_numbers: {ID трека: сквозной номер} (см. build_track_numbers).
                Если не передан, используются номера, запомненные
                remember_album_numbers().

        Returns:
            Итоговая DownloadStats.
        """
        if track_numbers:
            self._global_numbers.update({str(k): int(v) for k, v in track_numbers.items()})
        tracks = list(tracks)
        # Сквозные номера нужны, чтобы в многотомных релизах не повторялись
        # и номера, и имена файлов (см. prepare_track_numbers).
        self.prepare_track_numbers(tracks)
        total = len(tracks)
        stats = DownloadStats()

        for i, track in enumerate(tracks, start=1):
            result = self.download_track(track, index=i, total=total)
            stats.add(result)
            if result.status == STATUS_ERROR:
                print(f'  ❌ {result.message}')
            if self.config.delay and i < total:
                time.sleep(self.config.delay)

        return stats

    # ------------------------------------------------------------- covers/lyrics

    def _prepare_cover(self, track: Track, final_path: Path) -> Optional[CoverData]:
        """Скачивает обложку; при --embed-cover возвращает данные для встраивания,
        иначе сохраняет side-файл cover.jpg/png рядом с треком."""
        if not getattr(track, 'cover_uri', None):
            return None
        try:
            cover = self.cover_downloader.download_cover(track, size=self.config.get_cover_size())
        except Exception as e:
            logger.warning(f'Не удалось скачать обложку: {e}')
            return None
        if not cover:
            return None

        if self.config.embed_cover:
            return cover

        # side-файл: обложка сохраняется рядом (cover.jpg/.png), не встраивается
        ext = '.png' if cover.mime_type == 'image/png' else '.jpg'
        cover_file = final_path.parent / f'cover{ext}'
        try:
            cover_file.write_bytes(cover.data)
        except OSError as e:
            logger.warning(f'Не удалось сохранить обложку {cover_file}: {e}')
        return None

    def _prepare_lyrics(self, track: Track, final_path: Path) -> Optional[str]:
        """
        Готовит тексты согласно --lyrics-format (как в PR8):
        TEXT → только встроенный тег, LRC → side-файл .lrc (а если синхронного
        текста нет — обычный текст в теги), NONE → ничего.
        """
        fmt = self.config.get_lyrics_format()
        if fmt == 'NONE':
            return None

        if fmt == 'LRC':
            sync = self.lyrics_downloader.download_lyrics_sync(track)
            if sync:
                lrc_file = final_path.parent / (final_path.stem + '.lrc')
                try:
                    lrc_file.write_text(sync, encoding='utf-8')
                except OSError as e:
                    logger.warning(f'Не удалось сохранить {lrc_file}: {e}')
                return None
            # Синхронного текста нет — как в PR8, оставляем обычный в тегах.
            return self.lyrics_downloader.download_lyrics(track, format_type='TEXT')

        # TEXT: текст только встраивается в теги, отдельного файла нет (как в PR8)
        return self.lyrics_downloader.download_lyrics(track, format_type='TEXT')

    # ------------------------------------------------------------- collections

    def expand_short_tracks(self, tracks: Iterable[Any]) -> List[Any]:
        """
        Приводит TrackShort/ShortTrack к полноценным Track.

        Если у краткого трека есть встроенный полный .track (бывает в
        плейлистах) — берём его без запроса; иначе догружаем пакетами
        по 50 ID через client.tracks() (быстрее, чем fetch_track поштучно).
        Порядок треков сохраняется.

        Args:
            tracks: трэки любого вида (Track | TrackShort | None)

        Returns:
            Список треков без None; там, где догрузить не удалось,
            остаётся исходный краткий трек.
        """
        tracks = [t for t in tracks if t is not None]
        need_ids: List[str] = []
        for t in tracks:
            if hasattr(t, 'title'):
                continue
            inner = getattr(t, 'track', None)
            if inner is not None and hasattr(inner, 'title'):
                continue
            need_ids.append(str(getattr(t, 'track_id', None) or t.id))

        if not need_ids:
            return tracks

        mapping: Dict[str, Any] = {}
        for i in range(0, len(need_ids), 50):
            try:
                for full in self.client.tracks(need_ids[i:i + 50]) or []:
                    if full is not None:
                        mapping[str(full.id)] = full
            except yandex_music.exceptions.YandexMusicError as e:
                logger.warning(f"Не удалось догрузить пакет треков: {e}")

        result: List[Any] = []
        for t in tracks:
            if hasattr(t, 'title'):
                result.append(t)
                continue
            inner = getattr(t, 'track', None)
            if inner is not None and hasattr(inner, 'title'):
                result.append(inner)
                continue
            full = mapping.get(str(getattr(t, 'track_id', None) or t.id))
            result.append(full if full is not None else t)
        return result

    def album_tracks(self, album, apply_filters: bool = False,
                     artist_id: Optional[int] = None) -> List[Track]:
        """
        Возвращает треки альбома (через albums_with_tracks).

        Args:
            album: объект Album
            apply_filters: применять --only-music/--stick-to_artist (для потока артиста)
            artist_id: ID артиста для stick_to_artist
        """
        full_album = self.client.albums_with_tracks(album.id)
        if full_album is None:
            raise DownloadError(f"Альбом {album.id} не загружен")

        if apply_filters and not self._album_matches(full_album, artist_id):
            return []

        # В yandex-music 3.x у Album нет атрибута tracks — треки лежат в
        # volumes: список дисков, каждый — список треков.
        volumes = getattr(full_album, 'volumes', None)
        if volumes:
            tracks = [track for volume in volumes for track in volume]
        else:
            tracks = list(getattr(full_album, 'tracks', None) or [])
        self.remember_album_numbers(full_album)
        return tracks

    def remember_album_numbers(self, album: Any) -> Dict[str, int]:
        """
        Запоминает сквозные номера треков релиза (для {track_number} и тегов).

        Нужен только многодисковым релизам: tamTrackPosition.index там нумерует
        треки внутри тома, из-за чего номер 02 есть и на первом, и на втором
        диске — два разных трека получают одно имя файла.
        """
        numbers = build_track_numbers(album)
        self._global_numbers.update(numbers)
        return numbers

    def prepare_track_numbers(self, tracks: Iterable[Any]) -> None:
        """
        Гарантивает сквозные номера треков для многотомных релизов.

        Номера появляются сами при разборе альбома (album_tracks). Если же треки
        пришли из плейлиста, из --url или по --track-id, тома альбома заранее не
        известны: для каждого такого альбома (volume > 1) делаем по одному
        albums_with_tracks. Треки однотомных релизов не затрагиваем вовсе.
        """
        album_ids = set()
        for track in tracks:
            if str(_track_field(track, 'id')) in self._global_numbers:
                continue
            albums = _track_field(track, 'albums') or []
            album = albums[0] if albums else None
            position = getattr(album, 'track_position', None) if album is not None else None
            if position is not None and (getattr(position, 'volume', 0) or 0) > 1:
                album_ids.add(str(getattr(album, 'id', '')))

        for album_id in sorted(album_ids):
            if not album_id or album_id in self._fetched_album_ids:
                continue
            self._fetched_album_ids.add(album_id)
            try:
                full = self.client.albums_with_tracks(album_id)
            except yandex_music.exceptions.YandexMusicError as e:
                logger.debug(f'Не удалось получить тома альбома {album_id}: {e}')
                continue
            if full is not None:
                self.remember_album_numbers(full)

    def artist_albums(self, artist_id: int) -> List[Any]:
        """
        Возвращает все прямые альбомы артиста (с пагинацией).

        У ArtistAlbums нет атрибута total — количество лежит в result.pager
        (Pager: total / page / per_page).
        """
        albums: List[Any] = []
        page = 0
        while True:
            result = self.client.artists_direct_albums(artist_id, page=page, page_size=100)
            if not result or not result.albums:
                break
            albums.extend(result.albums)
            pager = getattr(result, 'pager', None)
            total = getattr(pager, 'total', None) if pager is not None else None
            if total is None or len(albums) >= total:
                break
            page += 1
        return albums

    def _album_matches(self, album: Any, artist_id: Optional[int]) -> bool:
        """Проверяет альбом фильтрами --only-music и --stick-to-artist."""
        if self.config.only_music:
            meta_type = getattr(album, 'meta_type', None)
            if meta_type is not None and meta_type != 'music':
                logger.debug(f"Пропуск не-музыкального альбома {album.id} ({meta_type})")
                return False
        if self.config.stick_to_artist and artist_id is not None:
            album_artists = getattr(album, 'artists', None) or []
            first_id = getattr(album_artists[0], 'id', None) if album_artists else None
            if first_id is not None and int(first_id) != int(artist_id):
                logger.debug(f"Пропуск сборника {album.id} (первый артист не {artist_id})")
                return False
        return True

    # ------------------------------------------------------------- reports

    def save_unavailable_report(self) -> Optional[Path]:
        """Сохраняет список недоступных треков (--save-unavailable)."""
        if not self.unavailable_tracks:
            return None
        path = Path(self.config.output_dir) / f'unavailable_tracks_{_timestamp()}.txt'
        ensure_directory(path.parent)
        lines = []
        for i, track in enumerate(self.unavailable_tracks, start=1):
            artists = _artists_string(track)
            albums = _track_field(track, 'albums') or []
            album = albums[0].title if albums and getattr(albums[0], 'title', None) else '?'
            lines.append(f'{i:4}. {artists} - {_title_string(track)}')
            lines.append(f'     ID: {getattr(track, "id", "?")}')
            lines.append(f'     Альбом: {album}')
        path.write_text('\n'.join(lines), encoding='utf-8')
        return path


def build_track_numbers(album: Any) -> Dict[str, int]:
    """
    Считает сквозные номера треков релиза: {ID трека: номер 1..N}.

    В многотомных релизах Яндекс хранит позицию как volume/index, где index
    нумерует треки внутри тома. Из-за этого номера повторяются: и на первом,
    и на втором диске есть, например, трек 02 с тем же названием — два разных
    трека получают одно имя файла. Здесь номера считаются сквозными по всем
    томам, как их показывает суммарно страница релиза.

    Args:
        album: Album из albums_with_tracks (с volumes) или его dict-представление

    Returns:
        Пустой словарь, если томов нет (однотомный релиз — трогать нечего).
    """
    volumes = getattr(album, 'volumes', None)
    if volumes is None and isinstance(album, dict):
        volumes = album.get('volumes')
    numbers: Dict[str, int] = {}
    if not volumes:
        return numbers

    offset = 0
    for volume in volumes:
        tracks = list(volume or [])
        for position, track in enumerate(tracks, start=1):
            track_id = getattr(track, 'id', None)
            if track_id is None and isinstance(track, dict):
                track_id = track.get('id')
            if track_id is not None:
                numbers[str(track_id)] = offset + position
        offset += len(tracks)
    return numbers


def _track_field(track: Any, name: str, default: Any = None) -> Any:
    """
    Поле трека или его встроенного полноценного .track (поддержка TrackShort).
    """
    value = getattr(track, name, None)
    if value is None:
        inner = getattr(track, 'track', None)
        if inner is not None:
            value = getattr(inner, name, None)
    return default if value is None else value


def _artists_string(track: Any) -> str:
    """Список исполнителей через запятую (или '?')."""
    artists = _track_field(track, 'artists')
    if not artists:
        return '?'
    return ', '.join(getattr(a, 'name', None) or getattr(a, 'title', '?') for a in artists)


def _title_string(track: Any) -> str:
    """Название трека (или запасной вариант по ID)."""
    title = _track_field(track, 'title')
    return title or f"ID {getattr(track, 'id', '?')}"


def _album_title(album: Any) -> str:
    """Название альбома (пустая строка, если альбома нет)."""
    return str(getattr(album, 'title', '') or '') if album is not None else ''


def _display_title(track: Any) -> str:
    """Строка 'Артисты — Название' для консоли и отчётов."""
    return f'{_artists_string(track)} — {_title_string(track)}'


def save_tracks_list(tracks: Iterable[Track], output_dir: str) -> Path:
    """
    Сохраняет список треков (--list-only): 'tracks_list_<ts>.txt',
    строки вида 'Альбом-артист - Название' (без отступа); недоступные помечаются 🚫.
    """
    tracks = list(tracks)
    path = Path(output_dir) / f'tracks_list_{_timestamp()}.txt'
    ensure_directory(path.parent)

    lines = []
    for track in tracks:
        if track is None:
            continue
        albums = _track_field(track, 'albums') or []
        album = albums[0] if albums else None
        album_artists = ''
        if album is not None and getattr(album, 'artists', None):
            album_artists = ', '.join(getattr(a, 'name', None) or getattr(a, 'title', '?')
                                      for a in album.artists)
        if not album_artists:
            album_artists = _artists_string(track)
        if album_artists == '?':
            # У подкастов и аудиокниг Яндекс не отдаёт артистов ни у трека, ни у
            # альбома (artists=[]): вместо '?' показываем название релиза.
            album_artists = _album_title(album) or '?'
        title = _title_string(track)
        available = _track_field(track, 'available')
        if available is False or _track_field(track, 'error'):
            lines.append(f'🚫 {album_artists} - {title} (НЕДОСТУПЕН)')
        else:
            lines.append(f'{album_artists} - {title}')

    path.write_text('\n'.join(lines), encoding='utf-8')
    return path


def recheck_bitrates(output_dir: str, threshold: int = 192) -> Dict[int, List[Path]]:
    """
    --recheck: проверяет реальные битрейты скачанных MP3 (mutagen).

    Args:
        output_dir: каталог с файлами
        threshold: минимальный «полноценный» битрейт для отбора (не влияет на учёт)

    Returns:
        {битрейт_кбит_с: [пути]} — сгруппированные по фактическому битрейту файлы.
    """
    from mutagen.mp3 import MP3

    groups: Dict[int, List[Path]] = {}
    root = Path(output_dir)
    for file in root.glob('**/*.mp3'):
        try:
            audio = MP3(file)
            if audio.info and audio.info.bitrate:
                kbps = int(audio.info.bitrate // 1000)
            else:
                kbps = 0
        except Exception as e:
            logger.warning(f'recheck: не удалось прочитать {file}: {e}')
            kbps = -1
        groups.setdefault(kbps, []).append(file)
    return groups


def write_bitrate_report(groups: Dict[int, List[Path]], output_dir: str) -> Path:
    """Сохраняет отчёт --recheck в 'bitrate_report_<ts>.txt' и печатает гистограмму."""
    path = Path(output_dir) / f'bitrate_report_{_timestamp()}.txt'
    ensure_directory(path.parent)

    total = sum(len(v) for v in groups.values())
    lines = [f'Проверка битрейтов MP3-файлов: {total} файлов', '']
    for bitrate in sorted(groups, reverse=True):
        count = len(groups[bitrate])
        bar = '█' * max(1, int(40 * count / total)) if total else ''
        label = f'{bitrate} kbps' if bitrate >= 0 else 'ошибка чтения'
        lines.append(f'  {label}: {count} файлов {bar}')
    report = '\n'.join(lines)
    path.write_text(report, encoding='utf-8')
    print(report)
    print(f'Отчёт сохранён: {path}')
    return path
