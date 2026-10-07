"""Match received IR timings to states stored in a SmartIR profile."""

from dataclasses import dataclass
from typing import Any

FRAME_GAP_MICROSECONDS = 12_000
MIN_FRAME_TIMINGS = 8
MIN_TIMING_TOLERANCE = 150
RELATIVE_TIMING_TOLERANCE = 0.30
ON_STATE = "__on__"


@dataclass(frozen=True, slots=True)
class CommandState:
    """A timing command and the climate state it represents."""

    timings: tuple[int, ...]
    hvac_mode: str
    fan_mode: str | None = None
    swing_mode: str | None = None
    temperature: float | None = None


def build_command_states(device: dict[str, Any]) -> list[CommandState]:
    """Create a searchable list of complete climate states."""
    commands = device["commands"]
    states = [
        CommandState(timings=timings, hvac_mode="off")
        for timings in timing_commands(commands["off"])
    ]
    if "on" in commands:
        states.extend(
            CommandState(timings=timings, hvac_mode=ON_STATE)
            for timings in timing_commands(commands["on"])
        )
    swing_modes = device.get("swingModes")

    for hvac_mode in device["operationModes"]:
        mode_commands = commands.get(hvac_mode, {})
        for fan_mode in device["fanModes"]:
            fan_commands = mode_commands.get(fan_mode)
            if not isinstance(fan_commands, dict):
                continue
            if swing_modes:
                for swing_mode in swing_modes:
                    swing_commands = fan_commands.get(swing_mode)
                    if isinstance(swing_commands, dict):
                        states.extend(
                            _temperature_states(
                                swing_commands,
                                hvac_mode,
                                fan_mode,
                                swing_mode,
                            )
                        )
            else:
                states.extend(
                    _temperature_states(
                        fan_commands,
                        hvac_mode,
                        fan_mode,
                        None,
                    )
                )
    return states


def find_command_state(
    received_timings: list[int],
    states: list[CommandState],
    *,
    current_hvac_mode: str,
    current_fan_mode: str | None,
    current_swing_mode: str | None,
    current_temperature: float | None,
) -> CommandState | None:
    """Find the best state represented by a received raw IR signal."""
    matches = [
        state for state in states if signals_match(state.timings, received_timings)
    ]
    if not matches:
        return None
    return min(
        matches,
        key=lambda state: _state_distance(
            state,
            current_hvac_mode,
            current_fan_mode,
            current_swing_mode,
            current_temperature,
        ),
    )


def signals_match(expected: tuple[int, ...], received: list[int]) -> bool:
    """Compare noisy captures, allowing repeat frames and timing variance."""
    expected_frames = _signal_frames(expected)
    received_frames = _signal_frames(tuple(received))
    return any(
        _frame_matches(expected_frame, received_frame)
        for expected_frame in expected_frames
        for received_frame in received_frames
    )


def _temperature_states(
    commands: dict[str, Any],
    hvac_mode: str,
    fan_mode: str,
    swing_mode: str | None,
) -> list[CommandState]:
    """Expand the temperature leaves under one mode/fan/swing branch."""
    states: list[CommandState] = []
    for temperature_key, command in commands.items():
        try:
            temperature = float(temperature_key)
        except (TypeError, ValueError):
            continue
        states.extend(
            CommandState(
                timings=timings,
                hvac_mode=hvac_mode,
                fan_mode=fan_mode,
                swing_mode=swing_mode,
                temperature=temperature,
            )
            for timings in timing_commands(command)
        )
    return states


def timing_commands(value: Any) -> list[tuple[int, ...]]:
    """Return one timing command or a list of sequential timing commands."""
    if value and isinstance(value, list) and isinstance(value[0], int):
        return [tuple(value)]
    if isinstance(value, list):
        commands: list[tuple[int, ...]] = []
        for item in value:
            commands.extend(timing_commands(item))
        return commands
    return []


def _signal_frames(timings: tuple[int, ...]) -> list[tuple[int, ...]]:
    """Split repeated transmissions on long spaces and drop trailing gaps."""
    frames: list[tuple[int, ...]] = []
    current: list[int] = []
    for timing in timings:
        if timing < 0 and abs(timing) >= FRAME_GAP_MICROSECONDS:
            if len(current) >= MIN_FRAME_TIMINGS:
                frames.append(tuple(current))
            current = []
        else:
            current.append(timing)
    if len(current) >= MIN_FRAME_TIMINGS:
        frames.append(tuple(current))
    if frames:
        return frames
    trimmed = list(timings)
    if trimmed and trimmed[-1] < 0:
        trimmed.pop()
    return [tuple(trimmed)] if trimmed else []


def _frame_matches(expected: tuple[int, ...], received: tuple[int, ...]) -> bool:
    """Compare one frame with bounded absolute and relative tolerance."""
    if abs(len(expected) - len(received)) > 1:
        return False
    shorter, longer = sorted((expected, received), key=len)
    if len(shorter) != len(longer) and longer[-1] >= 0:
        return False
    for expected_timing, received_timing in zip(expected, received, strict=False):
        if (expected_timing > 0) != (received_timing > 0):
            return False
        tolerance = max(
            MIN_TIMING_TOLERANCE,
            abs(expected_timing) * RELATIVE_TIMING_TOLERANCE,
        )
        if abs(abs(expected_timing) - abs(received_timing)) > tolerance:
            return False
    return True


def _state_distance(
    state: CommandState,
    hvac_mode: str,
    fan_mode: str | None,
    swing_mode: str | None,
    temperature: float | None,
) -> float:
    """Prefer the current value when several states share one IR command."""
    score = 0.0
    if state.hvac_mode != hvac_mode:
        score += 8
    if state.fan_mode is not None and state.fan_mode != fan_mode:
        score += 4
    if state.swing_mode is not None and state.swing_mode != swing_mode:
        score += 2
    if state.temperature is not None and temperature is not None:
        score += abs(state.temperature - temperature)
    return score
