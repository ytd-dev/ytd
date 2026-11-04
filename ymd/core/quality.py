"""
Модель качества и логика решения: как сохранить файл для запрошенного
качества и того, что реально вернул Яндекс API.

Значения качества (числовая модель, как в PR8/YMD):
    0          → AAC 64 кбит/с (.m4a)
    1          → AAC 192 кбит/с (.m4a)
    2          → «максимально доступное»: FLAC → .flac; AAC → MP3 (битрейт API);
                 MP3 → MP3 (битрейт API)
    192/256/320 → MP3 с указанным битрейтом, но не выше битрейта источника

--force-mp3 действует только для quality 0/1: вместо .m4a сохраняется MP3
(64 или 192 кбит/с соответственно).
"""

import logging
from dataclasses import dataclass
from typing import Optional

from ymd.config import MP3_BITRATES, VALID_QUALITIES
from ymd.exceptions import QualityError

logger = logging.getLogger(__name__)

# Кодчик Яндекса → (контейнер, кодек) для отображения и решения
FILE_FORMAT_MAPPING = {
    'flac': ('FLAC', 'FLAC'),
    'flac-mp4': ('MP4', 'FLAC'),
    'mp3': ('MP3', 'MP3'),
    'aac': ('MP4', 'AAC'),
    'he-aac': ('MP4', 'AAC'),
    'aac-mp4': ('MP4', 'AAC'),
    'he-aac-mp4': ('MP4', 'AAC'),
}

# Нативное расширение файла, который отдаёт API для конкретного кодека
NATIVE_EXTENSIONS = {
    'flac': '.flac',
    'flac-mp4': '.mp4',
    'mp3': '.mp3',
    'aac': '.m4a',
    'he-aac': '.m4a',
    'aac-mp4': '.m4a',
    'he-aac-mp4': '.m4a',
}


class DownloadQuality:
    """
    Качество загрузки (числовая модель).

    Args:
        value: одно из 0, 1, 2, 192, 256, 320
    """

    def __init__(self, value: int):
        if isinstance(value, bool) or not isinstance(value, int) or value not in VALID_QUALITIES:
            raise QualityError(
                f"Недопустимое значение качества: {value!r}. Допустимо: "
                f"{', '.join(str(q) for q in VALID_QUALITIES)}"
            )
        self.value = value

    @property
    def api_quality(self) -> str:
        """Параметр quality для Яндекс API: 'lq', 'nq', 'hq' или 'lossless'."""
        if self.value in (192, 256, 320):
            return 'hq'
        if self.value == 2:
            return 'lossless'
        if self.value == 1:
            return 'nq'
        return 'lq'

    @property
    def is_mp3_bitrate(self) -> bool:
        """True для 192/256/320 — запрошен MP3 с конкретным битрейтом."""
        return self.value in (192, 256, 320)

    @property
    def is_max(self) -> bool:
        """True для quality == 2 (максимально доступное)."""
        return self.value == 2

    @property
    def is_aac(self) -> bool:
        """True для quality 0/1 — запрошен AAC (64 или 192 кбит/с)."""
        return self.value in (0, 1)

    @property
    def aac_bitrate(self) -> int:
        """Целевой битрейт для AAC-качеств: 64 для 0, 192 для 1."""
        return 64 if self.value == 0 else 192

    def __eq__(self, other) -> bool:
        if isinstance(other, DownloadQuality):
            return self.value == other.value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.value)

    def __repr__(self) -> str:
        return f'DownloadQuality({self.value})'


