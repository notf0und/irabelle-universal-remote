"""Irabelle Universal Remote integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_PROFILE_CODE
from .profile import (
    DEVICE_TYPE_CLIMATE,
    DEVICE_TYPE_FAN,
    DEVICE_TYPE_LIGHT,
    DEVICE_TYPE_MEDIA_PLAYER,
    InvalidProfile,
    decode_profile_code,
    detect_device_type,
)

_LOGGER = logging.getLogger(__name__)

DEVICE_PLATFORM_MAP = {
    DEVICE_TYPE_CLIMATE: Platform.CLIMATE,
    DEVICE_TYPE_FAN: Platform.FAN,
    DEVICE_TYPE_LIGHT: Platform.LIGHT,
    DEVICE_TYPE_MEDIA_PLAYER: Platform.MEDIA_PLAYER,
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Irabelle Universal Remote from a config entry."""
    try:
        device = await hass.async_add_executor_job(
            decode_profile_code, entry.data[CONF_PROFILE_CODE]
        )
    except InvalidProfile:
        _LOGGER.error(
            "Invalid Irabelle Universal Remote profile in config entry %s",
            entry.entry_id,
        )
        return False
    device_type = detect_device_type(device)
    platform = DEVICE_PLATFORM_MAP.get(device_type)
    if platform is None:
        _LOGGER.error(
            "Unsupported Irabelle Universal Remote device type '%s' for entry %s",
            device_type,
            entry.entry_id,
        )
        return False
    await hass.config_entries.async_forward_entry_setups(entry, [platform])
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Irabelle Universal Remote config entry."""
    try:
        device = await hass.async_add_executor_job(
            decode_profile_code, entry.data[CONF_PROFILE_CODE]
        )
    except InvalidProfile:
        return True
    device_type = detect_device_type(device)
    platform = DEVICE_PLATFORM_MAP.get(device_type)
    if platform is None:
        return True
    return await hass.config_entries.async_unload_platforms(entry, [platform])
