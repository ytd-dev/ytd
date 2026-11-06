"""
Модель качества и логика решения: как сохранить файл для запрошенного
качества и того, что реально вернул Яндекс API.

Значения качества (числовая модель, как в PR8/YMD):
    0          → AAC 64 кбит/с (.m4a); с --force-mp3 → MP3 64 кбит/с CBR
    1          → AAC 192 кбит/с (.m4a); с --force-mp3 → MP3 192 кбит/с CBR
    2          → «максимально доступное»: FLAC → .flac (ремукс);
                 AAC/MP3 → .mp3 с битрейтом API (обычно 256)
    192/256/320 → всегда .mp3:
                  FLAC-источник  → транскодирование FLAC → MP3 запрошенного битрейта;
                  AAC/MP3        → MP3 битрейта min(запрошенный, источник),
                                   апскейла над источником нет

Наличие lossless (FLAC) API опрашивает в режимах 2, 192, 256, 320 — запрос
/get-file-info идёт с quality=lossless и flac-кодеками. --compatibility-level 1
FLAC не запрашивает вовсе. Реальный поток выбирает селектор
core.downloader.TrackDownloader.select_download_info().

--force-mp3 действует только для quality 0/1: вместо .m4a сохраняется MP3
(64 или 192 кбит/с соответственно).

mode_description() даёт человекочитаемое описание режима для шапки консоли
(cli/main.py печатает её до начала закачки) — по ней видно, какое качество
программа применяет на самом деле.
"""

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

import yandex_music.exceptions

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

