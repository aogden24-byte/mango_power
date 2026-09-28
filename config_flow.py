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
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import MangoApi, MangoAuth, MangoAuthError, MangoError, user_id_from_token
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CAPACITY_WH,
    CONF_CODE,
    CONF_EXPIRES_AT,
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


class MangoOptionsFlow(OptionsFlow):
    """Battery capacity (Wh) used for stored energy / time estimates."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_CAPACITY_WH, DEFAULT_CAPACITY_WH)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Required(CONF_CAPACITY_WH, default=current): vol.All(vol.Coerce(int), vol.Range(min=500, max=50000))}
            ),
        )
