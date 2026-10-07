"""Shared emitter helpers for Irabelle Universal Remote entities."""

import asyncio
import logging
from typing import Any

from infrared_protocols.commands import Command

from homeassistant.components.infrared import (
    InfraredEmitterConsumerEntity,
    InfraredReceivedSignal,
    async_subscribe_receiver,
)
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_CARRIER_FREQUENCY,
    CONF_TIMING_SCALE,
    DEFAULT_CARRIER_FREQUENCY,
    DEFAULT_TIMING_SCALE,
)
from .receiver import signals_match, timing_commands

SEQUENCE_COMMAND_DELAY = 0.5

_LOGGER = logging.getLogger(__name__)


class StoredRawCommand(Command):
    """An IR command backed by signed microsecond timings."""

    def __init__(self, timings: list[int], carrier_frequency: int) -> None:
        """Initialize a raw timing command with its carrier frequency."""
        super().__init__(modulation=carrier_frequency)
        self._timings = timings

    def get_raw_timings(self) -> list[int]:
        """Return alternating positive pulse and negative space timings."""
        return self._timings


def normalize_key(value: str) -> str:
    """Normalize key text for tolerant SmartIR command lookups."""
    return "".join(char.lower() for char in value if char.isalnum())


def find_command_value(node: Any, *aliases: str) -> Any:
    """Find a command in a dict by direct or normalized alias."""
    if not isinstance(node, dict):
        return None
    for alias in aliases:
        if alias in node:
            return node[alias]
    lookup = {normalize_key(str(key)): key for key in node}
    for alias in aliases:
        if key := lookup.get(normalize_key(alias)):
            return node[key]
    return None


def carrier_frequency_for_entry(entry: Any) -> int:
    """Return the saved carrier frequency, preserving old entry defaults."""
    return int(
        entry.options.get(
            CONF_CARRIER_FREQUENCY,
            entry.data.get(CONF_CARRIER_FREQUENCY, DEFAULT_CARRIER_FREQUENCY),
        )
    )


def timing_scale_for_entry(entry: Any) -> int:
    """Return the per-entry timing scale percentage."""
    return int(
        entry.options.get(
            CONF_TIMING_SCALE,
            entry.data.get(CONF_TIMING_SCALE, DEFAULT_TIMING_SCALE),
        )
    )


