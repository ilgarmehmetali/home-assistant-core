"""Support for LG webOS Smart TV."""

from __future__ import annotations

from contextlib import suppress
import logging
from typing import NamedTuple

from aiowebostv import WebOsClient, WebOsTvPairError
import voluptuous as vol

from homeassistant.components import notify as hass_notify
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_COMMAND,
    ATTR_ENTITY_ID,
    CONF_CLIENT_SECRET,
    CONF_HOST,
    CONF_NAME,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import Event, HomeAssistant, ServiceCall, SupportsResponse, ServiceResponse
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv, discovery
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_BUTTON,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_PAYLOAD,
    ATTR_SOUND_OUTPUT,
    DATA_CONFIG_ENTRY,
    DATA_HASS_CONFIG,
    DOMAIN,
    PLATFORMS,
    SERVICE_BUTTON,
    SERVICE_COMMAND,
    SERVICE_SELECT_SOUND_OUTPUT,
    WEBOSTV_EXCEPTIONS,
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CALL_SCHEMA = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.comp_entity_ids})


class ServiceMethodDetails(NamedTuple):
    """Details for SERVICE_TO_METHOD mapping."""

    method: str
    schema: vol.Schema
    supports_response: SupportsResponse


BUTTON_SCHEMA = CALL_SCHEMA.extend({vol.Required(ATTR_BUTTON): cv.string})

COMMAND_SCHEMA = CALL_SCHEMA.extend(
    {vol.Required(ATTR_COMMAND): cv.string, vol.Optional(ATTR_PAYLOAD): dict}
)

SOUND_OUTPUT_SCHEMA = CALL_SCHEMA.extend({vol.Required(ATTR_SOUND_OUTPUT): cv.string})

SERVICE_TO_METHOD = {
    SERVICE_BUTTON: ServiceMethodDetails(method="async_button", schema=BUTTON_SCHEMA, supports_response=SupportsResponse.NONE),
    SERVICE_COMMAND: ServiceMethodDetails(
        method="async_command", schema=COMMAND_SCHEMA, supports_response=SupportsResponse.ONLY,
    ),
    SERVICE_SELECT_SOUND_OUTPUT: ServiceMethodDetails(
        method="async_select_sound_output",
        schema=SOUND_OUTPUT_SCHEMA,
        supports_response=SupportsResponse.NONE,
    ),
}


async def async_button(client, button: str) -> None:
    """Send a button press."""
    return await client.button(button)

async def async_command(client, command: str, **kwargs: Any):
    """Send a command."""
    return await client.request(command, payload=kwargs.get(ATTR_PAYLOAD))

async def async_select_sound_output(client, sound_output: str) -> None:
    """Select the sound output."""
    return await client.change_sound_output(sound_output)

methods = {
    'async_button': async_button,
    'async_command': async_command,
    'async_select_sound_output': async_select_sound_output,
}
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the LG WebOS TV platform."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(DATA_CONFIG_ENTRY, {})
    hass.data[DOMAIN][DATA_HASS_CONFIG] = config

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set the config entry up."""
    host = entry.data[CONF_HOST]
    key = entry.data[CONF_CLIENT_SECRET]

    # Attempt a connection, but fail gracefully if tv is off for example.
    client = WebOsClient(host, key)
    with suppress(*WEBOSTV_EXCEPTIONS):
        try:
            await client.connect()
        except WebOsTvPairError as err:
            raise ConfigEntryAuthFailed(err) from err

    # If pairing request accepted there will be no error
    # Update the stored key without triggering reauth
    update_client_key(hass, entry, client)

    async def async_service_handler(service: ServiceCall) -> None:
        method = SERVICE_TO_METHOD[service.service]

        params = {
            key: value for key, value in service.data.items() if key != ATTR_ENTITY_ID
        }

        target_players = hass.data[DOMAIN][DATA_CONFIG_ENTRY]
        if target_players:
            params["client"] = next(iter(target_players.values()))  # Get the first player
            return await methods.get(method.method)(**params)
        raise Exception("No player found!")

    for service, method in SERVICE_TO_METHOD.items():
        hass.services.async_register(
            DOMAIN, service, async_service_handler, schema=method.schema, supports_response=method.supports_response
        )

    hass.data[DOMAIN][DATA_CONFIG_ENTRY][entry.entry_id] = client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # set up notify platform, no entry support for notify component yet,
    # have to use discovery to load platform.
    hass.async_create_task(
        discovery.async_load_platform(
            hass,
            "notify",
            DOMAIN,
            {
                CONF_NAME: entry.title,
                ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            },
            hass.data[DOMAIN][DATA_HASS_CONFIG],
        )
    )

    if not entry.update_listeners:
        entry.async_on_unload(entry.add_update_listener(async_update_options))

    async def async_on_stop(_event: Event) -> None:
        """Unregister callbacks and disconnect."""
        client.clear_state_update_callbacks()
        await client.disconnect()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_on_stop)
    )
    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_control_connect(host: str, key: str | None) -> WebOsClient:
    """LG Connection."""
    client = WebOsClient(host, key)
    try:
        await client.connect()
    except WebOsTvPairError:
        _LOGGER.warning("Connected to LG webOS TV %s but not paired", host)
        raise

    return client


def update_client_key(
    hass: HomeAssistant, entry: ConfigEntry, client: WebOsClient
) -> None:
    """Check and update stored client key if key has changed."""
    host = entry.data[CONF_HOST]
    key = entry.data[CONF_CLIENT_SECRET]

    if client.client_key != key:
        _LOGGER.debug("Updating client key for host %s", host)
        data = {CONF_HOST: host, CONF_CLIENT_SECRET: client.client_key}
        hass.config_entries.async_update_entry(entry, data=data)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        client = hass.data[DOMAIN][DATA_CONFIG_ENTRY].pop(entry.entry_id)
        await hass_notify.async_reload(hass, DOMAIN)
        client.clear_state_update_callbacks()
        await client.disconnect()

    # unregister service calls, check if this is the last entry to unload
    if unload_ok and not hass.data[DOMAIN][DATA_CONFIG_ENTRY]:
        for service in SERVICE_TO_METHOD:
            hass.services.async_remove(DOMAIN, service)

    return unload_ok
