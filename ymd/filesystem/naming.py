"""
Формирование имен файлов и структуры каталогов по шаблону --path-pattern.

Плейсхолдеры синхронизированы с cli/parser.py и config.py:
{id}, {title}, {artists}, {artist}, {album}, {album_artist}, {track_number},
{track_number_padded}, {real_track_number}, {real_track_number_padded},
{disc_number}, {year}, {genre}, {duration}, {bitrate},
{extension}, {extension_no_dot}, {playlist}
"""

import re
from pathlib import Path
from typing import Any, Dict, Optional

from ymd.exceptions import PathError
from ymd.filesystem.paths import sanitize_filename, sanitize_path

# Плейсхолдеры для шаблонов (должны совпадать с config.PATH_PLACEHOLDERS)
PLACEHOLDERS = {
    '{id}': 'ID трека',
    '{title}': 'Название трека',
    '{artists}': 'Исполнители (через запятую)',
    '{artist}': 'Первый исполнитель',
    '{album}': 'Название альбома',
    '{album_artist}': 'Исполнитель альбома',
    '{track_number}': 'Номер трека (в многотомных релизах сквозной, 1..N)',
    '{track_number_padded}': 'Номер трека (две цифры)',
    '{real_track_number}': 'Номер трека из API (внутри тома)',
    '{real_track_number_padded}': 'Номер трека из API (две цифры)',
    '{disc_number}': 'Номер диска',
    '{year}': 'Год выпуска',
    '{genre}': 'Жанр',
    '{duration}': 'Длительность (сек)',
    '{bitrate}': 'Битрейт',
    '{extension}': 'Расширение файла (например .mp3)',
    '{extension_no_dot}': 'Расширение файла без точки (например mp3)',
    '{playlist}': 'Название плейлиста (для источников-плейлистов)',
}

# Что вырезать из имени при обычном («безопасном») режиме
SAFE_PATH_CLEAR_RE = r"([^\w\-\'() ]|^\s+|\s+$)"
# Что вырезаем из пути при --unsafe-path (разрешаем подкаталоги, но не «выход» из них)
UNSAFE_PATH_CLEAR_RE = r"[/\\]+"


