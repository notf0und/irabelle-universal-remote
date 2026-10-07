"""Search and fetch devices from a published Irabelle Universal Remote code library.

The library lives in its own repository, so installing the integration never
downloads the code corpus. Two small artefacts are involved:

* ``index.json.gz``            searchable entries, no timings (a few hundred KiB)
* ``bundles/<appliance>/<brand>.jsonl.gz``   the devices themselves

The integration downloads the index once and caches it, searches it locally,
then fetches only the brand bundle holding the chosen device. This module is
pure: it parses bytes and ranks matches. Fetching (HTTP or a local directory)
belongs to the caller, which keeps the logic unit-testable without Home
Assistant or a network.
"""

from __future__ import annotations

import gzip
import json
import unicodedata
from dataclasses import dataclass
from typing import Any

from .smartir_json import ProfileBuild, SmartIrJsonError, build_profile

INDEX_FORMAT = "smartir-native-codes"
INDEX_VERSION = 1

# Placeholder until the codes repository exists; the config flow lets the user
# point at any base URL or local directory.
DEFAULT_LIBRARY_URL = "https://raw.githubusercontent.com/OWNER/smartir-native-codes/main"

INDEX_PATH = "index.json.gz"


class CodeLibraryError(ValueError):
    """Raised when a code library index or bundle cannot be used."""


@dataclass(frozen=True)
class LibraryEntry:
    """One selectable device in the code library."""

    id: str
    device_type: str
    appliance: str
    brand: str
    model: str
    bundle: str = ""
    line: int = 0
    keys: tuple[str, ...] = ()
    brand_cn: str = ""
    brand_tw: str = ""
    pinyin: str = ""
    protocol: str = ""
    carrier_frequency: int = 38000
    buttons_only: bool = False
    modes: int = 0
    swing: bool = False
    states: int = 0
    variants: int = 0
    stepped: bool = False
    min_temp: int = 0
    max_temp: int = 0

    @property
    def display_name(self) -> str:
        """Human label for a picker, e.g. ``Sony KDL-40 (TV)``."""
        brand = self.brand or self.brand_cn or "Unknown"
        model = self.model.strip()
        parts = [self.appliance or "Device"]
        name = brand if not model or model.lower() == brand.lower() else f"{brand} {model}"
        if not model:
            # Say so instead of showing a bare brand; the id tail keeps
            # look-alike records apart.
            name = f"{brand} (no model · {self.id[-4:]})"
        parts.append(name)
        return " - ".join(part for part in parts if part).strip()

    @property
    def target(self) -> str:
        """The kind of Home Assistant entity this entry will actually create."""
        label = {
            "climate": "climate",
            "fan": "fan",
            "light": "light",
            "media_player": "media player",
        }.get(self.device_type, self.device_type)
        if self.buttons_only:
            # An air-conditioner button remote has no mode/fan/temperature
            # matrix, so it becomes a media player, not a climate entity.
            label += " (button remote)"
        return label

    @property
    def summary(self) -> str:
        """Secondary line: buttons exposed, and a state-dependency warning.

        Air-conditioner button remotes store one code per state (temperature,
        mode), so the code to send depends on the assumed state. Other device
        types simply carry alternative codes for a button, which is ordinary
        and needs no warning.
        """
        if self.stepped:
            # The capability line already says what it can do.
            return ""
        if not self.keys:
            return ""
        text = f"{len(self.keys)} keys"
        if self.variants and self.appliance == "Air Conditioner":
            text += " · state-dependent"
        return text

    @property
    def capability(self) -> str:
        """Describe what a stepped air conditioner can actually do."""
        if self.stepped:
            parts = []
            parts.append(
                f"{self.modes} mode" + ("s" if self.modes != 1 else "")
                if self.modes
                else "on/off only"
            )
            if self.min_temp and self.max_temp:
                parts.append(f"{self.min_temp}–{self.max_temp} °C")
            return " - ".join(parts)
        return self._protocol_capability()

    def _protocol_capability(self) -> str:
        """Describe the protocol capability when the library knows it.

        Mirrors what the phone app reads from its database
        (``getAirRemoteFeatureMask`` / ``getAirRemoteModeMask``): how many
        operating modes a code set supports and whether it can swing.
        """
        if not self.modes:
            return ""
        text = f"{self.modes} modes"
        text += " · swing" if self.swing else " · no swing"
        if self.states:
            text += f" · {self.states} states"
        return text


