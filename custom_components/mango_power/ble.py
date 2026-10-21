"""Local Bluetooth link to a Mango Power E through Home Assistant's Bluetooth stack.

Works with local adapters and ESPHome Bluetooth proxies (active connections).
Read-only: it requests data and listens to pushes; controls stay on the cloud.
"""
from __future__ import annotations

import asyncio
import logging
import time
import warnings
from typing import TYPE_CHECKING, Any

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback

from . import ble_proto as bp
from .const import BLE_MODE_AUTO, BLE_MODE_OFF, BLE_POLL_INTERVAL

if TYPE_CHECKING:
    from .coordinator import MangoDeviceCoordinator

_LOGGER = logging.getLogger(__name__)

RETRY_MIN, RETRY_MAX = 15, 300
WATCHDOG_S = 45  # reconnect if the unit sends nothing for this long (it pushes every ~17.5 s)


def ble_name_for(sn: str) -> str:
    return f"MP{sn}"


@callback
def find_address(hass: HomeAssistant, sn: str) -> str | None:
    """Address of the unit advertising as MP<serial>, if any scanner has seen it."""
    target = ble_name_for(sn).upper()
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        if (info.name or "").upper() == target:
            return info.address
    return None


@callback
def scanners_for(hass: HomeAssistant, address: str) -> list[tuple[str, str, int]]:
    """[(source, scanner name, rssi)] for connectable scanners that currently see the unit."""
    out = []
    for dev in bluetooth.async_scanner_devices_by_address(hass, address, connectable=True):
        out.append((dev.scanner.source, dev.scanner.name, dev.advertisement.rssi))
    return sorted(out, key=lambda x: -x[2])


