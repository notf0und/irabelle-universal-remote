"""Media player platform for Irabelle Universal Remote."""

from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
)
from homeassistant.components.media_player.const import (
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one media player entity from its stored profile code."""
    data = await hass.async_add_executor_job(
        decode_profile_code, entry.data[CONF_PROFILE_CODE]
    )
    async_add_entities([SmartIrNativeMediaPlayer(entry, data)])


class SmartIrNativeMediaPlayer(
    SmartIrNativeReceiverEntity, MediaPlayerEntity, RestoreEntity
):
    """A TV/media player exposing only commands present in its SmartIR file."""

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
        self._sources = find_command_value(self._commands, "sources")
        if not isinstance(self._sources, dict):
            self._sources = {}

        self._attr_unique_id = f"{entry.entry_id}_media_player"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data[CONF_NAME],
            manufacturer=data.get("manufacturer"),
            model=", ".join(data.get("supportedModels", [])),
        )
        self._attr_device_class = MediaPlayerDeviceClass.TV
        self._attr_state = MediaPlayerState.OFF
        self._attr_is_volume_muted = False
        self._attr_source = None
        self._attr_source_list = [str(source) for source in self._sources]
        self._attr_supported_features = self._detect_supported_features()
        self._set_receiver_commands(self._build_receiver_commands())

    async def async_added_to_hass(self) -> None:
        """Restore the assumed state and start receiver tracking."""
        await super().async_added_to_hass()
        if (state := await self.async_get_last_state()) is None:
            return
        try:
            self._attr_state = MediaPlayerState(state.state)
        except ValueError:
            self._attr_state = MediaPlayerState.OFF
        source = state.attributes.get("source")
        if source in self._attr_source_list:
            self._attr_source = source
        self._attr_is_volume_muted = bool(
            state.attributes.get("is_volume_muted", False)
        )

    def _detect_supported_features(self) -> MediaPlayerEntityFeature:
        """Advertise only features backed by commands in this profile."""
        features = MediaPlayerEntityFeature(0)
        if find_command_value(self._commands, "on") is not None:
            features |= MediaPlayerEntityFeature.TURN_ON
        if find_command_value(self._commands, "off") is not None:
            features |= MediaPlayerEntityFeature.TURN_OFF
        if all(
            find_command_value(self._commands, alias) is not None
            for alias in ("volumeUp", "volumeDown")
        ):
            features |= MediaPlayerEntityFeature.VOLUME_STEP
        if find_command_value(self._commands, "mute") is not None:
            features |= MediaPlayerEntityFeature.VOLUME_MUTE
        if find_command_value(self._commands, "nextChannel") is not None:
            features |= MediaPlayerEntityFeature.NEXT_TRACK
        if find_command_value(self._commands, "previousChannel") is not None:
            features |= MediaPlayerEntityFeature.PREVIOUS_TRACK
        if self._sources:
            features |= (
                MediaPlayerEntityFeature.SELECT_SOURCE
                | MediaPlayerEntityFeature.PLAY_MEDIA
            )
        if find_command_value(self._commands, "play") is not None:
            features |= MediaPlayerEntityFeature.PLAY
        if find_command_value(self._commands, "pause") is not None:
            features |= MediaPlayerEntityFeature.PAUSE
        if find_command_value(self._commands, "stop") is not None:
            features |= MediaPlayerEntityFeature.STOP
        return features

    async def async_turn_on(self) -> None:
        """Turn on the media player."""
        await self._send_named_command("on")
        self._attr_state = MediaPlayerState.ON
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn off the media player."""
        await self._send_named_command("off")
        self._attr_state = MediaPlayerState.OFF
        self._attr_source = None
        self.async_write_ha_state()

    async def async_mute_volume(self, mute: bool) -> None:
        """Send SmartIR's mute toggle only when the assumed state must change."""
        if self._attr_is_volume_muted != mute:
            await self._send_named_command("mute")
            self._attr_is_volume_muted = mute
            self.async_write_ha_state()

    async def async_volume_up(self) -> None:
        """Raise the volume one IR step."""
        await self._send_named_command("volumeUp")

    async def async_volume_down(self) -> None:
        """Lower the volume one IR step."""
        await self._send_named_command("volumeDown")

    async def async_media_play(self) -> None:
        """Send an optional top-level play command."""
        await self._send_named_command("play")
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()

    async def async_media_pause(self) -> None:
        """Send an optional top-level pause command."""
        await self._send_named_command("pause")
        self._attr_state = MediaPlayerState.PAUSED
        self.async_write_ha_state()

    async def async_media_stop(self) -> None:
        """Send an optional top-level stop command."""
        await self._send_named_command("stop")
        self._attr_state = MediaPlayerState.IDLE
        self.async_write_ha_state()

    async def async_media_next_track(self) -> None:
        """Send the SmartIR next-channel command."""
        await self._send_named_command("nextChannel")

    async def async_media_previous_track(self) -> None:
        """Send the SmartIR previous-channel command."""
        await self._send_named_command("previousChannel")

    async def async_select_source(self, source: str) -> None:
        """Send one command from SmartIR's sources dictionary."""
        command = find_command_value(self._sources, source)
        if command is None:
            raise HomeAssistantError(f"Unknown SmartIR source '{source}'")
        await self._send_value(command)
        self._attr_source = source
        self._attr_state = MediaPlayerState.ON
        self.async_write_ha_state()

    async def async_play_media(
        self,
        media_type: str,
        media_id: str,
        **kwargs: Any,
    ) -> None:
        """Change channel by sending direct or sequential Channel N commands."""
        del kwargs
        if media_type not in (MediaType.CHANNEL, MediaType.CHANNEL.value, "channel"):
            raise HomeAssistantError("SmartIR TVs support channel media only")
        channel = str(media_id)
        if not channel.isdigit():
            raise HomeAssistantError("The SmartIR TV channel must be numeric")
        if self._attr_state == MediaPlayerState.OFF and (
            find_command_value(self._commands, "on") is not None
        ):
            await self.async_turn_on()

        direct_name = f"Channel {channel}"
        if (
            direct_command := find_command_value(self._sources, direct_name)
        ) is not None:
            await self._send_value(direct_command)
        else:
            for digit in channel:
                command = find_command_value(self._sources, f"Channel {digit}")
                if command is None:
                    raise HomeAssistantError(
                        f"SmartIR profile has no command for channel digit {digit}"
                    )
                await self._send_value(command)
        self._attr_source = direct_name
        self._attr_state = MediaPlayerState.ON
        self.async_write_ha_state()

    async def _send_named_command(self, *aliases: str) -> None:
        """Find and send a named command from the profile."""
        command = find_command_value(self._commands, *aliases)
        if command is None:
            raise HomeAssistantError(
                f"SmartIR media profile has no '{aliases[0]}' command"
            )
        await self._send_value(command)

    def _build_receiver_commands(self) -> dict[str, Any]:
        """Index all TV commands used to infer state from a physical remote."""
        receiver_commands: dict[str, Any] = {}
        for key in (
            "on",
            "off",
            "mute",
            "volumeUp",
            "volumeDown",
            "play",
            "pause",
            "stop",
            "nextChannel",
            "previousChannel",
        ):
            command = find_command_value(self._commands, key)
            if command is not None:
                receiver_commands[key] = command
        for source, command in self._sources.items():
            receiver_commands[f"source:{source}"] = command
        return receiver_commands

    def _handle_matched_receiver_command(self, key: str) -> bool:
        """Update media-player state from one known remote command."""
        old_state = (
            self._attr_state,
            self._attr_is_volume_muted,
            self._attr_source,
        )
        if key == "on":
            self._attr_state = MediaPlayerState.ON
        elif key == "off":
            self._attr_state = MediaPlayerState.OFF
            self._attr_source = None
        elif key == "mute":
            self._attr_is_volume_muted = not self._attr_is_volume_muted
        elif key == "play":
            self._attr_state = MediaPlayerState.PLAYING
        elif key == "pause":
            self._attr_state = MediaPlayerState.PAUSED
        elif key == "stop":
            self._attr_state = MediaPlayerState.IDLE
        elif key.startswith("source:"):
            self._attr_source = key.split(":", 1)[1]
            self._attr_state = MediaPlayerState.ON
        return old_state != (
            self._attr_state,
            self._attr_is_volume_muted,
            self._attr_source,
        )
