"""Services for the seventeentrack integration."""

from typing import Any, Final

from pyseventeentrack import Client as SeventeenTrackClient
from pyseventeentrack.errors import InvalidTrackingNumberError, RequestError
from pyseventeentrack.package import PACKAGE_STATUS_MAP, Package
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_CONFIG_ENTRY_ID, ATTR_FRIENDLY_NAME, ATTR_LOCATION
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, selector
from homeassistant.util import slugify

from . import SeventeenTrackCoordinator
from .const import (
    ATTR_DESTINATION_COUNTRY,
    ATTR_INFO_TEXT,
    ATTR_ORIGIN_COUNTRY,
    ATTR_PACKAGE_DESTINATION_COUNTRY,
    ATTR_PACKAGE_FRIENDLY_NAME,
    ATTR_PACKAGE_PARAM,
    ATTR_PACKAGE_PHONE,
    ATTR_PACKAGE_STATE,
    ATTR_PACKAGE_TRACKING_NUMBER,
    ATTR_PACKAGE_TYPE,
    ATTR_STATUS,
    ATTR_TIMESTAMP,
    ATTR_TRACKING_INFO_LANGUAGE,
    ATTR_TRACKING_NUMBER,
    DOMAIN,
    SERVICE_ADD_PACKAGE,
    SERVICE_ARCHIVE_PACKAGE,
    SERVICE_GET_PACKAGES,
)

SERVICE_GET_PACKAGES_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_PACKAGE_STATE): selector.SelectSelector(
            selector.SelectSelectorConfig(
                multiple=True,
                options=[
                    value.lower().replace(" ", "_")
                    for value in PACKAGE_STATUS_MAP.values()
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key=ATTR_PACKAGE_STATE,
            )
        ),
    }
)

SERVICE_ADD_PACKAGE_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_PACKAGE_TRACKING_NUMBER): cv.string,
        vol.Required(ATTR_PACKAGE_FRIENDLY_NAME): cv.string,
        vol.Optional(ATTR_PACKAGE_PARAM): cv.string,
        vol.Optional(ATTR_PACKAGE_PHONE): cv.string,
        vol.Optional(ATTR_PACKAGE_DESTINATION_COUNTRY): cv.string,
    }
)

SERVICE_ARCHIVE_PACKAGE_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_PACKAGE_TRACKING_NUMBER): cv.string,
    }
)

API_URL_BUYER: Final = "https://buyer.17track.net/orderapi/call"


async def _add_package_with_params(
    client: SeventeenTrackClient,
    tracking_number: str,
    friendly_name: str | None = None,
    param: str | None = None,
    phone: str | None = None,
    destination_country: str | None = None,
) -> None:
    """Add a package with additional parameters to 17Track.

    This function extends the pyseventeentrack library to support additional
    parameters required by certain carriers (e.g., GLS, PostNL) such as
    postal code, phone number, and destination country.
    
    The param field can contain:
    - Postal code only: "3078CM"
    - Country-Postal: "NL-1234AB" or "FR-75001"
    - Phone format varies by carrier
    """
    # Build the request parameters
    api_params: dict[str, Any] = {"TrackNos": [tracking_number]}
    
    # Add optional parameters at the same level as TrackNos
    if param:
        api_params["Param"] = param
    if phone:
        api_params["Phone"] = phone
    if destination_country:
        api_params["DestinationCountry"] = destination_country

    # Call the API directly using the client's request method
    add_resp: dict = await client._request(  # noqa: SLF001
        "post",
        API_URL_BUYER,
        json={
            "version": "1.0",
            "method": "AddTrackNo",
            "param": api_params,
        },
    )

    code = add_resp.get("Code")
    if code != 0:
        raise RequestError(f"Non-zero status code in response: {code}")

    if not friendly_name:
        return

    # Set the friendly name after adding the package
    packages = await client.profile.packages()
    try:
        new_package = next(p for p in packages if p.tracking_number == tracking_number)
    except StopIteration as err:
        raise InvalidTrackingNumberError(
            f"Recently added package not found by tracking number: {tracking_number}"
        ) from err

    await client.profile.set_friendly_name(new_package.id, friendly_name)


