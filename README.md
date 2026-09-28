# Mango Power E for Home Assistant (unofficial)

See and control your **Mango Power E** power stations from Home Assistant: battery level,
solar, charging, outputs, energy totals and more. Each unit shows up as its own device,
just like ESPHome or any other integration.

It talks to the same cloud service the official Mango Power app uses, so if your units
work in the app, they will work here.

> **Not affiliated with Mango Power.** This uses the app's undocumented API, which Mango
> could change at any time. Use at your own risk.

---

## What you get

For every Mango Power E on your account:

| | |
|---|---|
| 🔋 **Battery** | State of charge, stored energy (kWh), energy to full, time to full, time to empty |
| ☀️ **Power** | Solar input, grid (AC) input, AC output, DC output, total in/out, **net charge rate** |
| 📊 **Energy** | kWh totals for the Energy dashboard: solar, grid input, AC/DC output, battery charge/discharge, plus **Solar energy today** |
| 🎛️ **Controls** | AC output, DC output, smart charge and UPS mode switches; max AC input current and backup SOC selectors |
| 🩺 **Diagnostics** | Online, last seen, firmware, inverter temperature, AC voltage/frequency settings, and more |

Values refresh every 60 seconds. Commands usually take effect within 20 seconds.

---

## Before you start

You need:

- **Home Assistant** 2025.1 or newer
- **[HACS](https://hacs.xyz)** installed
- A **Mango Power E** that is online in the **Mango Power app** (on Wi-Fi)
- The **email address** you use to log in to the Mango Power app

---

## Step 1 — Install

1. In Home Assistant, open **HACS**.
2. Click the **⋮** menu (top right) → **Custom repositories**.
3. Paste `https://github.com/aogden24-byte/mango_power`, set **Type** to **Integration**, click **Add**.
4. Search HACS for **Mango Power**, open it and click **Download**.
5. **Restart Home Assistant** (Settings → System → ⏻ → Restart).

<details>
<summary>Installing without HACS</summary>

Download this repository, copy the `custom_components/mango_power` folder into your
Home Assistant `/config/custom_components/` folder (the folder name must be exactly
`mango_power`), then restart Home Assistant.
</details>

## Step 2 — Log in

1. Go to **Settings → Devices & services → Add integration**.
2. Search for **Mango Power (unofficial)**.
3. Enter the email you use in the Mango Power app.
4. Choose **Email me a code** (recommended) or **Password** if your account has one.
5. Enter the 6-digit code from your inbox.

That's it — your units are found automatically. Your password is never stored; only a
login token is kept in Home Assistant, and it renews itself.

## Step 3 — Check your devices

Open **Settings → Devices & services → Mango Power**. You should see one device per unit,
named as in the Mango app (e.g. "Mango E1"). Open one and compare the battery level and
power readings with the app.

## Step 4 (optional) — Set your battery capacity

Stored energy and time-to-full/empty are calculated from the battery capacity.
The default is **3500 Wh** (one Mango Power E).

On the Mango Power integration, click **Configure** and enter your capacity in Wh.
If you have expansion batteries, add 3500 Wh for each one.

## Step 5 (optional) — Energy dashboard

Go to **Settings → Dashboards → Energy** and add:

| Energy dashboard section | Choose |
|---|---|
| Solar panels | *Solar energy* |
| Home battery storage | *Battery charge energy* (in) and *Battery discharge energy* (out) |
| Grid consumption | *Grid input energy* (if you charge the unit from the wall) |
| Individual devices | *AC output energy*, *DC output energy* |

Give it an hour or two to collect data before the charts fill in.

---

## Using the controls

| Control | What it does |
|---|---|
| **AC output** | Turns the AC outlets (inverter) on/off |
| **DC output** | Turns the 12 V / USB outputs on/off |
| **Smart charge** | Same as the app's smart charge toggle |
| **UPS mode** | Same as the app's UPS toggle |
| **Max AC input current** | 10 / 15 / 30 A wall-charging limit |
| **Backup SOC** | 85 / 90 / 95 % reserve used by UPS mode |

> ⚠️ **30 A needs Mango's 30 A charging cable.** The app warns you about this;
> Home Assistant does not. Only pick 30 A if that cable is connected.

---

## How the calculated numbers work

- **Net charge rate** (*Net battery power*) = (solar + grid input) − (AC output + DC output).
  Positive means charging, negative means discharging. It ignores conversion losses, so
  treat it as a close estimate.
- **Stored energy** = battery % × capacity. **Energy to full** = the remaining %.
- **Time to full / empty** = energy left ÷ net charge rate. Shows *unknown* when idle.
- **Energy totals** are built from the 60-second readings and survive restarts. They are
  good estimates, not utility-meter accurate.

---

## Troubleshooting

**"Mango Power" doesn't appear in Add integration**
Restart Home Assistant after downloading. For manual installs, check the folder is
`/config/custom_components/mango_power/` and contains `__init__.py`.

**"No setup function defined" or "could not be loaded"**
The files are in the wrong place or incomplete — reinstall from HACS (⋮ → Redownload)
and restart.

**The login code never arrives**
Check spam, and make sure it's the same email you use in the Mango Power app.

**Devices show "unavailable"**
The unit is offline in Mango's cloud (Wi-Fi down, unit off) or Mango's servers are down.
Check the Mango app — if it's offline there too, it's not Home Assistant.

**A switch or setting didn't change**
Commands go through Mango's cloud and can take ~20 seconds. The unit must be online.

**Home Assistant asks me to log in again**
Your login token expired. Home Assistant shows a prompt on the Mango Power integration —
follow it and enter a new code from your email.

---

## Feedback and problems

- 💬 **Questions, "does it work with my unit?", ideas:** [Discussions](https://github.com/aogden24-byte/mango_power/discussions)
- 🐞 **Bugs:** [open an issue](https://github.com/aogden24-byte/mango_power/issues/new/choose)

When reporting a bug, a debug log helps a lot:

1. **Settings → Devices & services → Mango Power → ⋮ → Enable debug logging.**
2. Reproduce the problem.
3. **⋮ → Disable debug logging** — a log file downloads.
4. Attach it to your issue (it contains no password; remove your email if you like).

---

## Supported devices and regions

| | Status |
|---|---|
| Mango Power E | ✅ Supported |
| Other Mango models (M, micro-inverters, gateways) | ❌ Not yet — skipped during setup |
| US region | ✅ Tested |
| EU / JP / AU / CN | ⚠️ Should work, untested — please report back! |

## Local Bluetooth (future)

Mango Power E units also talk over Bluetooth using a protobuf protocol (service
`00000001-6D61-6E67-6F70-6F7765722021`, characteristic `00000006-…`). Units with Wi-Fi
module firmware W1.1.9 send live data; W1.1.5 only answers Wi-Fi setup. Local support
is planned.

## License

[MIT](LICENSE)
