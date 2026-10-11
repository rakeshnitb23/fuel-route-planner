import json
import logging
import socket
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from django.contrib.gis.geos import LineString, Point
from django.contrib.gis.measure import D

from fuel.models import TruckStop

OSRM_URL = (
    'https://router.project-osrm.org/route/v1/driving/'
    '{start_lon},{start_lat};{end_lon},{end_lat}?overview=simplified&geometries=geojson'
)
OSRM_TIMEOUT = 15
METERS_PER_MILE = 1609.34
CORRIDOR_MILES = 10
NO_ROUTE_CODES = {'NoRoute', 'NoSegment'}

logger = logging.getLogger(__name__)


class RoutingError(Exception):
    """The routing service failed or sent an unusable reply."""

    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


class RoutingTimeoutError(RoutingError):
    """The routing service did not answer within OSRM_TIMEOUT seconds."""


class NoRouteError(RoutingError):
    """The routing service worked, but there is no drivable road route."""


def _is_timeout(exc):
    reason = getattr(exc, 'reason', exc)
    return isinstance(reason, (TimeoutError, socket.timeout))


def get_route(start, end):
    """One OSRM call. start/end are (lon, lat). Returns (LineString SRID 4326, total miles)."""
    url = OSRM_URL.format(start_lon=start[0], start_lat=start[1], end_lon=end[0], end_lat=end[1])
    try:
        with urlopen(url, timeout=OSRM_TIMEOUT) as resp:
            data = json.load(resp)
    except HTTPError as exc:
        # OSRM answers "no route" with HTTP 400 and a JSON body.
        try:
            data = json.load(exc)
        except ValueError:
            data = {}
        if data.get('code') not in NO_ROUTE_CODES:
            logger.warning('OSRM HTTP %s: %s', exc.code, data or exc)
            raise RoutingError('Routing service unavailable. Try again later.') from exc
    except (URLError, TimeoutError, ValueError) as exc:
        if _is_timeout(exc):
            logger.warning('OSRM timed out after %ss', OSRM_TIMEOUT)
            raise RoutingTimeoutError(
                f'Routing service did not respond within {OSRM_TIMEOUT} seconds. Try again later.',
                timeout_seconds=OSRM_TIMEOUT,
            ) from exc
        logger.warning('OSRM request failed: %s', exc)
        raise RoutingError('Routing service unavailable. Try again later.') from exc

    if data.get('code') in NO_ROUTE_CODES:
        raise NoRouteError('No drivable road route between these locations.')
    if data.get('code') != 'Ok' or not data.get('routes'):
        logger.warning('OSRM unusable reply: %s', data)
        raise RoutingError('Routing service unavailable. Try again later.')

    route = data['routes'][0]
    line = LineString(route['geometry']['coordinates'], srid=4326)
    return line, route['distance'] / METERS_PER_MILE


def stops_along_route(route, total_miles):
    """Stops within the corridor, each with a mile marker, sorted by mile marker."""
    nearby = TruckStop.objects.filter(
        location__dwithin=(route, D(m=CORRIDOR_MILES * METERS_PER_MILE))
    )
    stops = []
    for stop in nearby:
        point = Point(stop.location.x, stop.location.y, srid=4326)
        stops.append({
            'id': stop.id,
            'name': stop.name,
            'city': stop.city,
            'state': stop.state,
            'price': float(stop.price),
            'mile_marker': route.project_normalized(point) * total_miles,
            'lon': stop.location.x,
            'lat': stop.location.y,
        })
    stops.sort(key=lambda s: s['mile_marker'])
    return stops