class MangoBleLink:
    """Keeps one connection open to one unit and feeds decoded data to its coordinator."""

    def __init__(self, hass: HomeAssistant, coordinator: MangoDeviceCoordinator, mode: str,
                 interval: int = BLE_POLL_INTERVAL) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.mode = mode or BLE_MODE_AUTO  # "auto", "off" or a scanner source
        self.interval = max(15, int(interval))  # keepalive requests; the unit pushes ~17.5 s
        self.raw_enabled = False  # set from options in __init__.py
        self._mtu = 185
        self.address: str | None = None
        self.connected = False
        self.via: str | None = None  # scanner name used for the connection
        self.rssi: int | None = None
        self._client: BleakClientWithServiceCache | None = None
        self._assembler = bp.FrameAssembler()
        self._task: asyncio.Task | None = None
        self._disconnected = asyncio.Event()
        self._stopping = False
        self._last_req: tuple[int, float] | None = None
        self._last_rx: float | None = None
        self._last_dump: float = 0.0

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self.mode == BLE_MODE_OFF:
            return
        self._task = self.hass.async_create_background_task(
            self._run(), f"mango_power_ble_{self.coordinator.sn}"
        )

    async def stop(self) -> None:
        self._stopping = True
        self._disconnected.set()
        if self._client:
            try:
                await self._client.disconnect()
            except BleakError:
                pass
        if self._task:
            self._task.cancel()

    # ---------------------------------------------------------------- connection loop

    def _pick_device(self):
        self.address = self.address or find_address(self.hass, self.coordinator.sn)
        if not self.address:
            return None
        if self.mode == BLE_MODE_AUTO:
            dev = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
            info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
            if info:
                self.rssi = info.rssi
                scanner = bluetooth.async_scanner_by_source(self.hass, info.source)
                self.via = scanner.name if scanner else info.source
            return dev
        for sd in bluetooth.async_scanner_devices_by_address(self.hass, self.address, connectable=True):
            if sd.scanner.source == self.mode:
                self.via, self.rssi = sd.scanner.name, sd.advertisement.rssi
                return sd.ble_device
        return None  # chosen proxy can't see the unit right now

    async def _run(self) -> None:
        delay = RETRY_MIN
        while not self._stopping:
            device = self._pick_device()
            if device is None:
                await asyncio.sleep(60)
                continue
            try:
                await self._session(device)
                delay = RETRY_MIN
            except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
                _LOGGER.debug("%s: Bluetooth session ended: %s", self.coordinator.sn, err)
            except Exception:  # noqa: BLE001 - never let the loop die
                _LOGGER.exception("%s: unexpected Bluetooth error", self.coordinator.sn)
            self._set_connected(False)
            if self._stopping:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX)

    async def _session(self, device) -> None:
        self._disconnected.clear()
        self._assembler.reset()
        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            f"Mango {self.coordinator.sn}",
            disconnected_callback=lambda _c: self._disconnected.set(),
            max_attempts=3,
        )
        self._client = client
        self._mtu = await self._negotiate_mtu(client)
        started = time.monotonic()
        self._last_rx = None
        reason = "unit or adapter closed the connection"
        try:
            await client.start_notify(bp.CHAR_UUID, self._on_notify)
            self._set_connected(True)
            _LOGGER.info(
                "%s: Bluetooth connected via %s, RSSI %s dBm, MTU %s (request interval %ss)",
                self.coordinator.sn, self.via, self.rssi, self._mtu, self.interval,
            )
            # The unit answers one full query on connect (static info, settings, realtime,
            # energy) and afterwards pushes realtime data on its own timer (~17.5 s).
            await self._send(bp.MT_QUERY_ALL)
            while not self._disconnected.is_set():
                try:
                    await asyncio.wait_for(self._disconnected.wait(), self.interval)
                except asyncio.TimeoutError:
                    # Watchdog: the link can stay "connected" but silent on a weak signal.
                    last = self._last_rx or started
                    if time.monotonic() - last > WATCHDOG_S:
                        reason = f"no data for {time.monotonic() - last:.0f}s (watchdog)"
                        break
                    await self._send(bp.MT_READ_REALTIME)  # keepalive; the unit sets the pace
        except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
            reason = f"error: {err}"
            raise
        finally:
            self._client = None
            _LOGGER.info(
                "%s: Bluetooth disconnected after %.0fs - %s",
                self.coordinator.sn, time.monotonic() - started, reason,
            )
            try:
                await client.disconnect()
            except BleakError:
                pass

    async def _send(self, msg_type: int) -> None:
        client = self._client
        if not client:
            return
        for frame in bp.build_frames(bp.request(msg_type), self._mtu):
            await client.write_gatt_char(bp.CHAR_UUID, frame, response=False)
        self._last_req = (msg_type, time.monotonic())
        _LOGGER.debug("%s: sent request type %s", self.coordinator.sn, msg_type)

    async def _negotiate_mtu(self, client) -> int:
        """MTU for framing. Local BlueZ adapters need an explicit MTU exchange first
        (otherwise bleak warns and returns the default); ESPHome proxies already know it."""
        backend = getattr(client, "_backend", None)
        if backend is not None and hasattr(backend, "_acquire_mtu"):
            try:
                await backend._acquire_mtu()  # noqa: SLF001 - BlueZ only
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s: MTU exchange failed (%s), using default", self.coordinator.sn, err)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            mtu = getattr(client, "mtu_size", 0) or 0
        mtu = mtu if mtu >= 23 else 185
        _LOGGER.debug("%s: using MTU %s", self.coordinator.sn, mtu)
        return mtu

    # ---------------------------------------------------------------- incoming data

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        candidates, next_idx = self._assembler.feed(bytes(data))
        if next_idx is not None and self._client:
            self.hass.async_create_task(
                self._client.write_gatt_char(bp.CHAR_UUID, bp.page_request(next_idx), response=False)
            )
            return
        for payload in candidates:
            try:
                decoded = bp.decode(payload)
            except bp.ProtoError:
                continue
            if self.raw_enabled:
                self.coordinator.async_raw_update(bp.flatten(payload))
            if _LOGGER.isEnabledFor(logging.DEBUG):
                # Full raw dump of every message (all fields, all levels) to hunt for
                # data the app doesn't use. Field numbers refer to mango.proto.
                _LOGGER.debug("%s: raw message (%d bytes) %s", self.coordinator.sn, len(payload), bp.raw_all(payload))
            if decoded.get("_type") == "ems":
                now = time.monotonic()
                since_req = f"{now - self._last_req[1]:.1f}s after request type {self._last_req[0]}" if self._last_req else "no request yet"
                since_rx = f"{now - self._last_rx:.1f}s since previous" if self._last_rx else "first message"
                self._last_rx = now
                _LOGGER.debug(
                    "%s: received message type %s (%d bytes, realtime=%s) - %s, %s",
                    self.coordinator.sn, decoded.get("_msg"), len(payload), "packs" in decoded or "pvPower" in decoded,
                    since_req, since_rx,
                )
                sn = decoded.get("sn")
                if sn and sn != self.coordinator.sn:
                    _LOGGER.warning("%s: Bluetooth data from %s ignored", self.coordinator.sn, sn)
                    return
                self.coordinator.async_ble_update(decoded, time.monotonic())
            return
        _LOGGER.debug("%s: undecodable Bluetooth payload %s", self.coordinator.sn, data.hex())

    @callback
    def _set_connected(self, value: bool) -> None:
        if self.connected != value:
            self.connected = value
            self.coordinator.async_update_listeners()
