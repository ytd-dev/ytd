"""
Тонкий слой над yandex-music-api.
"""

from ymd.api.client import init_client, get_token_via_device_flow, create_client_from_config
from ymd.api.resolver import LinkResolver

__all__ = [
    "init_client",
    "get_token_via_device_flow",
    "create_client_from_config",
    "LinkResolver",
]