# Mango Power for Home Assistant (unofficial)

Monitor and control **Mango Power E** portable power stations from Home Assistant,
using the same cloud API as the official Mango Power app.

> **Not affiliated with Mango Power.** This uses an undocumented API that may change
> or stop working at any time. Use at your own risk.

## Features

Each unit on your account appears as its own **device** with:

| Type | Entities |
|---|---|
| Sensors (reported) | State of charge, solar power, grid input power, AC output power, DC output power, inverter temperature |
| Sensors (calculated) | Total input power, total output power, **net battery power**, stored energy, energy to full, time to full, time to empty |
| Energy (kWh, for the Energy dashboard) | Solar energy, grid input energy, AC output energy, DC output energy, battery charge energy, battery discharge energy |
| Daily energy (kWh, resets at midnight) | **Solar energy today** (enabled), plus *…today* versions of the others (disabled by default; enable under the device) |
| Switches | AC output, DC output, smart charge, UPS mode |
| Selects | Max AC input current (10/15/30 A), backup SOC (85/90/95 %) |
| Diagnostic | Online, last active, firmware, rated capacity |
| Diagnostic (settings the app doesn't show, read-only) | AC output voltage setting, AC frequency setting, eco reserve SOC, running, 240V mode, timer mode |

Data refreshes every 60 seconds. Commands take effect within ~20 seconds.

### How the calculated values work

- **Net battery power** = (solar + grid input) − (AC output + DC output). Positive = charging,
  negative = discharging. It ignores conversion losses and the unit's own consumption, so it
  reads a little high while charging and a little low while discharging.
- **Stored energy** = state of charge × rated capacity.
- **Time to full / empty** = remaining energy ÷ net battery power. Shows *unknown* when the
  battery is idle (net power within ±5 W).
- **Energy totals** are integrated from the 60-second power readings (trapezoidal rule) and
  survive restarts. Gaps longer than 5 minutes (outages, restarts) are skipped rather than guessed.
  Short spikes between polls are not captured, so treat them as good estimates, not meter readings.

### Rated capacity

Defaults to **3500 Wh** (Mango Power E). If you have expansion batteries, open the integration's
**Configure** and add 3500 Wh per extra battery.

### Energy dashboard

Settings → Dashboards → Energy:

| Energy dashboard section | Use |
|---|---|
| Solar panels | *Solar energy* |
| Home battery storage | *Battery charge energy* (in) and *Battery discharge energy* (out) |
| Grid consumption | *Grid input energy*, if the unit is charged from your mains |
| Individual devices | *AC output energy*, *DC output energy* |

## Supported devices

| Model | Status |
|---|---|
| Mango Power E | ✅ Supported |
| Other Mango models (M, micro-inverters, gateways) | ❌ Not yet (ignored during setup) |

Region: **US** tested. EU/CN/JP/AU use the app's regional servers but are untested.

## Installation

### HACS (recommended)
1. HACS → ⋮ → **Custom repositories** → add `https://github.com/YOUR_GITHUB_USER/ha-mango-power`, category **Integration**.
2. Search **Mango Power**, download, and **restart Home Assistant**.

### Manual
Copy `custom_components/mango_power` into your `/config/custom_components/` folder and restart.

## Setup

**Settings → Devices & services → Add integration → Mango Power (unofficial)**

1. Enter the email you use in the Mango Power app.
2. Choose **Email me a code** (or **Password** if your account has one).
3. Enter the 6-digit code. Your units are discovered automatically.

Only a login token is stored (in Home Assistant's config storage), never your password.
If the token expires, Home Assistant will ask you to log in again.

## Notes

- **30 A input** requires Mango's 30 A cable. The app warns about this; this integration does not.
- Requires internet and Mango's cloud. For local data, see the protocol notes below.
- Mango has two cloud backends; units on the newer one can't receive firmware updates
  through the API, but monitoring and control work on both.

## Local Bluetooth (future)

The units also speak a protobuf protocol over BLE (service `00000001-6D61-6E67-6F70-6F7765722021`,
characteristic `00000006-...`). Units with Wi-Fi module firmware W1.1.9 push realtime data; W1.1.5
only answers Wi-Fi provisioning. Local support is planned.

## License

MIT
