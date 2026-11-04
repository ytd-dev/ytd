"""
Разрешение ссылок на Яндекс.Музыку в API-объекты.

Поддерживаемые форматы:
    https://music.yandex.ru/track/123456
    https://music.yandex.ru/album/123456
    https://music.yandex.ru/artist/123456
    https://music.yandex.ru/album/123456/track/789        (трек из альбома)
    https://music.yandex.ru/album/123456/from/disc/track  (номер трека из URL)
    https://music.yandex.ru/playlists/<uuid>              (плейлист по UUID)
    https://music.yandex.ru/playlists/lk.<uuid>           («Мне нравится» — по своему UUID)
    https://music.yandex.ru/users/<login>/playlists/<kind> (старый формат: числовой kind,
                                                          lk.<uuid> или likes — только свои)
    Чужой плейлист «Мне нравится» доступен по его lk.<uuid>; ссылка вида
    users/<login>/playlists/likes работает только для своего аккаунта (маршрута
    для чужих лайков у API нет).
UTM-параметры (?utm_source=...), прочие query и фрагменты (#...) игнорируются.
HTML-разбор — только крайний fallback, основной путь — официальный API.
"""

import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from yandex_music import Client, Track, Album, Playlist, Artist

from ymd.exceptions import ResolveError


class LikedPlaylist:
    """Лёгкая обёртка плейлиста «Мне нравится» (у API нет единого объекта)."""

    def __init__(self, tracks: list):
        self.tracks = tracks
        self.track_count = len(tracks)
        self.title = 'Мне нравится'
        self.owner = {'login': 'liked'}
        self.kind = 'liked'

    def __len__(self) -> int:
        return self.track_count


