from django.shortcuts import render
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel.locations import LocationError
from fuel.optimizer import InfeasibleRouteError, NoStationNearStartError
from fuel.routing import NoRouteError, RoutingError, RoutingTimeoutError
from fuel.router import choose_strategy
from fuel.serializers import RouteRequestSerializer, SmartRouteRequestSerializer
from fuel.services import build_response, compare_strategies, get_trip, optimize
from fuel.throttles import SafeScopedRateThrottle

# Domain error -> (HTTP status, error code). Subclasses before their parents.
ERRORS = [
    (NoRouteError, status.HTTP_422_UNPROCESSABLE_ENTITY, 'no_route'),
    (RoutingTimeoutError, status.HTTP_504_GATEWAY_TIMEOUT, 'routing_timeout'),
    (RoutingError, status.HTTP_502_BAD_GATEWAY, 'routing_unavailable'),
    (NoStationNearStartError, status.HTTP_422_UNPROCESSABLE_ENTITY, 'no_station_near_start'),
    (InfeasibleRouteError, status.HTTP_422_UNPROCESSABLE_ENTITY, 'range_gap'),
]


def error_body(code, message, details=None):
    return {'error': {'code': code, 'message': message, 'details': details or {}}}


def _domain_error(exc):
    """(body, status) for a known domain error."""
    if isinstance(exc, LocationError):
        details = {'field': exc.field, 'value': exc.value}
        return error_body(exc.code, str(exc), details), status.HTTP_400_BAD_REQUEST
    for err, http_status, code in ERRORS:
        if isinstance(exc, err):
            return error_body(code, str(exc), exc.details), http_status
    raise exc


DOMAIN_ERRORS = (LocationError, RoutingError, NoStationNearStartError, InfeasibleRouteError)


def _invalid(serializer):
    body = error_body('invalid_input', 'Invalid request.', serializer.errors)
    return body, status.HTTP_400_BAD_REQUEST


def _plan(data):
    """
    Validate input and plan the trip.
    Returns (result, error, cache_status): result or error is None; cache_status is
    HIT/MISS once the route lookup has run, else None.
    """
    serializer = RouteRequestSerializer(data=data)
    if not serializer.is_valid():
        return None, _invalid(serializer), None
    start, finish, strategy = (serializer.validated_data[k] for k in ('start', 'finish', 'strategy'))
    cache_status = None
    try:
        trip, cache_status = get_trip(start, finish)
        plan = optimize(trip, strategy)
    except DOMAIN_ERRORS as exc:
        return None, _domain_error(exc), cache_status
    return (start, finish, strategy, trip, plan), None, cache_status


def _respond(body, error, cache_status):
    response = Response(*error) if error else Response(body)
    if cache_status:
        response['X-Cache'] = cache_status
    return response


class RouteView(APIView):
    throttle_classes = [SafeScopedRateThrottle]
    throttle_scope = 'route'

    def post(self, request):
        result, error, cache_status = _plan(request.data)
        return _respond(result and build_response(*result), error, cache_status)


def _smart_plan(data):
    """
    Like _plan, but the strategy comes from the strategy router.
    Returns (body, error, cache_status).
    """
    serializer = SmartRouteRequestSerializer(data=data)
    if not serializer.is_valid():
        return None, _invalid(serializer), None
    start, finish, preference = (serializer.validated_data[k]
                                 for k in ('start', 'finish', 'preference'))
    cache_status = None
    try:
        trip, cache_status = get_trip(start, finish)
        plans, comparison, identical = compare_strategies(trip)  # infeasible: raises, no LLM call
    except DOMAIN_ERRORS as exc:
        return None, _domain_error(exc), cache_status
    choice = choose_strategy(preference, comparison, identical)
    strategy = choice['strategy']
    body = build_response(start, finish, strategy, trip, plans[strategy])
    body['decision'] = choice
    body['comparison'] = comparison
    return body, None, cache_status


class SmartRouteView(APIView):
    throttle_classes = [SafeScopedRateThrottle]
    throttle_scope = 'smart'

    def post(self, request):
        return _respond(*_smart_plan(request.data))


def route_map(request):
    result, error, _ = _plan(request.GET)
    if error:
        body, http_status = error
        context = {'error': body['error']}
        return render(request, 'fuel/map.html', context, status=http_status)
    start, finish, strategy, trip, plan = result
    context = {
        'start': start,
        'finish': finish,
        'strategy': strategy,
        'total_miles': round(trip['total_miles'], 1),
        'total_cost': round(plan['total_cost'], 2),
        'stop_count': len(plan['fuel_stops']),
        'map_data': {
            'route': trip['coordinates'],
            'stops': [
                {
                    'name': s['name'], 'city': s['city'], 'state': s['state'],
                    'price': round(s['price'], 3), 'mile_marker': round(s['mile_marker'], 1),
                    'gallons': round(s['gallons'], 2), 'cost': round(s['cost'], 2),
                    'lon': s['lon'], 'lat': s['lat'],
                }
                for s in plan['fuel_stops']
            ],
        },
    }
    return render(request, 'fuel/map.html', context)
