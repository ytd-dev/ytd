"""
Запись ID3/Vorbis/MP4-тегов в аудиофайлы.
"""

import logging
from pathlib import Path
from typing import Optional

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, APIC, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, TPOS, USLT, WOAF
from mutagen.id3._specs import ID3TimeStamp, PictureType
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

from yandex_music import Track

from ymd.exceptions import TagError
from ymd.metadata.covers import CoverData

logger = logging.getLogger(__name__)


class TagWriter:
    """
    Записывает теги в аудиофайлы (MP3 — ID3v2.4, FLAC — Vorbis comment,
    M4A/MP4 — MP4 atoms).
    """

    def write_tags(
        self,
        file_path: Path,
        track: Track,
        cover_data: Optional[CoverData] = None,
        lyrics: Optional[str] = None,
        track_number: Optional[int] = None,
        compatibility_level: int = 0,
    ) -> None:
        """
        Записывает теги в аудиофайл.

        Args:
            file_path: путь к файлу
            track: объект трека из yandex-music-api
            cover_data: данные обложки (встраиваются, если переданы)
            lyrics: текст песни (встраивается, если передан)
            track_number: сквозной номер трека в релизе (для многодисковых
                альбомов). Если передан, используется вместо trackPosition.index.
            compatibility_level: уровень совместимости (--compatibility-level):
                >= 1 сохраняет ID3 в версии 2.3 вместо 2.4.

        Raises:
            TagError: если не удалось записать теги.
        """
        try:
            if file_path.suffix == '.mp3':
                self._write_mp3_tags(file_path, track, cover_data, lyrics, track_number,
                                     compatibility_level)
            elif file_path.suffix == '.flac':
                self._write_flac_tags(file_path, track, cover_data, lyrics, track_number)
            elif file_path.suffix in ('.m4a', '.mp4'):
                self._write_mp4_tags(file_path, track, cover_data, lyrics, track_number)
            else:
                logger.warning(f"Неизвестный формат файла: {file_path.suffix}")

        except Exception as e:
            raise TagError(f"Ошибка при записи тегов в {file_path}: {e}") from e

    def _extract_track_info(self, track: Track,
                            explicit_track_number: Optional[int] = None) -> dict:
        """
        Извлекает информацию о треке для тегов.

        Исправление бага PR10: album_artists больше не подменяется списком
        исполнителей самого трека — если у альбома исполнители не указаны,
        поле остаётся пустым (тег albumartist просто не пишется).

        Args:
            track: объект трека
            explicit_track_number: сквозной номер трека в релизе. trackPosition.index
                в многодисковом альбоме нумерует треки внутри каждого диска
                (иногда от 0), поэтому для тегов trkn/TRCK/tracknumber предпочтителен
                сквозной номер, посчитанный по volumes релиза.
        """
        album = track.albums[0] if track.albums else None

        artists = [a.name for a in track.artists] if track.artists else []
        # Исполнитель альбома — только если реально известен у альбома
        album_artists = [a.name for a in album.artists] if album and album.artists else []

        # Номер трека и диска. trackPosition у Яндекса — это позиция ВНУТРИ тома
        # (volume/index), поэтому в двухдисковом релизе номера повторяются.
        # real_track_number — сырой index из API, track_number — сквозной (1..N
        # по всему релизу), если он известен из volumes альбома.
        position_index = None
        disc_number = None
        if album is not None:
            track_position = getattr(album, 'track_position', None)
            if track_position is not None:
                position_index = getattr(track_position, 'index', None)
                disc_number = getattr(track_position, 'volume', None)
        track_number = position_index
        if track_number is None:
            # запасной источник: у самого трека может быть position в album
            track_number = getattr(track, 'track_number', None)
        if explicit_track_number is not None:
            track_number = explicit_track_number

        # Год
        year = None
        if album is not None:
            if getattr(album, 'year', None):
                year = str(album.year)[:4]
            elif getattr(album, 'release_date', None):
                year = album.release_date[:4]
        if not year:
            # У подкастов и аудиокниг у альбома year/release_date обычно None,
            # но у каждого трека-эпизода заполнен pub_date ('2026-07-27').
            pub_date = getattr(track, 'pub_date', None)
            if pub_date:
                year = str(pub_date)[:4]

        genre = getattr(album, 'genre', None) if album else None
        if isinstance(genre, list):
            genre = genre[0] if genre else None

        return {
            'title': track.title or 'Unknown Title',
            'artists': artists,
            'album_artists': album_artists,
            'album_title': album.title if album else 'Unknown Album',
            'track_number': track_number,
            'real_track_number': position_index,
            'disc_number': disc_number,
            'year': year,
            'genre': genre,
            'track_id': track.id,
            'album_id': album.id if album else None,
            'url': f"https://music.yandex.ru/track/{track.id}",
        }

    def _write_mp3_tags(self, file_path: Path, track: Track,
                        cover_data: Optional[CoverData], lyrics: Optional[str],
                        track_number: Optional[int] = None,
                        compatibility_level: int = 0):
        """Записывает ID3 теги в MP3 файл (v2.4; v2.3 при compatibility_level >= 1)."""
        info = self._extract_track_info(track, explicit_track_number=track_number)

        try:
            audio = MP3(file_path)
            if audio.tags is None:
                audio.tags = ID3()
        except mutagen.MutagenError:
            audio = MP3(file_path)
            audio.tags = ID3()

        audio.tags['TIT2'] = TIT2(encoding=3, text=info['title'])
        audio.tags['TALB'] = TALB(encoding=3, text=info['album_title'])
        audio.tags['TPE1'] = TPE1(encoding=3, text=info['artists'])

        if info['album_artists']:
            audio.tags['TPE2'] = TPE2(encoding=3, text=info['album_artists'])

        if info['year']:
            audio.tags['TDRC'] = TDRC(encoding=3, text=[ID3TimeStamp(info['year'])])

        if info['track_number']:
            audio.tags['TRCK'] = TRCK(encoding=3, text=str(info['track_number']))

        if info['disc_number']:
            audio.tags['TPOS'] = TPOS(encoding=3, text=str(info['disc_number']))

        if info['genre']:
            audio.tags['TCON'] = TCON(encoding=3, text=info['genre'])

        # Множественные фреймы (APIC/USLT) хранятся под хеш-ключами, и простая
        # запись tags['USLT'] = ... при повторной записи тегов ДОПИСЫВАЕТ второй
        # фрейм, а не заменяет первый. Сначала удаляем старые полностью.
        if lyrics:
            audio.tags.delall('USLT')
            audio.tags['USLT'] = USLT(encoding=3, text=lyrics)

        if cover_data:
            audio.tags.delall('APIC')
            audio.tags['APIC'] = APIC(
                encoding=3,
                mime=cover_data.mime_type,
                type=3,  # Cover front
                data=cover_data.data,
            )

        # Ссылка на страницу трека. В mutagen WOAF — потомок UrlFrame:
        # конструктор принимает url=, а не text= (с text= писался пустой фрейм).
        if info['url']:
            audio.tags['WOAF'] = WOAF(url=info['url'])

        # --compatibility-level >= 1: ID3v2.3 (старые плееры не понимают 2.4).
        audio.save(v2_version=3 if compatibility_level >= 1 else 4)
        logger.debug(f"Теги записаны в MP3: {file_path}")

    def _write_flac_tags(self, file_path: Path, track: Track,
                         cover_data: Optional[CoverData], lyrics: Optional[str],
                         track_number: Optional[int] = None):
        """Записывает Vorbis-комментарии в FLAC файл."""
        info = self._extract_track_info(track, explicit_track_number=track_number)

        try:
            audio = FLAC(file_path)
        except Exception as e:
            # Если файл не FLAC (например ремукс не удался), пропускаем
            logger.warning(f"Не удалось открыть файл как FLAC: {e}")
            return

        audio['title'] = info['title']
        audio['album'] = info['album_title']
        audio['artist'] = info['artists']
        if info['album_artists']:
            audio['albumartist'] = info['album_artists']

        if info['year']:
            audio['date'] = info['year']

        if info['track_number']:
            audio['tracknumber'] = str(info['track_number'])

        if info['disc_number']:
            audio['discnumber'] = str(info['disc_number'])

        if info['genre']:
            audio['genre'] = info['genre']

        if lyrics:
            audio['lyrics'] = lyrics

        if cover_data:
            # Иначе при повторной записи тегов к файлу добавлялась вторая
            # (и третья) копия обложки —Picture не заменяется, а дописывается.
            audio.clear_pictures()
            picture = Picture()
            picture.type = PictureType.COVER_FRONT
            picture.mime = cover_data.mime_type
            picture.data = cover_data.data
            audio.add_picture(picture)

        audio['comment'] = info['url']

        audio.save()
        logger.debug(f"Теги записаны в FLAC: {file_path}")

    def _write_mp4_tags(self, file_path: Path, track: Track,
                        cover_data: Optional[CoverData], lyrics: Optional[str],
                        track_number: Optional[int] = None):
        """Записывает MP4-атомы в M4A/MP4 файл."""
        info = self._extract_track_info(track, explicit_track_number=track_number)

        audio = MP4(file_path)

        audio['\xa9nam'] = info['title']
        audio['\xa9alb'] = info['album_title']
        audio['\xa9ART'] = info['artists']
        if info['album_artists']:
            audio['aART'] = info['album_artists']

        if info['year']:
            audio['\xa9day'] = info['year']

        if info['track_number']:
            audio['trkn'] = [(int(info['track_number']), 0)]

        if info['disc_number']:
            audio['disk'] = [(int(info['disc_number']), 0)]

        if info['genre']:
            audio['\xa9gen'] = info['genre']

        if lyrics:
            audio['\xa9lyr'] = lyrics

        if cover_data:
            if cover_data.mime_type == 'image/png':
                cover_format = MP4Cover.FORMAT_PNG
            else:
                cover_format = MP4Cover.FORMAT_JPEG

            audio['covr'] = [MP4Cover(cover_data.data, imageformat=cover_format)]

        audio['\xa9cmt'] = info['url']

        audio.save()
        logger.debug(f"Теги записаны в MP4: {file_path}")
