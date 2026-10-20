"""Mango Power E Bluetooth protocol: framing + protobuf decoding (pure Python).

Reverse-engineered from the Mango app. See mango.proto in the repo for the schema.

Frame:   [idx u8, 1-based][len u16 BE][crc16 u16 BE][chunk]; chunk <= MTU-8.
         Multi-frame messages are sent highest index first; the receiver asks for the
         next one by writing [0x00, idx-1]. The device's CRC doesn't match CRC-16/MODBUS
         (the app ignores it too), so we only check the length.
Payload: protobuf BLEMessage{type=2 (EMS_PAYLOAD), emsPayload(4)=PayloadMessage}.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

_TEXT = re.compile(r"[A-Za-z0-9._:\-]+")

SERVICE_UUID = "00000001-6d61-6e67-6f70-6f7765722021"
CHAR_UUID = "00000006-6d61-6e67-6f70-6f7765722021"

BLE_EMS_PAYLOAD = 2
MT_READ_STATIC = 16
MT_READ_REALTIME = 17
MT_QUERY_ALL = 20


# ------------------------------------------------------------------ encoding


def _crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _varint(v: int) -> bytes:
    v &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def _vfield(f: int, v: int) -> bytes:
    return _varint(f << 3) + _varint(v)


def _lfield(f: int, b: bytes) -> bytes:
    return _varint(f << 3 | 2) + _varint(len(b)) + b


def request(msg_type: int) -> bytes:
    """BLEMessage{type=EMS_PAYLOAD, emsPayload{type, uuid}}."""
    payload = _vfield(1, msg_type) + _lfield(2, str(uuid.uuid4()).encode())
    return _vfield(1, BLE_EMS_PAYLOAD) + _lfield(4, payload)


def build_frames(payload: bytes, mtu: int = 185) -> list[bytes]:
    size = max(20, mtu - 8)
    chunks = [payload[i : i + size] for i in range(0, len(payload), size)] or [b""]
    frames = []
    for i, chunk in enumerate(chunks):
        hdr = bytes([i + 1]) + len(chunk).to_bytes(2, "big") + _crc16_modbus(chunk).to_bytes(2, "big")
        frames.insert(0, hdr + chunk)  # highest index first, like the app
    return frames


def page_request(idx: int) -> bytes:
    return bytes([0x00, idx - 1])


# ------------------------------------------------------------------ frame reassembly


class FrameAssembler:
    """Collects frames until index 1 arrives; returns the full payload."""

    def __init__(self) -> None:
        self._frames: list[tuple[int, bytes]] = []

    def feed(self, data: bytes) -> tuple[list[bytes], int | None]:
        """Returns (candidate payloads, index to request next or None).

        With several frames the chunk order is ambiguous (the app joins them in
        received order), so both received and ascending order are offered.
        """
        if len(data) >= 5:
            idx = data[0]
            ln = int.from_bytes(data[1:3], "big")
            if 5 + ln == len(data):
                self._frames.append((idx, bytes(data[5:])))
                if idx > 1:
                    return [], idx
                frames, self._frames = self._frames, []
                received = b"".join(c for _, c in frames)
                ascending = b"".join(c for _, c in sorted(frames))
                return ([received] if received == ascending else [received, ascending]), None
        # unframed: treat as a complete raw protobuf message
        self._frames = []
        return [bytes(data)], None

    def reset(self) -> None:
        self._frames = []


# ------------------------------------------------------------------ protobuf decoding


class ProtoError(ValueError):
    pass


def _rvarint(b: bytes, i: int) -> tuple[int, int]:
    v = s = 0
    while True:
        if i >= len(b):
            raise ProtoError("truncated varint")
        x = b[i]
        i += 1
        v |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return v, i
        if s > 70:
            raise ProtoError("varint too long")


def parse(b: bytes) -> dict[int, list[Any]]:
    """Generic decode: {field: [values]}; varints as int, length-delimited as bytes."""
    out: dict[int, list[Any]] = {}
    i = 0
    while i < len(b):
        key, i = _rvarint(b, i)
        f, wt = key >> 3, key & 7
        if f == 0:
            raise ProtoError("field 0")
        if wt == 0:
            v, i = _rvarint(b, i)
        elif wt == 2:
            ln, i = _rvarint(b, i)
            if i + ln > len(b):
                raise ProtoError("truncated bytes")
            v, i = b[i : i + ln], i + ln
        elif wt == 5:
            v, i = int.from_bytes(b[i : i + 4], "little"), i + 4
        elif wt == 1:
            v, i = int.from_bytes(b[i : i + 8], "little"), i + 8
        else:
            raise ProtoError(f"wire type {wt}")
        out.setdefault(f, []).append(v)
    return out


def _sub(m: dict, f: int) -> dict | None:
    v = m.get(f)
    if not v or not isinstance(v[0], (bytes, bytearray)):
        return None
    try:
        return parse(v[0])
    except ProtoError:
        return None


def _subs(m: dict, f: int) -> list[dict]:
    out = []
    for raw in m.get(f, []):
        if isinstance(raw, (bytes, bytearray)):
            try:
                out.append(parse(raw))
            except ProtoError:
                pass
    return out


def _u(m: dict | None, f: int) -> int | None:
    if not m or f not in m or not isinstance(m[f][0], int):
        return None
    v = m[f][0]
    return v - (1 << 64) if v >= 1 << 63 else v  # negative int32/int64


def _s(m: dict | None, f: int) -> int | None:
    """sint32/sint64 (zigzag)."""
    if not m or f not in m or not isinstance(m[f][0], int):
        return None
    v = m[f][0]
    return (v >> 1) ^ -(v & 1)


def _str(m: dict | None, f: int) -> str | None:
    if not m or f not in m or not isinstance(m[f][0], (bytes, bytearray)):
        return None
    try:
        return m[f][0].decode()
    except UnicodeDecodeError:
        return None


def _scale(v: int | None, k: float, nd: int = 2) -> float | None:
    return None if v is None else round(v * k, nd)


def _energy(m: dict | None) -> dict[str, Any] | None:
    """DataEnergy: power(f2, sint32), kwhPeriod/Today/Total (f5-f7, sint64) - raw units."""
    if m is None:
        return None
    return {"power": _s(m, 2), "volt": _u(m, 3), "current": _u(m, 4), "status": _u(m, 1),
            "today": _s(m, 6), "total": _s(m, 7)}


def decode(payload: bytes) -> dict[str, Any]:
    """Decode a BLEMessage into a flat dict of known values (missing = absent)."""
    root = parse(payload)
    if _u(root, 1) != BLE_EMS_PAYLOAD:
        return {"_type": "other"}
    pm = _sub(root, 4)
    if pm is None:
        return {"_type": "other"}
    out: dict[str, Any] = {"_type": "ems", "_msg": _u(pm, 1)}

    info = _sub(pm, 3)
    if info:
        out["sn"] = _str(info, 6)
        for f, name in ((1, "Ems"), (2, "PcsPfc"), (3, "PcsDc"), (4, "Mppt"), (5, "Bms")):
            ver = _sub(info, f)
            if ver:
                sw, hw = _str(ver, 1), _str(ver, 2)
                out[f"ver{name}"] = f"{sw}" + (f" (hw {hw})" if hw else "")
        ft = _sub(info, 7)
        if ft and all(_u(ft, i) is not None for i in range(1, 7)):
            y, mo, d, h, mi, s = (_u(ft, i) for i in range(1, 7))
            out["factoryTime"] = f"20{y:02d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"
        out["devType"] = _u(info, 8)

    setting = _sub(pm, 5)
    if setting:
        ems_set = _sub(setting, 1)
        if ems_set:
            out["reportInterval"] = _u(ems_set, 18)
            out["setMaxChargeCurrent"] = _u(ems_set, 11)
            out["setUpsAutoChargeSoc"] = _u(ems_set, 12)

    rt = _sub(pm, 4)
    if rt:
        ems = _sub(rt, 1)
        if ems:
            out.update(
                sysState=_u(ems, 1), workState=_u(ems, 2), warning=_u(ems, 4), error=_u(ems, 5),
                pvPower=_u(ems, 6), gridPower=_u(ems, 7), loadPower=_u(ems, 8),
                socReal=_u(ems, 9), socUser=_u(ems, 10), dcPower=_u(ems, 11),
            )
        pcs = _sub(rt, 2)
        if pcs:
            out.update(
                acVolt=_scale(_u(pcs, 1), 0.1, 1), acPower=_u(pcs, 3),
                dcVolt=_scale(_u(pcs, 4), 0.01), dcCurrent=_scale(_u(pcs, 5), 0.01),
                inletTemp=_scale(_s(pcs, 7), 0.1, 1), outletTemp=_scale(_s(pcs, 6), 0.1, 1),
                pcsErrors=[v for v in pcs.get(8, []) if isinstance(v, int)],
            )
        mppt = _sub(rt, 3)
        if mppt:  # scaling not yet confirmed -> raw values
            out.update(mpptVoltRaw=_u(mppt, 1), mpptCurrentRaw=_u(mppt, 2),
                       mpptPowerRaw=_u(mppt, 3), mpptTotalRaw=_u(mppt, 4))
        packs = []
        for bms in _subs(rt, 4):
            volt = _scale(_u(bms, 3), 0.001, 3)
            cur = _scale(_s(bms, 4), 0.01)
            packs.append({
                "stateRaw": _u(bms, 1),
                "state": _u(bms, 1), "soc": _u(bms, 2), "volt": volt, "current": cur,
                "power": round(volt * cur, 1) if volt is not None and cur is not None else None,
                "temp": _scale(_s(bms, 5), 0.1, 1), "capacity": _scale(_u(bms, 8), 0.01),
                "warnings": [ (w - (1 << 64) if w >= 1 << 63 else w) for w in bms.get(6, []) if isinstance(w, int)],
                "faults": [ (f - (1 << 64) if f >= 1 << 63 else f) for f in bms.get(7, []) if isinstance(f, int)],
            })
        if packs:
            out["packs"] = packs
            powers = [p["power"] for p in packs if p["power"] is not None]
            if powers:
                out["batteryPower"] = round(sum(powers), 1)
        col = _sub(rt, 5)
        if col:
            grid, batt = _sub(col, 4), _sub(col, 5)
            if batt:
                out.update(batterySoh=_u(batt, 5), batteryCapacityRaw=_u(batt, 6))
                bin_ = _sub(batt, 2)
                if bin_:  # battery charging side: power (sint), 0.1 V, 0.1 A
                    out.update(battInPower=_s(bin_, 2), battInVolt=_scale(_u(bin_, 3), 0.1, 1),
                               battInCurrent=_scale(_u(bin_, 4), 0.1, 1))
            out["energy"] = {
                k: v for k, v in {
                    "solar": _energy(_sub(col, 1)),
                    "load": _energy(_sub(col, 2)),
                    "grid_in": _energy(_sub(grid, 2)) if grid else None,
                    "grid_out": _energy(_sub(grid, 3)) if grid else None,
                    "battery_in": _energy(_sub(batt, 2)) if batt else None,
                    "battery_out": _energy(_sub(batt, 3)) if batt else None,
                }.items() if v is not None
            }
    return out


# ------------------------------------------------------------------ raw field dump (diagnostics)


def _dump(m: dict | None, depth: int = 0, max_depth: int = 3) -> dict:
    """Every field in a message, nested messages expanded (for spotting undocumented fields)."""
    if m is None:
        return {}
    out: dict = {}
    for f, vals in sorted(m.items()):
        items = []
        for v in vals:
            if isinstance(v, (bytes, bytearray)) and len(v) >= 3 and _TEXT.fullmatch(v.decode("latin-1")):
                items.append(v.decode())  # text (serial numbers, versions, uuids)
                continue
            if isinstance(v, (bytes, bytearray)) and depth < max_depth:
                try:
                    items.append(_dump(parse(v), depth + 1, max_depth))
                    continue
                except ProtoError:
                    items.append(v.hex())
                    continue
            items.append(v)
        out[f] = items[0] if len(items) == 1 else items
    return out


def raw_solar_fields(payload: bytes) -> dict | None:
    """All raw fields of the MPPT block and the collector's solar entry, or None."""
    try:
        root = parse(payload)
        pm = _sub(root, 4)
        rt = _sub(pm, 4) if pm else None
        if not rt:
            return None
        col = _sub(rt, 5)
        return {"mppt": _dump(_sub(rt, 3)), "collector_solar": _dump(_sub(col, 1) if col else None),
                "ems": _dump(_sub(rt, 1))}
    except ProtoError:
        return None


def raw_all(payload: bytes) -> dict | str:
    """The whole message, every field at every level (field numbers, not names)."""
    try:
        return _dump(parse(payload), 0, 8)
    except ProtoError:
        return payload.hex()


_SECTION = {"4.4": "rt", "4.3": "static", "4.5": "setting", "4.6": "history", "4.14": "fault",
            "4.11": "online", "4.13": "offline", "4.15": "module"}


def flatten(payload: bytes) -> dict[str, object]:
    """Every leaf value of a message as {"rt.1.6": 175, ...}; repeated fields get an index."""
    tree = raw_all(payload)
    out: dict[str, object] = {}
    if not isinstance(tree, dict):
        return out

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        else:
            if isinstance(node, int) and node >= 1 << 63:
                node -= 1 << 64  # negative int32/int64
            out[path] = node

    walk(tree, "")
    named = {}
    for path, v in out.items():
        for pre, name in _SECTION.items():
            if path == pre or path.startswith(pre + "."):
                path = name + path[len(pre):]
                break
        named[path] = v
    return named
