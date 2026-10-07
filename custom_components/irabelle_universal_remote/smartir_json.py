"""Convert SmartIR JSON device files into portable Irabelle Universal Remote profiles.

The web converter does this in the browser. Doing the same work here lets Home
Assistant ingest a SmartIR file directly, so adding a device no longer needs a
detour through the website.

Command formats understood on input:

* ``Broadlink``  - base64 Broadlink packets (the layout SmartIR ships)
* ``Raw``        - base64 LZSS payloads of little-endian 16-bit microseconds
* ``SignedRawTimings`` - Home Assistant native signed raw timing arrays

Everything is converted to Home Assistant signed raw timings in microseconds,
which is what a profile stores. This module deliberately imports nothing from
Home Assistant so it stays unit-testable on its own.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import json
import math
from dataclasses import dataclass
from typing import Any, Callable

from .profile import (
    DEVICE_TYPE_CLIMATE,
    DEVICE_TYPE_FAN,
    DEVICE_TYPE_LIGHT,
    DEVICE_TYPE_MEDIA_PLAYER,
    InvalidProfile,
    decode_profile_code,
    profile_name,
)

# Broadlink stores durations in ticks; microseconds = ticks / BRDLNK_UNIT.
BRDLNK_UNIT = 269 / 8192
BROADLINK_TAIL_TICKS = 0x0D05

FORMAT_BROADLINK = "broadlink"
FORMAT_RAW = "raw"
FORMAT_HA = "ha"
FORMAT_PRONTO = "pronto"

PROFILE_FORMAT = "smartir-native-profile"
PROFILE_VERSION = 1
PROFILE_PREFIX = "IRP1:"

FORMAT_NAMES = {
    FORMAT_BROADLINK: "Broadlink",
    FORMAT_RAW: "Raw MQTT",
    FORMAT_HA: "HA Infrared",
    FORMAT_PRONTO: "Pronto hex",
}

# Converted-command metadata written next to the device, matching the website.
_FORMAT_META: dict[str, dict[str, Any]] = {
    FORMAT_BROADLINK: {
        "supportedController": "Home Assistant Native Infrared",
        "commandsEncoding": "SignedRawTimings",
        "commandsUnit": "microseconds",
    },
    FORMAT_RAW: {
        "supportedController": "Home Assistant Native Infrared",
        "commandsEncoding": "SignedRawTimings",
        "commandsUnit": "microseconds",
    },
    FORMAT_PRONTO: {
        "commandsEncoding": "Pronto",
    },
    FORMAT_HA: {
        "supportedController": "Home Assistant Native Infrared",
        "commandsEncoding": "SignedRawTimings",
        "commandsUnit": "microseconds",
    },
}

# A climate file normally carries a top-level "off" command. Exports sometimes
# omit it, and a missing IR code cannot be derived from the other commands, so a
# file whose commands match a known protocol is completed with that protocol's
# standard power-off frame. Keep the ratio lowish: it only guards against mixed
# files, while the frame test (header plus checksum) is highly specific.
CLIMATE_OFF_MATCH_RATIO = 0.8


class SmartIrJsonError(ValueError):
    """Raised when a SmartIR JSON file cannot be converted."""


@dataclass(frozen=True)
class OffTemplate:
    """A protocol-specific power-off frame."""

    label: str
    # Broadlink base64 of the 7-byte short turn-off frame used by the
    # AR-RAH2E/AR-REB1E/AR-RY4/AR-REW1E family; identical to the code SmartIR
    # ships for the Fujitsu AR-REW1E. The website holds the same constant.
    broadlink: str


OFF_TEMPLATES = (
    OffTemplate(
        label="the standard Fujitsu AC power-off frame",
        broadlink=(
            "JgB2AGo1Dg0ODQ8nDwwNKQ4NDwwPDQ4nDycPDA8NDg0OKA4nDw0MDw4NDg0ODQ4N"
            "Dg0ODQ4NDwwPDA8NDg0OKA4NDg0ODQ4NDg0ODQ4NDycODQ8MDwwPDQ4oDg0ODQ4N"
            "Dg0ODQ4NDigODQ4oDigOKA4oDigOJw8ADQU="
        ),
    ),
)


@dataclass(frozen=True)
class ProfileBuild:
    """The result of converting a SmartIR JSON device."""

    code: str
    device: dict[str, Any]
    source_format: str
    device_type: str
    name: str
    filled_off_label: str | None = None


def _normalize_base64(text: str) -> bytes:
    """Decode base64 the way SmartIR files expect, tolerating "=" padding drift."""
    clean = "".join(text.split()).rstrip("=")
    if len(clean) % 4 == 1:
        raise SmartIrJsonError("The base64 code has an invalid length.")
    padded = clean + "=" * (-len(clean) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as err:
        raise SmartIrJsonError(f"The base64 code could not be decoded: {err}") from err


def get_timings_from_broadlink(code: str) -> list[int]:
    """Decode a Broadlink base64 packet into microseconds."""
    packet = _normalize_base64(code)
    if len(packet) < 4 or packet[0] != 0x26 or packet[1] != 0x00:
        raise SmartIrJsonError(
            "This does not look like a Broadlink code (expected a 0x26 0x00 header)."
        )
    declared = packet[2] | (packet[3] << 8)
    if declared <= 0 or declared > len(packet) - 4:
        raise SmartIrJsonError("The Broadlink code is truncated or has a bad length.")
    payload = packet[4 : 4 + declared]

    timings: list[int] = []
    index = 0
    while index < len(payload):
        value = payload[index]
        if value == 0:
            if index + 2 >= len(payload):
                raise SmartIrJsonError("The Broadlink code ends mid-duration.")
            value = (payload[index + 1] << 8) | payload[index + 2]
            index += 3
        else:
            index += 1
        timings.append(math.ceil(value / BRDLNK_UNIT))
    return timings


def _decompress(data: bytes) -> bytes:
    """Undo the LZSS variant used by LIRC/SmartIR "Raw" payloads."""
    out = bytearray()
    index = 0
    while index < len(data):
        header = data[index]
        index += 1
        if header < 32:
            length = header + 1
            out.extend(data[index : index + length])
            index += length
        else:
            length = header >> 5
            dist_high = header & 0x1F
            if length == 7:
                length += data[index]
                index += 1
            dist = ((dist_high << 8) | data[index]) + 1
            index += 1
            for _ in range(length + 2):
                if dist > len(out):
                    # A back-reference pointing before the start means the
                    # payload is corrupt; say so instead of crashing.
                    raise SmartIrJsonError(
                        "The Raw command payload is corrupt (bad back-reference)"
                    )
                out.append(out[-dist])
    return bytes(out)


def get_timings_from_raw(code: str) -> list[int]:
    """Decode a SmartIR "Raw" payload into microseconds."""
    decompressed = _decompress(_normalize_base64(code))
    return [
        decompressed[index] | (decompressed[index + 1] << 8)
        for index in range(0, len(decompressed) - 1, 2)
    ]


def is_timing_array(value: Any) -> bool:
    """Return True for a Home Assistant signed raw timing array."""
    if not isinstance(value, list) or not value:
        return False
    return all(
        isinstance(item, int) and not isinstance(item, bool)
        for item in value
    )


def get_timings_from_ha(command: Any) -> list[int]:
    """Flatten a Home Assistant timing array into absolute microseconds."""
    if not isinstance(command, list) or not command:
        raise SmartIrJsonError("Expected a Home Assistant IR timing array.")
    timings: list[int] = []
    for item in command:
        values = item if isinstance(item, list) else [item]
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SmartIrJsonError("Expected a Home Assistant IR timing array.")
            timings.append(abs(round(value)))
    return timings


PRONTO_PERIOD_US = 0.241246


def get_timings_from_pronto(code: str) -> list[int]:
    """Decode a Pronto CCF hex code into microsecond mark/space timings.

    A Pronto code is a list of 4-hex-digit words: a format word, the carrier
    period in units of 0.241246 us, the number of "once" and "repeat" pairs,
    then the mark/space durations as counts of that carrier period.
    """
    words = str(code).split()
    if len(words) < 6 or len(words) % 2:
        raise SmartIrJsonError("A Pronto code needs at least three word pairs")
    try:
        data = [int(word, 16) for word in words]
    except ValueError as err:
        raise SmartIrJsonError("A Pronto code contains a non-hex word") from err
    if data[0] not in (0x0000, 0x0100):
        raise SmartIrJsonError(
            f"Unsupported Pronto format word 0x{data[0]:04x} (only learned codes)"
        )
    period = data[1] * PRONTO_PERIOD_US
    if period <= 0:
        raise SmartIrJsonError("A Pronto code has a zero carrier period")
    pairs = (data[2] + data[3]) * 2
    timings = [round(value * period) for value in data[4 : 4 + pairs]]
    if not timings:
        raise SmartIrJsonError("A Pronto code carries no timings")
    if len(timings) % 2:
        # A trailing mark cannot be transmitted on its own.
        timings = timings[:-1]
    return timings or [1]


def pronto_carrier_frequency(code: str) -> int | None:
    """Return the carrier frequency a Pronto code was captured at."""
    words = str(code).split()
    if len(words) < 2:
        return None
    try:
        period = int(words[1], 16) * PRONTO_PERIOD_US
    except ValueError:
        return None
    if period <= 0:
        return None
    return int(round(1_000_000 / period))


def looks_like_pronto(value: Any) -> bool:
    """True when a command string is a Pronto code."""
    if not isinstance(value, str):
        return False
    words = value.split()
    if len(words) < 6 or len(words) % 2:
        return False
    words = words[:8]
    return all(
        len(word) == 4 and all(char in "0123456789abcdefABCDEF" for char in word)
        for word in words
    )


def to_ha_timings(timings: list[int]) -> list[int]:
    """Mark/space alternating timings: even entries are marks, odd are spaces."""
    return [
        value if index % 2 == 0 else -value
        for index, value in enumerate(timings)
    ]


def detect_format(data: dict[str, Any]) -> str:
    """Detect the command encoding of a SmartIR device file."""
    if (
        data.get("commandsEncoding") == "SignedRawTimings"
        or data.get("commandsUnit") == "microseconds"
    ):
        return FORMAT_HA
    if data.get("supportedController") == "Broadlink" or data.get("commandsEncoding") == "Base64":
        return FORMAT_BROADLINK
    if data.get("supportedController") == "MQTT" or data.get("commandsEncoding") == "Raw":
        return FORMAT_RAW
    if data.get("commandsEncoding") == "Pronto" or _commands_look_like_pronto(
        data.get("commands")
    ):
        return FORMAT_PRONTO
    raise SmartIrJsonError("The SmartIR command format could not be detected.")


def _commands_look_like_pronto(node: Any) -> bool:
    """True when the command tree holds Pronto hex strings."""
    if isinstance(node, str):
        return looks_like_pronto(node)
    if isinstance(node, list):
        return any(_commands_look_like_pronto(item) for item in node)
    if isinstance(node, dict):
        return any(_commands_look_like_pronto(item) for item in node.values())
    return False


def detect_device_type(data: dict[str, Any]) -> str:
    """Detect the entity platform a SmartIR device file describes."""
    explicit = str(data.get("type") or "").strip().lower()
    if explicit == "tv":
        return DEVICE_TYPE_MEDIA_PLAYER
    if explicit in (
        DEVICE_TYPE_CLIMATE,
        DEVICE_TYPE_FAN,
        DEVICE_TYPE_LIGHT,
        DEVICE_TYPE_MEDIA_PLAYER,
    ):
        return explicit
    if isinstance(data.get("operationModes"), list) and isinstance(data.get("fanModes"), list):
        return DEVICE_TYPE_CLIMATE
    if isinstance(data.get("speed"), list):
        return DEVICE_TYPE_FAN
    if isinstance(data.get("brightness"), list) or isinstance(data.get("colorTemperature"), list):
        return DEVICE_TYPE_LIGHT
    commands = data.get("commands")
    if isinstance(commands, dict) and any(
        key in commands for key in ("brighten", "dim", "warmer", "colder", "night")
    ):
        return DEVICE_TYPE_LIGHT
    return DEVICE_TYPE_MEDIA_PLAYER


def _timings_reader(source_format: str) -> Callable[[str], list[int]]:
    if source_format == FORMAT_BROADLINK:
        return get_timings_from_broadlink
    if source_format == FORMAT_RAW:
        return get_timings_from_raw
    if source_format == FORMAT_PRONTO:
        return get_timings_from_pronto
    raise SmartIrJsonError(f"Unsupported source format: {source_format}")


def _walk_commands(
    node: Any,
    transform: Callable[[Any], Any],
    should_transform: Callable[[Any], bool],
    path: tuple[str, ...] = (),
) -> Any:
    if should_transform(node):
        try:
            return transform(node)
        except SmartIrJsonError as err:
            location = ".".join(("commands", *path))
            raise SmartIrJsonError(f"Could not convert {location}: {err}") from err
    if isinstance(node, list):
        return [
            _walk_commands(value, transform, should_transform, (*path, str(index)))
            for index, value in enumerate(node)
        ]
    if isinstance(node, dict):
        return {
            key: _walk_commands(value, transform, should_transform, (*path, str(key)))
            for key, value in node.items()
        }
    return node


def convert_device(
    data: dict[str, Any],
    *,
    source_format: str | None = None,
    device_type: str | None = None,
) -> dict[str, Any]:
    """Return a copy of a SmartIR device with commands as HA raw timings."""
    if not isinstance(data, dict) or not isinstance(data.get("commands"), dict):
        raise SmartIrJsonError("Expected a SmartIR device JSON file with a commands object.")

    resolved_format = source_format or detect_format(data)
    resolved_type = device_type or detect_device_type(data)

    if resolved_format == FORMAT_HA:
        # Commands already are Home Assistant timing arrays.
        should_transform: Callable[[Any], bool] = is_timing_array
        transform: Callable[[Any], Any] = lambda command: to_ha_timings(  # noqa: E731
            get_timings_from_ha(command)
        )
    else:
        read_timings = _timings_reader(resolved_format)
        should_transform = lambda value: isinstance(value, str) and bool(value.strip())  # noqa: E731
        transform = lambda command: to_ha_timings(read_timings(command))  # noqa: E731

    converted = dict(data)
    converted["commands"] = _walk_commands(data["commands"], transform, should_transform)
    converted.update(_FORMAT_META[resolved_format])
    converted["type"] = resolved_type
    return converted


def fujitsu_climate_frame(timings: list[int]) -> list[int] | None:
    """Return the 16 state bytes of a Fujitsu AC frame, or None.

    Fujitsu AC long frames carry a 0x14 0x63 header and a byte-wise
    sum-complement checksum, as implemented by IRremoteESP8266.
    """
    if not isinstance(timings, list) or len(timings) < 258:
        return None
    frame: list[int] = []
    for byte_index in range(16):
        value = 0
        for bit in range(8):
            # Each data bit is a mark followed by a short (0) or long (1) space.
            space = timings[3 + (byte_index * 8 + bit) * 2]
            value = (value << 1) | (1 if space >= 800 else 0)
        frame.append(_reverse_bits(value))
    if frame[0] != 0x14 or frame[1] != 0x63:
        return None
    checksum = (0x100 - sum(frame[7:15])) & 0xFF
    if frame[15] != checksum:
        return None
    return frame


def _reverse_bits(value: int) -> int:
    result = 0
    for _ in range(8):
        result = (result << 1) | (value & 1)
        value >>= 1
    return result


def find_off_template(device: dict[str, Any]) -> OffTemplate | None:
    """Find a protocol template matching every command of a converted climate device."""
    commands = device.get("commands")
    if not isinstance(commands, dict) or not commands:
        return None

    total = 0
    scores = [0] * len(OFF_TEMPLATES)

    def visit(node: Any) -> None:
        nonlocal total
        if is_timing_array(node):
            total += 1
            for index in range(len(OFF_TEMPLATES)):
                if fujitsu_climate_frame([abs(value) for value in node]) is not None:
                    scores[index] += 1
            return
        if isinstance(node, list):
            for value in node:
                visit(value)
            return
        if isinstance(node, dict):
            for value in node.values():
                visit(value)

    visit(commands)
    if not total:
        return None
    for index, template in enumerate(OFF_TEMPLATES):
        if scores[index] / total >= CLIMATE_OFF_MATCH_RATIO:
            return template
    return None


def ensure_climate_off(device: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Fill in a missing climate "off" command from a known protocol template."""
    if detect_device_type(device) != DEVICE_TYPE_CLIMATE:
        return device, None
    commands = device.get("commands")
    if not isinstance(commands, dict) or commands.get("off"):
        return device, None
    template = find_off_template(device)
    if template is None:
        return device, None
    off_timings = to_ha_timings(get_timings_from_broadlink(template.broadlink))
    return {**device, "commands": {"off": off_timings, **commands}}, template.label