def _get_target_bitrate(requested: Optional[int], available: Optional[int],
                        default: int = 256) -> int:
    """
    Подбирает целевой битрейт MP3 с учётом битрейта источника.

    Никогда не превышает битрейт источника (нет смысла «расти» качество).

    Args:
        requested: запрошенный битрейт (192/256/320) или None (quality 2)
        available: битрейт, который заявил API (может быть None)
        default: битрейт по умолчанию, если API его не сообщил

    Returns:
        Целевой битрейт из стандартного списка Яндекса.
    """
    if available is None:
        # API не сообщил битрейт (например для AAC) — берём запрошенный или стандартный
        if requested in MP3_BITRATES:
            return requested
        return default

    # Самый большой стандартный битрейт, не превышающий источник
    available_snapped = next((b for b in MP3_BITRATES if b <= available),
                             min(MP3_BITRATES))

    if requested is None or requested == 2:
        # «Максимально доступное» — ровно то, что дал источник
        return available_snapped

    # 192/256/320 — не выше источника
    return min(requested, available_snapped)


@dataclass
class ConversionPlan:
    """
    Решение о том, как сохранить скачанный файл.

    Attributes:
        should_convert: нужен ли ffmpeg (конвертация или ремукс)
        output_extension: итоговое расширение ('.flac', '.mp3', '.m4a')
        target_bitrate: целевой битрейт для MP3-конвертации (или None)
        remux_only: достаточно переноса потока без перекодирования (-c:a copy)
        source_container/source_codec/source_bitrate: что вернул API
        reason: человекочитаемое объяснение решения
    """
    should_convert: bool = False
    output_extension: str = '.m4a'
    target_bitrate: Optional[int] = None
    remux_only: bool = False
    source_container: str = ''
    source_codec: str = ''
    source_bitrate: Optional[int] = None
    reason: str = ''

    @property
    def is_lossless_output(self) -> bool:
        """True, если итоговый файл — FLAC."""
        return self.output_extension == '.flac'

    def source_description(self) -> str:
        """Строка вида 'контейнер=MP4, кодек=AAC, заявленный битрейт=256'."""
        bitrate = self.source_bitrate if self.source_bitrate is not None else 'не указан'
        return (f"контейнер={self.source_container or '?'} "
                f"кодек={self.source_codec or '?'} заявленный битрейт={bitrate}")

    def output_label(self) -> str:
        """
        Метка итогового файла: 'FLAC', 'MP3 320kbps', 'AAC 192kbps' и т.п.

        Кодек берётся из расширения, которое получится на диске, битрейт —
        целевой (если задан) иначе заявленный API.
        """
        codec_by_extension = {'.mp3': 'MP3', '.m4a': 'AAC', '.mp4': 'AAC', '.flac': 'FLAC'}
        codec = codec_by_extension.get(self.output_extension) or (self.source_codec.upper() or '?')
        bitrate = self.target_bitrate if self.target_bitrate is not None else self.source_bitrate
        return f'{codec} {bitrate}kbps' if bitrate else codec


