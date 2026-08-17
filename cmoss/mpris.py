"""MPRIS — expose playback to the Linux desktop media UI.

Implements a ``org.mpris.MediaPlayer2`` D-Bus service on the session bus using
jeepney (already a dependency), driven by the player's observable `DataBox`es.
Everything runs on the Flet page loop — jeepney is fully async — so no extra
threads are needed and DataBox notifications (marshalled onto that same loop)
can be pushed straight to D-Bus.

jeepney value conventions used here:
* a D-Bus variant ``v`` is a ``(signature, value)`` tuple;
* ``a{sv}`` dicts map ``name -> (signature, value)``.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from jeepney import (
    DBusAddress,
    HeaderFields,
    MatchRule,
    MessageType,
    new_error,
    new_method_return,
    new_signal,
)
from jeepney.bus_messages import DBus
from jeepney.io.asyncio import DBusRouter, open_dbus_connection

from .media_control import MediaControl

log = logging.getLogger(__name__)

_MPRIS_PATH = "/org/mpris/MediaPlayer2"
_IF_ROOT = "org.mpris.MediaPlayer2"
_IF_PLAYER = "org.mpris.MediaPlayer2.Player"
_IF_PROPS = "org.freedesktop.DBus.Properties"
_IF_INTRO = "org.freedesktop.DBus.Introspectable"

_ERR_UNKNOWN_METHOD = "org.freedesktop.DBus.Error.UnknownMethod"
_ERR_UNKNOWN_PROPERTY = "org.freedesktop.DBus.Error.UnknownProperty"
_ERR_READONLY = "org.freedesktop.DBus.Error.PropertyReadOnly"
_ERR_INVALID_ARGS = "org.freedesktop.DBus.Error.InvalidArgs"
_ERR_NOT_SUPPORTED = "org.freedesktop.DBus.Error.NotSupported"

_STATUS = {"playing": "Playing", "paused": "Paused", "completed": "Stopped"}
_LOOP = {"off": "None", "all": "Playlist", "one": "Track"}


def _dbus_error(msg, name, text):
    return new_error(msg, name, "s", (text,))


class MprisControl(MediaControl):
    def __init__(self, store):
        super().__init__(store)
        self._name = "org.mpris.MediaPlayer2.cmoss"
        self._conn = None
        self._router = None
        self._task = None

    # -- lifecycle -------------------------------------------------------

    def _open(self):
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        self._task = loop.create_task(self._run())

    async def _run(self):
        try:
            conn = await open_dbus_connection("SESSION")
        except Exception as e:
            log.warning("MPRIS: no session bus (%s); disabled", e)
            return
        self._conn = conn
        router = DBusRouter(conn)
        self._router = router
        rule = MatchRule(type="method_call", path=_MPRIS_PATH)
        # Register the inbound filter *before* claiming the well-known name so a
        # client reacting to NameOwnerChanged (e.g. plasmashell's media model)
        # can never have its GetAll dropped in the gap. The queue must be able
        # to buffer a burst of calls (a media UI sends two GetAll at once).
        try:
            with router.filter(rule, bufsize=32) as queue:
                try:
                    reply = await router.send_and_get_reply(DBus().RequestName(self._name, 0))
                    if reply.body and reply.body[0] not in (1, 4):
                        log.warning("MPRIS: name %s taken (result %s); media UI disabled",
                                    self._name, reply.body[0])
                except Exception as e:
                    log.warning("MPRIS: RequestName failed: %s", e)

                self._emit_position(int(self.player.position_ms.get() or 0), seeked=True)

                while not self._closed:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        await self._handle(msg)
                    except Exception as e:
                        log.debug("MPRIS: handler failed: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("MPRIS: serve loop ended: %s", e)
        finally:
            await self._close_conn()

    async def _close_conn(self):
        conn, self._conn = self._conn, None
        self._router = None
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass

    def _shutdown(self):
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        if self._conn is not None:
            loop = self.loop
            if loop is not None and not loop.is_closed():
                try:
                    loop.create_task(self._close_conn())
                except Exception:
                    pass

    # -- outbound: signals -------------------------------------------------

    def _send(self, coro):
        router, loop = self._router, self.loop
        if router is None or loop is None or loop.is_closed() or self._closed:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            fut.add_done_callback(_on_sent)
        except Exception as e:
            log.debug("MPRIS: schedule send failed: %s", e)

    async def _props_changed(self, changed: dict):
        emitter = DBusAddress(_MPRIS_PATH, interface=_IF_PROPS)
        msg = new_signal(emitter, "PropertiesChanged", "sa{sv}as",
                         (_IF_PLAYER, dict(changed), []))
        await self._router.send(msg)

    async def _seeked(self, pos_us: int):
        emitter = DBusAddress(_MPRIS_PATH, interface=_IF_PLAYER)
        msg = new_signal(emitter, "Seeked", "x", (int(pos_us),))
        await self._router.send(msg)

    def _emit_state(self, _state):
        self._send(self._props_changed({"PlaybackStatus": ("s", self._mpris_status())}))
        self._send(self._seeked(int(self.player.position_ms.get() or 0) * 1000))

    def _emit_metadata(self, song):
        self._send(self._props_changed({"Metadata": ("a{sv}", self._metadata(song))}))

    def _emit_position(self, ms, seeked=False):
        if seeked:
            pos_us = int(ms or 0) * 1000
            self._send(self._props_changed({"Position": ("x", pos_us)}))
            self._send(self._seeked(pos_us))

    def _emit_duration(self, _ms):
        self._emit_metadata(self.player.playing.get())

    def _emit_volume(self, v):
        self._send(self._props_changed({"Volume": ("d", float(v or 0.0))}))

    def _emit_shuffle(self, on):
        self._send(self._props_changed({"Shuffle": ("b", bool(on))}))

    def _emit_repeat(self, mode):
        self._send(self._props_changed({"LoopStatus": ("s", self._mpris_loop(mode))}))

    def _emit_can_go(self):
        props = self._player_props()
        self._send(self._props_changed({
            "CanGoNext": props["CanGoNext"],
            "CanGoPrevious": props["CanGoPrevious"],
        }))

    # -- property values ----------------------------------------------------

    def _mpris_status(self):
        return _STATUS.get(str(self.player.state.get() or ""), "Stopped")

    def _mpris_loop(self, mode=None):
        if mode is None:
            mode = self.player.repeat.get()
        return _LOOP.get(str(mode or "off"), "None")

    @staticmethod
    def _track_id(song_id):
        if not song_id:
            return "/org/mpris/MediaPlayer2/Track/None"
        digest = hashlib.sha1(str(song_id).encode("utf-8", "replace")).hexdigest()[:16]
        return f"/org/mpris/MediaPlayer2/Track/{digest}"

    def _stream_url(self, song):
        try:
            return self.store.stream_url(song.id)
        except Exception:
            return ""

    def _metadata(self, song):
        if song is None:
            return {}
        meta = {
            "mpris:trackid": ("o", self._track_id(song.id)),
            "mpris:length": ("x", int(self.player.duration_ms.get() or 0) * 1000),
            "xesam:title": ("s", str(getattr(song, "title", None) or "")),
            "xesam:url": ("s", self._stream_url(song)),
        }
        artist = getattr(song, "artist", None) or getattr(song, "display_artist", None)
        if artist:
            meta["xesam:artist"] = ("as", [str(artist)])
        album = getattr(song, "album", None)
        if album:
            meta["xesam:album"] = ("s", str(album))
        album_artists = self._album_artists(song)
        if album_artists:
            meta["xesam:albumArtist"] = ("as", album_artists)
        cover = None
        if self.store is not None:
            cover_file = getattr(self.store, "cover_file_for", None)
            cover = cover_file(song) if cover_file else None
            if cover:
                cover = Path(cover).as_uri()
            else:
                cover = self.store.cover_url_for(song) if self.store else None
        if cover:
            meta["mpris:artUrl"] = ("s", str(cover))
        if getattr(song, "track", None):
            meta["xesam:trackNumber"] = ("i", int(song.track))
        return meta

    @staticmethod
    def _album_artists(song):
        aa = getattr(song, "display_album_artist", None) or getattr(song, "album_artist", None)
        if aa:
            return [str(aa)]
        aa_list = getattr(song, "album_artists", None) or []
        names = [a.name for a in aa_list if getattr(a, "name", None)]
        return names

    def _root_props(self):
        return {
            "CanQuit": ("b", True),
            "CanRaise": ("b", False),
            "HasTrackList": ("b", False),
            "Identity": ("s", "cmoss"),
            "SupportedUriSchemes": ("as", ["http", "https"]),
            "SupportedMimeTypes": ("as", []),
        }

    def _player_props(self):
        can_go = bool(self.player.queue) and self.player.playing.get() is not None
        return {
            "PlaybackStatus": ("s", self._mpris_status()),
            "LoopStatus": ("s", self._mpris_loop()),
            "Rate": ("d", 1.0),
            "Shuffle": ("b", bool(self.player.shuffle.get())),
            "Metadata": ("a{sv}", self._metadata(self.player.playing.get())),
            "Volume": ("d", float(self.player.volume.get() or 0.0)),
            "Position": ("x", int(self.player.position_ms.get() or 0) * 1000),
            "MinimumRate": ("d", 1.0),
            "MaximumRate": ("d", 1.0),
            "CanGoNext": ("b", can_go),
            "CanGoPrevious": ("b", can_go),
            "CanPlay": ("b", True),
            "CanPause": ("b", True),
            "CanSeek": ("b", True),
            "CanControl": ("b", True),
        }

    def _props_for(self, iface):
        if iface == _IF_ROOT:
            return self._root_props()
        if iface == _IF_PLAYER:
            return self._player_props()
        return {}

    def _get_prop(self, iface, name):
        props = self._props_for(iface)
        if name not in props:
            raise KeyError(name)
        return props[name]

    # -- inbound: method calls ----------------------------------------------

    async def _handle(self, msg):
        if msg.header.message_type is not MessageType.method_call:
            return
        iface = msg.header.fields.get(HeaderFields.interface)
        member = msg.header.fields.get(HeaderFields.member)
        reply = self._route(iface, member, msg)
        if reply is not None and self._router is not None:
            await self._router.send(reply)

    def _route(self, iface, member, msg):
        if iface == _IF_PROPS:
            return self._route_props(member, msg)
        if iface == _IF_INTRO and member == "Introspect":
            return new_method_return(msg, "s", (self._introspection(),))
        if iface == _IF_ROOT:
            return self._route_root(member, msg)
        if iface == _IF_PLAYER:
            return self._route_player(member, msg)
        return _dbus_error(msg, _ERR_UNKNOWN_METHOD, f"No such interface {iface}")

    def _route_root(self, member, msg):
        if member in ("Raise", "Quit"):
            return new_method_return(msg)
        return _dbus_error(msg, _ERR_UNKNOWN_METHOD, f"No such method {member}")

    def _route_props(self, member, msg):
        if member == "Get":
            iface, name = msg.body[0], msg.body[1]
            try:
                sig, val = self._get_prop(iface, name)
            except KeyError:
                return _dbus_error(msg, _ERR_UNKNOWN_PROPERTY, f"No such property {name}")
            return new_method_return(msg, "v", ((sig, val),))
        if member == "GetAll":
            iface = msg.body[0] if msg.body else ""
            return new_method_return(msg, "a{sv}", (self._props_for(iface),))
        if member == "Set":
            iface, name, variant = msg.body
            return self._route_set(iface, name, variant, msg)
        return _dbus_error(msg, _ERR_UNKNOWN_METHOD, f"No such method {member}")

    def _route_set(self, iface, name, variant, msg):
        if iface != _IF_PLAYER:
            return _dbus_error(msg, _ERR_READONLY, f"{name} is read-only")
        if name == "Volume":
            sig, val = variant
            if sig not in ("d", "i"):
                return _dbus_error(msg, _ERR_INVALID_ARGS, "Volume must be a double")
            self.cmd_volume(float(val))
            return new_method_return(msg)
        if name == "LoopStatus":
            _, val = variant
            mapping = {"None": "off", "Track": "one", "Playlist": "all"}
            if val not in mapping:
                return _dbus_error(msg, _ERR_INVALID_ARGS, f"Invalid LoopStatus {val}")
            self.cmd_repeat(mapping[val])
            return new_method_return(msg)
        if name == "Shuffle":
            _, val = variant
            self.cmd_shuffle(bool(val))
            return new_method_return(msg)
        if name == "Rate":
            return new_method_return(msg)
        return _dbus_error(msg, _ERR_READONLY, f"{name} is read-only")

    def _route_player(self, member, msg):
        if member == "Play":
            self.cmd_play()
        elif member == "Pause":
            self.cmd_pause()
        elif member == "PlayPause":
            self.cmd_toggle()
        elif member == "Stop":
            self.cmd_stop()
        elif member == "Next":
            self.cmd_next()
        elif member == "Previous":
            self.cmd_prev()
        elif member == "Seek":
            offset_us = msg.body[0] if msg.body else 0
            pos = int(self.player.position_ms.get() or 0) + int(offset_us) // 1000
            self.cmd_seek(max(0, pos))
        elif member == "SetPosition":
            _, pos_us = msg.body
            self.cmd_seek(max(0, int(pos_us) // 1000))
        elif member == "OpenUri":
            return _dbus_error(msg, _ERR_NOT_SUPPORTED, "OpenUri is not supported")
        else:
            return _dbus_error(msg, _ERR_UNKNOWN_METHOD, f"No such method {member}")
        return new_method_return(msg)

    @staticmethod
    def _introspection():
        return """<node>
  <interface name="org.mpris.MediaPlayer2">
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
    <method name="Raise"/>
    <method name="Quit"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" type="x" name="Offset"/></method>
    <method name="SetPosition"><arg direction="in" type="o" name="TrackId"/><arg direction="in" type="x" name="Position"/></method>
    <method name="OpenUri"><arg direction="in" type="s" name="Uri"/></method>
    <signal name="Seeked"><arg type="x"/></signal>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg direction="in" type="s" name="interface_name"/>
      <arg direction="in" type="s" name="property_name"/>
      <arg direction="out" type="v" name="value"/>
    </method>
    <method name="GetAll">
      <arg direction="in" type="s" name="interface_name"/>
      <arg direction="out" type="a{sv}" name="properties"/>
    </method>
    <method name="Set">
      <arg direction="in" type="s" name="interface_name"/>
      <arg direction="in" type="s" name="property_name"/>
      <arg direction="in" type="v" name="value"/>
    </method>
    <signal name="PropertiesChanged"><arg type="s"/><arg type="a{sv}"/><arg type="as"/></signal>
  </interface>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect"><arg direction="out" type="s"/></method>
  </interface>
</node>"""


def _on_sent(fut):
    if fut.cancelled():
        return
    try:
        exc = fut.exception()
    except Exception:
        return
    if exc is not None:
        log.debug("MPRIS: send failed: %s", exc)