class FileNamer:
    """
    Формирует имена файлов и структуру каталогов по шаблону.

    Args:
        unsafe_path: при True разрешает слеши внутри значений плейсхолдеров
            (подкаталоги), но всё равно запрещает '..' и управляющие символы.
    """

    def __init__(self, unsafe_path: bool = False):
        self.placeholders = PLACEHOLDERS
        self.unsafe_path = unsafe_path

    def format_filename(
        self,
        track_info: Dict[str, Any],
        pattern: str,
        extension: str = '.mp3',
    ) -> str:
        """
        Форматирует путь к файлу по шаблону.

        Args:
            track_info: информация о треке (см. build_track_info)
            pattern: шаблон пути, напр. '{artists} - {album}/{track_number_padded} - {title}'
            extension: расширение итогового файла, напр. '.mp3'

        Returns:
            Относительный путь к файлу с расширением.

        Raises:
            PathError: если путь сформировать не удалось.
        """
        try:
            # extension == '' означает «нужно имя без расширения» (для --skip-existing)
            ext_with_dot = ''
            if extension:
                ext_with_dot = extension if extension.startswith('.') else f'.{extension}'
            info = dict(track_info)
            info['extension'] = ext_with_dot
            info['extension_no_dot'] = ext_with_dot.lstrip('.')

            path_str = self._apply_placeholders(pattern, info)

            # Разбиваем на компоненты и очищаем каждый
            parts = [
                self._clean_component(part)
                for part in path_str.split('/')
                if part.strip()
            ]
            if not parts:
                raise PathError('Пустой путь после обработки шаблона')

            # Расширение ДОБАВЛЯЕМ к последнему компоненту, а не через with_suffix
            # (with_suffix ломает названия с точками: 'Mr. Brightside' → 'Mr.mp3')
            last = parts[-1]
            if not last.lower().endswith(ext_with_dot.lower()):
                last = last + ext_with_dot
            parts[-1] = last

            return str(Path(*parts))

        except PathError:
            raise
        except Exception as e:
            raise PathError(f"Ошибка при формировании пути: {e}") from e

    def _clean_component(self, part: str) -> str:
        """
        Очищает один компонент пути с учётом флага unsafe_path.

        В безопасном режиме действуют ограничения Windows (sanitize_filename).
        В небезопасном режиме сохраняются точки и прочие символы, но убираются
        управляющие символы и обратные слеши (прямые уже разбиты split('/')).
        """
        part = part.strip()
        # '..' недопустим в обоих режимах (защита от «выхода» за output_dir)
        part = part.replace('..', '')
        if self.unsafe_path:
            part = re.sub(r'[\x00-\x1f\x7f]', '', part)
            # Оставляем «спецсимволы» (точки, & , # и т.п.), но физически невозможные
            # для Windows символы всё равно заменяем, иначе запись файла упадёт
            part = re.sub(r'[<>:"|?*]', '_', part)
            part = re.sub(UNSAFE_PATH_CLEAR_RE, '_', part)
            # Windows не допускает имён, оканчивающихся точкой (например '{extension}' пустой)
            return part.rstrip('.') or 'unnamed'
        cleaned = sanitize_filename(part)
        return cleaned or 'unnamed'

    def _clean_value(self, value: Any) -> str:
        """
        Очищает ЗНАЧЕНИЕ плейсхолдера до подстановки в шаблон.

        Подстановка выполняется ДО разбиения результата на компоненты, поэтому
        '/' или '\\\\' внутри названия («Ирано-иракская война / Уроки истории»,
        «A / B», имена артистов с дробью) иначе создали бы лишний подкаталог и
        исказили имя файла. Ограничения Windows для самих компонентов
        применяются позже (см. _clean_component), здесь убираем только
        управляющие символы и слеши.
        """
        text = str(value)
        text = re.sub(r'[\x00-\x1f\x7f]', '', text)
        text = re.sub(r'[/\\]+', '_', text)
        return text.strip()

    def _apply_placeholders(self, pattern: str, track_info: Dict[str, Any]) -> str:
        """Подставляет значения трека в шаблон."""
        result = pattern

        def _val(key: str, default: str = '') -> str:
            value = track_info.get(key)
            return self._clean_value(value) if value not in (None, '') else default

        result = result.replace('{id}', _val('id', 'unknown'))
        result = result.replace('{title}', _val('title', 'Unknown Title'))
        result = result.replace('{album}', _val('album', 'Unknown Album'))

        artists = track_info.get('artists') or []
        if artists:
            result = result.replace('{artists}', ', '.join(self._clean_value(a) for a in artists))
            result = result.replace('{artist}', self._clean_value(artists[0]))
        else:
            result = result.replace('{artists}', 'Unknown Artist')
            result = result.replace('{artist}', 'Unknown Artist')

        album_artists = track_info.get('album_artists') or []
        result = result.replace('{album_artist}',
                                ', '.join(self._clean_value(a) for a in album_artists)
                                if album_artists else 'Unknown Artist')

        track_num = track_info.get('track_number')
        if track_num is not None:
            result = result.replace('{track_number}', str(track_num))
            result = result.replace('{track_number_padded}', f"{int(track_num):02d}")
        else:
            result = result.replace('{track_number}', '')
            result = result.replace('{track_number_padded}', '')

        # Номер трека «как в API» (trackPosition.index): в многотомных релизах
        # это номер внутри тома, {track_number} тем временем сквозной.
        real_num = track_info.get('real_track_number', track_num)
        if real_num is not None:
            result = result.replace('{real_track_number}', str(real_num))
            result = result.replace('{real_track_number_padded}', f"{int(real_num):02d}")
        else:
            result = result.replace('{real_track_number}', '')
            result = result.replace('{real_track_number_padded}', '')

        disc_num = track_info.get('disc_number')
        if disc_num is not None:
            result = result.replace('{disc_number}', str(disc_num))
        else:
            result = result.replace('{disc_number}', '')

        result = result.replace('{year}', _val('year'))
        result = result.replace('{genre}', _val('genre'))
        result = result.replace('{duration}', _val('duration'))
        result = result.replace('{bitrate}', _val('bitrate'))

        # Название плейлиста: заполняется только для источников-плейлистов
        # (см. ResolvedSource.playlist_title в api/resolver.py). В остальных
        # контекстах пустое значение — пустые сегменты пути убираются (_clean_path).
        result = result.replace('{playlist}', _val('playlist'))

        # Расширение (доступно и как {extension}, и как {extension_no_dot}).
        # Значения берутся «как есть»: пустая строка означает, что расширение не нужно.
        ext = track_info.get('extension')
        ext_no_dot = track_info.get('extension_no_dot')
        result = result.replace('{extension}', str(ext) if ext is not None else '.mp3')
        result = result.replace('{extension_no_dot}',
                                str(ext_no_dot) if ext_no_dot is not None else 'mp3')

        return self._clean_path(result)

    def _clean_path(self, path: str) -> str:
        """Нормализует разделители и убирает пустые сегменты."""
        path = re.sub(r'[\\/]+', '/', path)
        path = path.strip('/')
        path = '/'.join(p for p in path.split('/') if p.strip())
        return path

    def get_available_placeholders(self) -> Dict[str, str]:
        """Возвращает список доступных плейсхолдеров."""
        return self.placeholders

    def get_base_name(self, track_info: Dict[str, Any], pattern: str) -> str:
        """
        Возвращает базовое имя файла (без расширения) — используется
        для --skip-existing (сравнение по stem).
        """
        full = self.format_filename(track_info, pattern, extension='')
        return Path(full).name
