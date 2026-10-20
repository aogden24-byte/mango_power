"""UI setup: email -> emailed code (or password) -> done."""
from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import MangoApi, MangoAuth, MangoAuthError, MangoError, user_id_from_token
from .const import (
    BLE_MODE_AUTO,
    BLE_MODE_OFF,
    BLE_OPTION_PREFIX,
    CONF_ACCESS_TOKEN,
    CONF_CAPACITY_WH,
    CONF_CODE,
    CONF_EXPIRES_AT,
    CONF_EXPORT_PRICE,
    CONF_IMPORT_PRICE,
    CONF_RAW_FIELDS,
    DEFAULT_PRICE,
    CONF_METHOD,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    CONF_USER_ID,
    DEFAULT_CAPACITY_WH,
    DEFAULT_REGION,
    DOMAIN,
    REGIONS,
)

_LOGGER = logging.getLogger(__name__)

METHOD_CODE = "code"
METHOD_PASSWORD = "password"


class MangoPowerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MangoOptionsFlow()

    def __init__(self) -> None:
        self._email: str = ""
        self._region: str = DEFAULT_REGION

    def _auth(self) -> MangoAuth:
        return MangoAuth(async_get_clientsession(self.hass))

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip().lower()
            self._region = user_input[CONF_REGION]
            if user_input[CONF_METHOD] == METHOD_PASSWORD:
                return await self.async_step_password()
            try:
                await self._auth().send_code(self._email)
            except MangoAuthError as err:
                _LOGGER.warning("Sending code failed: %s", err)
                errors["base"] = "send_failed"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error sending code")
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_code()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=self._email): str,
                    vol.Required(CONF_METHOD, default=METHOD_CODE): SelectSelector(
                        SelectSelectorConfig(
                            options=[METHOD_CODE, METHOD_PASSWORD],
                            translation_key="method",
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(CONF_REGION, default=self._region): vol.In(list(REGIONS)),
                }
            ),
            errors=errors,
        )

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                sess = await self._auth().verify_code(self._email, user_input[CONF_CODE].strip())
            except MangoAuthError:
                errors["base"] = "invalid_code"
            else:
                return await self._finish(sess)
        return self.async_show_form(
            step_id="code",
            data_schema=vol.Schema({vol.Required(CONF_CODE): str}),
            description_placeholders={"email": self._email},
            errors=errors,
        )

    async def async_step_password(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                sess = await self._auth().password(self._email, user_input[CONF_PASSWORD])
            except MangoAuthError:
                errors["base"] = "invalid_auth"
            else:
                return await self._finish(sess)
        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": self._email},
            errors=errors,
        )

    async def _finish(self, sess: dict[str, Any]) -> ConfigFlowResult:
        uid = user_id_from_token(sess["access_token"])
        data = {
            CONF_EMAIL: self._email,
            CONF_REGION: self._region,
            CONF_USER_ID: uid,
            CONF_ACCESS_TOKEN: sess["access_token"],
            CONF_REFRESH_TOKEN: sess["refresh_token"],
            CONF_EXPIRES_AT: sess.get("expires_at") or time.time() + sess.get("expires_in", 3600),
        }
        await self.async_set_unique_id(uid)
        if self.source == "reauth":
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
        self._abort_if_unique_id_configured()

        # Sanity check: can we list devices with this login?
        api = MangoApi(async_get_clientsession(self.hass), REGIONS[self._region],
                       {"access_token": data[CONF_ACCESS_TOKEN], "refresh_token": data[CONF_REFRESH_TOKEN],
                        "expires_at": data[CONF_EXPIRES_AT]}, lambda t: None)
        try:
            await api.list_devices()
        except MangoError as err:
            _LOGGER.warning("Login worked but listing devices failed: %s", err)
        return self.async_create_entry(title=f"Mango Power ({self._email})", data=data)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._email = entry_data.get(CONF_EMAIL, "")
        self._region = entry_data.get(CONF_REGION, DEFAULT_REGION)
        return await self.async_step_user()


RAW_LABEL = "Advanced: create a sensor for every Bluetooth protocol field"


class MangoOptionsFlow(OptionsFlow):
    """Battery capacity, energy prices, plus Bluetooth mode per Mango Power E unit."""

    def _e_coords(self) -> list:
        coords = getattr(getattr(self.config_entry, "runtime_data", None), "coordinators", None) or []
        return [c for c in coords if getattr(c, "model", "mpe") == "mpe"]

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        labels = {f"Bluetooth - {c.device_name} (...{c.sn[-3:]})": c for c in self._e_coords()}

        if user_input is not None:
            data = {
                CONF_CAPACITY_WH: user_input[CONF_CAPACITY_WH],
                CONF_EXPORT_PRICE: user_input[CONF_EXPORT_PRICE],
                CONF_IMPORT_PRICE: user_input[CONF_IMPORT_PRICE],
                CONF_RAW_FIELDS: bool(user_input.get(RAW_LABEL, False)),
            }
            for label, coord in labels.items():
                data[f"{BLE_OPTION_PREFIX}{coord.sn}"] = user_input.get(label, BLE_MODE_AUTO)
            return self.async_create_entry(data=data)

        opts = self.config_entry.options
        schema: dict = {
            vol.Required(CONF_CAPACITY_WH, default=opts.get(CONF_CAPACITY_WH, DEFAULT_CAPACITY_WH)):
                vol.All(vol.Coerce(int), vol.Range(min=500, max=50000)),
            vol.Required(CONF_EXPORT_PRICE, default=opts.get(CONF_EXPORT_PRICE, DEFAULT_PRICE)):
                vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
            vol.Required(CONF_IMPORT_PRICE, default=opts.get(CONF_IMPORT_PRICE, DEFAULT_PRICE)):
                vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        }
        for label, coord in labels.items():
            current = opts.get(f"{BLE_OPTION_PREFIX}{coord.sn}", BLE_MODE_AUTO)
            choices = [
                {"value": BLE_MODE_AUTO, "label": "Auto - best signal, automatic failover"},
                {"value": BLE_MODE_OFF, "label": "Off - cloud only"},
            ]
            for source, name, rssi in self._scanners(coord):
                choices.append({"value": source, "label": f"Only via {name} ({rssi} dBm)"})
            if current not in [c["value"] for c in choices]:
                choices.append({"value": current, "label": f"Only via {current} (not in range right now)"})
            schema[vol.Required(label, default=current)] = SelectSelector(
                SelectSelectorConfig(options=choices, mode=SelectSelectorMode.DROPDOWN)
            )
        # Advanced option last, as an explicit toggle selector with its own label
        schema[vol.Required(RAW_LABEL, default=bool(opts.get(CONF_RAW_FIELDS, False)))] = BooleanSelector()
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))

    def _scanners(self, coord) -> list[tuple[str, str, int]]:
        if "bluetooth" not in self.hass.config.components:
            return []
        from .ble import find_address, scanners_for

        address = (coord.ble_link.address if coord.ble_link and coord.ble_link.address else None) \
            or find_address(self.hass, coord.sn)
        return scanners_for(self.hass, address) if address else []
