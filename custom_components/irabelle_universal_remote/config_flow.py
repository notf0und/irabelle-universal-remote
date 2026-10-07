"""Config flow for Irabelle Universal Remote."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.components.infrared import (
    DOMAIN as INFRARED_DOMAIN,
    async_get_emitters,
    async_get_receivers,
    async_send_command,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    FileSelector,
    FileSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .emitter_base import StoredRawCommand, find_command_value
from .codes_library import (
    DEFAULT_LIBRARY_URL,
    appliance_options,
    INDEX_PATH,
    CodeLibraryError,
    LibraryEntry,
    entry_profile,
    parse_bundle,
    parse_index,
    search,
)
from .const import (
    CONF_CARRIER_FREQUENCY,
    CONF_HUMIDITY_SENSOR,
    CONF_POWER_SENSOR,
    CONF_POWER_SENSOR_RESTORE_STATE,
    CONF_TEMPERATURE_SENSOR,
    CONF_TEST_ACTION,
    CONF_TEST_FAN,
    CONF_TEST_MODE,
    CONF_TEST_SWING,
    CONF_INFRARED_ENTITY_ID,
    CONF_INFRARED_RECEIVER_ENTITY_ID,
    CONF_LIBRARY_APPLIANCE,
    CONF_LIBRARY_BRAND,
    CONF_LIBRARY_CHOICE,
    CONF_LIBRARY_DEVICE_TYPE,
    CONF_LIBRARY_MODEL,
    CONF_LIBRARY_QUERY,
    CONF_LIBRARY_URL,
    CONF_NAME,
    CONF_PROFILE_CODE,
    CONF_SMARTIR_JSON,
    CONF_SMARTIR_JSON_FILE,
    CONF_TIMING_SCALE,
    DEFAULT_CARRIER_FREQUENCY,
    DEFAULT_TIMING_SCALE,
    DOMAIN,
    LIBRARY_ANY,
    LIBRARY_DEVICE_TYPES,
    LIBRARY_INDEX_TTL,
    LIBRARY_RESULT_LIMIT,
    MAX_CARRIER_FREQUENCY,
    TEST_ACTIONS,
    TEST_COMMANDS,
    MAX_TIMING_SCALE,
    MIN_CARRIER_FREQUENCY,
    MIN_TIMING_SCALE,
)
from .profile import (
    InvalidProfile,
    decode_profile_code,
    detect_device_type,
    profile_name,
)
from .smartir_json import (
    ProfileBuild,
    SmartIrJsonError,
    build_profile,
    load_smartir_json,
)

_LOGGER = logging.getLogger(__name__)


def _read_uploaded_text(hass: HomeAssistant, file_id: str) -> str:
    """Read an uploaded file on the executor thread (see file_upload).

    ``process_uploaded_file`` is a context manager that deletes the temporary
    upload when it exits, so it must run inside a single executor job.
    """
    with process_uploaded_file(hass, file_id) as path:
        return path.read_text(encoding="utf-8")


def _build_profile_from_text(text: str) -> ProfileBuild:
    """Parse and convert SmartIR JSON; runs on the executor thread."""
    return build_profile(load_smartir_json(text))


def _library_cache_dir(hass: HomeAssistant) -> Path:
    """Directory holding the cached library index and the last used location."""
    return Path(hass.config.path(".storage")) / DOMAIN


def _index_cache_path(hass: HomeAssistant, location: str) -> Path:
    """Cache file for one library location."""
    digest = hashlib.sha1(location.encode()).hexdigest()[:12]
    return _library_cache_dir(hass) / f"index-{digest}.json.gz"


def _read_remembered_location(hass: HomeAssistant) -> str:
    """Return the library location used last time, for the form default."""
    try:
        data = json.loads((_library_cache_dir(hass) / "library.json").read_text())
    except (OSError, json.JSONDecodeError):
        return DEFAULT_LIBRARY_URL
    return str(data.get("url") or DEFAULT_LIBRARY_URL)


def _remember_location(hass: HomeAssistant, location: str) -> None:
    """Persist the library location between flows and restarts."""
    directory = _library_cache_dir(hass)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "library.json").write_text(json.dumps({"url": location}))


def _write_cache(path: Path, data: bytes) -> None:
    """Write the cached index (executor thread)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _find_test_command(device: dict[str, Any]) -> tuple[str, list[int]] | None:
    """Return the most distinctive command to transmit for a device."""
    commands = device.get("commands")
    if not isinstance(commands, dict):
        return None
    device_type = str(device.get("type") or "media_player")
    for name in TEST_COMMANDS.get(device_type, ("off", "on")):
        value = find_command_value(commands, name)
        if isinstance(value, list) and value and all(
            isinstance(item, int) and not isinstance(item, bool) for item in value
        ):
            return name, list(value)
    return None


async def _async_send_test_timings(
    hass: HomeAssistant,
    emitter_entity_id: str,
    timings: list[int],
    carrier_frequency: int,
    timing_scale: int,
) -> None:
    """Transmit one command through the chosen emitter (patchable in tests)."""
    scaled = [round(timing * timing_scale / 100) for timing in timings]
    await async_send_command(
        hass, emitter_entity_id, StoredRawCommand(scaled, carrier_frequency)
    )


