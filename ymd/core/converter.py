"""
Конвертация аудиофайлов через ffmpeg.

Политика:
  * ffmpeg проверяется лениво — только когда реально нужна конвертация
    (ошибка «ffmpeg не найден» не должна мешать скачиванию);
  * remux-only (FLAC в MP4 → FLAC) выполняется без перекодирования (-c:a copy);
  * транскодирование в MP3 идёт с точным целевым битрейтом (-b:a Nk);
  * сбой конвертации не прерывает загрузку: файл остаётся в исходном формате.
"""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from ymd.core.quality import ConversionPlan
from ymd.exceptions import ConversionError
from ymd.filesystem.paths import get_unique_filename

logger = logging.getLogger(__name__)


class AudioConverter:
    """
    Конвертирует загруженные файлы согласно ConversionPlan.

    Args:
        ffmpeg_path: явный путь к ffmpeg (по умолчанию ищется в PATH)
    """

    def __init__(self, ffmpeg_path: Optional[str] = None):
        self._ffmpeg_path = ffmpeg_path
        self._warned_missing = False

    @property
    def ffmpeg_path(self) -> Optional[str]:
        """Путь к ffmpeg (None, если не найден)."""
        return self._ffmpeg_path or shutil.which('ffmpeg')

    def ffmpeg_available(self) -> bool:
        """True, если ffmpeg доступен."""
        return self.ffmpeg_path is not None

    def convert(self, input_path: Path, plan: ConversionPlan,
                overwrite: bool = False) -> Path:
        """
        Приводит файл к плану и возвращает итоговый путь.

        Если ffmpeg нужен, но не найден — печатает понятную ошибку и оставляет
        файл в исходном формате (загрузка продолжается).

        Args:
            input_path: скачанный файл (нативное расширение кодека API)
            plan: решение о сохранении из core.quality.decide_quality
            overwrite: перезаписывать существующий целевой файл (--overwrite).
                Без флага чужие файлы не затираются: берётся свободное имя,
                а ffmpeg вызывается с '-n' (запрет перезаписи).

        Returns:
            Финальный путь к файлу (input_path, если конвертация не нужна
            или не удалась).

        Raises:
            ConversionError: только при критическом сбое записи файла.
        """
        target = input_path.with_name(input_path.stem + plan.output_extension)

        if not plan.should_convert:
            # Просто переименовываем нативный файл в плановое расширение
            if input_path != target:
                if target.exists() and not overwrite:
                    target = get_unique_filename(target)
                shutil.move(str(input_path), str(target))
            return target

        ffmpeg = self.ffmpeg_path
        if not ffmpeg:
            msg = ('⚠️  ffmpeg не найден! Требуется для: '
                   f'{plan.reason or "конвертации"}. '
                   'Установите ffmpeg (https://ffmpeg.org) и повторите.'
                   ' Сохраняем в исходном формате.')
            if not self._warned_missing:
                print(msg)
                self._warned_missing = True
            logger.warning('ffmpeg не найден — файл остаётся в исходном формате')
            return input_path

        # '-y' только когда пользователь явно разрешил перезапись (--overwrite);
        # иначе '-n' — ffmpeg не имеет права затирать существующий target.
        args = [ffmpeg, '-y' if overwrite else '-n', '-i', str(input_path), '-vn']

        if plan.remux_only:
            args += ['-map', '0:a:0', '-c:a', 'copy']
        else:
            args += ['-c:a', 'libmp3lame']
            if plan.target_bitrate:
                args += ['-b:a', f'{plan.target_bitrate}k']

        args += [str(target)]

        logger.debug(f"ffmpeg: {' '.join(args)}")

        # Файл, который существовал ДО запуска ffmpeg, не считается результатом
        # конвертации: при '-n' ffmpeg не пишет в target. Более того, ffmpeg 9.x
        # на существующем файле с '-n' возвращает код 0 («already exists. Exiting»),
        # поэтому успех проверяем и по коду возврата, и по признаку «файл был создан
        # сейчас». Иначе чужой target остался бы, а входной файл был бы удалён зря.
        target_existed_before = target.exists()

        try:
            result = subprocess.run(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            logger.error('ffmpeg: превышен таймаут конвертации')
            if not target_existed_before:
                self._cleanup_partial(target)
            return input_path
        except OSError as e:
            logger.error(f'ffmpeg: не удалось запустить процесс: {e}')
            if not target_existed_before:
                self._cleanup_partial(target)
            return input_path

        conversion_ok = (result.returncode == 0 and target.exists()
                         and target.stat().st_size > 0)
        if conversion_ok and target_existed_before and not overwrite:
            # ffmpeg с '-n' ничего не писал (rc может быть и 0) — это не успех.
            conversion_ok = False

        if not conversion_ok:
            if target_existed_before and not overwrite:
                logger.warning(f'ffmpeg: целевой файл уже существует, не перезаписываем: {target}')
                print(f'  ⚠️ Файл уже существует (без --overwrite не перезаписываем): '
                      f'{target.name}, оставляем исходник {input_path.name}')
            else:
                stderr = (result.stderr or b'').decode('utf-8', 'replace')
                logger.error(f"ffmpeg завершися с ошибкой {result.returncode}: {stderr[-400:]}")
                print(f'  ⚠️  Конвертация не удалась, сохраняем в исходном формате: {input_path.name}')
            if not target_existed_before:
                self._cleanup_partial(target)
            return input_path

        # Успех — удаляем исходник
        try:
            input_path.unlink()
        except OSError:
            pass

        logger.debug(f"Конвертировано: {input_path.name} → {target.name}")
        return target

    def _cleanup_partial(self, path: Path) -> None:
        """Удаляет незавершённый выходной файл."""
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    def ensure_ffmpeg(self) -> None:
        """
        Явная проверка ffmpeg (для --debug/предварительной проверки).

        Raises:
            ConversionError: если ffmpeg не найден.
        """
        if not self.ffmpeg_available():
            raise ConversionError(
                'ffmpeg не найден в PATH. Установите ffmpeg (https://ffmpeg.org) — '
                'он требуется для конвертации в MP3 и ремукса FLAC.'
            )
