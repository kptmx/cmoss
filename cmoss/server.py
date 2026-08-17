"""Async wrapper around libopensonic's AsyncConnection.

Every call runs on the app's asyncio loop and raises a friendly
`ServerError` instead of raw libopensonic exceptions. Returns the
typed media objects from libopensonic directly (AlbumID3, Child, ...).
"""
import logging

from libopensonic import AsyncConnection, errors as sonic_errors

from .config import Config, effective_port

log = logging.getLogger(__name__)


class ServerError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _wrap(e: Exception) -> ServerError:
    if isinstance(e, sonic_errors.SonicError):
        return ServerError(str(e) or e.__class__.__name__, code=getattr(e, "code", None))
    return ServerError(f"{e.__class__.__name__}: {e}")


class Server:
    def __init__(self, cfg: Config):
        self.config = cfg
        self._conn: AsyncConnection | None = None

    @property
    def conn(self) -> AsyncConnection:
        if self._conn is None:
            self._conn = AsyncConnection(
                base_url=self.config.server,
                username=self.config.username,
                password=self.config.password,
                api_key=self.config.api_key or None,
                port=effective_port(self.config),
                server_path=self.config.server_path,
                app_name=self.config.app_name,
                api_version=self.config.api_version,
                legacy_auth=self.config.legacy_auth,
                use_get=True,
            )
        return self._conn

    async def close(self):
        if self._conn is not None:
            try:
                await self._conn.cleanup()
            except Exception:
                pass
            self._conn = None

    # -- session ---------------------------------------------------------

    async def ping(self) -> bool:
        try:
            return await self.conn.ping()
        except Exception as e:
            raise _wrap(e) from e

    async def get_license(self) -> dict:
        try:
            return await self.conn.get_license()
        except Exception as e:
            raise _wrap(e) from e

    # -- library ---------------------------------------------------------

    async def get_artists(self):
        """Flattened, alphabetically sorted list of ArtistID3."""
        try:
            artists = await self.conn.get_artists()
        except Exception as e:
            raise _wrap(e) from e
        out = []
        for idx in artists.index or []:
            out.extend(idx.artist or [])
        out.extend(artists.shortcut or [])
        out.sort(key=lambda a: (a.sort_name or a.name or "").lower())
        return out

    async def get_artist(self, artist_id: str):
        try:
            return await self.conn.get_artist(artist_id)
        except Exception as e:
            raise _wrap(e) from e

    async def get_album(self, album_id: str):
        try:
            return await self.conn.get_album(album_id)
        except Exception as e:
            raise _wrap(e) from e

    async def get_album_list2(self, ltype: str, size: int = 60):
        try:
            return await self.conn.get_album_list2(ltype, size=size)
        except Exception as e:
            raise _wrap(e) from e

    async def get_random_songs(self, size: int = 50):
        try:
            return await self.conn.get_random_songs(size=size)
        except Exception as e:
            raise _wrap(e) from e

    async def get_songs_by_genre(self, genre: str, count: int = 100):
        try:
            return await self.conn.get_songs_by_genre(genre, count=count)
        except Exception as e:
            raise _wrap(e) from e

    async def get_genres(self):
        try:
            return await self.conn.get_genres()
        except Exception as e:
            raise _wrap(e) from e

    async def search3(self, query: str, artist_count=10, album_count=10, song_count=30):
        try:
            return await self.conn.search3(
                query,
                artist_count=artist_count,
                album_count=album_count,
                song_count=song_count,
            )
        except Exception as e:
            raise _wrap(e) from e

    # -- playlists -------------------------------------------------------

    async def get_playlists(self):
        try:
            return await self.conn.get_playlists()
        except Exception as e:
            raise _wrap(e) from e

    async def get_playlist(self, playlist_id: str):
        try:
            return await self.conn.get_playlist(playlist_id)
        except Exception as e:
            raise _wrap(e) from e

    async def create_playlist(self, name: str, song_ids: list[str]):
        try:
            return await self.conn.create_playlist(name, song_ids=song_ids)
        except Exception as e:
            raise _wrap(e) from e

    async def update_playlist(self, pid: str, song_ids_to_add: list[str] | None = None,
                              song_indices_to_remove: list[int] | None = None):
        try:
            return await self.conn.update_playlist(pid, song_ids_to_add=song_ids_to_add,
                                                   song_indices_to_remove=song_indices_to_remove)
        except Exception as e:
            raise _wrap(e) from e

    async def delete_playlist(self, pid: str):
        try:
            return await self.conn.delete_playlist(pid)
        except Exception as e:
            raise _wrap(e) from e

    # -- starred / ratings ------------------------------------------------

    async def get_starred2(self):
        try:
            return await self.conn.get_starred2()
        except Exception as e:
            raise _wrap(e) from e

    async def star(self, sids=None, album_ids=None, artist_ids=None) -> bool:
        try:
            return await self.conn.star(sids=sids, album_ids=album_ids, artist_ids=artist_ids)
        except Exception as e:
            raise _wrap(e) from e

    async def unstar(self, sids=None, album_ids=None, artist_ids=None) -> bool:
        try:
            return await self.conn.unstar(sids=sids, album_ids=album_ids, artist_ids=artist_ids)
        except Exception as e:
            raise _wrap(e) from e

    async def set_rating(self, item_id: str, rating: int) -> bool:
        try:
            return await self.conn.set_rating(item_id, rating)
        except Exception as e:
            raise _wrap(e) from e

    async def scrobble(self, sid: str, submission: bool = True, listen_time: int | None = None) -> bool:
        try:
            return await self.conn.scrobble(sid, submission=submission, listen_time=listen_time)
        except Exception as e:
            raise _wrap(e) from e

    async def set_now_playing(self, sid: str, state: str = "playing",
                              position_ms: int | None = None) -> bool:
        """Report playback state to the server's Now Playing page.

        Uses the OpenSubsonic `playbackReport` extension (the `reportPlayback`
        endpoint) rather than the legacy `scrobble` (submission=false). The
        legacy handler drops `state` and `positionMs` (Navidrome hardcodes
        `state=playing` and reads only `position` in seconds), so a pause would
        be re-reported as "playing at 0" and the server's position estimate
        would restart from zero. `reportPlayback` carries the exact state
        (playing/paused/stopped) and a millisecond position.

        `ignoreScrobble=true` keeps Navidrome's server-side auto-scrobble off —
        cmoss submits real scrobbles itself via the legacy `scrobble` endpoint,
        so without it a stopped report would double-scrobble."""
        try:
            conn = self.conn
            res = await conn._do_request("reportPlayback", {
                "mediaId": sid,
                "mediaType": "song",
                "positionMs": int(position_ms or 0),
                "state": state,
                "playbackRate": 1.0,
                "ignoreScrobble": True,
            })
            dres = await conn._handle_info_res(res)
            conn._check_status(dres)
            return True
        except Exception as e:
            raise _wrap(e) from e

    # -- play queue -------------------------------------------------------

    async def save_play_queue(self, qids, current=None, position=None) -> bool:
        try:
            return await self.conn.save_play_queue(qids, current=current, position=position)
        except Exception as e:
            raise _wrap(e) from e

    async def get_play_queue(self):
        try:
            return await self.conn.get_play_queue()
        except Exception as e:
            raise _wrap(e) from e

    async def get_now_playing(self):
        try:
            return await self.conn.get_now_playing()
        except Exception as e:
            raise _wrap(e) from e