async def _get_packages(call: ServiceCall) -> ServiceResponse:
    """Get packages from 17Track."""
    config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
    package_states = call.data.get(ATTR_PACKAGE_STATE, [])

    await _validate_service(call.hass, config_entry_id)

    seventeen_coordinator: SeventeenTrackCoordinator = call.hass.data[DOMAIN][
        config_entry_id
    ]
    live_packages = sorted(
        await seventeen_coordinator.client.profile.packages(
            show_archived=seventeen_coordinator.show_archived
        )
    )

    return {
        "packages": [
            _package_to_dict(package)
            for package in live_packages
            if slugify(package.status) in package_states or package_states == []
        ]
    }


async def _add_package(call: ServiceCall) -> None:
    """Add a new package to 17Track."""
    config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
    tracking_number = call.data[ATTR_PACKAGE_TRACKING_NUMBER]
    friendly_name = call.data[ATTR_PACKAGE_FRIENDLY_NAME]
    param = call.data.get(ATTR_PACKAGE_PARAM)
    phone = call.data.get(ATTR_PACKAGE_PHONE)
    destination_country = call.data.get(ATTR_PACKAGE_DESTINATION_COUNTRY)

    await _validate_service(call.hass, config_entry_id)

    seventeen_coordinator: SeventeenTrackCoordinator = call.hass.data[DOMAIN][
        config_entry_id
    ]

    # Check if additional parameters are provided
    if param or phone or destination_country:
        # Use custom API call with additional parameters
        await _add_package_with_params(
            seventeen_coordinator.client,
            tracking_number,
            friendly_name,
            param,
            phone,
            destination_country,
        )
    else:
        # Use standard library method
        await seventeen_coordinator.client.profile.add_package(
            tracking_number, friendly_name
        )


async def _archive_package(call: ServiceCall) -> None:
    config_entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
    tracking_number = call.data[ATTR_PACKAGE_TRACKING_NUMBER]

    await _validate_service(call.hass, config_entry_id)

    seventeen_coordinator: SeventeenTrackCoordinator = call.hass.data[DOMAIN][
        config_entry_id
    ]

    await seventeen_coordinator.client.profile.archive_package(tracking_number)


def _package_to_dict(package: Package) -> dict[str, Any]:
    result = {
        ATTR_DESTINATION_COUNTRY: package.destination_country,
        ATTR_ORIGIN_COUNTRY: package.origin_country,
        ATTR_PACKAGE_TYPE: package.package_type,
        ATTR_TRACKING_INFO_LANGUAGE: package.tracking_info_language,
        ATTR_TRACKING_NUMBER: package.tracking_number,
        ATTR_LOCATION: package.location,
        ATTR_STATUS: package.status,
        ATTR_INFO_TEXT: package.info_text,
        ATTR_FRIENDLY_NAME: package.friendly_name,
    }
    if timestamp := package.timestamp:
        result[ATTR_TIMESTAMP] = timestamp.isoformat()
    return result


async def _validate_service(hass: HomeAssistant, config_entry_id: str) -> None:
    entry: ConfigEntry | None = hass.config_entries.async_get_entry(config_entry_id)
    if not entry:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_config_entry",
            translation_placeholders={
                "config_entry_id": config_entry_id,
            },
        )
    if entry.state != ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unloaded_config_entry",
            translation_placeholders={
                "config_entry_id": entry.title,
            },
        )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up the services for the seventeentrack integration."""

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_PACKAGES,
        _get_packages,
        schema=SERVICE_GET_PACKAGES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_PACKAGE,
        _add_package,
        schema=SERVICE_ADD_PACKAGE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ARCHIVE_PACKAGE,
        _archive_package,
        schema=SERVICE_ARCHIVE_PACKAGE_SCHEMA,
    )
