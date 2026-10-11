from unittest import mock

from django.conf import settings
from django.core.cache import cache
from redis.exceptions import ConnectionError as RedisConnectionError
from rest_framework.test import APITestCase

from fuel.routing import RoutingError
from fuel.services import LOCK_WAIT_SECONDS, route_key
from fuel.tests.test_api import ROUTE, TRIP, URL, add_stop

OTHER_ROUTE = {'start': 'st. louis, mo', 'finish': 'Chicago, IL'}


def cached_trip():
    """What get_trip caches for ROUTE: plain data only."""
    def stop(i, mile, price):
        return {'id': i, 'name': f'Stop {i}', 'city': 'Town', 'state': 'TX', 'price': price,
                'mile_marker': mile, 'lon': -100.0 + mile / 60, 'lat': 35.0}
    return {'coordinates': [list(c) for c in ROUTE.coords], 'total_miles': 600.0,
            'stops': [stop(1, 12.0, 3.50), stop(2, 240.0, 3.00)]}


@mock.patch('fuel.services.get_route', return_value=(ROUTE, 600.0))
class RouteCacheTests(APITestCase):

    @classmethod
    def setUpTestData(cls):
        add_stop(1, 0.02, 3.50)
        add_stop(2, 0.40, 3.00)

    def setUp(self):
        cache.clear()

    def post(self, body=TRIP):
        return self.client.post(URL, body, format='json')

    def test_tests_use_locmem_with_prefix_and_version(self, get_route):
        config = settings.CACHES['default']
        self.assertEqual(config['BACKEND'], 'django.core.cache.backends.locmem.LocMemCache')
        self.assertEqual((config['KEY_PREFIX'], config['VERSION']), ('fuelroute', 1))

    def test_second_identical_request_is_hit_without_osrm(self, get_route):
        first, second = self.post(), self.post()
        self.assertEqual(first['X-Cache'], 'MISS')
        self.assertEqual(second['X-Cache'], 'HIT')
        self.assertEqual(second.json(), first.json())
        self.assertEqual(get_route.call_count, 1)

    def test_different_spellings_share_one_entry(self, get_route):
        self.assertEqual(route_key('st. louis, mo', 'Chicago, IL'),
                         route_key('Saint Louis, Missouri', 'chicago , illinois'))
        first = self.post({'start': 'st. louis, mo', 'finish': 'Chicago, IL'})
        second = self.post({'start': 'Saint Louis, Missouri', 'finish': 'chicago , illinois'})
        self.assertEqual((first['X-Cache'], second['X-Cache']), ('MISS', 'HIT'))
        self.assertEqual(get_route.call_count, 1)

    def test_cached_value_is_plain_data(self, get_route):
        self.post()
        trip = cache.get(route_key(TRIP['start'], TRIP['finish']))
        self.assertIsInstance(trip['coordinates'][0], list)
        self.assertIsInstance(trip['total_miles'], float)
        for stop in trip['stops']:
            for value in stop.values():
                self.assertIsInstance(value, (str, int, float))

    def test_header_on_optimizer_error_but_not_on_validation_error(self, get_route):
        get_route.return_value = (ROUTE, 1200.0)  # stops end at mile 480: range_gap
        resp = self.post(OTHER_ROUTE)
        self.assertEqual((resp.status_code, resp['X-Cache']), (422, 'MISS'))
        resp = self.post({'start': 'Atlantis, NY', 'finish': 'Chicago, IL'})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn('X-Cache', resp)

    def test_failed_osrm_call_is_not_cached_and_lock_is_released(self, get_route):
        key = route_key(TRIP['start'], TRIP['finish'])
        get_route.side_effect = RoutingError('down')
        self.assertEqual(self.post().status_code, 502)
        self.assertIsNone(cache.get(key))
        self.assertIsNone(cache.get(key + ':lock'))
        get_route.side_effect = None
        resp = self.post()
        self.assertEqual((resp.status_code, resp['X-Cache']), (200, 'MISS'))
        self.assertEqual(get_route.call_count, 2)


@mock.patch('fuel.services.sleep')
@mock.patch('fuel.services.get_route', return_value=(ROUTE, 600.0))
class StampedeLockTests(APITestCase):

    @classmethod
    def setUpTestData(cls):
        add_stop(1, 0.02, 3.50)
        add_stop(2, 0.40, 3.00)

    def setUp(self):
        cache.clear()
        self.key = route_key(TRIP['start'], TRIP['finish'])
        self.lock_key = self.key + ':lock'

    def post(self):
        return self.client.post(URL, TRIP, format='json')

    def test_waiter_uses_route_cached_by_lock_holder(self, get_route, sleep):
        cache.add(self.lock_key, 1, timeout=30)  # another request is calling OSRM
        sleep.side_effect = lambda s: cache.set(self.key, cached_trip(), 60)  # ...and finishes
        resp = self.post()
        self.assertEqual((resp.status_code, resp['X-Cache']), (200, 'HIT'))
        get_route.assert_not_called()
        self.assertEqual(sleep.call_count, 1)

    def test_waiter_computes_at_once_when_holder_fails(self, get_route, sleep):
        cache.add(self.lock_key, 1, timeout=30)
        sleep.side_effect = lambda s: cache.delete(self.lock_key)  # holder failed, cached nothing
        resp = self.post()
        self.assertEqual((resp.status_code, resp['X-Cache']), (200, 'MISS'))
        self.assertEqual(get_route.call_count, 1)
        self.assertEqual(sleep.call_count, 1)  # did not wait the full 20 s

    def test_waiter_computes_itself_after_timeout(self, get_route, sleep):
        cache.add(self.lock_key, 1, timeout=30)  # holder never finishes
        clock = iter(range(0, 1000, 1))
        with mock.patch('fuel.services.monotonic', side_effect=lambda: next(clock)):
            resp = self.post()
        self.assertEqual((resp.status_code, resp['X-Cache']), (200, 'MISS'))
        self.assertEqual(get_route.call_count, 1)
        self.assertLessEqual(sleep.call_count, LOCK_WAIT_SECONDS)

    def test_lock_holder_releases_lock_on_success(self, get_route, sleep):
        self.post()
        self.assertIsNotNone(cache.get(self.key))
        self.assertIsNone(cache.get(self.lock_key))
        sleep.assert_not_called()


@mock.patch('fuel.services.sleep')
@mock.patch('fuel.services.get_route', return_value=(ROUTE, 600.0))
class CacheDownTests(APITestCase):
    """Every cache call raises, as when Redis is unreachable."""

    @classmethod
    def setUpTestData(cls):
        add_stop(1, 0.02, 3.50)
        add_stop(2, 0.40, 3.00)

    def setUp(self):
        error = RedisConnectionError('Connection refused')
        patcher = mock.patch('fuel.services.cache')
        broken = patcher.start()
        self.addCleanup(patcher.stop)
        for op in ('get', 'set', 'add', 'delete'):
            getattr(broken, op).side_effect = error

    def test_request_still_succeeds_as_miss(self, get_route, sleep):
        with self.assertLogs('fuel.services', 'WARNING') as logs:
            resp = self.client.post(URL, TRIP, format='json')
        self.assertEqual((resp.status_code, resp['X-Cache']), (200, 'MISS'))
        self.assertEqual(resp.json()['total_gallons'], 60.0)
        self.assertTrue(any('Cache get failed' in line for line in logs.output))

    def test_no_lock_wait_when_cache_is_down(self, get_route, sleep):
        with self.assertLogs('fuel.services', 'WARNING'):
            self.client.post(URL, TRIP, format='json')
        sleep.assert_not_called()
        self.assertEqual(get_route.call_count, 1)
