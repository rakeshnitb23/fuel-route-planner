import io
import json
import socket
from unittest import mock
from urllib.error import HTTPError, URLError

from django.contrib.gis.geos import LineString, Point
from django.test import SimpleTestCase, TestCase

from fuel.models import TruckStop
from fuel.routing import (
    NoRouteError,
    RoutingError,
    RoutingTimeoutError,
    get_route,
    stops_along_route,
)

OSRM_OK = {
    'code': 'Ok',
    'routes': [{
        'distance': 160934.0,  # 100 miles in meters
        'geometry': {'type': 'LineString', 'coordinates': [[-97.0, 32.0], [-96.0, 33.0]]},
    }],
}


def fake_response(payload):
    """Stands in for urlopen(): usable as `with urlopen(...) as resp`."""
    resp = mock.MagicMock()
    resp.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
    return resp


def http_error(status, payload):
    body = io.BytesIO(json.dumps(payload).encode())
    return HTTPError('https://osrm', status, 'error', {}, body)


@mock.patch('fuel.routing.urlopen')
class GetRouteTests(SimpleTestCase):

    def test_success_returns_line_and_miles(self, urlopen):
        urlopen.return_value = fake_response(OSRM_OK)
        line, miles = get_route((-97.0, 32.0), (-96.0, 33.0))
        self.assertEqual(line.srid, 4326)
        self.assertEqual(line.coords, ((-97.0, 32.0), (-96.0, 33.0)))
        self.assertAlmostEqual(miles, 100.0, places=2)

    def test_calls_osrm_once_with_lon_lat_order(self, urlopen):
        urlopen.return_value = fake_response(OSRM_OK)
        get_route((-97.0, 32.0), (-96.0, 33.0))
        self.assertEqual(urlopen.call_count, 1)
        url = urlopen.call_args.args[0]
        self.assertIn('/driving/-97.0,32.0;-96.0,33.0?', url)
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 15)

    def test_timeout_on_read(self, urlopen):
        urlopen.side_effect = TimeoutError('timed out')
        with self.assertRaises(RoutingTimeoutError) as ctx:
            get_route((0, 0), (1, 1))
        self.assertEqual(ctx.exception.details, {'timeout_seconds': 15})

    def test_timeout_on_connect(self, urlopen):
        urlopen.side_effect = URLError(socket.timeout('timed out'))
        with self.assertRaises(RoutingTimeoutError):
            get_route((0, 0), (1, 1))

    def test_network_failure(self, urlopen):
        urlopen.side_effect = URLError('Name or service not known')
        with self.assertRaises(RoutingError) as ctx:
            get_route((0, 0), (1, 1))
        self.assertNotIsInstance(ctx.exception, (RoutingTimeoutError, NoRouteError))
        self.assertNotIn('Name or service', str(ctx.exception))  # raw error stays in the log

    def test_no_route_reply_is_http_400(self, urlopen):
        urlopen.side_effect = http_error(400, {'code': 'NoRoute', 'message': 'Impossible route'})
        with self.assertRaises(NoRouteError):
            get_route((0, 0), (1, 1))

    def test_other_http_error_is_unavailable(self, urlopen):
        urlopen.side_effect = http_error(500, {'code': 'InternalError'})
        with self.assertRaises(RoutingError) as ctx:
            get_route((0, 0), (1, 1))
        self.assertNotIsInstance(ctx.exception, NoRouteError)

    def test_unusable_reply_is_unavailable(self, urlopen):
        urlopen.return_value = fake_response({'code': 'Ok', 'routes': []})
        with self.assertRaises(RoutingError):
            get_route((0, 0), (1, 1))


class StopsAlongRouteTests(TestCase):
    """Corridor filter (ST_DWithin, 10 miles) and mile markers."""

    @classmethod
    def setUpTestData(cls):
        def add(opis_id, lon, lat, price=3.0):
            TruckStop.objects.create(
                opis_id=opis_id, name=f'Stop {opis_id}', address='I-40', city='Town',
                state='TX', price=price, location=Point(lon, lat, srid=4326),
            )
        add(1, -99.0, 35.0)      # on the route, 10% along
        add(2, -95.0, 35.05)     # ~3.5 miles off the route, 50% along
        add(3, -92.0, 35.0)      # on the route, 80% along
        add(4, -95.0, 35.5)      # ~35 miles off the route: outside the corridor
        cls.route = LineString([(-100.0, 35.0), (-90.0, 35.0)], srid=4326)

    def test_only_stops_within_10_miles(self):
        stops = stops_along_route(self.route, 600)
        self.assertEqual([s['name'] for s in stops], ['Stop 1', 'Stop 2', 'Stop 3'])

    def test_mile_markers_sorted_and_proportional(self):
        stops = stops_along_route(self.route, 600)
        miles = [s['mile_marker'] for s in stops]
        self.assertEqual(miles, sorted(miles))
        for actual, expected in zip(miles, [60, 300, 480]):
            self.assertAlmostEqual(actual, expected, delta=0.5)

    def test_stop_fields(self):
        stop = stops_along_route(self.route, 600)[0]
        self.assertEqual(set(stop), {'id', 'name', 'city', 'state', 'price',
                                     'mile_marker', 'lon', 'lat'})
        self.assertIsInstance(stop['price'], float)
