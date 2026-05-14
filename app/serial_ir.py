"""Serial IR transport and Samsung-oriented service."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


def _normalized_key_token(value: str) -> str:
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum())


_KEY_ALIASES: dict[str, list[str]] = {
    "power": ["power"],
    "home": ["home"],
    "back": ["back", "return"],
    "menu": ["menu", "settings", "qmenu"],
    "input": ["input", "source"],
    "up": ["up", "dpadup"],
    "down": ["down", "dpaddown"],
    "left": ["left", "dpadleft"],
    "right": ["right", "dpadright"],
    "enter": ["enter", "ok", "select", "center", "dpadcenter"],
    "ok": ["ok", "enter", "select", "center", "dpadcenter"],
    "mute": ["mute"],
    "volup": ["volup", "volumeup", "volplus"],
    "voldown": ["voldown", "volumedown", "volminus"],
    "chup": ["chup", "channelup"],
    "chdown": ["chdown", "channeldown"],
    "exit": ["exit"],
}


def _build_alias_index(aliases: dict[str, list[str]]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for canonical, terms in aliases.items():
        unique_terms = [item for item in dict.fromkeys([canonical, *terms]) if item]
        for term in unique_terms:
            token = _normalized_key_token(term)
            if not token:
                continue
            existing = index.get(token, [])
            index[token] = [item for item in dict.fromkeys(existing + unique_terms) if item]
    return index


_ALIAS_INDEX = _build_alias_index(_KEY_ALIASES)


def _lookup_candidates(key_name: str) -> list[str]:
    raw = str(key_name or "").strip()
    if not raw:
        return []
    candidates: list[str] = [raw, raw.lower(), raw.upper()]
    token = _normalized_key_token(raw)
    if token:
        candidates.append(token)
        for term in _ALIAS_INDEX.get(token, []):
            candidates.extend([term, term.lower(), term.upper()])
    unique: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        marker = str(item or "").strip()
        if not marker:
            continue
        lowered = marker.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        unique.append(marker)
    return unique


_STANDARD_DATASET_PAYLOAD: dict[str, Any] = {
    "version": 1,
    "devices": {
        "samsung_tv_default": {
            "brand": "Samsung",
            "model": "Samsung TV (AA59-00741A starter set)",
            "sender_channel": "D2",
            "codes": {
                "POWER": {"protocol": "SAMSUNG", "code": "0xE0E040BF", "bits": 32},
                "SOURCE": {"protocol": "SAMSUNG", "code": "0xE0E0807F", "bits": 32},
                "MUTE": {"protocol": "SAMSUNG", "code": "0xE0E0F00F", "bits": 32},
                "VOL_UP": {"protocol": "SAMSUNG", "code": "0xE0E0E01F", "bits": 32},
                "VOL_DOWN": {"protocol": "SAMSUNG", "code": "0xE0E0D02F", "bits": 32},
                "CH_UP": {"protocol": "SAMSUNG", "code": "0xE0E048B7", "bits": 32},
                "CH_DOWN": {"protocol": "SAMSUNG", "code": "0xE0E008F7", "bits": 32},
                "MENU": {"protocol": "SAMSUNG", "code": "0xE0E058A7", "bits": 32},
                "UP": {"protocol": "SAMSUNG", "code": "0xE0E006F9", "bits": 32},
                "LEFT": {"protocol": "SAMSUNG", "code": "0xE0E0A659", "bits": 32},
                "RIGHT": {"protocol": "SAMSUNG", "code": "0xE0E046B9", "bits": 32},
                "DOWN": {"protocol": "SAMSUNG", "code": "0xE0E08679", "bits": 32},
                "ENTER": {"protocol": "SAMSUNG", "code": "0xE0E016E9", "bits": 32},
                "RETURN": {"protocol": "SAMSUNG", "code": "0xE0E01AE5", "bits": 32},
                "EXIT": {"protocol": "SAMSUNG", "code": "0xE0E0B44B", "bits": 32},
                "HOME": {"protocol": "SAMSUNG", "code": "0xE0E09E61", "bits": 32},
            },
        },
        "samsung": {
            "brand": "Samsung",
            "model": "Samsung TV (alias)",
            "sender_channel": "D2",
            "codes": {
                "POWER": {"protocol": "SAMSUNG", "code": "0xE0E040BF", "bits": 32},
                "SOURCE": {"protocol": "SAMSUNG", "code": "0xE0E0807F", "bits": 32},
                "MUTE": {"protocol": "SAMSUNG", "code": "0xE0E0F00F", "bits": 32},
                "VOL_UP": {"protocol": "SAMSUNG", "code": "0xE0E0E01F", "bits": 32},
                "VOL_DOWN": {"protocol": "SAMSUNG", "code": "0xE0E0D02F", "bits": 32},
                "CH_UP": {"protocol": "SAMSUNG", "code": "0xE0E048B7", "bits": 32},
                "CH_DOWN": {"protocol": "SAMSUNG", "code": "0xE0E008F7", "bits": 32},
                "MENU": {"protocol": "SAMSUNG", "code": "0xE0E058A7", "bits": 32},
                "UP": {"protocol": "SAMSUNG", "code": "0xE0E006F9", "bits": 32},
                "LEFT": {"protocol": "SAMSUNG", "code": "0xE0E0A659", "bits": 32},
                "RIGHT": {"protocol": "SAMSUNG", "code": "0xE0E046B9", "bits": 32},
                "DOWN": {"protocol": "SAMSUNG", "code": "0xE0E08679", "bits": 32},
                "ENTER": {"protocol": "SAMSUNG", "code": "0xE0E016E9", "bits": 32},
                "RETURN": {"protocol": "SAMSUNG", "code": "0xE0E01AE5", "bits": 32},
                "EXIT": {"protocol": "SAMSUNG", "code": "0xE0E0B44B", "bits": 32},
                "HOME": {"protocol": "SAMSUNG", "code": "0xE0E09E61", "bits": 32},
            },
        },
        "lg_tv_default": {
            "brand": "LG",
            "model": "LG TV (AKB74475481 starter set)",
            "sender_channel": "D2",
            "codes": {
                "POWER": {"protocol": "NEC", "code": "0x20DF10EF", "bits": 32},
                "SOURCE": {"protocol": "NEC", "code": "0x20DFD02F", "bits": 32},
                "MUTE": {"protocol": "NEC", "code": "0x20DF906F", "bits": 32},
                "VOL_UP": {"protocol": "NEC", "code": "0x20DF40BF", "bits": 32},
                "VOL_DOWN": {"protocol": "NEC", "code": "0x20DFC03F", "bits": 32},
                "CH_UP": {"protocol": "NEC", "code": "0x20DF00FF", "bits": 32},
                "CH_DOWN": {"protocol": "NEC", "code": "0x20DF807F", "bits": 32},
                "MENU": {"protocol": "NEC", "code": "0x20DFC23D", "bits": 32},
                "HOME": {"protocol": "NEC", "code": "0x20DF3EC1", "bits": 32},
                "UP": {"protocol": "NEC", "code": "0x20DF02FD", "bits": 32},
                "DOWN": {"protocol": "NEC", "code": "0x20DF827D", "bits": 32},
                "LEFT": {"protocol": "NEC", "code": "0x20DFE01F", "bits": 32},
                "RIGHT": {"protocol": "NEC", "code": "0x20DF609F", "bits": 32},
                "ENTER": {"protocol": "NEC", "code": "0x20DF22DD", "bits": 32},
                "RETURN": {"protocol": "NEC", "code": "0x20DF14EB", "bits": 32},
                "EXIT": {"protocol": "NEC", "code": "0x20DFDA25", "bits": 32},
            },
        },
        "lg": {
            "brand": "LG",
            "model": "LG TV (alias)",
            "sender_channel": "D2",
            "codes": {
                "POWER": {"protocol": "NEC", "code": "0x20DF10EF", "bits": 32},
                "SOURCE": {"protocol": "NEC", "code": "0x20DFD02F", "bits": 32},
                "MUTE": {"protocol": "NEC", "code": "0x20DF906F", "bits": 32},
                "VOL_UP": {"protocol": "NEC", "code": "0x20DF40BF", "bits": 32},
                "VOL_DOWN": {"protocol": "NEC", "code": "0x20DFC03F", "bits": 32},
                "CH_UP": {"protocol": "NEC", "code": "0x20DF00FF", "bits": 32},
                "CH_DOWN": {"protocol": "NEC", "code": "0x20DF807F", "bits": 32},
                "MENU": {"protocol": "NEC", "code": "0x20DFC23D", "bits": 32},
                "HOME": {"protocol": "NEC", "code": "0x20DF3EC1", "bits": 32},
                "UP": {"protocol": "NEC", "code": "0x20DF02FD", "bits": 32},
                "DOWN": {"protocol": "NEC", "code": "0x20DF827D", "bits": 32},
                "LEFT": {"protocol": "NEC", "code": "0x20DFE01F", "bits": 32},
                "RIGHT": {"protocol": "NEC", "code": "0x20DF609F", "bits": 32},
                "ENTER": {"protocol": "NEC", "code": "0x20DF22DD", "bits": 32},
                "RETURN": {"protocol": "NEC", "code": "0x20DF14EB", "bits": 32},
                "EXIT": {"protocol": "NEC", "code": "0x20DFDA25", "bits": 32},
            },
        },
    },
}


class SerialIrTransport:
    """Minimal NodeMCU serial transport based on the reference implementation."""

    _PORT_LOCKS: dict[str, threading.Lock] = {}
    _LOCK_GUARD = threading.Lock()

    def __init__(self, port: str, baudrate: int = 115200, timeout_seconds: float = 3.0):
        self._port = self._resolve_port(str(port or "").strip())
        self._baudrate = int(baudrate)
        self._timeout_seconds = float(timeout_seconds)
        self._serial_conn: Any = None
        self._last_used_monotonic: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._port)

    @property
    def port(self) -> str:
        return self._port

    def _port_lock(self) -> threading.Lock:
        with self._LOCK_GUARD:
            if self._port not in self._PORT_LOCKS:
                self._PORT_LOCKS[self._port] = threading.Lock()
            return self._PORT_LOCKS[self._port]

    @staticmethod
    def _resolve_port(port: str) -> str:
        if port:
            return port
        candidates = []
        for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*"):
            candidates.extend(sorted(str(path) for path in Path("/").glob(pattern.lstrip("/"))))
        if len(candidates) == 1:
            return candidates[0]
        return ""

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._port:
            return {"success": False, "error": "IR serial port is not configured"}
        try:
            import serial  # type: ignore
        except Exception:
            return {"success": False, "error": "pyserial is not installed"}

        serial_exception_type = getattr(serial, "SerialException", Exception)
        with self._port_lock():
            for attempt in range(2):
                try:
                    if attempt > 0:
                        self._close_serial_unlocked()
                    ser = self._ensure_serial_unlocked(serial)
                    start = time.monotonic()
                    legacy_cmd = str(payload.get("legacy_cmd") or "").strip()
                    expect_raw = payload.get("expect_prefixes")
                    expect_prefixes: list[str] = []
                    if isinstance(expect_raw, str):
                        expect_prefixes = [expect_raw]
                    elif isinstance(expect_raw, (list, tuple)):
                        expect_prefixes = [str(item) for item in expect_raw if str(item or "").strip()]
                    if legacy_cmd:
                        return self._request_legacy_line(
                            ser,
                            legacy_cmd,
                            expect_prefixes,
                            start,
                            float(payload.get("timeout_seconds") or self._timeout_seconds),
                        )

                    if hasattr(ser, "reset_input_buffer"):
                        try:
                            ser.reset_input_buffer()
                        except Exception:
                            pass
                    raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                    ser.write(raw)
                    ser.flush()
                    deadline = start + max(0.2, float(payload.get("timeout_seconds") or self._timeout_seconds))
                    last_text = ""
                    while time.monotonic() < deadline:
                        line = ser.readline()
                        if not line:
                            continue
                        text = line.decode(errors="replace").strip()
                        if not text:
                            continue
                        last_text = text
                        if text.startswith("{") and text.endswith("}"):
                            try:
                                parsed = json.loads(text)
                            except Exception:
                                parsed = None
                            if isinstance(parsed, dict):
                                parsed.setdefault("success", bool(parsed.get("ok", True)))
                                parsed.setdefault("raw_line", text)
                                parsed.setdefault("transport", "serial")
                                return parsed
                        upper = text.upper()
                        if upper.startswith("OK") or upper.startswith("IRSEND") or upper.startswith("IRSENT:"):
                            return {"success": True, "raw_line": text, "transport": "serial"}
                        if upper.startswith("ERR"):
                            return {"success": False, "error": text, "raw_line": text, "transport": "serial"}
                    if last_text:
                        return {"success": False, "error": f"No JSON response from NodeMCU (last line: {last_text})", "transport": "serial"}
                    return {"success": False, "error": "No response from NodeMCU over serial", "transport": "serial"}
                except serial_exception_type as exc:
                    self._close_serial_unlocked()
                    message = str(exc)
                    lowered = message.lower()
                    if ("returned no data" in lowered or "multiple access on port" in lowered) and attempt == 0:
                        time.sleep(0.2)
                        continue
                    return {"success": False, "error": f"Serial request failed: {message}"}
                except Exception as exc:
                    self._close_serial_unlocked()
                    return {"success": False, "error": f"Serial request failed: {exc}"}
        return {"success": False, "error": "Serial request failed: unknown error"}

    def _ensure_serial_unlocked(self, serial_module: Any) -> Any:
        if self._serial_conn is not None and bool(getattr(self._serial_conn, "is_open", True)):
            return self._serial_conn
        conn = serial_module.Serial(self._port, self._baudrate, timeout=0.05, write_timeout=0.2)
        for attr, value in (("dtr", False), ("rts", False)):
            try:
                setattr(conn, attr, value)
            except Exception:
                pass
        self._serial_conn = conn
        self._last_used_monotonic = time.monotonic()
        return conn

    def _close_serial_unlocked(self) -> None:
        conn = self._serial_conn
        self._serial_conn = None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass

    def _request_legacy_line(
        self,
        ser: Any,
        command: str,
        expect_prefixes: list[str],
        start: float,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        if hasattr(ser, "reset_input_buffer"):
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
        ser.write((command.rstrip("\n") + "\n").encode("utf-8"))
        ser.flush()
        deadline = start + max(0.2, timeout_seconds)
        last_text = ""
        while time.monotonic() < deadline:
            line = ser.readline()
            if not line:
                continue
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            last_text = text
            upper = text.upper()
            if upper.startswith("OK") or upper.startswith("IRSEND") or upper.startswith("IRSENT:"):
                return {"success": True, "raw_line": text, "transport": "serial"}
            if any(text.startswith(prefix) for prefix in expect_prefixes):
                return {"success": True, "raw_line": text, "transport": "serial"}
            if upper.startswith("ERR"):
                return {"success": False, "error": text, "raw_line": text, "transport": "serial"}
        if last_text:
            return {"success": False, "error": f"No response for legacy command (last line: {last_text})", "transport": "serial"}
        return {"success": False, "error": "No response from NodeMCU over serial", "transport": "serial"}


class SamsungSerialIrService:
    """Minimal Samsung IR sender with optional learned-payload dataset support."""

    _DAB_TO_SAMSUNG_KEY_MAP: dict[str, str] = {
        "PRESS_UP": "UP",
        "PRESS_DOWN": "DOWN",
        "PRESS_LEFT": "LEFT",
        "PRESS_RIGHT": "RIGHT",
        "PRESS_OK": "ENTER",
        "PRESS_BACK": "RETURN",
        "PRESS_HOME": "HOME",
        "PRESS_MENU": "MENU",
        "PRESS_INPUT": "SOURCE",
        "PRESS_POWER": "POWER",
        "PRESS_MUTE": "MUTE",
        "PRESS_VOLUME_UP": "VOL_UP",
        "PRESS_VOLUME_DOWN": "VOL_DOWN",
        "PRESS_CHANNEL_UP": "CH_UP",
        "PRESS_CHANNEL_DOWN": "CH_DOWN",
    }

    def __init__(
        self,
        dataset_path: str,
        serial_port: str,
        baudrate: int = 115200,
        timeout_seconds: float = 3.0,
        sender_channel: str = "D2",
    ):
        self._dataset_path = Path(dataset_path)
        self._ensure_dataset_exists()
        self._transport = SerialIrTransport(serial_port, baudrate=baudrate, timeout_seconds=timeout_seconds)
        self._sender_channel = str(sender_channel or "D2").strip() or "D2"

    @property
    def configured(self) -> bool:
        return self._transport.configured

    @property
    def port(self) -> str:
        return self._transport.port

    def normalize_key_name(self, key_name: str) -> str:
        raw = str(key_name or "").strip().upper().replace(" ", "_").replace("-", "_")
        if raw in self._DAB_TO_SAMSUNG_KEY_MAP:
            return self._DAB_TO_SAMSUNG_KEY_MAP[raw]
        return raw

    def send_key(self, device_id: str, key_name: str) -> dict[str, Any]:
        normalized_key = self.normalize_key_name(key_name)
        if not normalized_key:
            return {"success": False, "error": "key_name is required"}

        payload = self._lookup_dataset_payload(device_id, normalized_key)
        if isinstance(payload, dict):
            sendp = self._build_sendp_legacy_cmd(payload)
            if sendp:
                response = self._transport.request(
                    {
                        "legacy_cmd": sendp,
                        "expect_prefixes": ["IRSENT:"],
                        "timeout_seconds": 0.35,
                    }
                )
                if bool(response.get("success")):
                    return {
                        "success": True,
                        "device_id": device_id,
                        "key_name": normalized_key,
                        "sender_channel": self._sender_channel,
                        "raw": response,
                        "strategy": "legacy_sendp",
                    }

        response = self._transport.request(
            {
                "legacy_cmd": f"SENDK:samsung,{normalized_key}",
                "expect_prefixes": ["IRSENT:"],
                "timeout_seconds": 0.35,
            }
        )
        if bool(response.get("success")):
            return {
                "success": True,
                "device_id": device_id,
                "key_name": normalized_key,
                "sender_channel": self._sender_channel,
                "raw": response,
                "strategy": "legacy_sendk",
            }
        return {
            "success": False,
            "device_id": device_id,
            "key_name": normalized_key,
            "error": str(response.get("error") or "IR send failed"),
            "raw": response,
        }

    def send_key_sequence(self, device_id: str, keys: list[str], delay_seconds: float = 0.25) -> dict[str, Any]:
        results = []
        for index, key in enumerate(keys):
            result = self.send_key(device_id=device_id, key_name=key)
            results.append(result)
            if not bool(result.get("success")):
                return {"success": False, "results": results, "error": result.get("error")}
            if index < len(keys) - 1:
                time.sleep(max(0.0, delay_seconds))
        return {"success": True, "results": results}

    def _lookup_dataset_payload(self, device_id: str, key_name: str) -> dict[str, Any] | None:
        if not self._dataset_path.exists():
            return None
        try:
            payload = json.loads(self._dataset_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        devices = payload.get("devices") if isinstance(payload, dict) else None
        if not isinstance(devices, dict):
            return None

        for candidate_device_id in self._device_lookup_candidates(device_id, devices):
            record = devices.get(candidate_device_id)
            if not isinstance(record, dict):
                continue
            keys = record.get("keys") if isinstance(record.get("keys"), dict) else {}
            codes = record.get("codes") if isinstance(record.get("codes"), dict) else {}
            merged = {**codes, **keys}
            for candidate in _lookup_candidates(key_name):
                direct = merged.get(candidate)
                if isinstance(direct, dict):
                    return direct
                for candidate_key, candidate_value in merged.items():
                    if (
                        _normalized_key_token(str(candidate_key))
                        == _normalized_key_token(candidate)
                        and isinstance(candidate_value, dict)
                    ):
                        return candidate_value
        return None

    def _ensure_dataset_exists(self) -> None:
        if self._dataset_path.exists():
            return
        self._dataset_path.parent.mkdir(parents=True, exist_ok=True)
        self._dataset_path.write_text(
            json.dumps(_STANDARD_DATASET_PAYLOAD, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _device_lookup_candidates(device_id: str, devices: dict[str, Any]) -> list[str]:
        raw = str(device_id or "").strip()
        if not raw:
            return []
        candidates = [raw]
        lowered = raw.lower()
        if lowered == "samsung_tv_default":
            candidates.append("samsung")
        elif lowered == "lg_tv_default":
            candidates.append("lg")
        elif "samsung" in lowered:
            candidates.append("samsung_tv_default")
            candidates.append("samsung")
        elif lowered == "lg":
            candidates.append("lg_tv_default")
        elif "lg" in lowered:
            candidates.append("lg_tv_default")
            candidates.append("lg")
        for candidate_id, candidate_record in devices.items():
            if not isinstance(candidate_record, dict):
                continue
            brand = str(candidate_record.get("brand") or "").strip().lower()
            if "samsung" in lowered and brand == "samsung":
                candidates.append(str(candidate_id))
            if lowered.startswith("lg") and brand == "lg":
                candidates.append(str(candidate_id))
        return [item for item in dict.fromkeys(candidates) if item]

    @staticmethod
    def _build_sendp_legacy_cmd(payload: dict[str, Any]) -> str | None:
        protocol = str(payload.get("protocol") or "").strip().upper()
        code = str(payload.get("code") or "").strip()
        bits = payload.get("bits")
        if not protocol or not code:
            return None
        if bits is None or str(bits).strip() == "":
            return f"SENDP:{protocol},{code}"
        try:
            bits_int = int(bits)
        except Exception:
            bits_int = None
        if bits_int and bits_int > 0:
            return f"SENDP:{protocol},{code},{bits_int}"
        return f"SENDP:{protocol},{code}"