def decide_quality(quality: DownloadQuality, codec: str, bitrate: Optional[int],
                   force_mp3: bool = False) -> ConversionPlan:
    """
    Определяет, как сохранить файл с учётом запрошенного качества и ответа API.

    Правила:
      * quality 0/1 → .m4a (AAC 64/192). С --force-mp3 → MP3 64/192 (ffmpeg).
      * quality 192/256/320 → .mp3 с целевым битрейтом, не выше источника.
      * quality 2 («максимум»):
          FLAC-источник  → .flac (ремукс, если контейнер mp4);
          AAC-источник   → .mp3 с битрейтом API (обычно 256);
          MP3-источник   → .mp3 как есть (битрейт API).

    Args:
        quality: запрошенное качество
        codec: кодек из FileDownloadInfo ('flac', 'mp3', 'aac', ...)
        bitrate: битрейт из FileDownloadInfo (None, если API не сообщил)
        force_mp3: флаг --force-mp3 (только для quality 0/1)

    Returns:
        ConversionPlan с решением и объяснением.
    """
    codec = (codec or '').lower()
    container, codec_label = FILE_FORMAT_MAPPING.get(codec, ('?', codec.upper() or '?'))
    native_ext = NATIVE_EXTENSIONS.get(codec, '.dat')

    plan = ConversionPlan(source_container=container, source_codec=codec_label,
                          source_bitrate=bitrate, output_extension=native_ext)

    if quality.is_aac:
        target = quality.aac_bitrate  # 64 или 192
        if codec_label in ('AAC', '?'):
            # Обычный случай: API отдал AAC в контейнере MP4
            # ('?' — неизвестный кодек, считаем его AAC, как раньше).
            plan.output_extension = '.m4a'
            if force_mp3:
                plan.output_extension = '.mp3'
                plan.should_convert = True
                plan.target_bitrate = target
                plan.reason = f'--force-mp3: конвертируем в MP3 {target}kbps'
            else:
                plan.reason = 'AAC в контейнере MP4 (.m4a) как есть'
            return plan

        # AAC у трека нет — API отдал другой кодек (обычно MP3: у части треков,
        # особенно подкастов/аудиокниг, AAC-вариант отсутствует). Сохраняем то,
        # что реально пришло, с честным расширением, иначе в .m4a лягут MP3-байты
        # и теги не запишутся («not a MP4 file»).
        if force_mp3:
            plan.output_extension = '.mp3'
            if codec == 'mp3' and bitrate == target:
                plan.reason = f'Уже MP3 {target}kbps, перекодирование не требуется'
            else:
                plan.should_convert = True
                plan.target_bitrate = target
                plan.reason = f'--force-mp3: {codec_label} → MP3 {target}kbps'
            return plan

        plan.output_extension = native_ext
        bitrate_text = f' {bitrate}kbps' if bitrate else ''
        plan.reason = (f'AAC недоступен, API отдал {codec_label}{bitrate_text} — '
                       f'сохраняем как есть')
        return plan

    if quality.is_mp3_bitrate:
        # 192/256/320: всегда MP3, битрейт не выше источника
        target = _get_target_bitrate(quality.value, bitrate)
        plan.output_extension = '.mp3'
        plan.target_bitrate = target
        if codec == 'mp3' and bitrate == target:
            plan.remux_only = False
            plan.should_convert = False
            plan.reason = f'MP3 {target}kbps из источника — сохраняем как есть'
        elif codec == 'mp3':
            # битрейт источника другой — транскодируем точно в target
            plan.should_convert = True
            plan.reason = (f'MP3 источника {bitrate}kbps → целевой {target}kbps '
                           f'(битрейт понижен до доступного)')
        else:
            plan.should_convert = True
            plan.reason = f'{codec_label} → MP3 {target}kbps'
        return plan

    # quality == 2: максимально доступное
    if codec_label == 'FLAC':
        plan.output_extension = '.flac'
        if native_ext == '.flac':
            plan.reason = 'FLAC из источника — сохраняем как есть'
        else:
            plan.should_convert = True
            plan.remux_only = True
            plan.reason = 'FLAC в контейнере MP4 → ремукс в .flac'
        return plan

    if codec_label == 'MP3':
        target = _get_target_bitrate(None, bitrate)
        plan.output_extension = '.mp3'
        plan.target_bitrate = target
        plan.reason = f'MP3 из источника с битрейтом API ({target}kbps)'
        # Поток MP3 можно сохранить без перекодирования
        plan.should_convert = False
        return plan

    # AAC/HE-AAC на quality 2 → MP3 с битрейтом API (обычно 256)
    target = _get_target_bitrate(None, bitrate)
    plan.output_extension = '.mp3'
    plan.target_bitrate = target
    plan.should_convert = True
    plan.reason = f'AAC → MP3 {target}kbps (битрейт по данным API)'
    return plan


def describe_available(codec: str, bitrate: Optional[int]) -> str:
    """Возвращает описание ответа API: 'FLAC 320kbps', 'AAC 256kbps', 'FLAC' и т.п."""
    codec = (codec or '').lower()
    _, codec_label = FILE_FORMAT_MAPPING.get(codec, ('?', codec.upper() or '?'))
    if codec_label == 'FLAC':
        return 'FLAC'
    if bitrate:
        return f'{codec_label} {bitrate}kbps'
    return codec_label