class SmartIrNativeEmitterEntity(InfraredEmitterConsumerEntity):
    """Base emitter consumer with common availability and send helpers."""

    _attr_assumed_state = True
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False

    def __init__(
        self,
        infrared_emitter_entity_id: str,
        carrier_frequency: int = DEFAULT_CARRIER_FREQUENCY,
        timing_scale: int = DEFAULT_TIMING_SCALE,
    ) -> None:
        """Initialize emitter state tracking."""
        self._infrared_emitter_entity_id = infrared_emitter_entity_id
        self._carrier_frequency = carrier_frequency
        self._timing_scale = timing_scale
        self._emitter_available = True

    async def async_added_to_hass(self) -> None:
        """Track emitter availability whenever the entity is added."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._infrared_emitter_entity_id],
                self._emitter_state_changed,
            )
        )
        self._refresh_emitter_availability()

    @property
    def available(self) -> bool:
        """Mark this entity unavailable when its emitter is unavailable."""
        return super().available and self._emitter_available

    @callback
    def _emitter_state_changed(self, _event: Event[EventStateChangedData]) -> None:
        """Update availability when the configured emitter state changes."""
        self._refresh_emitter_availability()
        self.async_write_ha_state()

    @callback
    def _refresh_emitter_availability(self) -> None:
        """Cache current emitter availability from Home Assistant state."""
        emitter_state = self.hass.states.get(self._infrared_emitter_entity_id)
        self._emitter_available = (
            emitter_state is not None and emitter_state.state != STATE_UNAVAILABLE
        )

    async def _send_value(self, value: Any) -> None:
        """Send one timing array or a sequence of timing arrays."""
        if not isinstance(value, list) or not value:
            raise HomeAssistantError("Profile command payload is invalid")
        if all(isinstance(item, int) and not isinstance(item, bool) for item in value):
            commands = [value]
        elif all(
            isinstance(item, list)
            and item
            and all(
                isinstance(nested, int) and not isinstance(nested, bool)
                for nested in item
            )
            for item in value
        ):
            commands = value
        else:
            raise HomeAssistantError("Profile command payload is invalid")
        for index, timings in enumerate(commands):
            transmit_timings = [
                round(timing * self._timing_scale / 100) for timing in timings
            ]
            _LOGGER.debug(
                "Sending raw IR signal with %d timings at %d Hz and %d%% scale: %s",
                len(transmit_timings),
                self._carrier_frequency,
                self._timing_scale,
                transmit_timings,
            )
            await self._send_command(
                StoredRawCommand(transmit_timings, self._carrier_frequency)
            )
            if index + 1 < len(commands):
                await asyncio.sleep(SEQUENCE_COMMAND_DELAY)


class SmartIrNativeReceiverEntity(SmartIrNativeEmitterEntity):
    """Emitter entity that can also consume optional infrared receiver signals."""

    def __init__(
        self,
        infrared_emitter_entity_id: str,
        infrared_receiver_entity_id: str | None,
        carrier_frequency: int = DEFAULT_CARRIER_FREQUENCY,
        timing_scale: int = DEFAULT_TIMING_SCALE,
    ) -> None:
        """Initialize optional receiver tracking."""
        super().__init__(
            infrared_emitter_entity_id,
            carrier_frequency,
            timing_scale,
        )
        self._infrared_receiver_entity_id = infrared_receiver_entity_id
        self._remove_receiver_subscription: CALLBACK_TYPE | None = None
        self._receiver_command_timings: dict[str, list[tuple[int, ...]]] = {}

    async def async_added_to_hass(self) -> None:
        """Track receiver lifecycle when the entity is added."""
        await super().async_added_to_hass()
        if self._infrared_receiver_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._infrared_receiver_entity_id],
                    self._receiver_state_changed,
                )
            )
            self.async_on_remove(self._unsubscribe_receiver)
            receiver_state = self.hass.states.get(self._infrared_receiver_entity_id)
            if receiver_state is not None and receiver_state.state != STATE_UNAVAILABLE:
                self._subscribe_receiver()

    @callback
    def _receiver_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Subscribe while receiver is available and unsubscribe otherwise."""
        new_state = event.data["new_state"]
        if new_state is None or new_state.state == STATE_UNAVAILABLE:
            self._unsubscribe_receiver()
        else:
            self._subscribe_receiver()

    @callback
    def _subscribe_receiver(self) -> None:
        """Subscribe to the optional Infrared receiver."""
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
            self._remove_receiver_subscription = None

    @callback
    def _unsubscribe_receiver(self) -> None:
        """Remove the optional receiver subscription."""
        if self._remove_receiver_subscription is None:
            return
        self._remove_receiver_subscription()
        self._remove_receiver_subscription = None

    def _set_receiver_commands(self, commands: dict[str, Any]) -> None:
        """Cache timing sequences used to match received commands."""
        receiver_command_timings: dict[str, list[tuple[int, ...]]] = {}
        for key, value in commands.items():
            timings = timing_commands(value)
            if timings:
                receiver_command_timings[key] = timings
        self._receiver_command_timings = receiver_command_timings

    def _match_received_command_key(self, timings: list[int]) -> str | None:
        """Return the first configured command key that matches timings."""
        for key, command_sequences in self._receiver_command_timings.items():
            if any(signals_match(sequence, timings) for sequence in command_sequences):
                return key
        return None

    @callback
    def _handle_received_signal(self, signal: InfraredReceivedSignal) -> None:
        """Apply state updates when a known receiver command is observed."""
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
        if (key := self._match_received_command_key(signal.timings)) is None:
            _LOGGER.debug("Received IR signal did not match this SmartIR profile")
            return
        _LOGGER.debug("Received IR signal matched profile command %s", key)
        if self._handle_matched_receiver_command(key):
            self.async_write_ha_state()

    def _handle_matched_receiver_command(self, key: str) -> bool:
        """Handle one matched receiver command and return whether state changed."""
        raise NotImplementedError