# Кодеки lossless-варианта Яндекса (FLAC в сыром и MP4 контейнере)
LOSSLESS_CODECS = ('flac', 'flac-mp4')


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

    def api_quality(self, compatibility_level: int = 0) -> str:
        """
        Параметр quality для подписанного /get-file-info: 'lq', 'nq', 'hq' или 'lossless'.

        Для 192/256/320 (как и для 2) запрашивается 'lossless' — иначе API не
        сообщит о наличии FLAC-потока и выбрать его для чистой MP3-ссылки
        нельзя. --compatibility-level 1 lossless не запрашивает никогда.
        """
        if self.is_aac:
            return 'nq' if self.value == 1 else 'lq'
        if compatibility_level >= 1:
            return 'hq'
        return 'lossless'

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
        # API не сообщил битрейт (например для lossless — битрейт 0/неизвестен):
        # берём запрошенный или стандартный.
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
    Решение о том, как сохранить скачанный файл (структура решения селектора
    select_download_info): что качаем, чем и с каким битрейтом сохраняем.

    Attributes:
        should_convert: нужен ли ffmpeg (транскодирование или ремукс) —
            «флаг необходимости транскодирования»
        output_extension: итоговое расширение ('.flac', '.mp3', '.m4a')
        target_codec: целевой кодек на выходе ('flac', 'mp3', 'aac')
        target_bitrate: целевой битрейт для MP3-кодирования (или None)
        remux_only: достаточно переноса потока без перекодирования (-c:a copy)
        source_container/source_codec/source_bitrate: что вернул API
        reason: человекочитаемое объяснение решения
    """
    should_convert: bool = False
    output_extension: str = '.m4a'
    target_codec: str = ''
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

    @property
    def is_flac_source(self) -> bool:
        """True, если API отдал lossless (FLAC в сыром или MP4 контейнере)."""
        return self.source_codec == 'FLAC'

    def source_description(self) -> str:
        """Строка 'lossless FLAC (источник)' для lossless и 'контейнер=MP4 кодек=AAC заявленный битрейт=256' для lossy."""
        if self.is_flac_source:
            return 'lossless FLAC (источник)'
        bitrate = self.source_bitrate if self.source_bitrate is not None else 'не указан'
        return (f"контейнер={self.source_container or '?'} "
                f"кодек={self.source_codec or '?'} заявленный битрейт={bitrate}")

    def save_action(self) -> str:
        """
        Формулировка второй сервисной строки консоли.

        FLAC → MP3 — это осознанное транскодирование lossless-источника,
        о нём сообщаем отдельно («Будет сконвертирован»); во всех остальных
        случаях — обычное сохранение с расширением по плану.
        """
        if self.is_flac_source and self.output_extension == '.mp3':
            return 'Будет сконвертирован'
        return 'Будет сохранен с расширением'

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

    def ffmpeg_action(self) -> str:
        """Строка операции ffmpeg для лога и сообщения об ошибке (без экранирования)."""
        if self.remux_only:
            return f'{self.source_codec} в {self.output_extension} без перекодирования'
        if self.is_flac_source and self.output_extension == '.mp3':
            return f'FLAC -> MP3 {self.target_bitrate}kbps CBR'
        source = self.source_codec or '?'
        codec = self.target_codec or self.output_extension.lstrip('.')
        bitrate = f' {self.target_bitrate}kbps' if self.target_bitrate else ''
        return f'{source} -> {codec.upper()}{bitrate}'


def decide_quality(quality: DownloadQuality, codec: str, bitrate: Optional[int],
                   force_mp3: bool = False, compatibility_level: int = 0) -> ConversionPlan:
    """
    Определяет, как сохранить файл с учётом запрошенного качества и ответа API.

    Правила (матрица v0.0.2):
      * quality 0/1 → .m4a (AAC 64/192). С --force-mp3 → MP3 64/192 (ffmpeg).
      * quality 192/256/320 → всегда .mp3:
          FLAC-источник  → MP3 запрошенного битрейта (CBR, через ffmpeg);
          AAC/MP3        → MP3 битрейта min(запрошенный, источник);
                           MP3-источник с целевым битрейтом сохраняется как есть.
      * quality 2 («максимум»):
          FLAC-источник  → .flac (ремукс, если контейнер mp4);
          AAC-источник   → .mp3 с битрейтом API (обязательная конвертация);
          MP3-источник   → .mp3 как есть (битрейт API).

    Args:
        quality: запрошенное качество
        codec: кодек из FileDownloadInfo ('flac', 'mp3', 'aac', ...)
        bitrate: битрейт из FileDownloadInfo (None, если API не сообщил)
        force_mp3: флаг --force-mp3 (только для quality 0/1)
        compatibility_level: --compatibility-level; при >= 1 FLAC игнорируется
            даже если API вдруг его вернул (запросом мы его не просим)

    Returns:
        ConversionPlan с решением и объяснением.
    """
    codec = (codec or '').lower()

    # Двойная защита --compatibility-level 1: запрос без flac-кодеков обычно и
    # так не отдаёт lossless, но если сервер всё же вернул FLAC — считаем его
    # lossy-потоком MP4/AAC, чтобы файл не стал FLAC'ом.
    if compatibility_level >= 1 and codec in LOSSLESS_CODECS:
        logger.warning('--compatibility-level 1: API вернул FLAC, игнорируем lossless')
        codec = 'aac'

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
            plan.target_codec = 'aac'
            if force_mp3:
                plan.output_extension = '.mp3'
                plan.target_codec = 'mp3'
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
            plan.target_codec = 'mp3'
            if codec == 'mp3' and bitrate == target:
                plan.reason = f'Уже MP3 {target}kbps, перекодирование не требуется'
            else:
                plan.should_convert = True
                plan.target_bitrate = target
                plan.reason = f'--force-mp3: {codec_label} → MP3 {target}kbps'
            return plan

        plan.output_extension = native_ext
        plan.target_codec = codec_label.lower()
        bitrate_text = f' {bitrate}kbps' if bitrate else ''
        plan.reason = (f'AAC недоступен, API отдал {codec_label}{bitrate_text} — '
                       f'сохраняем как есть')
        return plan

    if quality.is_mp3_bitrate:
        # 192/256/320: расширение строго .mp3 при любом ответе API
        plan.output_extension = '.mp3'
        plan.target_codec = 'mp3'

        if codec_label == 'FLAC':
            # v0.0.2: у трека есть lossless — качаем FLAC и перегоняем в
            # запрошенный битрейт CBR. Битрейт lossless-потока API не сообщает,
            # поэтому он не «потолком», а целью является запрошенное значение.
            target = quality.value if quality.value in MP3_BITRATES else 320
            plan.target_bitrate = target
            plan.should_convert = True
            plan.reason = f'FLAC -> MP3 {target}kbps CBR'
            return plan

        # Lossy-источник (AAC/MP3): целевой битрейт не выше источника
        target = _get_target_bitrate(quality.value, bitrate)
        plan.target_bitrate = target
        if codec == 'mp3' and bitrate == target:
            # Нужный MP3 уже в источнике — сохраняем напрямую, без ffmpeg
            plan.remux_only = False
            plan.should_convert = False
            plan.reason = f'MP3 {target}kbps из источника — сохраняем как есть'
        elif codec == 'mp3':
            # битрейт источника другой — транскодируем точно в target
            plan.should_convert = True
            plan.reason = (f'MP3 {bitrate}kbps -> MP3 {target}kbps '
                           f'(битрейт понижен до доступного)')
        else:
            plan.should_convert = True
            plan.reason = f'{codec_label} -> MP3 {target}kbps'
        return plan

    # quality == 2: максимально доступное
    if codec_label == 'FLAC':
        plan.output_extension = '.flac'
        plan.target_codec = 'flac'
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
        plan.target_codec = 'mp3'
        plan.target_bitrate = target
        plan.reason = f'MP3 из источника с битрейтом API ({target}kbps)'
        # Поток MP3 можно сохранить без перекодирования
        plan.should_convert = False
        return plan

    # AAC/HE-AAC на quality 2 → обязательный MP3 с битрейтом API (обычно 256),
    # а не сырой .m4a (матрица v0.0.2)
    target = _get_target_bitrate(None, bitrate)
    plan.output_extension = '.mp3'
    plan.target_codec = 'mp3'
    plan.target_bitrate = target
    plan.should_convert = True
    plan.reason = f'AAC -> MP3 {target}kbps (битрейт по данным API)'
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


def mode_description(quality, force_mp3: bool = False, compatibility_level: int = 0) -> str:
    """
    Человекочитаемое описание режима качества для шапки консоли.

    Строка печатается до начала закачки и показывает **фактическое** качество,
    с которым программа будет работать: если в консоли видно не то, что
    набрано в командной строке, значит флаг `-q` до программы не дошёл
    (чаще всего из-за `&` в ссылке без кавычек — cmd/bash режут команду строку).
    --recheck этот режим не описывает: там качество не используется.

    Args:
        quality: одно из 0, 1, 2, 192, 256, 320 (или DownloadQuality)
        force_mp3: флаг --force-mp3 (действует только для 0/1)
        compatibility_level: --compatibility-level; при >= 1 FLAC не запрашивается

    Returns:
        Строка вида '-q 320 → MP3 320kbps CBR (FLAC проверяется; …)'.
    """
    q = quality if isinstance(quality, DownloadQuality) else DownloadQuality(quality)
    compat = '--compatibility-level 1: FLAC не запрашивается'

    if q.is_aac:
        if force_mp3:
            return f'-q {q.value} → MP3 {q.aac_bitrate}kbps CBR (--force-mp3)'
        return f'-q {q.value} → AAC {q.aac_bitrate}kbps, .m4a без конвертации'

    if q.is_mp3_bitrate:
        if compatibility_level >= 1:
            return (f'-q {q.value} → MP3 {q.value}kbps CBR, но не выше источника '
                    f'({compat})')
        return (f'-q {q.value} → MP3 {q.value}kbps CBR (FLAC проверяется; '
                f'без FLAC — не выше источника)')

    if compatibility_level >= 1:
        return (f'-q {q.value} → максимум из MP3/AAC источника в .mp3 '
                f'(битрейт не выше источника; {compat})')
    return (f'-q {q.value} → FLAC → .flac (ремукс); без FLAC → .mp3 '
            f'с битрейтом источника')


def request_codecs(quality: DownloadQuality, compatibility_level: int = 0) -> List[str]:
    """
    Список кодеков для подписанного запроса /get-file-info (по предпочтению).

    v0.0.2: режимы 192/256/320 запрашивают FLAC наравне с 2 — если lossless
    есть, он становится источником для чистой MP3 запрошенного битрейта.
    --compatibility-level 1 flac-кодеки не запрашивает никогда.
    """
    if quality.is_aac:
        return ['aac', 'he-aac', 'aac-mp4', 'he-aac-mp4']
    codecs = ['flac', 'flac-mp4', 'mp3', 'aac-mp4', 'he-aac-mp4', 'aac', 'he-aac']
    if compatibility_level >= 1:
        # максимально совместимый контейнер: FLAC не запрашиваем
        codecs = [c for c in codecs if c not in LOSSLESS_CODECS]
    return codecs


def select_download_info(client: Any, track: Any, quality: DownloadQuality,
                         compatibility_level: int = 0,
                         force_mp3: bool = False) -> Optional[Tuple[Any, ConversionPlan]]:
    """
    Селектор потоков (v0.0.2): подписанный /get-file-info + решение о сохранении.

    Для -q 192/256/320 проверяет наличие lossless-потока (FLAC) аналогично
    режиму 2: если FLAC доступен — он скачивается и перекодируется в MP3
    запрошенного битрейта; если нет — действует правило min(запрошенный,
    источник). --compatibility-level 1 игнорирует FLAC в любом режиме.

    Args:
        client: инициализированный Client Яндекс.Музыки
        track: трек (объект Track)
        quality: запрошенное качество
        compatibility_level: --compatibility-level
        force_mp3: --force-mp3 (только для quality 0/1)

    Returns:
        (download_info, ConversionPlan) — выбранный поток для загрузки и
        структура решения: целевой кодек (plan.target_codec), целевой битрейт
        (plan.target_bitrate) и флаг транскодирования (plan.should_convert);
        None, если у трека нет файла.
    """
    try:
        file_info = client.tracks_file_info(
            track.id,
            quality=quality.api_quality(compatibility_level),
            codecs=request_codecs(quality, compatibility_level),
            transport='raw',
        )
    except yandex_music.exceptions.YandexMusicError as e:
        logger.error(f"tracks_file_info({track.id}) не удался: {e}")
        return None

    if not file_info or not file_info.download_info:
        return None

    download_info = file_info.download_info
    # Яндекс отдаёт bitrate уже в кбит/с (64/192/256/320; 0 — lossless).
    bitrate_kbps = int(download_info.bitrate) if download_info.bitrate else None
    plan = decide_quality(quality, download_info.codec, bitrate_kbps,
                          force_mp3=force_mp3,
                          compatibility_level=compatibility_level)
    return download_info, plan