def parse_index(data: bytes) -> list[LibraryEntry]:
    """Parse a gzipped index into entries."""
    try:
        payload = json.loads(gzip.decompress(data))
    except (OSError, json.JSONDecodeError, EOFError) as err:
        raise CodeLibraryError(f"The code library index could not be read: {err}") from err
    if not isinstance(payload, dict) or payload.get("format") != INDEX_FORMAT:
        raise CodeLibraryError("The code library index has an unsupported format.")
    if payload.get("version") != INDEX_VERSION:
        raise CodeLibraryError(
            f"The code library index version {payload.get('version')!r} is not supported."
        )
    entries: list[LibraryEntry] = []
    for raw in payload.get("entries") or []:
        if not isinstance(raw, dict):
            continue
        try:
            entries.append(
                LibraryEntry(
                    id=str(raw.get("id") or ""),
                    device_type=str(raw.get("type") or "media_player"),
                    appliance=str(raw.get("appliance") or ""),
                    brand=str(raw.get("brand") or ""),
                    brand_cn=str(raw.get("brand_cn") or ""),
                    brand_tw=str(raw.get("brand_tw") or ""),
                    pinyin=str(raw.get("pinyin") or ""),
                    model=str(raw.get("model") or ""),
                    protocol=str(raw.get("protocol") or ""),
                    keys=tuple(raw.get("keys") or ()),
                    carrier_frequency=int(raw.get("freq") or 38000),
                    buttons_only=bool(raw.get("buttons_only")),
                    modes=int(raw.get("modes") or 0),
                    swing=bool(raw.get("swing")),
                    states=int(raw.get("states") or 0),
                    variants=int(raw.get("variants") or 0),
                    stepped=bool(raw.get("stepped")),
                    min_temp=int(raw.get("min_temp") or 0),
                    max_temp=int(raw.get("max_temp") or 0),
                    bundle=str(raw.get("bundle") or ""),
                    line=int(raw.get("line") or 0),
                )
            )
        except (TypeError, ValueError):
            continue
    return entries


def _fold(value: str) -> str:
    """Lowercase, strip accents — so ``Sony``, ``sony`` and ``SONY`` all match."""
    normalised = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in normalised if not unicodedata.combining(ch)).lower().strip()


def appliance_options(entries: list[LibraryEntry]) -> list[str]:
    """Distinct appliance names present in a library, for a picker."""
    return sorted({entry.appliance for entry in entries if entry.appliance})


def match_score(entry: LibraryEntry, query: str) -> int:
    """Rank an entry against a free-text query. 0 means no match."""
    needle = _fold(query)
    if not needle:
        return 1
    brand = _fold(entry.brand)
    brand_cn = _fold(entry.brand_cn) or _fold(entry.brand_tw)
    pinyin = _fold(entry.pinyin)
    model = _fold(entry.model)
    protocol = _fold(entry.protocol)

    score = 0
    if brand == needle or brand_cn == needle:
        score = 100
    elif brand.startswith(needle) or pinyin.startswith(needle):
        score = 80
    elif needle in brand or needle in pinyin or needle in brand_cn:
        score = 60
    if model == needle:
        score = max(score, 95)
    elif model.startswith(needle):
        score = max(score, 70)
    elif needle in model:
        score = max(score, 50)
    if needle in protocol:
        score = max(score, 40)
    if not score and needle in _fold(entry.id):
        score = 10
    # Prefer richer remotes when scores tie.
    return score + min(len(entry.keys), 9) if score else 0