def _matrix_command(
    commands: Any,
    *,
    mode: str | None = None,
    fan: str | None = None,
    swing: str | None = None,
) -> tuple[Any, str]:
    """Resolve a full code set down to one transmittable state.

    SmartIR nests commands as mode / fan / [swing] / temperature. Whichever of
    those we are not told to use is left free, and a mid-range temperature is
    chosen so the appliance visibly changes state.
    """
    if not isinstance(commands, dict) or not commands:
        return None, ""
    chosen_mode = mode
    if chosen_mode is None:
        modes = [key for key, value in commands.items() if isinstance(value, dict)]
        if not modes:
            return None, ""
        chosen_mode = "cool" if "cool" in modes else modes[0]
    node = commands.get(chosen_mode)
    if not isinstance(node, dict):
        return None, ""
    if fan is not None and isinstance(node.get(fan), dict):
        node = node[fan]
    elif isinstance(node, dict) and any(isinstance(v, dict) for v in node.values()):
        # fan level: prefer "auto" when the code set has it
        fan_key = "auto" if isinstance(node.get("auto"), dict) else next(
            key for key, value in node.items() if isinstance(value, dict)
        )
        node = node[fan_key]
    if swing is not None and isinstance(node.get(swing), dict):
        node = node[swing]
    elif isinstance(node, dict) and any(isinstance(v, dict) for v in node.values()):
        swing_key = next(key for key, value in node.items() if isinstance(value, dict))
        node = node[swing_key]
    if not isinstance(node, dict):
        return None, ""
    temperatures = sorted(
        (key for key in node if str(key).lstrip("-").isdigit()), key=lambda key: int(key)
    )
    if not temperatures:
        return None, ""
    middle = temperatures[len(temperatures) // 2]
    return node[middle], f"{chosen_mode} {middle} C"


def _matrix_probe(commands: Any) -> tuple[Any, str]:
    """Backwards-compatible shorthand for the default mid-range state."""
    return _matrix_command(commands)


def _cached_index(path: Path) -> bytes | None:
    """Return cached index bytes when they are still fresh."""
    import time

    try:
        if time.time() - path.stat().st_mtime < LIBRARY_INDEX_TTL:
            return path.read_bytes()
    except OSError:
        return None
    return None


def _optional_entity(key: str, current: str | None) -> Any:
    """Marker for an optional entity field.

    Home Assistant validates a default through the selector, and an entity
    selector rejects None, so the default has to be left out when unset.
    """
    return vol.Optional(key, default=current) if current else vol.Optional(key)


def _device_schema(
    name: str,
    emitters: list[str],
    receivers: list[str],
    *,
    include_receiver: bool = True,
    emitter: str | None = None,
    receiver: str | None = None,
    temperature_sensor: str | None = None,
    humidity_sensor: str | None = None,
    power_sensor: str | None = None,
    power_sensor_restore: bool = False,
    carrier_frequency: int = DEFAULT_CARRIER_FREQUENCY,
    timing_scale: int = DEFAULT_TIMING_SCALE,
    profile_code: str | None = None,
) -> vol.Schema:
    """Build the shared device form for setup and reconfiguration."""
    emitter_marker = (
        vol.Required(CONF_INFRARED_ENTITY_ID, default=emitter)
        if emitter
        else vol.Required(CONF_INFRARED_ENTITY_ID)
    )
    schema_fields: dict[vol.Marker, Any] = {
        vol.Required(CONF_NAME, default=name): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
        emitter_marker: EntitySelector(
            EntitySelectorConfig(
                domain=INFRARED_DOMAIN,
                include_entities=emitters,
            )
        ),
        vol.Required(
            CONF_CARRIER_FREQUENCY,
            default=carrier_frequency,
        ): _carrier_frequency_selector(),
        vol.Required(
            CONF_TIMING_SCALE,
            default=timing_scale,
        ): _timing_scale_selector(),
    }
    if include_receiver:
        receiver_marker = (
            vol.Optional(CONF_INFRARED_RECEIVER_ENTITY_ID, default=receiver)
            if receiver
            else vol.Optional(CONF_INFRARED_RECEIVER_ENTITY_ID)
        )
        receiver_config = {"domain": INFRARED_DOMAIN}
        if receivers:
            receiver_config["include_entities"] = receivers
        schema_fields[receiver_marker] = EntitySelector(
            EntitySelectorConfig(**receiver_config)
        )
    # Optional room sensors, exactly as upstream SmartIR offers them: they feed
    # current_temperature / current_humidity on the climate card.
    schema_fields[_optional_entity(CONF_TEMPERATURE_SENSOR, temperature_sensor)] = (
        EntitySelector(EntitySelectorConfig(domain="sensor", device_class="temperature"))
    )
    schema_fields[_optional_entity(CONF_HUMIDITY_SENSOR, humidity_sensor)] = (
        EntitySelector(EntitySelectorConfig(domain="sensor", device_class="humidity"))
    )
    schema_fields[
        _optional_entity(CONF_POWER_SENSOR, power_sensor)
    ] = EntitySelector(
        EntitySelectorConfig(domain="binary_sensor", device_class="power")
    )
    schema_fields[
        vol.Optional(CONF_POWER_SENSOR_RESTORE_STATE, default=power_sensor_restore)
    ] = BooleanSelector()
    if profile_code is not None:
        schema_fields[
            vol.Required(CONF_PROFILE_CODE, default=profile_code)
        ] = _profile_code_selector()
    return vol.Schema(schema_fields)


def _profile_code_selector() -> TextSelector:
    """Build the multiline editor used for IRP1 profile codes."""
    return TextSelector(
        TextSelectorConfig(
            multiline=True,
            type=TextSelectorType.TEXT,
        )
    )


def _smartir_json_selector() -> TextSelector:
    """Build the multiline editor used for pasted SmartIR JSON files."""
    return TextSelector(
        TextSelectorConfig(
            multiline=True,
            type=TextSelectorType.TEXT,
        )
    )


def _carrier_frequency_selector() -> NumberSelector:
    """Build the carrier-frequency control in Hertz."""
    return NumberSelector(
        NumberSelectorConfig(
            min=MIN_CARRIER_FREQUENCY,
            max=MAX_CARRIER_FREQUENCY,
            step=1_000,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="Hz",
        )
    )


def _timing_scale_selector() -> NumberSelector:
    """Build the output timing-scale control in percent."""
    return NumberSelector(
        NumberSelectorConfig(
            min=MIN_TIMING_SCALE,
            max=MAX_TIMING_SCALE,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="%",
        )
    )


def _normalize_updated_profile(
    profile_code: str,
    current_profile_code: str,
) -> tuple[str | None, str | None]:
    """Validate an edited profile and keep the existing entity platform."""
    try:
        device = decode_profile_code(profile_code)
        current_device = decode_profile_code(current_profile_code)
    except InvalidProfile:
        return None, "invalid_profile"
    if detect_device_type(device) != detect_device_type(current_device):
        return None, "profile_type_mismatch"
    return "".join(profile_code.split()), None


class SmartIrNativeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add entities from portable SmartIR profile codes."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._profile_code: str | None = None
        self._suggested_name: str | None = None
        self._device_type: str | None = None
        self._filled_off_label: str | None = None
        self._library_location: str | None = None
        self._library_matches: list[LibraryEntry] = []
        self._library_total = 0
        self._library_emitter: str | None = None
        self._library_receiver: str | None = None
        self._device: dict[str, Any] | None = None
        self._entry_data: dict[str, Any] | None = None
        self._candidates: list[LibraryEntry] = []
        self._candidate_index = 0
        self._test_key: str | None = None
        self._test_sends = 0
        self._test_error = ""
        self._provisional = False

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> "SmartIrNativeOptionsFlow":
        """Return the editable emitter and receiver options flow."""
        return SmartIrNativeOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how the device profile is provided."""
        if not async_get_emitters(self.hass):
            return self.async_abort(reason="no_infrared_emitters")
        return self.async_show_menu(
            step_id="user",
            menu_options=["code_library", "smartir_json", "profile_code"],
        )

    async def async_step_code_library(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Search the published code library (or a local copy of it)."""
        if user_input is None and not self._library_emitter:
            # Choose the emitter first: every test transmission uses it.
            return await self.async_step_code_library_emitter()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        entries: list[LibraryEntry] = []
        if user_input is not None:
            location = str(
                user_input.get(CONF_LIBRARY_URL) or DEFAULT_LIBRARY_URL
            ).strip()
            # Brand and model, like the phone app. The older single query field
            # still works when the new fields are not filled in.
            brand = str(
                user_input.get(CONF_LIBRARY_BRAND)
                or user_input.get(CONF_LIBRARY_QUERY)
                or ""
            ).strip()
            model = str(user_input.get(CONF_LIBRARY_MODEL) or "").strip()
            # One picker only: what the device *is*. Which platform entity it
            # becomes is our business, not the user's.
            device_type = str(user_input.get(CONF_LIBRARY_DEVICE_TYPE) or LIBRARY_ANY)
            appliance = str(user_input.get(CONF_LIBRARY_APPLIANCE) or LIBRARY_ANY)
            try:
                entries = await self._async_library_entries(location)
            except CodeLibraryError as err:
                errors["base"] = "library_unavailable"
                placeholders["error"] = str(err)
            else:
                matches = search(
                    entries,
                    brand=brand,
                    model=model,
                    device_type=None if device_type == LIBRARY_ANY else device_type,
                    appliance=None if appliance == LIBRARY_ANY else appliance,
                    limit=None,
                )
                found = len(matches)
                if found > LIBRARY_RESULT_LIMIT:
                    matches = matches[:LIBRARY_RESULT_LIMIT]
                if not matches:
                    hint = ""
                    if device_type != LIBRARY_ANY or appliance != LIBRARY_ANY:
                        # Say what the filter hid instead of leaving a dead end.
                        others = search(entries, brand=brand, model=model, limit=3)
                        if others:
                            hint = (
                                f" {len(others)} other match(es) create a different "
                                f"entity type, for example {others[0].display_name} "
                                f"→ {others[0].target}."
                            )
                    errors["base"] = "library_no_match"
                    described = " ".join(x for x in (brand, model) if x) or "anything"
                    filters = " · ".join(
                        x for x in (
                            device_type if device_type != LIBRARY_ANY else "",
                            appliance if appliance != LIBRARY_ANY else "",
                        ) if x
                    )
                    placeholders["error"] = (
                        f"{len(entries)} devices are indexed, none matching "
                        f"{described!r}" + (f" as {filters}" if filters else "")
                    ) + "." + hint
                else:
                    self._library_location = location
                    self._library_matches = matches
                    self._library_total = found
                    await self.hass.async_add_executor_job(
                        _remember_location, self.hass, location
                    )
                    return await self.async_step_code_library_select()

        elif not entries:
            # First display: the index is needed only to offer the appliance
            # picker. A failure here is not an error yet — the user may be
            # about to type a different location.
            try:
                entries = await self._async_library_entries(
                    await self.hass.async_add_executor_job(
                        _read_remembered_location, self.hass
                    )
                )
            except Exception as err:  # noqa: BLE001
                # Purely cosmetic enrichment: the appliance list simply stays
                # short when the index cannot be reached yet.
                _LOGGER.debug("Appliance list unavailable: %s", err)
                entries = []

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_LIBRARY_URL,
                    default=await self.hass.async_add_executor_job(
                        _read_remembered_location, self.hass
                    ),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Optional(CONF_LIBRARY_BRAND, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Optional(CONF_LIBRARY_MODEL, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Optional(
                    CONF_LIBRARY_APPLIANCE, default=LIBRARY_ANY
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[LIBRARY_ANY, *appliance_options(entries)],
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="code_library",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_code_library_emitter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the emitter that will test and then drive the device."""
        emitters = async_get_emitters(self.hass)
        if not emitters:
            return self.async_abort(reason="no_infrared_emitters")
        if user_input is not None:
            self._library_emitter = str(user_input[CONF_INFRARED_ENTITY_ID])
            if receiver := user_input.get(CONF_INFRARED_RECEIVER_ENTITY_ID):
                self._library_receiver = str(receiver)
            return await self.async_step_code_library()
        # The same entity selectors the device step uses, so the picker shows
        # friendly names as a dropdown instead of raw ids.
        schema = vol.Schema(
            {
                vol.Required(CONF_INFRARED_ENTITY_ID): EntitySelector(
                    EntitySelectorConfig(
                        domain=INFRARED_DOMAIN, include_entities=emitters
                    )
                ),
                vol.Optional(CONF_INFRARED_RECEIVER_ENTITY_ID): EntitySelector(
                    EntitySelectorConfig(
                        domain=INFRARED_DOMAIN,
                        include_entities=async_get_receivers(self.hass),
                    )
                ),
            }
        )
        return self.async_show_form(step_id="code_library_emitter", data_schema=schema)

    async def async_step_code_library_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one of the matching library devices and convert it."""
        if not self._library_matches or not self._library_location:
            return self.async_abort(reason="library_expired")

        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                chosen_index = int(user_input[CONF_LIBRARY_CHOICE])
                chosen = self._library_matches[chosen_index]
            except (KeyError, ValueError, IndexError):
                errors["base"] = "library_no_match"
            else:
                try:
                    device = await self._async_library_device(
                        self._library_location, chosen
                    )
                    build = entry_profile(chosen, device)
                except CodeLibraryError as err:
                    errors["base"] = "library_device_invalid"
                    placeholders["error"] = str(err)
                else:
                    self._profile_code = build.code
                    self._suggested_name = build.name
                    self._device_type = build.device_type
                    self._filled_off_label = build.filled_off_label
                    self._device = build.device
                    # Keep the other matches so the test step can offer the next one.
                    self._candidates = list(self._library_matches)
                    self._candidate_index = chosen_index
                    return await self.async_step_device()

        options = [
            SelectOptionDict(
                value=str(index),
                label=" - ".join(
                    part
                    for part in (
                        entry.display_name,
                        entry.capability or entry.summary,
                    )
                    if part
                ),
            )
            for index, entry in enumerate(self._library_matches)
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_LIBRARY_CHOICE): SelectSelector(
                    SelectSelectorConfig(options=options)
                )
            }
        )
        if self._library_total > len(self._library_matches):
            placeholders["result_count"] = (
                f"Showing the {len(self._library_matches)} most complete of "
                f"{self._library_total} matches - narrow the brand or model to see more."
            )
        else:
            placeholders["result_count"] = (
                f"{len(self._library_matches)} device"
                + ("s" if len(self._library_matches) != 1 else "")
                + " matched."
            )
        return self.async_show_form(
            step_id="code_library_select",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _async_library_bytes(self, location: str, relative: str) -> bytes:
        """Read one library file over HTTP(S) or from a local path."""
        if location.lower().startswith(("http://", "https://")):
            session = async_get_clientsession(self.hass)
            url = f"{location.rstrip('/')}/{relative.lstrip('/')}"
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    if response.status != 200:
                        raise CodeLibraryError(
                            f"{url} returned HTTP {response.status}."
                        )
                    return await response.read()
            except aiohttp.ClientError as err:
                raise CodeLibraryError(f"{url} could not be fetched: {err}") from err

        raw = location[7:] if location.lower().startswith("file://") else location
        path = Path(raw).expanduser()
        if path.is_dir():
            path = path / relative
        try:
            return await self.hass.async_add_executor_job(path.read_bytes)
        except OSError as err:
            raise CodeLibraryError(f"{path} could not be read: {err}") from err

    async def _async_library_index(self, location: str) -> bytes:
        """Return the index, using the on-disk cache for remote locations."""
        if not location.lower().startswith(("http://", "https://")):
            # Local copies are cheap to read and may be regenerated at any time.
            return await self._async_library_bytes(location, INDEX_PATH)

        cache = _index_cache_path(self.hass, location)
        cached = await self.hass.async_add_executor_job(_cached_index, cache)
        if cached is not None:
            return cached
        data = await self._async_library_bytes(location, INDEX_PATH)
        await self.hass.async_add_executor_job(_write_cache, cache, data)
        return data

    async def _async_library_entries(self, location: str) -> list[LibraryEntry]:
        """Load and parse the library index."""
        return parse_index(await self._async_library_index(location))

    async def _async_library_device(
        self, location: str, entry: LibraryEntry
    ) -> dict[str, Any]:
        """Fetch the single device record the entry points at."""
        data = await self._async_library_bytes(location, entry.bundle)
        return parse_bundle(data, entry.line)

    async def async_step_profile_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Accept and validate a profile code generated by the website."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                device = decode_profile_code(user_input[CONF_PROFILE_CODE])
            except InvalidProfile:
                errors[CONF_PROFILE_CODE] = "invalid_profile"
            else:
                self._profile_code = "".join(
                    user_input[CONF_PROFILE_CODE].split()
                )
                self._suggested_name = profile_name(device)
                self._device_type = detect_device_type(device)
                self._device = device
                return await self.async_step_device()

        schema = vol.Schema(
            {
                vol.Required(CONF_PROFILE_CODE): _profile_code_selector()
            }
        )
        return self.async_show_form(
            step_id="profile_code",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_smartir_json(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Convert a SmartIR JSON file (SmartIR's own code format)."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                text = await self._async_smartir_json_text(user_input)
                build = await self.hass.async_add_executor_job(
                    _build_profile_from_text, text
                )
            except SmartIrJsonError as err:
                errors["base"] = "invalid_smartir_json"
                placeholders["error"] = str(err)
            else:
                self._profile_code = build.code
                self._suggested_name = build.name
                self._device_type = build.device_type
                self._filled_off_label = build.filled_off_label
                self._device = build.device
                return await self.async_step_device()

        schema = vol.Schema(
            {
                vol.Optional(CONF_SMARTIR_JSON_FILE): FileSelector(
                    FileSelectorConfig(accept=".json,application/json")
                ),
                vol.Optional(CONF_SMARTIR_JSON): _smartir_json_selector(),
            }
        )
        return self.async_show_form(
            step_id="smartir_json",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _async_smartir_json_text(self, user_input: dict[str, Any]) -> str:
        """Return the SmartIR JSON text from an upload or the paste box."""
        file_id = user_input.get(CONF_SMARTIR_JSON_FILE)
        if file_id:
            try:
                return await self.hass.async_add_executor_job(
                    _read_uploaded_text, self.hass, file_id
                )
            except (OSError, ValueError) as err:
                raise SmartIrJsonError(
                    f"The uploaded file could not be read: {err}"
                ) from err
        text = str(user_input.get(CONF_SMARTIR_JSON) or "").strip()
        if not text:
            raise SmartIrJsonError(
                "Choose a SmartIR JSON file to upload, or paste its contents."
            )
        return text

    def _create_entry(self) -> ConfigFlowResult:
        """Create the config entry from the collected device settings."""
        data = self._entry_data or {}
        title = str(data.get(CONF_NAME) or self._suggested_name or DOMAIN)
        if getattr(self, "_provisional", False):
            # Marked so it is obvious which entity is still being evaluated;
            # reconfiguring it later drops the marker.
            title = f"{title} (testing)"
        return self.async_create_entry(
            title=title,
            data=data,
            description=(
                "irabelle_universal_remote_off_filled" if self._filled_off_label else None
            ),
            description_placeholders=(
                {"frame": self._filled_off_label}
                if self._filled_off_label
                else None
            ),
        )

    def _has_more_candidates(self) -> bool:
        """True when the library search had further devices to try."""
        return self._candidate_index + 1 < len(self._candidates)

    async def async_step_test(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Poke the candidate with its own buttons, then confirm or move on.

        One invisible command proves nothing: an air conditioner that is
        already off answers an "off" test with silence, exactly like a wrong
        code set. So the buttons this code set actually has become menu
        actions, with It worked / Next / Previous / Save anyway underneath -
        the same "try remotes until one reacts" loop as the phone app.
        """
        if user_input is not None:
            return await self._async_test_action(
                str(user_input.get(CONF_TEST_ACTION) or "send")
            )
        return self._show_test_menu()

    def _show_test_menu(self) -> ConfigFlowResult:
        """Render the test menu for the candidate under test."""
        options = [f"test_{name}" for name in self._testable_buttons()]
        options.append("test_next")
        if self._candidate_index:
            options.append("test_previous")
        options.append("test_create_provisional")
        options.append("test_confirm")
        options.append("test_save_anyway")
        # A menu has no fields, so anything that went wrong is reported in the
        # description rather than as a form error.
        return self.async_show_menu(
            step_id="test",
            menu_options=options,
            description_placeholders=self._test_placeholders(),
        )

    def _sent_summary(self) -> str:
        """Describe what has been sent to the candidate so far."""
        if not self._test_sends:
            return "Nothing has been sent yet."
        times = "once" if self._test_sends == 1 else f"{self._test_sends} times"
        return f"Sent the '{self._test_key}' command {times}."

    def _capability_summary(self) -> str:
        """List what this code set can do, so a wrong one is obvious up front."""
        device = self._device or {}
        if isinstance(device.get("stepped"), dict):
            step = device["stepped"]
            parts = []
            if step.get("modes"):
                parts.append(f"{len(step['modes'])} modes")
            if step.get("tempUp") and step.get("tempDown"):
                parts.append(f"{device.get('minTemperature')}-{device.get('maxTemperature')} C")
            parts.append("buttons: " + ", ".join(self._testable_buttons()))
            return "This code set provides " + " - ".join(parts) + "."
        modes = device.get("operationModes") or []
        fans = device.get("fanModes") or []
        swings = device.get("swingModes") or []
        low = device.get("minTemperature")
        high = device.get("maxTemperature")
        details = []
        if modes:
            details.append("modes: " + ", ".join(str(mode) for mode in modes))
        if fans:
            details.append("fan: " + ", ".join(str(fan) for fan in fans))
        details.append(
            "swing: " + ", ".join(str(swing) for swing in swings) if swings else "no swing"
        )
        if isinstance(low, int | float) and isinstance(high, int | float):
            details.append(f"{low:g}-{high:g} C")
        return "This code set provides " + " - ".join(details) + "."

    def _test_placeholders(self) -> dict[str, str]:
        """Describe the candidate under test and what has been sent."""
        data = self._entry_data or {}
        return {
            "device": self._suggested_name or "",
            "emitter": str(data.get(CONF_INFRARED_ENTITY_ID) or ""),
            "sent": self._sent_summary(),
            "position": (
                f"Code set {self._candidate_index + 1} of {len(self._candidates)}."
                if self._candidates
                else ""
            ),
            "error": self._test_error,
            "capabilities": self._capability_summary(),
            "dashboard_note": (
                "Prefer to judge it for real? Create it and drive the actual "
                "climate card, then swap the code set from Configure if it is wrong."
            ),
        }

    def _testable_buttons(self) -> list[str]:
        """The buttons this candidate can actually be tested with."""
        device = self._device or {}
        step = device.get("stepped")
        if isinstance(step, dict):
            found = []
            if step.get("on"):
                found.append("power_on")
            if step.get("off"):
                found.append("power_off")
            if step.get("tempUp"):
                found.append("warmer")
            if step.get("tempDown"):
                found.append("cooler")
            if step.get("modes"):
                found.append("mode")
            return found
        commands = device.get("commands") or {}
        found = []
        if find_command_value(commands, "on"):
            found.append("power_on")
        if find_command_value(commands, "off"):
            found.append("power_off")
        for mode in device.get("operationModes") or []:
            if isinstance(commands.get(mode), dict):
                found.append("mode")
                break
        if len([f for f in device.get("fanModes") or [] if f]) > 1:
            found.append("fan")
        if device.get("swingModes"):
            found.append("swing")
        if _matrix_probe(commands)[0]:
            found.append("temperature")
        return found

    async def _async_test_action(self, action: str) -> ConfigFlowResult:
        """Run one menu action: send a button, confirm, or move candidate."""
        if action in ("confirm", "save_anyway"):
            return self._create_entry()
        if action == "next":
            self._test_error = ""
            if not await self._async_advance_candidate(1):
                self._test_error = (
                    "That was the last code set for this search - widen the "
                    "brand or model to see more."
                )
            return self._show_test_menu()
        if action == "previous":
            self._test_error = ""
            await self._async_advance_candidate(-1)
            return self._show_test_menu()
        if action == "send":
            # Backwards compatible entry point (also used by the tests).
            action = (self._testable_buttons() or ["power_off"])[0]
        await self._async_send_test_button(action)
        return self._show_test_menu()

    async def async_step_test_power_on(self, user_input=None):
        """Send this code set's power-on button."""
        return await self._async_test_action("power_on")

    async def async_step_test_power_off(self, user_input=None):
        """Send this code set's power-off button."""
        return await self._async_test_action("power_off")

    async def async_step_test_warmer(self, user_input=None):
        """Send its "warmer" button."""
        return await self._async_test_action("warmer")

    async def async_step_test_cooler(self, user_input=None):
        """Send its "cooler" button."""
        return await self._async_test_action("cooler")

    async def async_step_test_mode(self, user_input=None):
        """Ask which mode to try, then send it (names come from the code set)."""
        modes = self._testable_modes()
        if not modes:
            self._test_error = "This code set offers no selectable modes."
            return self._show_test_menu()
        if user_input is not None:
            chosen = str(user_input.get(CONF_TEST_MODE) or modes[0])
            await self._async_send_test_mode(chosen)
            return self._show_test_menu()
        schema = vol.Schema(
            {
                vol.Required(CONF_TEST_MODE, default=modes[0]): SelectSelector(
                    SelectSelectorConfig(options=modes)
                )
            }
        )
        return self.async_show_form(
            step_id="test_mode",
            data_schema=schema,
            description_placeholders={"modes": ", ".join(modes)},
        )

    async def async_step_test_fan(self, user_input=None):
        """Ask which fan speed to try, then send it."""
        speeds = [str(speed) for speed in (self._device or {}).get("fanModes") or [] if speed]
        if not speeds:
            self._test_error = "This code set offers no fan speeds."
            return self._show_test_menu()
        if user_input is not None:
            chosen = str(user_input.get(CONF_TEST_FAN) or speeds[0])
            code, label = _matrix_command(
                (self._device or {}).get("commands") or {}, fan=chosen
            )
            if code:
                await self._send_test_code(code, f"fan {chosen} ({label})")
            else:
                self._test_error = f"No state for fan {chosen}."
            return self._show_test_menu()
        return self.async_show_form(
            step_id="test_fan",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TEST_FAN, default=speeds[0]): SelectSelector(
                        SelectSelectorConfig(options=speeds)
                    )
                }
            ),
            description_placeholders={"fans": ", ".join(speeds)},
        )

    async def async_step_test_swing(self, user_input=None):
        """Ask which swing state to try, then send it."""
        states = [str(state) for state in (self._device or {}).get("swingModes") or [] if state]
        if not states:
            self._test_error = "This code set offers no swing states."
            return self._show_test_menu()
        if user_input is not None:
            chosen = str(user_input.get(CONF_TEST_SWING) or states[0])
            code, label = _matrix_command(
                (self._device or {}).get("commands") or {}, swing=chosen
            )
            if code:
                await self._send_test_code(code, f"swing {chosen} ({label})")
            else:
                self._test_error = f"No state for swing {chosen}."
            return self._show_test_menu()
        return self.async_show_form(
            step_id="test_swing",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TEST_SWING, default=states[0]): SelectSelector(
                        SelectSelectorConfig(options=states)
                    )
                }
            ),
            description_placeholders={"swings": ", ".join(states)},
        )

    def _testable_modes(self) -> list[str]:
        """The modes this code set can select, in its own words."""
        device = self._device or {}
        step = device.get("stepped")
        if isinstance(step, dict):
            names = device.get("modeNames") or device.get("operationModes") or []
            return [str(name) for name in names[: len(step.get("modes") or [])]] or [
                f"mode {index + 1}" for index in range(len(step.get("modes") or []))
            ]
        commands = device.get("commands") or {}
        return [
            str(mode)
            for mode in device.get("operationModes") or []
            if isinstance(commands.get(mode), dict)
        ]

    async def async_step_test_temperature(self, user_input=None):
        """Send a mid-range temperature (full code sets)."""
        return await self._async_test_action("temperature")

    async def async_step_test_create_provisional(self, user_input=None):
        """Create it now, so the real climate card can be used to judge it."""
        self._provisional = True
        return self._create_entry()

    async def async_step_test_confirm(self, user_input=None):
        """The device reacted: create the entity."""
        return self._create_entry()

    async def async_step_test_save_anyway(self, user_input=None):
        """Save without confirming."""
        return self._create_entry()

    async def async_step_test_next(self, user_input=None):
        """Try the next code set."""
        return await self._async_test_action("next")

    async def async_step_test_previous(self, user_input=None):
        """Go back to the previous code set."""
        return await self._async_test_action("previous")

    async def _async_send_test_mode(self, mode: str) -> None:
        """Send one mode's state, so the user can watch the appliance react."""
        device = self._device or {}
        step = device.get("stepped")
        if isinstance(step, dict):
            names = self._testable_modes()
            index = names.index(mode) if mode in names else 0
            variants = step.get("modes") or []
            if index < len(variants):
                await self._send_test_code(variants[index]["code"], f"mode {mode}")
            return
        commands = device.get("commands") or {}
        node = commands.get(mode)
        code, label = _matrix_probe({mode: node} if isinstance(node, dict) else {})
        if not code:
            self._test_error = f"No {mode} state to send in this code set."
            return
        await self._send_test_code(code, f"{mode} {label}" if label else mode)

    async def _async_send_test_button(self, name: str) -> None:
        """Transmit one button of the candidate being tested."""
        data = self._entry_data or {}
        device = self._device or {}
        step = device.get("stepped")
        timings: Any = None
        label = name.replace("_", " ")
        if isinstance(step, dict):
            if name == "power_on":
                timings = step.get("on")
            elif name == "power_off":
                timings = step.get("off")
            elif name in ("warmer", "cooler"):
                variants = step.get("tempUp" if name == "warmer" else "tempDown") or []
                if variants:
                    middle = variants[len(variants) // 2]
                    timings = middle["code"]
                    label = f"{label} from {middle['temp']} C"
            elif name == "mode" and step.get("modes"):
                timings = step["modes"][0]["code"]
        if timings is None:
            commands = device.get("commands") or {}
            if name == "power_on":
                timings = find_command_value(commands, "on")
            elif name == "power_off":
                timings = find_command_value(commands, "off")
            elif name == "temperature":
                timings = _matrix_probe(commands)[0]
        if not isinstance(timings, list) or not timings:
            self._test_error = "This code set has no button for that test."
            return
        await self._send_test_code(timings, label)

    async def _send_test_code(self, timings: list[int], label: str) -> None:
        """Transmit one test code and remember what was sent."""
        data = self._entry_data or {}
        try:
            await _async_send_test_timings(
                self.hass,
                str(data.get(CONF_INFRARED_ENTITY_ID) or ""),
                timings,
                int(data.get(CONF_CARRIER_FREQUENCY) or DEFAULT_CARRIER_FREQUENCY),
                int(data.get(CONF_TIMING_SCALE) or DEFAULT_TIMING_SCALE),
            )
        except (HomeAssistantError, OSError, ValueError) as err:
            self._test_error = f"The test command could not be sent: {err}"
            return
        self._test_sends += 1
        self._test_key = label
        self._test_error = ""

    async def _widen_candidates(self) -> None:
        """Add more code sets once the current list runs out.

        A narrow search (brand + model) can leave a single candidate, which
        made "try the next match" re-send the same device forever. Falling back
        to brand + appliance, then brand alone, keeps the search moving.
        """
        if not self._library_location or not self._library_matches:
            return
        first = self._library_matches[0]
        try:
            entries = await self._async_library_entries(self._library_location)
        except Exception as err:  # noqa: BLE001 - widening is best effort
            _LOGGER.debug("Could not widen the candidate list: %s", err)
            return
        known = {entry.id for entry in self._candidates}
        for query in (
            {"brand": first.brand, "appliance": first.appliance},
            {"brand": first.brand},
            {"appliance": first.appliance},
        ):
            fresh = [
                item
                for item in search(entries, limit=None, **query)
                if item.id not in known
            ]
            if fresh:
                self._candidates.extend(fresh)
                return

    async def _async_advance_candidate(self, offset: int = 1, _guard: int = 0) -> bool:
        """Move to another candidate code set; False when there is none."""
        if not self._library_location:
            return False
        # Skip past code sets that cannot be converted at all, however many
        # there are in a row; they are dropped so they never come back.
        if _guard > len(self._candidates) + 1:
            return False
        index = self._candidate_index + offset
        if index >= len(self._candidates):
            await self._widen_candidates()
        if index < 0 or index >= len(self._candidates):
            return False
        entry = self._candidates[index]
        try:
            device = await self._async_library_device(self._library_location, entry)
            build = entry_profile(entry, device)
        except Exception as err:  # noqa: BLE001 - one bad code set must not stop the flow
            _LOGGER.debug("Skipping unconvertible library entry %s: %s", entry.id, err)
            self._candidates.pop(index)
            self._candidate_index = min(index, len(self._candidates) - 1)
            return await self._async_advance_candidate(1 if offset >= 0 else -1, _guard + 1)
        self._candidate_index = index
        self._profile_code = build.code
        self._suggested_name = build.name
        self._device_type = build.device_type
        self._filled_off_label = build.filled_off_label
        self._device = build.device
        self._test_sends = 0
        self._test_key = None
        if self._entry_data is not None:
            self._entry_data[CONF_PROFILE_CODE] = build.code
        return True

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose an emitter and name for the new entity."""
        if (
            self._profile_code is None
            or self._suggested_name is None
            or self._device_type is None
        ):
            return self.async_abort(reason="invalid_profile")

        emitters = async_get_emitters(self.hass)
        if not emitters:
            return self.async_abort(reason="no_infrared_emitters")

        if user_input is not None:
            entry_data = {
                CONF_NAME: user_input[CONF_NAME],
                CONF_INFRARED_ENTITY_ID: user_input[CONF_INFRARED_ENTITY_ID],
                CONF_CARRIER_FREQUENCY: int(user_input[CONF_CARRIER_FREQUENCY]),
                CONF_TIMING_SCALE: int(user_input[CONF_TIMING_SCALE]),
                CONF_PROFILE_CODE: self._profile_code,
            }
            if receiver := user_input.get(CONF_INFRARED_RECEIVER_ENTITY_ID):
                entry_data[CONF_INFRARED_RECEIVER_ENTITY_ID] = receiver
            for sensor_key in (CONF_TEMPERATURE_SENSOR, CONF_HUMIDITY_SENSOR):
                if sensor := user_input.get(sensor_key):
                    entry_data[sensor_key] = sensor
            if power := user_input.get(CONF_POWER_SENSOR):
                entry_data[CONF_POWER_SENSOR] = power
            entry_data[CONF_POWER_SENSOR_RESTORE_STATE] = bool(
                user_input.get(CONF_POWER_SENSOR_RESTORE_STATE)
            )
            self._entry_data = entry_data
            # Offer a transmit-and-confirm step when the profile has a command
            # distinctive enough to judge by (a power command, normally).
            # Offer the test step whenever there is anything to press, which
            # includes stepped code sets whose only buttons are modes.
            if self._device is not None and (
                self._testable_buttons() or _find_test_command(self._device)
            ):
                return await self.async_step_test()
            return self._create_entry()

        schema = _device_schema(
            self._suggested_name,
            emitters,
            async_get_receivers(self.hass),
            emitter=self._library_emitter,
            receiver=self._library_receiver,
        )
        return self.async_show_form(step_id="device", data_schema=schema)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the emitter, optional receiver, or entity name."""
        entry = self._get_reconfigure_entry()
        emitters = async_get_emitters(self.hass)
        current_emitter = entry.data[CONF_INFRARED_ENTITY_ID]
        if current_emitter not in emitters:
            emitters.append(current_emitter)
        receivers = async_get_receivers(self.hass)
        current_receiver = entry.data.get(CONF_INFRARED_RECEIVER_ENTITY_ID)
        if current_receiver and current_receiver not in receivers:
            receivers.append(current_receiver)

        current_profile_code = entry.data[CONF_PROFILE_CODE]
        current_carrier_frequency = int(
            entry.options.get(
                CONF_CARRIER_FREQUENCY,
                entry.data.get(CONF_CARRIER_FREQUENCY, DEFAULT_CARRIER_FREQUENCY),
            )
        )
        current_timing_scale = int(
            entry.options.get(
                CONF_TIMING_SCALE,
                entry.data.get(CONF_TIMING_SCALE, DEFAULT_TIMING_SCALE),
            )
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            profile_code, profile_error = _normalize_updated_profile(
                user_input[CONF_PROFILE_CODE],
                current_profile_code,
            )
            if profile_error is not None:
                errors[CONF_PROFILE_CODE] = profile_error
            else:
                assert profile_code is not None
                updated_data = {
                    **entry.data,
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_INFRARED_ENTITY_ID: user_input[CONF_INFRARED_ENTITY_ID],
                    CONF_CARRIER_FREQUENCY: int(
                        user_input[CONF_CARRIER_FREQUENCY]
                    ),
                    CONF_TIMING_SCALE: int(user_input[CONF_TIMING_SCALE]),
                    CONF_INFRARED_RECEIVER_ENTITY_ID: user_input.get(
                        CONF_INFRARED_RECEIVER_ENTITY_ID
                    ),
                    CONF_PROFILE_CODE: profile_code,
                }
                updated_options = {
                    **entry.options,
                    CONF_INFRARED_ENTITY_ID: user_input[CONF_INFRARED_ENTITY_ID],
                    CONF_CARRIER_FREQUENCY: int(
                        user_input[CONF_CARRIER_FREQUENCY]
                    ),
                    CONF_TIMING_SCALE: int(user_input[CONF_TIMING_SCALE]),
                    CONF_INFRARED_RECEIVER_ENTITY_ID: user_input.get(
                        CONF_INFRARED_RECEIVER_ENTITY_ID
                    ),
                    CONF_TEMPERATURE_SENSOR: user_input.get(
                        CONF_TEMPERATURE_SENSOR
                    ),
                    CONF_HUMIDITY_SENSOR: user_input.get(CONF_HUMIDITY_SENSOR),
                    CONF_POWER_SENSOR: user_input.get(CONF_POWER_SENSOR),
                    CONF_POWER_SENSOR_RESTORE_STATE: bool(
                        user_input.get(CONF_POWER_SENSOR_RESTORE_STATE)
                    ),
                }
                return self.async_update_reload_and_abort(
                    entry,
                    title=user_input[CONF_NAME],
                    data=updated_data,
                    options=updated_options,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_device_schema(
                entry.data[CONF_NAME],
                emitters,
                receivers,
                emitter=current_emitter,
                receiver=current_receiver,
                carrier_frequency=current_carrier_frequency,
                timing_scale=current_timing_scale,
                profile_code=(
                    user_input[CONF_PROFILE_CODE]
                    if user_input is not None
                    else current_profile_code
                ),
            ),
            errors=errors,
        )


class SmartIrNativeOptionsFlow(OptionsFlowWithReload):
    """Edit the profile code and Infrared entities."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save editable Infrared entities."""
        entry = self.config_entry
        current_emitter = entry.options.get(
            CONF_INFRARED_ENTITY_ID,
            entry.data[CONF_INFRARED_ENTITY_ID],
        )
        current_receiver = entry.options.get(
            CONF_INFRARED_RECEIVER_ENTITY_ID,
            entry.data.get(CONF_INFRARED_RECEIVER_ENTITY_ID),
        )
        current_temperature_sensor = entry.options.get(
            CONF_TEMPERATURE_SENSOR, entry.data.get(CONF_TEMPERATURE_SENSOR)
        )
        current_humidity_sensor = entry.options.get(
            CONF_HUMIDITY_SENSOR, entry.data.get(CONF_HUMIDITY_SENSOR)
        )
        current_power_sensor = entry.options.get(
            CONF_POWER_SENSOR, entry.data.get(CONF_POWER_SENSOR)
        )
        current_power_sensor_restore = bool(
            entry.options.get(
                CONF_POWER_SENSOR_RESTORE_STATE,
                entry.data.get(CONF_POWER_SENSOR_RESTORE_STATE, False),
            )
        )
        current_profile_code = entry.data[CONF_PROFILE_CODE]
        current_carrier_frequency = int(
            entry.options.get(
                CONF_CARRIER_FREQUENCY,
                entry.data.get(CONF_CARRIER_FREQUENCY, DEFAULT_CARRIER_FREQUENCY),
            )
        )
        current_timing_scale = int(
            entry.options.get(
                CONF_TIMING_SCALE,
                entry.data.get(CONF_TIMING_SCALE, DEFAULT_TIMING_SCALE),
            )
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            profile_code, profile_error = _normalize_updated_profile(
                user_input[CONF_PROFILE_CODE],
                current_profile_code,
            )
            if profile_error is not None:
                errors[CONF_PROFILE_CODE] = profile_error
            else:
                assert profile_code is not None
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_PROFILE_CODE: profile_code,
                    },
                )
                data = {
                    CONF_INFRARED_ENTITY_ID: user_input[CONF_INFRARED_ENTITY_ID],
                    CONF_CARRIER_FREQUENCY: int(
                        user_input[CONF_CARRIER_FREQUENCY]
                    ),
                    CONF_TIMING_SCALE: int(user_input[CONF_TIMING_SCALE]),
                    CONF_INFRARED_RECEIVER_ENTITY_ID: user_input.get(
                        CONF_INFRARED_RECEIVER_ENTITY_ID
                    ),
                }
                return self.async_create_entry(data=data)

        emitters = async_get_emitters(self.hass)
        if current_emitter not in emitters:
            emitters.append(current_emitter)
        receivers = async_get_receivers(self.hass)
        if current_receiver and current_receiver not in receivers:
            receivers.append(current_receiver)

        emitter_marker = vol.Required(
            CONF_INFRARED_ENTITY_ID,
            default=current_emitter,
        )
        schema_fields: dict[vol.Marker, Any] = {
            emitter_marker: EntitySelector(
                EntitySelectorConfig(
                    domain=INFRARED_DOMAIN,
                    include_entities=emitters,
                )
            )
        }
        schema_fields[
            vol.Required(
                CONF_CARRIER_FREQUENCY,
                default=current_carrier_frequency,
            )
        ] = _carrier_frequency_selector()
        schema_fields[
            vol.Required(
                CONF_TIMING_SCALE,
                default=current_timing_scale,
            )
        ] = _timing_scale_selector()
        receiver_marker = (
            vol.Optional(
                CONF_INFRARED_RECEIVER_ENTITY_ID,
                default=current_receiver,
            )
            if current_receiver
            else vol.Optional(CONF_INFRARED_RECEIVER_ENTITY_ID)
        )
        receiver_config = {"domain": INFRARED_DOMAIN}
        if receivers:
            receiver_config["include_entities"] = receivers
        schema_fields[receiver_marker] = EntitySelector(
            EntitySelectorConfig(**receiver_config)
        )
        # Optional room sensors, exactly as upstream SmartIR offers them: they
        # feed current_temperature / current_humidity on the climate card.
        schema_fields[
            _optional_entity(CONF_TEMPERATURE_SENSOR, current_temperature_sensor)
        ] = EntitySelector(
            EntitySelectorConfig(domain="sensor", device_class="temperature")
        )
        schema_fields[
            _optional_entity(CONF_HUMIDITY_SENSOR, current_humidity_sensor)
        ] = EntitySelector(
            EntitySelectorConfig(domain="sensor", device_class="humidity")
        )
        schema_fields[
            _optional_entity(CONF_POWER_SENSOR, current_power_sensor)
        ] = EntitySelector(
            EntitySelectorConfig(domain="binary_sensor", device_class="power")
        )
        schema_fields[
            vol.Optional(
                CONF_POWER_SENSOR_RESTORE_STATE,
                default=current_power_sensor_restore,
            )
        ] = BooleanSelector()
        schema_fields[
            vol.Required(
                CONF_PROFILE_CODE,
                default=(
                    user_input[CONF_PROFILE_CODE]
                    if user_input is not None
                    else current_profile_code
                ),
            )
        ] = _profile_code_selector()
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
        )
