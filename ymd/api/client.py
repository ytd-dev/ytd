"""
Инициализация клиента Яндекс.Музыки: таймауты, повторы запросов,
отключение проверки сертификатов и Device Flow для получения токена.
"""

import logging
import stat
import time
import warnings
from pathlib import Path
from typing import Any, Optional

import yandex_music.exceptions
from yandex_music import Client
from yandex_music.utils.request import Request

from ymd.config import AppConfig
from ymd.exceptions import AuthenticationError

logger = logging.getLogger(__name__)

_INSECURE_WARNING_SHOWN = False


class InsecureRequest(Request):
    """Request c verify=False — для --no-check-certificate (самоподписанные цепочки)."""

    def _request_wrapper(self, *args: Any, **kwargs: Any) -> bytes:
        kwargs['verify'] = False
        return super()._request_wrapper(*args, **kwargs)


def _suppress_insecure_warnings() -> None:
    """Отключает предупреждения urllib3 о непроверяемых сертификатах (один печатный вывод)."""
    global _INSECURE_WARNING_SHOWN
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:  # pragma: no cover - urllib3 всегда есть у requests
        pass
    warnings.filterwarnings('ignore', message='Unverified HTTPS request')
    if not _INSECURE_WARNING_SHOWN:
        print('⚠️  Проверка SSL-сертификатов отключена (--no-check-certificate)')
        _INSECURE_WARNING_SHOWN = True


def _install_retry_wrapper(request_obj: Request, tries: int, retry_delay: float) -> None:
    """
    Оборачивает request_obj._request_wrapper в повторы при сетевых ошибках.

    Args:
        request_obj: объект Request клиента
        tries: число попыток (0 = бесконечно)
        retry_delay: пауза между попытками, секунд
    """
    original = request_obj._request_wrapper

    def wrapper(*args: Any, **kwargs: Any) -> bytes:
        attempt = 0
        while True:
            attempt += 1
            try:
                return original(*args, **kwargs)
            except (yandex_music.exceptions.NotFoundError,
                    yandex_music.exceptions.BadRequestError,
                    yandex_music.exceptions.UnauthorizedError):
                # 404/400/401 — не сетевые сбои: повторять такие запросы
                # бессмысленно (NotFoundError/BadRequestError наследуются от
                # NetworkError и иначе попали бы в ретраи ниже). Пробрасываем.
                raise
            except (yandex_music.exceptions.NetworkError,
                    yandex_music.exceptions.TimedOutError) as e:
                if tries and attempt >= tries:
                    logger.error(f"Запрос не удался после {attempt} попыток: {e}")
                    raise
                logger.warning(f"Сетевая ошибка (попытка {attempt}): {e}. "
                               f"Повтор через {retry_delay} с...")
                time.sleep(retry_delay)

    request_obj._request_wrapper = wrapper


def init_client(
    token: Optional[str] = None,
    timeout: int = 20,
    proxy_url: Optional[str] = None,
    tries: int = 20,
    retry_delay: float = 5,
    no_check_certificate: bool = False,
) -> Client:
    """
    Инициализирует клиент Яндекс.Музыки.

    Args:
        token: OAuth токен (если не указан, будет получен через Device Flow)
        timeout: таймаут запросов, секунд
        proxy_url: URL прокси (опционально)
        tries: число попыток запроса при сетевых ошибках (0 = бесконечно)
        retry_delay: пауза между попытками, секунд
        no_check_certificate: отключить проверку SSL-сертификатов

    Returns:
        Инициализированный Client

    Raises:
        AuthenticationError: если не удалось авторизоваться
    """
    try:
        if not token:
            logger.info("Токен не указан, пробуем получить через Device Flow...")
            token = get_token_via_device_flow()

        if no_check_certificate:
            _suppress_insecure_warnings()
            request: Request = InsecureRequest(timeout=timeout, proxy_url=proxy_url)
        else:
            request = Request(timeout=timeout, proxy_url=proxy_url)

        _install_retry_wrapper(request, tries=tries, retry_delay=retry_delay)

        client = Client(token, request=request)
        client.init()

        logger.info("Клиент Яндекс.Музыки инициализирован")
        return client

    except AuthenticationError:
        raise
    except yandex_music.exceptions.UnauthorizedError as e:
        raise AuthenticationError(
            f"Не удалось авторизоваться: токен недействителен или истёк ({e})"
        ) from e
    except Exception as e:
        raise AuthenticationError(f"Ошибка при инициализации клиента: {e}") from e


def _save_token_to_user_env(token_str: str) -> None:
    """
    Сохраняет токен в ~/.ymd/.env (строка YANDEX_MUSIC_TOKEN=...).

    Существующие остальные строки файла сохраняются; если строка токена уже
    есть, она заменяется. На POSIX-системах выставляются права 0600
    (на Windows chmod ограничен флагом «только чтение» — ошибка игнорируется).
    Ошибка записи не прерывает работу: токен всё равно будет возвращён.
    """
    try:
        config_dir = Path.home() / ".ymd"
        config_dir.mkdir(parents=True, exist_ok=True)
        env_file = config_dir / ".env"

        lines = []
        token_written = False
        if env_file.is_file():
            # utf-8-sig: файл, созданный Блокнотом или PowerShell, может быть
            # с BOM — без его снятия существующая строка токена не найдётся
            for line in env_file.read_text(encoding="utf-8-sig").splitlines():
                if line.strip().startswith("YANDEX_MUSIC_TOKEN="):
                    lines.append(f"YANDEX_MUSIC_TOKEN={token_str}")
                    token_written = True
                else:
                    lines.append(line)
        if not token_written:
            lines.append(f"YANDEX_MUSIC_TOKEN={token_str}")

        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # На POSIX-системах выставляем права rw------- (0600)
        try:
            env_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except (AttributeError, OSError):
            pass

        print(f"🔒 Токен сохранён в {env_file} "
              f"(последующие запуски не потребуют авторизации)")
    except OSError as e:
        logger.warning(f"Не удалось сохранить токен в ~/.ymd/.env: {e}")


def get_token_via_device_flow() -> str:
    """
    Получает OAuth токен через Device Flow (Яндекс просит подтвердить вход
    в браузере по коду).

    Returns:
        Access token (автоматически сохраняется в ~/.ymd/.env)

    Raises:
        AuthenticationError: если не удалось получить токен
    """
    import webbrowser

    from yandex_music import Client as _Client

    def on_code(code):
        url = code.verification_url
        user_code = code.user_code
        print("\n" + "=" * 60)
        print(f"1. Откройте в браузере: {url} (или https://oauth.yandex.ru/device)")
        print(f"2. Введите код: {user_code}")
        print("3. Подтвердите вход на странице Яндекса")
        print("=" * 60 + "\n")

        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        client = _Client()
        token = client.device_auth(on_code=on_code)

        print(f"\n✅ Токен получен: {token.access_token[:10]}...")
        print(f"   Истекает через: {token.expires_in} секунд")

        _save_token_to_user_env(token.access_token)
        return token.access_token

    except Exception as e:
        raise AuthenticationError(f"Не удалось получить токен через Device Flow: {e}") from e


def create_client_from_config(config: AppConfig) -> Client:
    """
    Создает клиент из конфигурации AppConfig.

    Args:
        config: конфигурация приложения

    Returns:
        Инициализированный Client
    """
    return init_client(
        token=config.token,
        timeout=config.timeout,
        proxy_url=config.proxy_url,
        tries=config.tries,
        retry_delay=config.retry_delay,
        no_check_certificate=config.no_check_certificate,
    )