def build_profile(data: dict[str, Any]) -> ProfileBuild:
    """Convert a SmartIR JSON device into a validated portable profile code."""
    if not isinstance(data, dict):
        raise SmartIrJsonError("Expected a SmartIR device JSON file.")
    if not isinstance(data.get("commands"), dict) and not isinstance(
        data.get("stepped"), dict
    ):
        # A stepped (button) air conditioner may have no matrix commands at
        # all: its codes live in the "stepped" block.
        raise SmartIrJsonError(
            "Expected a SmartIR device JSON file with a commands object."
        )
    source_format = detect_format(data)
    device_type = detect_device_type(data)
    converted = convert_device(data, source_format=source_format, device_type=device_type)
    converted, filled_off_label = ensure_climate_off(converted)

    profile = {
        "format": PROFILE_FORMAT,
        "version": PROFILE_VERSION,
        "device": converted,
    }
    payload = json.dumps(profile, separators=(",", ":")).encode()
    # mtime=0 keeps the code byte-for-byte reproducible for the same input.
    code = PROFILE_PREFIX + base64.b64encode(gzip.compress(payload, 9, mtime=0)).decode()

    try:
        validated = decode_profile_code(code)
    except InvalidProfile as err:
        raise SmartIrJsonError(f"The converted profile is not valid: {err}") from err

    return ProfileBuild(
        code=code,
        device=validated,
        source_format=source_format,
        device_type=device_type,
        name=profile_name(validated),
        filled_off_label=filled_off_label,
    )


def load_smartir_json(text: str) -> dict[str, Any]:
    """Parse SmartIR JSON text, raising a user-facing error when it is not JSON."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise SmartIrJsonError(f"The file is not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise SmartIrJsonError("Expected a SmartIR device JSON object.")
    return data
