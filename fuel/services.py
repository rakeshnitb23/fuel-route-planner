import logging
from time import monotonic, sleep
from urllib.parse import urlencode

from django.core.cache import cache
from django.urls import reverse
from redis.exceptions import RedisError

from fuel.locations import LocationError, location_key, resolve_location
from fuel.optimizer import STRATEGIES
from fuel.routing import get_route, stops_along_route

logger = logging.getLogger(__name__)

ROUTE_CACHE_TIMEOUT = 60 * 60 * 24
LOCK_TIMEOUT = 30          # seconds; longer than the 15 s OSRM timeout
LOCK_WAIT_SECONDS = 20     # how long other requests wait for the lock holder
LOCK_POLL_SECONDS = 0.2

HIT, MISS = 'HIT', 'MISS'
ACQUIRED, HELD, UNAVAILABLE = 'acquired', 'held', 'unavailable'

CACHE_ERRORS = (RedisError, ConnectionError, TimeoutError)


# Safe cache access: a cache outage is a miss / no-op, never a failed request.

def _cache_failed(op, key, exc):
    logger.warning('Cache %s failed for %s: %s', op, key, exc)


def cache_get(key):
    try:
        return cache.get(key)
    except CACHE_ERRORS as exc:
        _cache_failed('get', key, exc)
        return None


def cache_set(key, value, timeout):
    try:
        cache.set(key, value, timeout)
    except CACHE_ERRORS as exc:
        _cache_failed('set', key, exc)


def cache_delete(key):
    try:
        cache.delete(key)
    except CACHE_ERRORS as exc:
        _cache_failed('delete', key, exc)


def cache_lock(key):
    """ACQUIRED, HELD (another request has it), or UNAVAILABLE (cache is down)."""
    try:
        return ACQUIRED if cache.add(key, 1, timeout=LOCK_TIMEOUT) else HELD
    except CACHE_ERRORS as exc:
        _cache_failed('add', key, exc)
        return UNAVAILABLE


# Trip lookup.

def _locate(field, text):
    """(normalized key, (lon, lat)) for one input; tags errors with the field name."""
    try:
        return location_key(text), resolve_location(text)
    except LocationError as exc:
        exc.field = field
        raise


def _route_key(start_key, finish_key):
    return 'route:' + '|'.join([*start_key, *finish_key]).replace(' ', '-')


def route_key(start, finish):
    """Cache key for a trip: the same for every spelling of the same two cities."""
    return _route_key(location_key(start), location_key(finish))


def _build_trip(start_coords, finish_coords):
    """One OSRM call + corridor search. Plain data only, safe to cache."""
    route, total_miles = get_route(start_coords, finish_coords)
    return {
        'coordinates': [list(c) for c in route.coords],
        'total_miles': total_miles,
        'stops': stops_along_route(route, total_miles),
    }


def _wait_for_route(key, lock_key):
    """Poll for another request's result. None if it never appears."""
    deadline = monotonic() + LOCK_WAIT_SECONDS
    while monotonic() < deadline:
        sleep(LOCK_POLL_SECONDS)
        trip = cache_get(key)
        if trip is not None:
            return trip
        if cache_get(lock_key) is None:
            # The holder finished without caching (its OSRM call failed): stop waiting.
            return cache_get(key)
    return None


def get_trip(start, finish):
    """
    Route + corridor stops for a trip, cached by normalized "start|finish".
    Returns (trip, HIT or MISS). Only one request per trip calls OSRM at a time.
    """
    start_key, start_coords = _locate('start', start)
    finish_key, finish_coords = _locate('finish', finish)
    key = _route_key(start_key, finish_key)

    trip = cache_get(key)
    if trip is not None:
        return trip, HIT

    lock_key = key + ':lock'
    lock = cache_lock(lock_key)
    if lock == HELD:
        trip = _wait_for_route(key, lock_key)
        if trip is not None:
            return trip, HIT

    try:
        trip = _build_trip(start_coords, finish_coords)
        cache_set(key, trip, ROUTE_CACHE_TIMEOUT)  # only reached on success
    finally:
        if lock == ACQUIRED:
            cache_delete(lock_key)
    return trip, MISS


def optimize(trip, strategy='lowest_cost'):
    """Fresh optimizer run (under 1 ms) for the chosen strategy."""
    return STRATEGIES[strategy](trip['stops'], trip['total_miles'])


def compare_strategies(trip):
    """
    Run all three strategies on one trip (under 1 ms each, no extra OSRM calls).
    Returns (plans by name, comparison rows, identical). Infeasible trips raise the
    optimizer's error: stop spacing is the same for every strategy, so all fail together.
    """
    plans = {name: optimize(trip, name) for name in STRATEGIES}
    base = plans['lowest_cost']['total_cost']
    comparison = []
    for name, plan in plans.items():
        cost = round(plan['total_cost'], 2)
        extra = cost - round(base, 2)
        comparison.append({
            'strategy': name,
            'stops': len(plan['fuel_stops']),
            'total_cost': cost,
            'extra_cost': round(extra, 2),
            'extra_cost_pct': round(100 * extra / base, 2) if base else 0.0,
            'description': STRATEGIES[name].__doc__,
        })
    shapes = {
        tuple((round(s['mile_marker'], 1), round(s['gallons'], 2)) for s in plan['fuel_stops'])
        for plan in plans.values()
    }
    return plans, comparison, len(shapes) == 1


def build_response(start, finish, strategy, trip, plan):
    query = urlencode({'start': start, 'finish': finish, 'strategy': strategy})
    return {
        'start': start,
        'finish': finish,
        'strategy': strategy,
        'total_distance_miles': round(trip['total_miles'], 1),
        'total_gallons': round(plan['total_gallons'], 2),
        'total_cost': round(plan['total_cost'], 2),
        'fuel_stops': [
            {
                'name': s['name'],
                'city': s['city'],
                'state': s['state'],
                'price': round(s['price'], 3),
                'mile_marker': round(s['mile_marker'], 1),
                'gallons': round(s['gallons'], 2),
                'cost': round(s['cost'], 2),
                'lon': s['lon'],
                'lat': s['lat'],
            }
            for s in plan['fuel_stops']
        ],
        'route': {'type': 'LineString', 'coordinates': trip['coordinates']},
        'map_url': f"{reverse('route-map')}?{query}",
    }
