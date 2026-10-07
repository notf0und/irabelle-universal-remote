"""Climate platform for Irabelle Universal Remote."""

import asyncio
import logging
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.components.infrared import (
    InfraredReceivedSignal,
    async_subscribe_receiver,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, STATE_UNAVAILABLE, UnitOfTemperature
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_HUMIDITY_SENSOR,
    CONF_POWER_SENSOR,
    CONF_POWER_SENSOR_RESTORE_STATE,
    CONF_INFRARED_ENTITY_ID,
    CONF_INFRARED_RECEIVER_ENTITY_ID,
    CONF_NAME,
    CONF_PROFILE_CODE,
    CONF_TEMPERATURE_SENSOR,
    DOMAIN,
)
from .emitter_base import (
    SmartIrNativeEmitterEntity,
    carrier_frequency_for_entry,
    timing_scale_for_entry,
)
from .profile import decode_profile_code
from .receiver import ON_STATE, build_command_states, find_command_state

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one climate entity from its stored profile code."""
    data = await hass.async_add_executor_job(
        decode_profile_code, entry.data[CONF_PROFILE_CODE]
    )
    async_add_entities([SmartIrNativeClimate(entry, data)])


class SmartIrNativeClimate(SmartIrNativeEmitterEntity, ClimateEntity, RestoreEntity):
    """A native Infrared climate entity driven by SmartIR command data."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, entry: ConfigEntry, data: dict[str, Any]) -> None:
        """Initialize the entity from validated profile data."""
        infrared_emitter_entity_id = entry.options.get(
            CONF_INFRARED_ENTITY_ID,
            entry.data[CONF_INFRARED_ENTITY_ID],
        )
        super().__init__(
            infrared_emitter_entity_id,
            carrier_frequency_for_entry(entry),
            timing_scale_for_entry(entry),
        )
        self._infrared_receiver_entity_id = entry.options.get(
            CONF_INFRARED_RECEIVER_ENTITY_ID,
            entry.data.get(CONF_INFRARED_RECEIVER_ENTITY_ID),
        )
        # Optional room sensors, as upstream SmartIR offers: they drive
        # current_temperature / current_humidity on the climate card.
        self._temperature_sensor = entry.options.get(
            CONF_TEMPERATURE_SENSOR, entry.data.get(CONF_TEMPERATURE_SENSOR)
        )
        self._humidity_sensor = entry.options.get(
            CONF_HUMIDITY_SENSOR, entry.data.get(CONF_HUMIDITY_SENSOR)
        )
        # A binary sensor that reports whether the appliance is really on, so
        # the assumed state follows reality (upstream SmartIR calls this the
        # power sensor, with an option to restore the state on restart).
        self._power_sensor = entry.options.get(
            CONF_POWER_SENSOR, entry.data.get(CONF_POWER_SENSOR)
        )
        self._power_sensor_restore_state = bool(
            entry.options.get(
                CONF_POWER_SENSOR_RESTORE_STATE,
                entry.data.get(CONF_POWER_SENSOR_RESTORE_STATE, False),
            )
        )
        self._commands = data["commands"]
        self._received_command_states = build_command_states(data)
        self._remove_receiver_subscription: CALLBACK_TYPE | None = None
        self._send_lock = asyncio.Lock()
        self._last_on_mode: HVACMode | None = None

        self._attr_unique_id = f"{entry.entry_id}_climate"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data[CONF_NAME],
            manufacturer=data.get("manufacturer"),
            model=", ".join(data.get("supportedModels", [])),
        )
        self._attr_min_temp = data["minTemperature"]
        self._attr_max_temp = data["maxTemperature"]
        self._attr_target_temperature_step = data.get("precision", 1)
        self._attr_target_temperature = self._attr_min_temp
        self._attr_current_temperature = self._read_temperature_sensor()
        self._attr_current_humidity = self._read_humidity_sensor()
        self._attr_hvac_modes = [HVACMode.OFF] + [
            HVACMode(mode) for mode in data["operationModes"]
        ]
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_fan_modes = data["fanModes"]
        self._attr_fan_mode = self._attr_fan_modes[0]
        self._attr_swing_modes = data.get("swingModes")
        self._attr_swing_mode = (
            self._attr_swing_modes[0] if self._attr_swing_modes else None
        )
        self._attr_supported_features = (
            ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
        )
        if self._attr_swing_modes:
            self._attr_supported_features |= ClimateEntityFeature.SWING_MODE

        # Button air conditioners: one code per button *and state*, so the
        # entity tracks the state it assumes and picks the matching code.
        self._stepped: dict[str, Any] | None = data.get("stepped")
        self._step_modes: list[HVACMode] = []
        self._step_family: int | None = None
        self._assumed_temperature = float(self._attr_target_temperature)
        if self._stepped:
            names = entry.data.get("mode_names") or data.get("modeNames") or data["operationModes"]
            self._step_modes = [HVACMode(name) for name in names[: len(self._stepped["modes"])]]
            self._attr_hvac_modes = [HVACMode.OFF, *self._step_modes]
            self._attr_hvac_mode = HVACMode.OFF
            # No fan or swing control: those codes are state-dependent too.
            self._attr_fan_modes = []
            self._attr_fan_mode = None
            self._attr_swing_modes = None
            self._attr_swing_mode = None
            self._attr_supported_features = (
                ClimateEntityFeature.TURN_ON
                | ClimateEntityFeature.TURN_OFF
                | ClimateEntityFeature.TARGET_TEMPERATURE
            )
            self._step_family = self._stepped["modes"][0].get("family")
            self._assumed_temperature = float(
                round((self._attr_min_temp + self._attr_max_temp) / 2)
            )
            self._attr_target_temperature = self._assumed_temperature

    def _read_temperature_sensor(self) -> float | None:
        """Current room temperature from the configured sensor."""
        return self._read_sensor(self._temperature_sensor)

    def _read_humidity_sensor(self) -> float | None:
        """Current room humidity from the configured sensor."""
        return self._read_sensor(self._humidity_sensor)

    def _read_sensor(self, entity_id: str | None) -> float | None:
        """Read a numeric sensor state, tolerating unavailable values."""
        if not entity_id or self.hass is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        if entity_id == self._temperature_sensor:
            unit = state.attributes.get("unit_of_measurement")
            if unit in ("°F", "F"):
                value = (value - 32) * 5 / 9
        return round(value, 1)

    def _power_sensor_is_on(self) -> bool | None:
        """True/False from the power sensor, None when it cannot say."""
        if not self._power_sensor or self.hass is None:
            return None
        state = self.hass.states.get(self._power_sensor)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        return state.state == "on"

    @callback
    def _power_sensor_changed(self, event: Event[EventStateChangedData]) -> None:
        """Follow the power sensor: it reports reality, so we do not transmit."""
        is_on = self._power_sensor_is_on()
        if is_on is None:
            return
        if not is_on and self._attr_hvac_mode != HVACMode.OFF:
            self._last_on_mode = self._attr_hvac_mode
            self._attr_hvac_mode = HVACMode.OFF
        elif is_on and self._attr_hvac_mode == HVACMode.OFF and self._last_on_mode:
            self._attr_hvac_mode = self._last_on_mode
        self.async_write_ha_state()

    @callback
    def _sensor_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Follow the room sensors so the card stays accurate."""
        self._attr_current_temperature = self._read_temperature_sensor()
        self._attr_current_humidity = self._read_humidity_sensor()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore state and subscribe to the optional Infrared receiver."""
        await super().async_added_to_hass()
        sensors = [
            entity_id
            for entity_id in (self._temperature_sensor, self._humidity_sensor)
            if entity_id
        ]
        if sensors:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, sensors, self._sensor_state_changed
                )
            )
            # The entity is only attached to hass now, so this is the first
            # moment the room sensors can actually be read.
            self._attr_current_temperature = self._read_temperature_sensor()
            self._attr_current_humidity = self._read_humidity_sensor()
            self.async_write_ha_state()
        if self._power_sensor:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._power_sensor], self._power_sensor_changed
                )
            )
            is_on = self._power_sensor_is_on()
            if is_on is False:
                self._last_on_mode = self._last_on_mode or self._attr_hvac_mode
                self._attr_hvac_mode = HVACMode.OFF
                self.async_write_ha_state()
            elif is_on and self._power_sensor_restore_state and self._last_on_mode:
                self._attr_hvac_mode = self._last_on_mode
                self.async_write_ha_state()
        if self._infrared_receiver_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._infrared_receiver_entity_id],
                    self._receiver_state_changed,
                )
            )
            self.async_on_remove(self._unsubscribe_receiver)
            receiver_state = self.hass.states.get(
                self._infrared_receiver_entity_id
            )
            if receiver_state is not None and receiver_state.state != STATE_UNAVAILABLE:
                self._subscribe_receiver()
        if (state := await self.async_get_last_state()) is None:
            return
        try:
            self._attr_hvac_mode = HVACMode(state.state)
        except ValueError:
            self._attr_hvac_mode = HVACMode.OFF
        if (temperature := state.attributes.get("temperature")) is not None:
            self._attr_target_temperature = temperature
        if (fan_mode := state.attributes.get("fan_mode")) in self._attr_fan_modes:
            self._attr_fan_mode = fan_mode
        if self._attr_swing_modes:
            swing_mode = state.attributes.get("swing_mode")
            if swing_mode in self._attr_swing_modes:
                self._attr_swing_mode = swing_mode
        if self._attr_hvac_mode != HVACMode.OFF:
            self._last_on_mode = self._attr_hvac_mode

    @callback
    def _receiver_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Follow receiver reloads without changing climate availability."""
        new_state = event.data["new_state"]
        if new_state is None or new_state.state == STATE_UNAVAILABLE:
            self._unsubscribe_receiver()
        else:
            self._subscribe_receiver()

    @callback
    def _subscribe_receiver(self) -> None:
        """Subscribe when the optional receiver is available."""
        if (
            not self._infrared_receiver_entity_id
            or self._remove_receiver_subscription is not None
        ):
            return
        try:
            self._remove_receiver_subscription = async_subscribe_receiver(
                self.hass,
                self._infrared_receiver_entity_id,
                self._handle_received_signal,
            )
        except HomeAssistantError:
            _LOGGER.warning(
                "Unable to subscribe to Infrared receiver %s",
                self._infrared_receiver_entity_id,
            )

    @callback
    def _unsubscribe_receiver(self) -> None:
        """Remove the optional receiver subscription."""
        if self._remove_receiver_subscription is None:
            return
        self._remove_receiver_subscription()
        self._remove_receiver_subscription = None

    @callback
    def _handle_received_signal(self, signal: InfraredReceivedSignal) -> None:
        """Update assumed climate state when a known remote command is received."""
        _LOGGER.debug(
            "Received raw IR signal with %d timings%s: %s",
            len(signal.timings),
            (
                f" at {signal.modulation} Hz"
                if signal.modulation is not None
                else ""
            ),
            signal.timings,
        )
        matched = find_command_state(
            signal.timings,
            self._received_command_states,
            current_hvac_mode=self._attr_hvac_mode.value,
            current_fan_mode=self._attr_fan_mode,
            current_swing_mode=self._attr_swing_mode,
            current_temperature=self._attr_target_temperature,
        )
        if matched is None:
            _LOGGER.debug("Received IR signal did not match this SmartIR profile")
            return
        _LOGGER.debug("Received IR signal matched a SmartIR climate command")

        if matched.hvac_mode == HVACMode.OFF.value:
            if self._attr_hvac_mode != HVACMode.OFF:
                self._last_on_mode = self._attr_hvac_mode
            self._attr_hvac_mode = HVACMode.OFF
        elif matched.hvac_mode == ON_STATE:
            self._attr_hvac_mode = (
                self._last_on_mode or self._attr_hvac_modes[1]
            )
            self._last_on_mode = self._attr_hvac_mode
        else:
            self._attr_hvac_mode = HVACMode(matched.hvac_mode)
            self._last_on_mode = self._attr_hvac_mode
            if matched.fan_mode is not None:
                self._attr_fan_mode = matched.fan_mode
            if matched.swing_mode is not None:
                self._attr_swing_mode = matched.swing_mode
            if matched.temperature is not None:
                self._attr_target_temperature = matched.temperature
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature and optionally the HVAC mode."""
        if self._stepped:
            async with self._send_lock:
                await self._async_step_temperature(kwargs)
            self.async_write_ha_state()
            return
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            temperature = min(self.max_temp, max(self.min_temp, temperature))
            step = self.target_temperature_step or 1
            self._attr_target_temperature = round(temperature / step) * step

        requested_mode = kwargs.get("hvac_mode")
        if requested_mode is not None:
            self._attr_hvac_mode = HVACMode(requested_mode)
            if self._attr_hvac_mode != HVACMode.OFF:
                self._last_on_mode = self._attr_hvac_mode
        if self._attr_hvac_mode != HVACMode.OFF or requested_mode is not None:
            await self._send_current_command()
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode and transmit its complete state."""
        if self._stepped:
            async with self._send_lock:
                await self._async_step_mode(HVACMode(hvac_mode))
            self.async_write_ha_state()
            return
        self._attr_hvac_mode = HVACMode(hvac_mode)
        if self._attr_hvac_mode != HVACMode.OFF:
            self._last_on_mode = self._attr_hvac_mode
        await self._send_current_command()
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the fan mode."""
        if self._stepped:
            raise HomeAssistantError(
                "This device exposes no separate fan control"
            )
        self._attr_fan_mode = fan_mode
        if self._attr_hvac_mode != HVACMode.OFF:
            await self._send_current_command()
        self.async_write_ha_state()

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set the swing mode."""
        self._attr_swing_mode = swing_mode
        if self._attr_hvac_mode != HVACMode.OFF:
            await self._send_current_command()
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Turn on using the last active or first supported mode."""
        await self.async_set_hvac_mode(
            self._last_on_mode or self._attr_hvac_modes[1]
        )

    async def async_turn_off(self) -> None:
        """Turn off the climate device."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def _async_step_mode(self, mode: HVACMode) -> None:
        """Select a mode with its own button; power on first when needed."""
        if mode == HVACMode.OFF:
            await self._send_value(self._stepped["off"])
            self._attr_hvac_mode = HVACMode.OFF
            return
        if mode not in self._step_modes:
            raise HomeAssistantError(f"Unsupported mode for this device: {mode}")
        index = self._step_modes.index(mode)
        if self._attr_hvac_mode == HVACMode.OFF:
            await self._send_value(self._stepped["on"])
            await asyncio.sleep(0.5)
        variant = self._stepped["modes"][index]
        await self._send_value(variant["code"])
        if (family := variant.get("family")) is not None:
            self._step_family = family
        if (mark := variant.get("mark")) is not None:
            # The mode button carries the state it was captured in; the unit
            # adopts that state, so our assumption has to follow it.
            self._assumed_temperature = float(mark)
            self._attr_target_temperature = self._assumed_temperature
            _LOGGER.debug("Mode %s resets assumed temperature to %s", mode, mark)
        self._attr_hvac_mode = mode
        self._last_on_mode = mode

    async def _async_step_temperature(self, kwargs: dict[str, Any]) -> None:
        """Reach a temperature by pressing up or down once per degree."""
        requested_mode = kwargs.get("hvac_mode")
        if requested_mode is not None and HVACMode(requested_mode) != self._attr_hvac_mode:
            await self._async_step_mode(HVACMode(requested_mode))
        if (requested := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        target = float(
            min(self._attr_max_temp, max(self._attr_min_temp, requested))
        )
        if self._attr_hvac_mode == HVACMode.OFF:
            self._attr_target_temperature = target
            self._assumed_temperature = target
            return
        guard = 0
        while self._assumed_temperature != target and guard < 64:
            guard += 1
            rising = target > self._assumed_temperature
            code = self._find_step_code(rising, self._assumed_temperature)
            if code is None:
                _LOGGER.warning(
                    "No %s code for %s C; assumed temperature stays at %s",
                    "warmer" if rising else "cooler",
                    self._assumed_temperature,
                    self._assumed_temperature,
                )
                break
            await self._send_value(code)
            self._assumed_temperature += 1 if rising else -1
            self._attr_target_temperature = self._assumed_temperature
            await asyncio.sleep(0.4)

    def _find_step_code(self, rising: bool, temperature: float) -> list[int] | None:
        """Pick the up/down code valid for the temperature we assume we are at."""
        variants = self._stepped["tempUp" if rising else "tempDown"]
        candidates = [
            item for item in variants if item["temp"] == int(round(temperature))
        ]
        if not candidates:
            return None
        if self._step_family is not None:
            for item in candidates:
                if item.get("family") == self._step_family:
                    return item["code"]
        return candidates[0]["code"]

    async def _send_current_command(self) -> None:
        """Resolve and send the command for the complete current state."""
        async with self._send_lock:
            if self._attr_hvac_mode == HVACMode.OFF:
                await self._send_value(self._commands["off"])
                return
            if "on" in self._commands:
                await self._send_value(self._commands["on"])
                await asyncio.sleep(0.5)
            command = self._commands[self._attr_hvac_mode.value][self._attr_fan_mode]
            if self._attr_swing_modes:
                command = command[self._attr_swing_mode]
            temperature = f"{self._attr_target_temperature:g}"
            await self._send_value(command[temperature])
