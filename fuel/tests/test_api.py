from unittest import mock

from django.contrib.gis.geos import LineString, Point
from django.core.cache import cache
from rest_framework.test import APITestCase

from fuel.models import TruckStop
from fuel.routing import NoRouteError, RoutingError, RoutingTimeoutError

URL = '/api/route/'
MAP_URL = '/api/route/map/'

# A straight 600-mile route along latitude 35. OSRM is never called in these tests.
ROUTE = LineString([(-100.0, 35.0), (-90.0, 35.0)], srid=4326)
TRIP = {'start': 'Amarillo, TX', 'finish': 'Memphis, TN'}


def add_stop(opis_id, fraction, price):
    """A stop on the fake route, `fraction` of the way along it."""
    TruckStop.objects.create(
        opis_id=opis_id, name=f'Stop {opis_id}', address='I-40', city='Town', state='TX',
        price=price, location=Point(-100.0 + 10.0 * fraction, 35.0, srid=4326),
    )


@mock.patch('fuel.services.get_route', return_value=(ROUTE, 600.0))
class RouteApiTests(APITestCase):

    @classmethod
    def setUpTestData(cls):
        add_stop(1, 0.02, 3.50)   # mile 12
        add_stop(2, 0.40, 3.00)   # mile 240
        add_stop(3, 0.75, 3.40)   # mile 450

    def setUp(self):
        cache.clear()

    def test_response_format(self, get_route):
        resp = self.client.post(URL, TRIP, format='json')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(list(data), ['start', 'finish', 'strategy', 'total_distance_miles',
                                      'total_gallons', 'total_cost', 'fuel_stops', 'route',
                                      'map_url'])
        self.assertEqual(list(data['fuel_stops'][0]),
                         ['name', 'city', 'state', 'price', 'mile_marker', 'gallons', 'cost',
                          'lon', 'lat'])
        self.assertEqual(data['route']['type'], 'LineString')
        self.assertEqual(data['route']['coordinates'][0], [-100.0, 35.0])  # lon, lat
        self.assertTrue(data['map_url'].startswith('/api/route/map/?start=Amarillo'))

    def test_totals_and_stops(self, get_route):
        data = self.client.post(URL, TRIP, format='json').json()
        self.assertEqual(data['strategy'], 'lowest_cost')
        self.assertEqual(data['total_distance_miles'], 600.0)
        self.assertEqual(data['total_gallons'], 60.0)
        self.assertEqual([s['name'] for s in data['fuel_stops']], ['Stop 1', 'Stop 2'])
        self.assertAlmostEqual(data['total_cost'], sum(s['cost'] for s in data['fuel_stops']),
                               places=1)

    def test_each_strategy(self, get_route):
        for strategy in ['lowest_cost', 'balanced', 'fewest_stops']:
            with self.subTest(strategy=strategy):
                resp = self.client.post(URL, {**TRIP, 'strategy': strategy}, format='json')
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()['strategy'], strategy)
                self.assertEqual(resp.json()['total_gallons'], 60.0)

    def test_routing_is_cached_across_strategies_and_input_spelling(self, get_route):
        self.client.post(URL, TRIP, format='json')
        self.client.post(URL, {**TRIP, 'strategy': 'balanced'}, format='json')
        self.client.post(URL, {'start': ' amarillo ,  texas', 'finish': 'Memphis, Tennessee'},
                         format='json')
        self.assertEqual(get_route.call_count, 1)

    def test_map_page_reuses_cache(self, get_route):
        map_url = self.client.post(URL, TRIP, format='json').json()['map_url']
        resp = self.client.get(map_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'leaflet@1.9.4')
        self.assertContains(resp, 'Amarillo, TX → Memphis, TN')
        self.assertEqual(get_route.call_count, 1)


@mock.patch('fuel.services.get_route', return_value=(ROUTE, 600.0))
class ErrorTests(APITestCase):

    def setUp(self):
        cache.clear()

    def assertError(self, resp, status, code):
        self.assertEqual(resp.status_code, status)
        body = resp.json()
        self.assertEqual(list(body), ['error'])
        self.assertEqual(list(body['error']), ['code', 'message', 'details'])
        self.assertEqual(body['error']['code'], code)
        return body['error']

    def test_unknown_city(self, get_route):
        resp = self.client.post(URL, {'start': 'Amarillo, TX', 'finish': 'Atlantis, NY'},
                                format='json')
        err = self.assertError(resp, 400, 'unknown_city')
        self.assertEqual(err['details'], {'field': 'finish', 'value': 'Atlantis, NY'})
        get_route.assert_not_called()  # cities are checked before routing

    def test_location_without_state(self, get_route):
        resp = self.client.post(URL, {'start': 'Amarillo', 'finish': 'Memphis, TN'}, format='json')
        err = self.assertError(resp, 400, 'invalid_input')
        self.assertEqual(err['details']['field'], 'start')

    def test_missing_field_and_bad_strategy(self, get_route):
        resp = self.client.post(URL, {'start': 'Amarillo, TX', 'strategy': 'fastest'},
                                format='json')
        err = self.assertError(resp, 400, 'invalid_input')
        self.assertEqual(set(err['details']), {'finish', 'strategy'})

    def test_routing_timeout(self, get_route):
        get_route.side_effect = RoutingTimeoutError('timed out', timeout_seconds=15)
        err = self.assertError(self.client.post(URL, TRIP, format='json'), 504, 'routing_timeout')
        self.assertEqual(err['details'], {'timeout_seconds': 15})

    def test_routing_unavailable(self, get_route):
        get_route.side_effect = RoutingError('Routing service unavailable. Try again later.')
        self.assertError(self.client.post(URL, TRIP, format='json'), 502, 'routing_unavailable')

    def test_no_route(self, get_route):
        get_route.side_effect = NoRouteError('No drivable road route between these locations.')
        self.assertError(self.client.post(URL, TRIP, format='json'), 422, 'no_route')

    def test_failures_are_not_cached(self, get_route):
        get_route.side_effect = RoutingError('down')
        self.client.post(URL, TRIP, format='json')
        get_route.side_effect = None
        resp = self.client.post(URL, TRIP, format='json')
        self.assertEqual(get_route.call_count, 2)
        self.assertEqual(resp.status_code, 422)  # routed fine this time; no stations in this test

    def test_no_station_near_start(self, get_route):
        err = self.assertError(self.client.post(URL, TRIP, format='json'),
                               422, 'no_station_near_start')
        self.assertEqual(err['details'], {'searched_miles': 500})

    def test_gap_over_500_miles(self, get_route):
        add_stop(1, 0.02, 3.00)   # only one stop, at mile 12, on a 600-mile route
        err = self.assertError(self.client.post(URL, TRIP, format='json'), 422, 'range_gap')
        self.assertEqual(err['details'], {'from_mile': 12.0, 'from_stop': 'Town, TX',
                                          'max_range': 500})

    def test_malformed_json(self, get_route):
        resp = self.client.post(URL, '{"start": ', content_type='application/json')
        self.assertError(resp, 400, 'parse_error')

    def test_wrong_method(self, get_route):
        self.assertError(self.client.get(URL), 405, 'method_not_allowed')

    def test_map_page_error(self, get_route):
        resp = self.client.get(MAP_URL, {'start': 'Atlantis, NY', 'finish': 'Memphis, TN'})
        self.assertEqual(resp.status_code, 400)
        self.assertContains(resp, 'Unknown US city', status_code=400)
        self.assertContains(resp, 'unknown_city', status_code=400)
