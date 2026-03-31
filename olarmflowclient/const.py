"""Constants for the OlarmFlowClient integration."""

from enum import IntEnum

BASE_URL = "https://api.olarm.com"
MQTT_HOST = "mqtt-pubapi.olarm.com"
MQTT_PORT = 443
MQTT_USER = "public-api-user-v1"
MQTT_KEEPALIVE = 30


class ZonesTypes(IntEnum):
    """Zone sensor types returned in ``deviceProfile.zonesTypes``.

    Each zone on an Olarm-connected alarm panel has a numeric type that
    describes the physical sensor wired to that zone.  The values below are
    the codes returned by the Olarm API in the ``zonesTypes`` list.

    Because ``ZonesTypes`` is an :class:`~enum.IntEnum`, members compare equal
    to their underlying ``int`` and can be used anywhere an ``int`` is
    expected (e.g. as dictionary keys or in ``match`` statements).
    """

    NA = 0
    DOOR = 10
    WINDOW = 11
    MOTION_INDOOR = 20
    MOTION_OUTDOOR = 21
    PANIC_BUTTON = 50
    PANIC_ZONE = 51
    NOT_IN_USE = 90
