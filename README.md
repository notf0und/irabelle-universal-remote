# Irabelle Universal Remote


[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=notf0und&repository=irabelle-universal-remote&category=integration)

A Home Assistant integration for IR appliances: browse a code library, test a code set with the
appliance in front of you, and drive climate, media player, fan and light entities - plus a
browser-only converter for raw IR command payloads.

It supports:
- Broadlink Base64 to Raw MQTT
- Raw MQTT to Broadlink Base64
- Broadlink Base64 to Home Assistant signed raw timings
- Raw MQTT to Home Assistant signed raw timings
- Home Assistant signed raw timings to Raw MQTT
- Home Assistant signed raw timings to Broadlink Base64

No uploads, no Python, everything runs locally.

The converter can process a full SmartIR JSON file or a single pasted IR code.

It can also turn a SmartIR JSON file into a compact `IRP1:` profile code for the reusable Irabelle Universal Remote custom integration. Supported device types are climate, fan, light, and TV/media_player. Each profile creates a native Home Assistant entity and sends commands through a Home Assistant Infrared emitter entity. If the configured emitter is unavailable, the entity is marked unavailable too.

Some climate exports omit the `off` command that a climate profile requires. A missing IR code cannot be derived from the remaining commands, so when a climate file's commands match a known IR protocol (currently the Fujitsu AC family), the converter fills in that protocol's standard power-off frame automatically and reports it in the status line. If the protocol is not recognized, the converter asks for an `off` command to be added instead. Plain format conversions are left untouched.

The website can also decode an existing `IRP1:` profile back into a downloadable SmartIR JSON file. The output command format can be selected as Broadlink, Raw MQTT, or Home Assistant signed raw timings.

The non-climate platforms follow the official SmartIR JSON layouts:

- Fan speed lists are exposed as Home Assistant percentages, with optional forward/reverse direction and oscillation when those commands exist.
- Light brightness and color temperature use SmartIR's relative `brighten`, `dim`, `warmer`, and `colder` commands. The optional `night` command maps to brightness 1.
- TV/media-player entities expose only the controls found in the file. SmartIR `sources` are selectable, and numeric channels are sent using `Channel N` commands.

## Irabelle Universal Remote installation

Irabelle Universal Remote requires Home Assistant 2026.6 or newer.

- [Irabelle Universal Remote integration source](https://github.com/notf0und/irabelle-universal-remote/tree/main/custom_components/smartir_native)

1. Click the **Open in HACS** button above — it opens HACS on this repository and adds it as a custom *Integration* repository. By hand: HACS → ⋮ → *Custom repositories* → `notf0und/irabelle-universal-remote`, category **Integration**.
2. Download Irabelle Universal Remote in HACS and restart Home Assistant.
3. **Settings → Devices & services → Add integration → Irabelle Universal Remote**, then pick one of three sources:
   - **Browse the code library** — point it at a code-library repository (or a local copy of one holding `index.json.gz` and `bundles/`) and search by brand or model. Pinyin works too, so `fushitong` finds Fujitsu. The index is downloaded once and cached for six hours; only the chosen device's bundle is fetched.
   - **SmartIR JSON file** — upload or paste a SmartIR code file (for example `codes/climate/1280.json`). The command format and the entity type are detected automatically, so no conversion step is needed.
   - **IRP1 profile code** — paste a code produced by the website, as before.
4. Choose the Infrared emitter, name the entity, and optionally choose an Infrared receiver.
5. **Test the device**: a power command is transmitted through the emitter and you confirm whether the device responded. If not, **try the next match** from the library search, or save it anyway. Confirming creates the entity.

Downloading and converting a file by hand is no longer required, though the website remains available for single codes, batch conversion, and inspecting profiles.

Install the integration only once. Repeat steps 3-5 for every additional IR device. A receiver is optional; without one, the entity continues to work in transmit-only assumed-state mode. Use the integration's **Configure** button to view or replace the `IRP1:` profile code, change the emitter, and add or replace a receiver later. The replacement code must describe the same device type. The **Reconfigure** menu can also edit these fields together with the device name. Conversion and profile creation happen locally, in the browser or in Home Assistant.

Light profiles may use one shared IR command for both `on` and `off`. Irabelle Universal Remote treats this as a toggle command: Home Assistant avoids redundant transmissions, and an optional receiver toggles the entity state when the physical remote is used.

[Try it!](https://tomer2526.github.io/Convert-Broadlink-Codes-to-Row-MQTT-Format/)

## SmartIR Guide
- [Zigbee SmartIR guide](https://community.home-assistant.io/t/guide-how-to-use-the-zs06-or-ufo-r11-zigbee-ir-controllers-with-smartir/939301?u=tomer11)

## SmartIR Project
- [SmartIR GitHub repository](https://github.com/smartHomeHub/SmartIR/tree/master?tab=readme-ov-file)

## Home Assistant Infrared
- [Home Assistant infrared entity documentation](https://developers.home-assistant.io/docs/core/entity/infrared/)
- [Zigbee IR bridge for native Home Assistant infrared](https://github.com/tomer2526/IR-Wrapper-for-Zigbee-IR-Bluster/tree/main/custom_components/z2m_ir_bridge)