def _brand_matches(entry: LibraryEntry, needle: str) -> bool:
    """True when the needle names the brand (or appears in the model)."""
    for value in (entry.brand, entry.brand_cn, entry.brand_tw, entry.pinyin):
        folded = _fold(value)
        if folded and needle in folded:
            return True
    # Being forgiving keeps a model typed into the brand box working.
    return needle in _fold(entry.model)


def search(
    entries: list[LibraryEntry],
    query: str = "",
    *,
    brand: str | None = None,
    model: str | None = None,
    device_type: str | None = None,
    appliance: str | None = None,
    include_button_only: bool = True,
    limit: int | None = 40,
) -> list[LibraryEntry]:
    """Return the best matching entries.

    ``brand`` and ``model`` mirror the phone app's two search boxes; ``query``
    remains as a single free-text field matching anything.
    """
    appliance_folded = _fold(appliance) if appliance else None
    brand_needle = _fold(brand) if brand else ""
    model_needle = _fold(model) if model else ""
    scored: list[tuple[int, LibraryEntry]] = []

    for entry in entries:
        if device_type and entry.device_type != device_type:
            continue
        if appliance_folded and _fold(entry.appliance) != appliance_folded:
            continue
        if not include_button_only and entry.buttons_only:
            continue

        if brand_needle or model_needle:
            score = 100
            if brand_needle and not _brand_matches(entry, brand_needle):
                continue
            if model_needle:
                if model_needle not in _fold(entry.model):
                    continue
                score += 20
        else:
            score = match_score(entry, query)
        if score:
            scored.append((score, entry))

    # Rank what a user can actually use: a named model first, then the most
    # complete record. A full state matrix (selectable modes, one transmission)
    # beats a stepped button remote; within either, more modes and a wider
    # temperature range beat narrower ones. Without this, thousands of sparse
    # user uploads bury the good code sets.
    item_score = {entry.id: score for score, entry in scored}

    def usefulness(entry: LibraryEntry) -> tuple:
        completeness = (
            0 if entry.stepped else 1000
        ) + entry.modes * 10 + max(0, entry.max_temp - entry.min_temp) + min(
            len(entry.keys), 20
        )
        return (
            -item_score[entry.id],
            not entry.model.strip(),
            -completeness,
            entry.display_name,
        )

    scored.sort(key=lambda item: usefulness(item[1]))
    ranked = [entry for _, entry in scored]
    return ranked if limit is None else ranked[:limit]


def parse_bundle(data: bytes, line: int) -> dict[str, Any]:
    """Return the device stored at ``line`` of a brand bundle."""
    try:
        text = gzip.decompress(data).decode("utf-8", errors="replace")
    except (OSError, EOFError, gzip.BadGzipFile):
        # A bundle served uncompressed (some static hosts do this) is still
        # perfectly readable; only fail when it is not JSON lines either.
        stripped = data.lstrip()
        if not stripped.startswith(b"{"):
            raise CodeLibraryError("The brand bundle could not be read")
        text = data.decode("utf-8", errors="replace")
    for index, raw in enumerate(text.splitlines()):
        if index != line:
            continue
        raw = raw.strip()
        if not raw:
            break
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as err:
            raise CodeLibraryError(f"The brand bundle record is invalid: {err}") from err
        device = record.get("device")
        if not isinstance(device, dict):
            raise CodeLibraryError("The brand bundle record has no device.")
        return device
    raise CodeLibraryError(f"The brand bundle does not contain line {line}.")


def entry_profile(entry: LibraryEntry, device: dict[str, Any]) -> ProfileBuild:
    """Convert a fetched device into a validated profile build."""
    try:
        return build_profile(device)
    except SmartIrJsonError as err:
        raise CodeLibraryError(str(err)) from err
