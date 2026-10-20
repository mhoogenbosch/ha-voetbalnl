"""Config flow for the Voetbal.nl integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .api import (
    VoetbalNLApi,
    VoetbalNLAuthError,
    VoetbalNLConnectionError,
    normalize_team_url,
)
from .const import (
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_TEAM_NAME,
    CONF_TEAM_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ERROR_AUTH_FAILED,
    EXAMPLE_TEAM_URL,
    ERROR_CANNOT_CONNECT,
    ERROR_INVALID_TEAM_URL,
    ERROR_TEAM_NOT_FOUND,
    ERROR_UNKNOWN,
)

_LOGGER = logging.getLogger(__name__)

STEP_AUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.EMAIL,
                autocomplete="username",
            )
        ),
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.PASSWORD,
                autocomplete="current-password",
            )
        ),
    }
)

STEP_TEAM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TEAM_NAME): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        vol.Optional(CONF_TEAM_URL, default=""): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
        ),
    }
)

class VoetbalNLConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Voetbal.nl."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._auth_data: dict[str, Any] = {}
        self._api: VoetbalNLApi | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Collect and validate credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api = VoetbalNLApi()
            try:
                await api.login(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except VoetbalNLAuthError:
                errors["base"] = ERROR_AUTH_FAILED
            except VoetbalNLConnectionError:
                errors["base"] = ERROR_CANNOT_CONNECT
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during authentication")
                errors["base"] = ERROR_UNKNOWN
            else:
                self._auth_data = {
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                }
                self._api = api
                return await self.async_step_team()
            finally:
                if errors:
                    await api.close()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_AUTH_SCHEMA,
            errors=errors,
        )

    async def async_step_team(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Collect and validate team information."""
        errors: dict[str, str] = {}

        if user_input is not None:
            team_name = user_input[CONF_TEAM_NAME].strip()
            raw_url = (user_input.get(CONF_TEAM_URL) or "").strip()
            team_url = normalize_team_url(raw_url) if raw_url else None
            if raw_url and not team_url:
                errors["base"] = ERROR_INVALID_TEAM_URL

            # If team URL not provided, search by name
            if not raw_url:
                try:
                    assert self._api is not None
                    teams = await self._api.search_team(team_name)
                    if teams:
                        # Use the first (best) match
                        team_url = teams[0].url
                        # Use the canonical name from the search result
                        if teams[0].name:
                            team_name = teams[0].name
                        _LOGGER.debug(
                            "Found team '%s' at %s", team_name, team_url
                        )
                    else:
                        errors["base"] = ERROR_TEAM_NOT_FOUND
                except VoetbalNLConnectionError:
                    errors["base"] = ERROR_CANNOT_CONNECT
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Unexpected error searching for team")
                    errors["base"] = ERROR_UNKNOWN

            if not errors and team_url:
                # Prevent duplicate entries for the same team
                await self.async_set_unique_id(team_name.lower())
                self._abort_if_unique_id_configured()

                if self._api:
                    await self._api.close()

                return self.async_create_entry(
                    title=f"Voetbal.nl – {team_name}",
                    data={
                        **self._auth_data,
                        CONF_TEAM_NAME: team_name,
                        CONF_TEAM_URL: team_url,
                        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                    },
                )

        return self.async_show_form(
            step_id="team",
            data_schema=STEP_TEAM_SCHEMA,
            errors=errors,
            description_placeholders={"example_url": EXAMPLE_TEAM_URL},
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when the session has expired."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm re-authentication."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api = VoetbalNLApi()
            try:
                await api.login(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except VoetbalNLAuthError:
                errors["base"] = ERROR_AUTH_FAILED
            except VoetbalNLConnectionError:
                errors["base"] = ERROR_CANNOT_CONNECT
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during re-authentication")
                errors["base"] = ERROR_UNKNOWN
            else:
                await api.close()
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
            finally:
                if errors:
                    await api.close()

        # Prefill the e-mail address; only the password usually needs retyping
        username = (user_input or {}).get(CONF_USERNAME) or (
            self._get_reauth_entry().data.get(CONF_USERNAME, "")
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_AUTH_SCHEMA, {CONF_USERNAME: username}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return VoetbalNLOptionsFlow()


class VoetbalNLOptionsFlow(OptionsFlow):
    """Options flow for adjusting scan interval and team details."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            raw_url = (user_input.get(CONF_TEAM_URL) or "").strip()
            if raw_url:
                team_url = normalize_team_url(raw_url)
                if team_url:
                    return self.async_create_entry(
                        data={**user_input, CONF_TEAM_URL: team_url}
                    )
                errors["base"] = ERROR_INVALID_TEAM_URL
            else:
                # Empty field = use the URL from setup
                data = dict(user_input)
                data.pop(CONF_TEAM_URL, None)
                return self.async_create_entry(data=data)

        current = self.config_entry.data
        options = self.config_entry.options

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=options.get(
                        CONF_SCAN_INTERVAL,
                        current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5, max=1440, step=5, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    CONF_TEAM_URL,
                    default=options.get(
                        CONF_TEAM_URL, current.get(CONF_TEAM_URL, "")
                    ),
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={"example_url": EXAMPLE_TEAM_URL},
        )
