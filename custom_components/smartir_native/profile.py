"""Decode and validate portable Irabelle Universal Remote profile codes."""

import base64
import binascii
import json
import zlib
from typing import Any

PROFILE_PREFIX = "IRP1:"
PROFILE_FORMAT = "smartir-native-profile"
MAX_COMPRESSED_SIZE = 2 * 1024 * 1024
MAX_DECOMPRESSED_SIZE = 16 * 1024 * 1024
MAX_TIMING = 10_000_000
VALID_HVAC_MODES = {"auto", "cool", "dry", "fan_only", "heat", "heat_cool"}
DEVICE_TYPE_CLIMATE = "climate"
DEVICE_TYPE_FAN = "fan"
DEVICE_TYPE_LIGHT = "light"
DEVICE_TYPE_MEDIA_PLAYER = "media_player"
DEVICE_TYPE_TV = "tv"
VALID_DEVICE_TYPES = {
    DEVICE_TYPE_CLIMATE,
    DEVICE_TYPE_FAN,
    DEVICE_TYPE_LIGHT,
    DEVICE_TYPE_MEDIA_PLAYER,
    DEVICE_TYPE_TV,
}


class InvalidProfile(ValueError):
    """Raised when a profile code is invalid or unsupported."""


def decode_profile_code(profile_code: str) -> dict[str, Any]:
    """Decode a versioned, gzip-compressed Irabelle Universal Remote profile."""
    clean = "".join(profile_code.split())
    if not clean.startswith(PROFILE_PREFIX):
        raise InvalidProfile("Unsupported profile code version")
    encoded = clean.removeprefix(PROFILE_PREFIX)
    if not encoded or len(encoded) > (MAX_COMPRESSED_SIZE * 4 // 3) + 8:
        raise InvalidProfile("Profile code is empty or too large")

    try:
        compressed = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as err:
        raise InvalidProfile("Profile code is not valid base64") from err

    if not compressed or len(compressed) > MAX_COMPRESSED_SIZE:
        raise InvalidProfile("Profile code is empty or too large")

    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        raw = decompressor.decompress(compressed, MAX_DECOMPRESSED_SIZE + 1)
    except zlib.error as err:
        raise InvalidProfile("Profile code could not be decompressed") from err
    if (
        len(raw) > MAX_DECOMPRESSED_SIZE
        or decompressor.unconsumed_tail
        or decompressor.unused_data
        or not decompressor.eof
    ):
        raise InvalidProfile("Decompressed profile is too large or incomplete")

    try:
        profile = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise InvalidProfile("Profile does not contain valid JSON") from err

    if (
        not isinstance(profile, dict)
        or profile.get("format") != PROFILE_FORMAT
        or profile.get("version") != 1
        or not isinstance(profile.get("device"), dict)
    ):
        raise InvalidProfile("Profile structure or version is not supported")

    device = profile["device"]
    _validate_device(device)
    return device


def profile_name(device: dict[str, Any]) -> str:
    """Build a useful default entity name from profile metadata."""
    manufacturer = str(device.get("manufacturer") or "SmartIR").strip()
    models = device.get("supportedModels") or []
    if models:
        model = str(models[0]).strip()
    else:
        model = detect_device_type(device).replace("_", " ").title()
    return f"{manufacturer} {model}".strip()


def detect_device_type(device: dict[str, Any]) -> str:
    """Detect the SmartIR device type from explicit or structural hints."""
    explicit = str(device.get("type") or "").strip().lower()
    if explicit in VALID_DEVICE_TYPES:
        return (
            DEVICE_TYPE_MEDIA_PLAYER
            if explicit == DEVICE_TYPE_TV
            else explicit
        )
    if (
        isinstance(device.get("operationModes"), list)
        and isinstance(device.get("fanModes"), list)
    ):
        return DEVICE_TYPE_CLIMATE
    if isinstance(device.get("speed"), list):
        return DEVICE_TYPE_FAN
    if isinstance(device.get("brightness"), list) or isinstance(
        device.get("colorTemperature"), list
    ):
        return DEVICE_TYPE_LIGHT
    return DEVICE_TYPE_MEDIA_PLAYER


def _validate_device(device: dict[str, Any]) -> None:
    """Validate the SmartIR metadata and command tree used by the entity."""
    commands = device.get("commands")
    if isinstance(device.get("stepped"), dict):
        _validate_stepped_device(device)
        return
    if not isinstance(commands, dict) or not commands:
        raise InvalidProfile("Profile must contain a commands object")
    device_type = detect_device_type(device)
    if device_type == DEVICE_TYPE_CLIMATE:
        _validate_climate_device(device)
    elif device_type == DEVICE_TYPE_FAN:
        _validate_fan_device(device)
    elif device_type == DEVICE_TYPE_LIGHT:
        _validate_light_device(device)
    else:
        _validate_media_player_device(device)
    _validate_command_node(commands)


def _validate_stepped_device(device: dict[str, Any]) -> None:
    """Validate a button air conditioner: its codes live in "stepped"."""
    step = device["stepped"]
    _validate_models(device)
    if not isinstance(step.get("tempUp"), list) or not isinstance(
        step.get("tempDown"), list
    ):
        raise InvalidProfile("A stepped profile needs tempUp and tempDown lists")
    if not isinstance(step.get("modes"), list):
        raise InvalidProfile("A stepped profile needs a modes list")
    for name in ("on", "off"):
        code = step.get(name)
        if code is not None and (
            not isinstance(code, list)
            or not code
            or not all(isinstance(value, int) for value in code)
        ):
            raise InvalidProfile(f"Invalid {name} code in a stepped profile")
    if not (step.get("modes") or step.get("tempUp") or step.get("on") or step.get("off")):
        raise InvalidProfile("A stepped profile needs at least one button")
    commands = device.get("commands")
    if isinstance(commands, dict) and commands:
        _validate_command_node(commands)


def _validate_models(device: dict[str, Any]) -> None:
    """Validate supported model values when present."""
    models = device.get("supportedModels")
    if models is None:
        return
    if not isinstance(models, list) or not models:
        raise InvalidProfile("Missing or empty field: supportedModels")
    if not all(isinstance(item, str) and item for item in models):
        raise InvalidProfile("Invalid values in field: supportedModels")


def _validate_climate_device(device: dict[str, Any]) -> None:
    """Validate SmartIR climate-specific fields."""
    _validate_models(device)
    for key in ("operationModes", "fanModes"):
        value = device.get(key)
        if not isinstance(value, list) or not value:
            raise InvalidProfile(f"Missing or empty field: {key}")
        if not all(isinstance(item, str) and item for item in value):
            raise InvalidProfile(f"Invalid values in field: {key}")
    if not set(device["operationModes"]).issubset(VALID_HVAC_MODES):
        raise InvalidProfile("Profile contains an unsupported HVAC mode")
    for key in ("minTemperature", "maxTemperature", "precision"):
        if isinstance(device.get(key), bool) or not isinstance(
            device.get(key), int | float
        ):
            raise InvalidProfile(f"Missing or invalid field: {key}")
    if device["minTemperature"] >= device["maxTemperature"]:
        raise InvalidProfile("Minimum temperature must be below maximum temperature")
    if (swing_modes := device.get("swingModes")) is not None and (
        not isinstance(swing_modes, list)
        or not swing_modes
        or not all(isinstance(item, str) and item for item in swing_modes)
    ):
        raise InvalidProfile("Profile contains invalid swing modes")
    if "off" not in device["commands"] and not isinstance(
        device.get("stepped"), dict
    ):
        # A stepped (button) air conditioner may have no power button at all:
        # its mode or temperature codes are then the only way to drive it.
        raise InvalidProfile("Profile must contain an off command")


def _validate_fan_device(device: dict[str, Any]) -> None:
    """Validate SmartIR fan-specific fields."""
    _validate_models(device)
    speed = device.get("speed")
    if not isinstance(speed, list) or not speed:
        raise InvalidProfile("Missing or empty field: speed")
    if not all(isinstance(item, str) and item for item in speed):
        raise InvalidProfile("Invalid values in field: speed")
    if "off" not in device["commands"] and not isinstance(
        device.get("stepped"), dict
    ):
        # A stepped (button) air conditioner may have no power button at all:
        # its mode or temperature codes are then the only way to drive it.
        raise InvalidProfile("Profile must contain an off command")
    speed_trees = [
        device["commands"].get(key)
        for key in ("default", "forward", "reverse")
        if isinstance(device["commands"].get(key), dict)
    ]
    if not speed_trees:
        raise InvalidProfile("Fan profile does not contain a speed command tree")
    if any(
        not any(str(speed_value) in tree for tree in speed_trees)
        for speed_value in speed
    ):
        raise InvalidProfile("Fan profile is missing one or more speed commands")


def _validate_light_device(device: dict[str, Any]) -> None:
    """Validate SmartIR light-specific fields."""
    _validate_models(device)
    for key in ("brightness", "colorTemperature"):
        values = device.get(key)
        if values is None:
            continue
        if not isinstance(values, list) or not values:
            raise InvalidProfile(f"Missing or empty field: {key}")
        if not all(
            isinstance(item, int | float) and not isinstance(item, bool)
            for item in values
        ):
            raise InvalidProfile(f"Invalid values in field: {key}")
        if values != sorted(values) or len(values) != len(set(values)):
            raise InvalidProfile(f"Field must be sorted with unique values: {key}")
    if "on" not in device["commands"] or "off" not in device["commands"]:
        raise InvalidProfile("Light profile must contain both on and off commands")


def _validate_media_player_device(device: dict[str, Any]) -> None:
    """Validate SmartIR media-player/tv fields."""
    _validate_models(device)
    if "on" not in device["commands"] and "off" not in device["commands"]:
        raise InvalidProfile("Profile must contain an on or off command")


def _validate_command_node(node: Any) -> None:
    """Ensure every command leaf is a bounded signed timing array."""
    if isinstance(node, dict):
        if not node:
            raise InvalidProfile("Command tree contains an empty object")
        for value in node.values():
            _validate_command_node(value)
        return

    if not isinstance(node, list) or not node:
        raise InvalidProfile("Command tree contains an invalid value")
    if all(isinstance(value, int) and not isinstance(value, bool) for value in node):
        if any(value == 0 or abs(value) > MAX_TIMING for value in node):
            raise InvalidProfile("Timing array contains an invalid duration")
        return
    for value in node:
        _validate_command_node(value)
