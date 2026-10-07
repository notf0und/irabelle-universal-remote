"""Light platform for Irabelle Universal Remote."""

import asyncio
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_INFRARED_ENTITY_ID,
    CONF_INFRARED_RECEIVER_ENTITY_ID,
    CONF_NAME,
    CONF_PROFILE_CODE,
    DOMAIN,
)
from .emitter_base import (
    SmartIrNativeReceiverEntity,
    carrier_frequency_for_entry,
    find_command_value,
    timing_scale_for_entry,
)
from .profile import decode_profile_code
from .receiver import timing_commands

STEP_COMMAND_DELAY = 0.3


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one light entity from its stored profile code."""
    data = await hass.async_add_executor_job(
        decode_profile_code, entry.data[CONF_PROFILE_CODE]
    )
    async_add_entities([SmartIrNativeLight(entry, data)])


class SmartIrNativeLight(SmartIrNativeReceiverEntity, LightEntity, RestoreEntity):
    """A light using SmartIR's relative brightness and color commands."""

    def __init__(self, entry: ConfigEntry, data: dict[str, Any]) -> None:
        """Initialize the entity from validated profile data."""
        super().__init__(
            entry.options.get(
                CONF_INFRARED_ENTITY_ID,
                entry.data[CONF_INFRARED_ENTITY_ID],
            ),
            entry.options.get(
                CONF_INFRARED_RECEIVER_ENTITY_ID,
                entry.data.get(CONF_INFRARED_RECEIVER_ENTITY_ID),
            ),
            carrier_frequency_for_entry(entry),
            timing_scale_for_entry(entry),
        )
        self._commands = data["commands"]
        self._brightness_levels = [int(level) for level in data.get("brightness", [])]
        self._color_temperatures = [
            int(level) for level in data.get("colorTemperature", [])
        ]
        self._power_command_is_toggle = self._commands_are_identical(
            find_command_value(self._commands, "on"),
            find_command_value(self._commands, "off"),
        )
        self._send_lock = asyncio.Lock()

        self._attr_unique_id = f"{entry.entry_id}_light"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data[CONF_NAME],
            manufacturer=data.get("manufacturer"),
            model=", ".join(data.get("supportedModels", [])),
        )
        self._attr_is_on = False
        self._attr_brightness = (
            self._brightness_levels[-1] if self._brightness_levels else None
        )
        self._attr_color_temp_kelvin = (
            self._color_temperatures[-1] if self._color_temperatures else None
        )

        self._supports_brightness = bool(
            self._brightness_levels
            and find_command_value(self._commands, "brighten") is not None
            and find_command_value(self._commands, "dim") is not None
        ) or find_command_value(self._commands, "night") is not None
        self._supports_color_temperature = bool(
            self._color_temperatures
            and find_command_value(self._commands, "warmer") is not None
            and find_command_value(self._commands, "colder") is not None
        )

        if self._supports_color_temperature:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_min_color_temp_kelvin = self._color_temperatures[0]
            self._attr_max_color_temp_kelvin = self._color_temperatures[-1]
        elif self._supports_brightness:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS
        else:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
            self._attr_color_mode = ColorMode.ONOFF
        self._set_receiver_commands(self._build_receiver_commands())

    async def async_added_to_hass(self) -> None:
        """Restore the assumed state and start receiver tracking."""
        await super().async_added_to_hass()
        if (state := await self.async_get_last_state()) is None:
            return
        self._attr_is_on = state.state == "on"
        if self._supports_brightness:
            brightness = state.attributes.get(ATTR_BRIGHTNESS)
            if isinstance(brightness, int | float):
                self._attr_brightness = self._nearest_level(
                    self._brightness_levels, int(brightness)
                )
        if self._supports_color_temperature:
            kelvin = state.attributes.get(ATTR_COLOR_TEMP_KELVIN)
            if isinstance(kelvin, int | float):
                self._attr_color_temp_kelvin = self._nearest_level(
                    self._color_temperatures, int(kelvin)
                )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on and apply relative SmartIR brightness/color steps."""
        async with self._send_lock:
            on_command = find_command_value(self._commands, "on")
            sent_command = False
            if not self._attr_is_on and on_command is not None:
                await self._send_value(on_command)
                sent_command = True

            if (
                ATTR_COLOR_TEMP_KELVIN in kwargs
                and self._supports_color_temperature
            ):
                await self._set_color_temperature(
                    int(kwargs[ATTR_COLOR_TEMP_KELVIN])
                )
                sent_command = True

            if ATTR_BRIGHTNESS in kwargs and self._supports_brightness:
                await self._set_brightness(int(kwargs[ATTR_BRIGHTNESS]))
                sent_command = True

            if not sent_command:
                if on_command is None:
                    raise HomeAssistantError(
                        "SmartIR light profile has no usable turn-on command"
                    )
                if not self._power_command_is_toggle or not self._attr_is_on:
                    await self._send_value(on_command)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        del kwargs
        command = find_command_value(self._commands, "off")
        if command is None:
            raise HomeAssistantError("SmartIR light profile has no off command")
        if self._power_command_is_toggle and not self._attr_is_on:
            return
        await self._send_value(command)
        self._attr_is_on = False
        self.async_write_ha_state()

    async def _set_brightness(self, requested: int) -> None:
        """Translate an absolute HA brightness to repeated SmartIR step commands."""
        if requested <= 1 and (
            night := find_command_value(self._commands, "night")
        ) is not None:
            await self._send_value(night)
            self._attr_brightness = 1
            return
        if not self._brightness_levels:
            return
        target = self._nearest_level(
            self._brightness_levels, max(1, min(255, requested))
        )
        current = self._nearest_level(
            self._brightness_levels,
            self._attr_brightness or self._brightness_levels[-1],
        )
        old_index = self._brightness_levels.index(current)
        new_index = self._brightness_levels.index(target)
        difference = new_index - old_index
        if difference == 0:
            self._attr_brightness = target
            return
        command = find_command_value(
            self._commands, "brighten" if difference > 0 else "dim"
        )
        if command is None:
            raise HomeAssistantError("SmartIR light lacks brightness step commands")
        count = abs(difference)
        if new_index in (0, len(self._brightness_levels) - 1):
            count = len(self._brightness_levels)
        await self._send_repeated(command, count)
        self._attr_brightness = target

    async def _set_color_temperature(self, requested: int) -> None:
        """Translate an absolute Kelvin target to warmer/colder commands."""
        target = self._nearest_level(self._color_temperatures, requested)
        current = self._nearest_level(
            self._color_temperatures,
            self._attr_color_temp_kelvin or self._color_temperatures[-1],
        )
        old_index = self._color_temperatures.index(current)
        new_index = self._color_temperatures.index(target)
        difference = new_index - old_index
        if difference == 0:
            self._attr_color_temp_kelvin = target
            return
        command = find_command_value(
            self._commands, "colder" if difference > 0 else "warmer"
        )
        if command is None:
            raise HomeAssistantError("SmartIR light lacks color step commands")
        count = abs(difference)
        if new_index in (0, len(self._color_temperatures) - 1):
            count = len(self._color_temperatures)
        await self._send_repeated(command, count)
        self._attr_color_temp_kelvin = target

    async def _send_repeated(self, command: Any, count: int) -> None:
        """Send a relative remote command enough times to reach a target step."""
        for index in range(count):
            await self._send_value(command)
            if index + 1 < count:
                await asyncio.sleep(STEP_COMMAND_DELAY)

    @staticmethod
    def _nearest_level(levels: list[int], requested: int) -> int:
        """Return the closest configured SmartIR level."""
        return min(levels, key=lambda level: abs(level - requested))

    def _build_receiver_commands(self) -> dict[str, Any]:
        """Index absolute and relative light commands for receiver matching."""
        receiver_commands: dict[str, Any] = {}
        if self._power_command_is_toggle:
            receiver_commands["toggle"] = find_command_value(self._commands, "on")
            power_keys: tuple[str, ...] = ()
        else:
            power_keys = ("on", "off")
        for key in (*power_keys, "brighten", "dim", "warmer", "colder", "night"):
            command = find_command_value(self._commands, key)
            if command is not None:
                receiver_commands[key] = command
        return receiver_commands

    def _handle_matched_receiver_command(self, key: str) -> bool:
        """Apply one known physical-remote light command."""
        old_state = (
            self._attr_is_on,
            self._attr_brightness,
            self._attr_color_temp_kelvin,
        )
        if key == "toggle":
            self._attr_is_on = not self._attr_is_on
        elif key == "off":
            self._attr_is_on = False
        elif key == "on":
            self._attr_is_on = True
        elif key == "night":
            self._attr_is_on = True
            self._attr_brightness = 1
        elif key in ("brighten", "dim") and self._brightness_levels:
            self._attr_is_on = True
            self._attr_brightness = self._move_level(
                self._brightness_levels,
                self._attr_brightness or self._brightness_levels[-1],
                1 if key == "brighten" else -1,
            )
        elif key in ("warmer", "colder") and self._color_temperatures:
            self._attr_is_on = True
            self._attr_color_temp_kelvin = self._move_level(
                self._color_temperatures,
                self._attr_color_temp_kelvin or self._color_temperatures[-1],
                1 if key == "colder" else -1,
            )
        return old_state != (
            self._attr_is_on,
            self._attr_brightness,
            self._attr_color_temp_kelvin,
        )

    @staticmethod
    def _commands_are_identical(first: Any, second: Any) -> bool:
        """Return whether on and off contain the same timing sequence."""
        first_timings = timing_commands(first)
        return bool(first_timings) and first_timings == timing_commands(second)

    @classmethod
    def _move_level(cls, levels: list[int], current: int, change: int) -> int:
        """Move one step through a SmartIR level list with bounds."""
        index = levels.index(cls._nearest_level(levels, current))
        return levels[max(0, min(len(levels) - 1, index + change))]