class LinkResolver:
    """
    Разрешает ссылки (или одиночные ID) в объекты Яндекс.Музыки:
    Track | Album | Artist | Playlist | LikedPlaylist.
    """

    def __init__(self, client: Client):
        self.client = client

        # Регулярные выражения (по path без query/fragment)
        self.track_re = re.compile(r'/track(?:/from/[^/]+(?:/[^/]+)?)?/(\d+)')
        self.track_plain_re = re.compile(r'/track/(\d+)')
        self.album_re = re.compile(r'/album/(\d+)')
        self.artist_re = re.compile(r'/artist/(\d+)')
        # Старый формат ДО нового: users/<login>/playlists/<kind>
        self.playlist_legacy_re = re.compile(r'/users/([^/]+)/playlists/([^/?#]+)')
        self.playlist_re = re.compile(r'/playlists/([^/?#]+)')

    def resolve(self, url_or_id: str) -> Any:
        """
        Разрешает ссылку или ID в объект Яндекс.Музыки.

        Args:
            url_or_id: URL вида https://music.yandex.ru/album/123 или числовой ID трека

        Returns:
            Track | Album | Artist | Playlist | LikedPlaylist

        Raises:
            ResolveError: если ссылку разрешить не удалось.
        """
        raw = str(url_or_id).strip()
        if not raw:
            raise ResolveError('Пустая ссылка')

        # Одиночный числовой ID
        if raw.isdigit():
            return self.resolve_track_id(int(raw))

        # «music.yandex.ru/album/1» без схемы — добавляем https://
        probe = raw if '://' in raw else f'https://{raw}'
        parsed = urlparse(probe)
        path = parsed.path or ''
        if not path.startswith('/'):
            path = '/' + path

        # URL с query (utm) и fragment (#) — query/fragment уже отрезаны urlparse

        # Трек (в т.ч. /album/<id>/track/<id>) — проверяем раньше альбома
        match = self.track_plain_re.search(path)
        if match:
            return self.resolve_track_id(int(match.group(1)))

        # Альбом
        match = self.album_re.search(path)
        if match:
            return self.resolve_album_id(int(match.group(1)))

        # Исполнитель
        match = self.artist_re.search(path)
        if match:
            return self.resolve_artist_id(int(match.group(1)))

        # Старый формат плейлиста: users/<login>/playlists/<kind>
        # (важно: раньше общего playlists/-паттерна)
        match = self.playlist_legacy_re.search(path)
        if match:
            return self._resolve_owner_playlist(match.group(1), match.group(2))

        # Новый формат: playlists/<uuid>, в т.ч. playlists/lk.<uuid> и ar.<uuid>
        match = self.playlist_re.search(path)
        if match:
            playlist_id = match.group(1)
            # lk.<uuid> — уникальный UUID конкретного плейлиста «Мне нравится»,
            # он резолвится через API как обычный плейлист (client.playlist).
            # Свои лайки возвращает только ключевое слово likes/liked.
            if playlist_id.lower() in ('likes', 'liked'):
                return self._resolve_liked_playlist()
            return self._resolve_playlist_uuid(playlist_id)

        raise ResolveError(f"Не удалось распознать ссылку: {url_or_id}")

    # ------------------------------------------------------------------ IDs

    def resolve_track_id(self, track_id: int) -> Track:
        """
        Разрешает числовой ID трека в полноценный Track.

        Client.tracks() уже возвращает полные Track (у Track нет fetch_track,
        он есть только у TrackShort).
        """
        tracks = self.client.tracks([int(track_id)])
        if not tracks or tracks[0] is None:
            raise ResolveError(f"Трек не найден: {track_id}")
        return tracks[0]

    def resolve_album_id(self, album_id: int) -> Album:
        """Разрешает числовой ID альбома."""
        albums = self.client.albums([int(album_id)])
        if not albums or albums[0] is None:
            raise ResolveError(f"Альбом не найден: {album_id}")
        return albums[0]

    def resolve_artist_id(self, artist_id: int) -> Artist:
        """Разрешает числовой ID исполнителя."""
        artists = self.client.artists([int(artist_id)])
        if not artists or artists[0] is None:
            raise ResolveError(f"Исполнитель не найден: {artist_id}")
        return artists[0]

    def resolve_playlist(self, playlist_id: str) -> Any:
        """
        Разрешает ID плейлиста без URL: 'owner/kind', UUID или 'likes'.

        Args:
            playlist_id: 'likes', UUID плейлиста или 'owner/kind'

        Returns:
            Playlist | LikedPlaylist

        Raises:
            ResolveError: если плейлист не найден.
        """
        pid = str(playlist_id).strip()
        if not pid:
            raise ResolveError('Пустой ID плейлиста')

        low = pid.lower()
        if low in ('likes', 'liked'):
            return self._resolve_liked_playlist()

        if '/' in pid:
            owner, kind = pid.split('/', 1)
            return self._resolve_owner_playlist(owner, kind)

        return self._resolve_playlist_uuid(pid)

    def _resolve_id(self, track_id: str) -> Any:
        """Совместимость со старым именем: разрешает числовой ID трека."""
        if str(track_id).isdigit():
            return self.resolve_track_id(int(track_id))
        raise ResolveError(f"Не удалось разрешить ID: {track_id}")

    # ------------------------------------------------------------- Playlists

    def _resolve_owner_playlist(self, owner: str, kind: str) -> Any:
        """
        Плейлист в старом формате ссылки: users/<login>/playlists/<kind>.

        kind бывает трёх видов:
          * 'likes'/'liked' — «Мне нравится»: своего аккаунта берём из
            users_likes_tracks(), чужие API не отдаёт (см. _resolve_liked_playlist);
          * числовой kind (1000, 1005, ...) — GET /users/<login>/playlists/<kind>;
          * служебный kind генерируемого плейлиста (lk.<uuid>, ar.<uuid>, ...) —
            тем маршрутом не отдаётся (not-found), берём GET /playlist/<kind>.
        """
        low = str(kind).strip()
        if low.lower() in ('likes', 'liked'):
            return self._resolve_liked_playlist(owner)
        if self._is_generated_kind(low):
            return self._resolve_playlist_uuid(low)
        try:
            playlist = self.client.users_playlists(low, owner)
        except Exception as e:
            raise ResolveError(f"Плейлист не найден: {owner}/{low} ({e})") from e
        if playlist:
            return playlist
        raise ResolveError(f"Плейлист не найден: {owner}/{low}")

    @staticmethod
    def _is_generated_kind(kind: str) -> bool:
        """Служебные kind генерируемых плейлистов: lk.<uuid>, ar.<uuid>, uza.<uuid>..."""
        return bool(re.match(r'^[a-z]{2,5}\.', str(kind).lower()))

    def _is_self_owner(self, owner: str) -> bool:
        """'me', uid или логин текущего аккаунта."""
        low = str(owner).strip().lower()
        if low in ('me', ''):
            return True
        try:
            if str(getattr(self.client, 'account_uid', '') or '') == low:
                return True
        except Exception:
            pass
        own = self._own_login()
        return bool(own and low == str(own).lower())

    def _own_login(self) -> Optional[str]:
        """
        Логин текущего аккаунта (кэшируется).

        В yandex-music нет метода «пользователь по uid», поэтому логин берётся
        из владельцев собственных плейлистов (users_playlists_list) — если
        клиент умеет users(), сначала пробуем его.
        """
        cached = getattr(self, '_own_login_cache', None)
        if cached is not None:
            return cached or None
        login = ''
        try:
            uid = getattr(self.client, 'account_uid', None)
            if uid is not None and hasattr(self.client, 'users'):
                users = self.client.users([uid]) or []
                if users:
                    login = str(getattr(users[0], 'login', '') or '')
        except Exception as e:
            logger.debug(f"Логин аккаунта через users() не получен: {e}")
        if not login:
            try:
                for p in (self.client.users_playlists_list() or []):
                    owner = getattr(p, 'owner', None)
                    owner_login = str(getattr(owner, 'login', '') or '')
                    if owner_login:
                        login = owner_login
                        break
            except Exception as e:
                logger.debug(f"Логин аккаунта через плейлисты не получен: {e}")
        self._own_login_cache = login
        return login or None

    def _resolve_playlist_uuid(self, playlist_id: str) -> Any:
        """
        Разрешает плейлист по UUID (включая lk.<uuid> и ar.<uuid>).

        Порядок: официальный API (client.playlist) → список собственных
        плейлистов пользователя → HTML только как крайний fallback.
        """
        # 1. Прямой запрос плейлиста по UUID (работает для публичных плейлистов)
        try:
            playlist = self.client.playlist(playlist_id)
            if playlist:
                return playlist
        except Exception as e:
            # 2. Ищем среди плейлистов текущего пользователя
            last_error = e
            try:
                my_playlists = self.client.users_playlists_list() or []
                for p in my_playlists:
                    kind = str(getattr(p, 'kind', ''))
                    title = str(getattr(p, 'title', ''))
                    # У Playlist нет .uuid — полный UUID хранится в playlist_uuid
                    uuid_attr = str(getattr(p, 'playlist_uuid', '') or '')
                    if (kind == str(playlist_id)
                            or uuid_attr.lower() == str(playlist_id).lower()
                            or playlist_id in title):
                        owner = getattr(getattr(p, 'owner', None), 'login', None) \
                            or getattr(getattr(p, 'owner', None), 'uid', None) \
                            or getattr(p, 'owner_id', '') or 0
                        return self.client.users_playlists(kind, owner)
            except Exception:
                pass
            # 3. HTML — только как последний шанс
            try:
                return self._resolve_playlist_html(playlist_id)
            except Exception:
                raise ResolveError(
                    f"Плейлист не найден: {playlist_id} ({last_error})"
                ) from last_error

        raise ResolveError(f"Плейлист не найден: {playlist_id}")

    def _resolve_liked_playlist(self, owner: Optional[str] = None) -> LikedPlaylist:
        """
        Разрешает плейлист «Мне нравится».

        У API Яндекс.Музыки НЕТ маршрута для лайков чужого пользователя:
        GET /users/<login>/playlists/likes отвечает not-found (проверено и на
        своём, и на чужом аккаунте). Ссылка users/<login>/playlists/likes
        корректна только для своего аккаунта; для чужого сразу даём понятную
        ошибку, а не тихо подменяем чужие лайки своими (дефект PR10).
        Публичные чужие «Мне нравится» скачиваются по их собственному UUID:
        https://music.yandex.ru/playlists/lk.<uuid>.

        users_likes_tracks() возвращает TracksList с краткими треками
        (у них нет title/artists), поэтому полноценные Track догружаются
        пакетами по 50 ID через client.tracks() — быстрее, чем fetch_track().
        """
        if owner is not None and not self._is_self_owner(owner):
            raise ResolveError(
                f"Чужие «Мне нравится» (users/{owner}/playlists/likes) через API "
                f"не получаются: маршрута /users/<login>/playlists/likes у API нет. "
                f"Скачайте плейлист по его собственному UUID: "
                f"https://music.yandex.ru/playlists/lk.<uuid> "
                f"(кнопка «Поделиться» на странице «Мне нравится»)"
            )
        try:
            likes = self.client.users_likes_tracks()
        except Exception as e:
            raise ResolveError(f"Ошибка при получении избранных треков: {e}") from e

        # yandex-music 3.x: TracksList с .tracks; старые версии/мок-клиенты: .list
        short_tracks = list(getattr(likes, 'tracks', None)
                            or getattr(likes, 'list', None)
                            or [])
        if not short_tracks:
            raise ResolveError('Нет избранных треков')

        full_tracks: list = []
        ids = [str(getattr(t, 'track_id', None) or t.id) for t in short_tracks]
        for i in range(0, len(ids), 50):
            try:
                full_tracks.extend(self.client.tracks(ids[i:i + 50]) or [])
            except Exception as e:
                logger.debug(f"Пакет треков не догружен ({e}), оставляем краткие")
                full_tracks.extend(short_tracks[i:i + 50])

        return LikedPlaylist(full_tracks)

    def _resolve_playlist_html(self, playlist_id: str) -> Any:
        """Разрешает плейлист через HTML (крайний fallback, если API не сработал)."""
        import json
        import requests

        url = f"https://music.yandex.ru/playlists/{playlist_id}"

        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()

            html = response.text

            match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                playlist_data = self._find_playlist_in_state(data)
                if playlist_data:
                    owner = playlist_data.get('owner', {})
                    owner_login = owner.get('login') or owner.get('uid')
                    kind = playlist_data.get('kind') or playlist_data.get('id')

                    if owner_login and kind:
                        return self.client.users_playlists(str(kind), str(owner_login))

            raise ResolveError(f"Не удалось загрузить плейлист {playlist_id} через HTML")

        except ResolveError:
            raise
        except Exception as e:
            raise ResolveError(f"Ошибка при HTML-загрузке плейлиста {playlist_id}: {e}") from e

    def _find_playlist_in_state(self, obj: Any, depth: int = 0) -> Optional[dict]:
        """Рекурсивно ищет данные плейлиста в INITIAL_STATE."""
        if depth > 10:
            return None

        if isinstance(obj, dict):
            if 'playlist' in obj and isinstance(obj['playlist'], dict):
                p = obj['playlist']
                if 'kind' in p and 'owner' in p:
                    return p

            for value in obj.values():
                result = self._find_playlist_in_state(value, depth + 1)
                if result:
                    return result

        elif isinstance(obj, list):
            for item in obj:
                result = self._find_playlist_in_state(item, depth + 1)
                if result:
                    return result

        return None
