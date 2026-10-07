"""Fan platform for Irabelle Universal Remote."""

from typing import Any

from homeassistant.components.fan import (
    ATTR_DIRECTION,
    ATTR_OSCILLATING,
    ATTR_PERCENTAGE,
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    FanEntity,
    FanEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one fan entity from its stored profile code."""
    data = await hass.async_add_executor_job(
        decode_profile_code, entry.data[CONF_PROFILE_CODE]
    )
    async_add_entities([SmartIrNativeFan(entry, data)])


class SmartIrNativeFan(SmartIrNativeReceiverEntity, FanEntity, RestoreEntity):
    """A fan using the command-tree layout from official SmartIR files."""

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
        self._speed_steps = [str(item) for item in data["speed"]]
        self._current_speed: str | None = None
        self._last_on_speed = self._speed_steps[0]

        self._attr_unique_id = f"{entry.entry_id}_fan"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data[CONF_NAME],
            manufacturer=data.get("manufacturer"),
            model=", ".join(data.get("supportedModels", [])),
        )
        self._attr_is_on = False
        self._attr_percentage = 0
        self._attr_speed_count = len(self._speed_steps)
        self._attr_oscillating = False
        self._attr_current_direction = None

        features = (
            FanEntityFeature.TURN_ON
            | FanEntityFeature.TURN_OFF
            | FanEntityFeature.SET_SPEED
        )
        if all(
            isinstance(find_command_value(self._commands, direction), dict)
            for direction in (DIRECTION_FORWARD, DIRECTION_REVERSE)
        ):
            features |= FanEntityFeature.DIRECTION
            self._attr_current_direction = DIRECTION_FORWARD
        if find_command_value(self._commands, "oscillate") is not None:
            features |= FanEntityFeature.OSCILLATE
        self._attr_supported_features = features
        self._set_receiver_commands(self._build_receiver_commands())

    async def async_added_to_hass(self) -> None:
        """Restore the assumed state and start receiver tracking."""
        await super().async_added_to_hass()
        if (state := await self.async_get_last_state()) is None:
            return
        percentage = state.attributes.get(ATTR_PERCENTAGE)
        if isinstance(percentage, int | float) and percentage > 0:
            self._set_speed_state(
                percentage_to_ordered_list_item(self._speed_steps, percentage)
            )
        else:
            self._set_off_state()
        if (
            self._attr_supported_features & FanEntityFeature.DIRECTION
            and state.attributes.get(ATTR_DIRECTION)
            in (DIRECTION_FORWARD, DIRECTION_REVERSE)
        ):
            self._attr_current_direction = state.attributes[ATTR_DIRECTION]
        if self._attr_supported_features & FanEntityFeature.OSCILLATE:
            self._attr_oscillating = bool(
                state.attributes.get(ATTR_OSCILLATING, False)
            )

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn on at the requested or most recently used speed."""
        if (
            direction := kwargs.get(ATTR_DIRECTION)
        ) is not None and self._attr_supported_features & FanEntityFeature.DIRECTION:
            self._attr_current_direction = direction
        if preset_mode is not None:
            if preset_mode not in self._speed_steps:
                raise HomeAssistantError("Unsupported SmartIR fan speed")
            speed = preset_mode
        elif percentage is not None:
            if percentage <= 0:
                await self.async_turn_off()
                return
            speed = percentage_to_ordered_list_item(
                self._speed_steps, percentage
            )
        else:
            speed = self._last_on_speed
        await self._send_speed_command(speed)
        self._set_speed_state(speed)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the fan."""
        del kwargs
        command = find_command_value(self._commands, "off")
        if command is None:
            raise HomeAssistantError("SmartIR fan profile has no off command")
        await self._send_value(command)
        self._set_off_state()
        self.async_write_ha_state()

    async def async_set_percentage(self, percentage: int) -> None:
        """Map Home Assistant's percentage to a SmartIR speed label."""
        if percentage <= 0:
            await self.async_turn_off()
            return
        await self.async_turn_on(percentage=max(1, min(100, percentage)))

    async def async_set_direction(self, direction: str) -> None:
        """Select the forward or reverse SmartIR command tree."""
        if not self._attr_supported_features & FanEntityFeature.DIRECTION:
            raise HomeAssistantError("This SmartIR fan does not support direction")
        if direction not in (DIRECTION_FORWARD, DIRECTION_REVERSE):
            raise HomeAssistantError("Unsupported fan direction")
        self._attr_current_direction = direction
        if self._attr_is_on and self._current_speed is not None:
            await self._send_speed_command(self._current_speed)
        self.async_write_ha_state()

    async def async_oscillate(self, oscillating: bool) -> None:
        """Send the fan's oscillation toggle command."""
        command = find_command_value(self._commands, "oscillate")
        if command is None:
            raise HomeAssistantError("This SmartIR fan does not support oscillation")
        if self._attr_oscillating != oscillating:
            await self._send_value(command)
            self._attr_oscillating = oscillating
            self.async_write_ha_state()

    async def _send_speed_command(self, speed: str) -> None:
        """Send the speed from default, forward, or reverse command trees."""
        command = self._resolve_speed_command(speed)
        if command is None:
            raise HomeAssistantError(
                f"SmartIR fan profile has no command for speed '{speed}'"
            )
        await self._send_value(command)

    def _resolve_speed_command(self, speed: str) -> Any:
        """Resolve a speed exactly as the original SmartIR fan platform does."""
        direction = self._attr_current_direction or "default"
        tree = find_command_value(self._commands, direction)
        if isinstance(tree, dict):
            command = find_command_value(tree, speed)
            if command is not None:
                return command
        default_tree = find_command_value(self._commands, "default")
        if isinstance(default_tree, dict):
            command = find_command_value(default_tree, speed)
            if command is not None:
                return command
        for fallback_direction in (DIRECTION_FORWARD, DIRECTION_REVERSE):
            fallback_tree = find_command_value(self._commands, fallback_direction)
            if isinstance(fallback_tree, dict):
                command = find_command_value(fallback_tree, speed)
                if command is not None:
                    return command
        return None

    def _set_speed_state(self, speed: str) -> None:
        """Update all Home Assistant speed attributes consistently."""
        self._current_speed = speed
        self._last_on_speed = speed
        self._attr_percentage = ordered_list_item_to_percentage(
            self._speed_steps, speed
        )
        self._attr_is_on = True

    def _set_off_state(self) -> None:
        """Update all Home Assistant power attributes consistently."""
        self._current_speed = None
        self._attr_percentage = 0
        self._attr_is_on = False

    def _build_receiver_commands(self) -> dict[str, Any]:
        """Index all complete fan commands for receiver matching."""
        receiver_commands: dict[str, Any] = {}
        if (off := find_command_value(self._commands, "off")) is not None:
            receiver_commands["off"] = off
        if (oscillate := find_command_value(self._commands, "oscillate")) is not None:
            receiver_commands["oscillate"] = oscillate
        for direction_key in ("default", DIRECTION_FORWARD, DIRECTION_REVERSE):
            tree = find_command_value(self._commands, direction_key)
            if not isinstance(tree, dict):
                continue
            for speed in self._speed_steps:
                command = find_command_value(tree, speed)
                if command is not None:
                    receiver_commands[f"{direction_key}:{speed}"] = command
        return receiver_commands

    def _handle_matched_receiver_command(self, key: str) -> bool:
        """Update fan state from a known physical-remote command."""
        old_state = (
            self._attr_is_on,
            self._attr_percentage,
            self._attr_current_direction,
            self._attr_oscillating,
        )
        if key == "off":
            self._set_off_state()
        elif key == "oscillate":
            self._attr_oscillating = not self._attr_oscillating
        elif ":" in key:
            direction, speed = key.split(":", 1)
            if direction in (DIRECTION_FORWARD, DIRECTION_REVERSE):
                self._attr_current_direction = direction
            self._set_speed_state(speed)
        return old_state != (
            self._attr_is_on,
            self._attr_percentage,
            self._attr_current_direction,
            self._attr_oscillating,
        )
