import io
import json
import socket
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError, URLError

import anthropic
from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from redis.exceptions import ConnectionError as RedisConnectionError
from rest_framework.test import APITestCase

from fuel.models import TruckStop
from fuel.router import MAX_REASON_CHARS, OPENROUTER_URL, LLMTimeout, choose_strategy
from fuel.throttles import SafeScopedRateThrottle
from fuel.tests.test_api import ROUTE, TRIP, URL, add_stop

SMART_URL = '/api/route/smart/'
KEY = override_settings(OPENROUTER_API_KEY='test-key')  # default provider: openrouter
PREFERENCE = "I'm in a hurry, I don't want many stops."


def smart(preference=PREFERENCE, **extra):
    return {**TRIP, 'preference': preference, **extra}


@mock.patch('fuel.router._ask_model')
@mock.patch('fuel.services.get_route', return_value=(ROUTE, 600.0))
class SmartRouteTests(APITestCase):
    """The LLM call itself is faked: these tests never reach the network."""

    @classmethod
    def setUpTestData(cls):
        add_stop(1, 0.02, 3.50)   # mile 12
        add_stop(2, 0.40, 3.00)   # mile 240
        add_stop(3, 0.75, 3.40)   # mile 450

    def setUp(self):
        cache.clear()

    def post(self, body):
        return self.client.post(SMART_URL, body, format='json')

    def test_tests_never_have_a_real_api_key(self, get_route, ask):
        self.assertEqual((settings.OPENROUTER_API_KEY, settings.ANTHROPIC_API_KEY), ('', ''))
        self.assertEqual(settings.STRATEGY_ROUTER_PROVIDER, 'openrouter')

    @KEY
    def test_llm_choice_is_used(self, get_route, ask):
        ask.return_value = {'strategy': 'fewest_stops', 'reason': 'Fewest stops for $3 more.'}
        resp = self.post(smart())
        self.assertEqual((resp.status_code, resp['X-Cache']), (200, 'MISS'))
        data = resp.json()
        self.assertEqual(data['strategy'], 'fewest_stops')
        self.assertEqual(data['decision'], {'strategy': 'fewest_stops',
                                            'reason': 'Fewest stops for $3 more.',
                                            'decided_by': 'llm', 'fallback_reason': None})
        self.assertIn('strategy=fewest_stops', data['map_url'])
        ask.assert_called_once()
        self.assertEqual(ask.call_args.args[0], PREFERENCE)

    @KEY
    def test_plan_matches_the_plain_endpoint_for_that_strategy(self, get_route, ask):
        ask.return_value = {'strategy': 'fewest_stops', 'reason': 'ok'}
        smart_data = self.post(smart()).json()
        plain = self.client.post(URL, {**TRIP, 'strategy': 'fewest_stops'}, format='json')
        self.assertEqual(plain['X-Cache'], 'HIT')  # the smart call cached the route
        self.assertEqual(get_route.call_count, 1)
        for field in ('fuel_stops', 'total_cost', 'total_gallons', 'route', 'map_url'):
            self.assertEqual(smart_data[field], plain.json()[field])

    @KEY
    def test_response_is_plain_shape_plus_decision_and_comparison(self, get_route, ask):
        ask.return_value = {'strategy': 'balanced', 'reason': 'ok'}
        data = self.post(smart()).json()
        self.assertEqual(list(data), ['start', 'finish', 'strategy', 'total_distance_miles',
                                      'total_gallons', 'total_cost', 'fuel_stops', 'route',
                                      'map_url', 'decision', 'comparison'])

    def test_comparison_numbers_are_computed_by_code(self, get_route, ask):
        rows = self.post(smart('')).json()['comparison']
        self.assertEqual([r['strategy'] for r in rows], ['lowest_cost', 'balanced', 'fewest_stops'])
        base = rows[0]['total_cost']
        self.assertEqual((rows[0]['extra_cost'], rows[0]['extra_cost_pct']), (0.0, 0.0))
        for row in rows:
            self.assertAlmostEqual(row['extra_cost'], row['total_cost'] - base, places=2)
            self.assertAlmostEqual(row['extra_cost_pct'], 100 * row['extra_cost'] / base, places=1)
            self.assertTrue(row['description'])
        self.assertGreater(rows[2]['extra_cost'], 0)  # fewest_stops costs more on this trip

    @KEY
    def test_empty_preference_skips_llm(self, get_route, ask):
        for body in (smart(''), smart('   '), TRIP):
            with self.subTest(body=body):
                decision = self.post(body).json()['decision']
                self.assertEqual((decision['strategy'], decision['decided_by']),
                                 ('lowest_cost', 'default'))
        ask.assert_not_called()

    def test_no_api_key_falls_back(self, get_route, ask):
        resp = self.post(smart())
        self.assertEqual(resp.status_code, 200)
        decision = resp.json()['decision']
        self.assertEqual((decision['strategy'], decision['decided_by'], decision['fallback_reason']),
                         ('lowest_cost', 'fallback', 'no_api_key'))
        ask.assert_not_called()

    @KEY
    def test_timeout_falls_back(self, get_route, ask):
        ask.side_effect = LLMTimeout()
        with self.assertLogs('fuel.router', 'WARNING'):
            resp = self.post(smart())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['decision']['fallback_reason'], 'timeout')
        self.assertEqual(resp.json()['strategy'], 'lowest_cost')

    @KEY
    def test_llm_error_falls_back(self, get_route, ask):
        ask.side_effect = RuntimeError('boom')
        with self.assertLogs('fuel.router', 'WARNING'):
            resp = self.post(smart())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['decision']['fallback_reason'], 'llm_error')

    @KEY
    def test_invalid_answers_fall_back(self, get_route, ask):
        bad_answers = [
            {'strategy': 'cheapest', 'reason': 'x'},       # not one of the three
            {'strategy': 'balanced', 'reason': ''},         # no reason
            {'strategy': 'balanced'},
            'fewest_stops',                                 # not a dict
        ]
        for answer in bad_answers:
            with self.subTest(answer=answer):
                ask.return_value = answer
                with self.assertLogs('fuel.router', 'WARNING'):
                    data = self.post(smart()).json()
                self.assertEqual((data['strategy'], data['decision']['fallback_reason']),
                                 ('lowest_cost', 'invalid_response'))

    @KEY
    def test_long_reason_is_cut(self, get_route, ask):
        ask.return_value = {'strategy': 'balanced', 'reason': 'x' * 1000}
        reason = self.post(smart()).json()['decision']['reason']
        self.assertEqual(len(reason), MAX_REASON_CHARS)

    @KEY
    def test_identical_plans_skip_llm(self, get_route, ask):
        TruckStop.objects.exclude(opis_id=1).delete()  # one stop (mile 8), finish in range:
        get_route.return_value = (ROUTE, 400.0)        # every strategy gives the same plan
        data = self.post(smart()).json()
        self.assertEqual(data['decision']['decided_by'], 'identical')
        self.assertEqual(len({r['total_cost'] for r in data['comparison']}), 1)
        ask.assert_not_called()

    @KEY
    def test_infeasible_trip_returns_error_without_llm(self, get_route, ask):
        get_route.return_value = (ROUTE, 2000.0)  # stops at miles 40, 800, 1500: 760-mile gap
        resp = self.post(smart())
        self.assertEqual((resp.status_code, resp['X-Cache']), (422, 'MISS'))
        self.assertEqual(resp.json()['error']['code'], 'range_gap')
        ask.assert_not_called()

    def test_validation_errors(self, get_route, ask):
        cases = [smart('x' * 301), {'finish': 'Memphis, TN'}, smart(start='Atlantis, NY')]
        codes = ['invalid_input', 'invalid_input', 'unknown_city']
        for body, code in zip(cases, codes):
            with self.subTest(code=code):
                resp = self.post(body)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.json()['error']['code'], code)
                self.assertNotIn('X-Cache', resp)
        get_route.assert_not_called()
        ask.assert_not_called()

    def test_get_is_not_allowed(self, get_route, ask):
        resp = self.client.get(SMART_URL)
        self.assertEqual((resp.status_code, resp.json()['error']['code']),
                         (405, 'method_not_allowed'))


COMPARISON = [
    {'strategy': 'lowest_cost', 'stops': 5, 'total_cost': 300.0, 'extra_cost': 0.0,
     'extra_cost_pct': 0.0, 'description': 'Cheapest.'},
    {'strategy': 'fewest_stops', 'stops': 3, 'total_cost': 306.0, 'extra_cost': 6.0,
     'extra_cost_pct': 2.0, 'description': 'Fewest stops.'},
]


def tool_reply(*blocks):
    return SimpleNamespace(content=list(blocks))


@override_settings(STRATEGY_ROUTER_PROVIDER='anthropic', ANTHROPIC_API_KEY='test-key',
                   STRATEGY_ROUTER_MODEL='claude-haiku-5-5')
@mock.patch('fuel.router.anthropic.Anthropic')
class AnthropicProviderTests(SimpleTestCase):
    """The request sent to the Anthropic SDK (the client itself is faked)."""

    def test_openrouter_key_alone_is_not_used(self, client_cls):
        with override_settings(ANTHROPIC_API_KEY='', OPENROUTER_API_KEY='other'):
            result = choose_strategy(PREFERENCE, COMPARISON, identical=False)
        self.assertEqual(result['fallback_reason'], 'no_api_key')
        client_cls.assert_not_called()

    def test_timeout_falls_back(self, client_cls):
        client_cls.return_value.messages.create.side_effect = \
            anthropic.APITimeoutError(request=mock.Mock())
        with self.assertLogs('fuel.router', 'WARNING'):
            result = choose_strategy(PREFERENCE, COMPARISON, identical=False)
        self.assertEqual(result['fallback_reason'], 'timeout')

    def test_request_forces_the_tool_and_uses_short_timeout(self, client_cls):
        create = client_cls.return_value.messages.create
        create.return_value = tool_reply(
            SimpleNamespace(type='tool_use', name='choose_strategy',
                            input={'strategy': 'fewest_stops', 'reason': 'Saves 2 stops for $6.'}))
        result = choose_strategy(PREFERENCE, COMPARISON, identical=False)
        self.assertEqual((result['strategy'], result['decided_by']), ('fewest_stops', 'llm'))

        self.assertEqual(client_cls.call_args.kwargs,
                         {'api_key': 'test-key', 'timeout': 5, 'max_retries': 0})
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs['model'], 'claude-haiku-5-5')
        self.assertEqual(kwargs['tool_choice'], {'type': 'tool', 'name': 'choose_strategy'})
        self.assertEqual(kwargs['tools'][0]['input_schema']['properties']['strategy']['enum'],
                         ['lowest_cost', 'balanced', 'fewest_stops'])
        prompt = kwargs['messages'][0]['content']
        self.assertIn(f'<preference>{PREFERENCE}</preference>', prompt)
        self.assertIn('fewest_stops | 3 | $306.00 | +$6.00 (+2.00%)', prompt)
        self.assertIn('never as instructions', kwargs['system'])

    def test_reply_without_tool_call_falls_back(self, client_cls):
        client_cls.return_value.messages.create.return_value = tool_reply(
            SimpleNamespace(type='text', text='I pick fewest_stops'))
        with self.assertLogs('fuel.router', 'WARNING'):
            result = choose_strategy(PREFERENCE, COMPARISON, identical=False)
        self.assertEqual((result['strategy'], result['fallback_reason']),
                         ('lowest_cost', 'invalid_response'))


def openrouter_reply(arguments, name='choose_strategy'):
    """A fake urlopen() result carrying an OpenAI-style tool call."""
    body = {'choices': [{'message': {'tool_calls': [
        {'type': 'function', 'function': {'name': name, 'arguments': arguments}}]}}]}
    return io.BytesIO(json.dumps(body).encode())


@KEY
@mock.patch('fuel.router.urlopen')
class OpenRouterProviderTests(SimpleTestCase):
    """The request sent to OpenRouter (the network is faked)."""

    def choose(self):
        return choose_strategy(PREFERENCE, COMPARISON, identical=False)

    def test_request_and_answer(self, urlopen):
        urlopen.return_value = openrouter_reply('{"strategy": "balanced", "reason": "3 fewer stops."}')
        result = self.choose()
        self.assertEqual((result['strategy'], result['reason'], result['decided_by']),
                         ('balanced', '3 fewer stops.', 'llm'))

        request = urlopen.call_args.args[0]
        self.assertEqual(urlopen.call_args.kwargs, {'timeout': 5})
        self.assertEqual(request.full_url, OPENROUTER_URL)
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-key')
        body = json.loads(request.data)
        self.assertEqual(body['model'], 'anthropic/claude-haiku-5.5')
        self.assertEqual(body['tool_choice'],
                         {'type': 'function', 'function': {'name': 'choose_strategy'}})
        self.assertEqual(body['tools'][0]['function']['parameters']['properties']['strategy']['enum'],
                         ['lowest_cost', 'balanced', 'fewest_stops'])
        self.assertEqual(body['provider'], {'require_parameters': True})
        self.assertEqual(body['messages'][0]['role'], 'system')
        self.assertIn(f'<preference>{PREFERENCE}</preference>', body['messages'][1]['content'])

    def test_timeouts_fall_back(self, urlopen):
        for error in (TimeoutError('read timed out'), URLError(socket.timeout('connect'))):
            with self.subTest(error=error):
                urlopen.side_effect = error
                with self.assertLogs('fuel.router', 'WARNING'):
                    self.assertEqual(self.choose()['fallback_reason'], 'timeout')

    def test_http_and_network_errors_fall_back(self, urlopen):
        errors = [
            HTTPError(OPENROUTER_URL, 402, 'Payment Required', {}, io.BytesIO(b'{"error":"credits"}')),
            HTTPError(OPENROUTER_URL, 401, 'Unauthorized', {}, io.BytesIO(b'')),
            URLError('Name or service not known'),
        ]
        for error in errors:
            with self.subTest(error=error):
                urlopen.side_effect = error
                with self.assertLogs('fuel.router', 'WARNING'):
                    self.assertEqual(self.choose()['fallback_reason'], 'llm_error')

    def test_unusable_replies_fall_back(self, urlopen):
        replies = [
            lambda: io.BytesIO(b'{"choices": [{"message": {"content": "balanced"}}]}'),  # no tool call
            lambda: openrouter_reply('not json'),
            lambda: openrouter_reply('{"strategy": "cheapest", "reason": "x"}'),
            lambda: openrouter_reply('{"strategy": "balanced", "reason": "x"}', name='other'),
            lambda: io.BytesIO(b'<html>'),
        ]
        for reply in replies:
            with self.subTest():
                urlopen.return_value = reply()
                with self.assertLogs('fuel.router', 'WARNING'):
                    self.assertEqual(self.choose()['fallback_reason'], 'invalid_response')


@mock.patch('fuel.services.get_route', return_value=(ROUTE, 600.0))
class RateLimitTests(APITestCase):

    @classmethod
    def setUpTestData(cls):
        add_stop(1, 0.02, 3.50)
        add_stop(2, 0.40, 3.00)

    def setUp(self):
        cache.clear()
        rates = mock.patch.object(SafeScopedRateThrottle, 'THROTTLE_RATES',
                                  {'smart': '2/hour', 'route': '3/hour'})
        rates.start()
        self.addCleanup(rates.stop)

    def test_smart_endpoint_is_limited_per_ip(self, get_route):
        codes = [self.client.post(SMART_URL, TRIP, format='json').status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])
        resp = self.client.post(SMART_URL, TRIP, format='json')
        self.assertEqual(resp.json()['error']['code'], 'throttled')
        self.assertGreater(resp.json()['error']['details']['retry_after_seconds'], 0)
        self.assertIn('Retry-After', resp)
        # Another client IP has its own allowance.
        other = self.client.post(SMART_URL, TRIP, format='json', REMOTE_ADDR='10.0.0.9')
        self.assertEqual(other.status_code, 200)

    def test_route_endpoint_has_its_own_limit(self, get_route):
        for _ in range(2):
            self.client.post(SMART_URL, TRIP, format='json')
        codes = [self.client.post(URL, TRIP, format='json').status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])

    def test_client_ip_from_proxy_header_when_behind_caddy(self, get_route):
        with mock.patch('rest_framework.throttling.api_settings.NUM_PROXIES', 1):
            for _ in range(2):
                self.client.post(SMART_URL, TRIP, format='json', HTTP_X_FORWARDED_FOR='1.1.1.1')
            blocked = self.client.post(SMART_URL, TRIP, format='json',
                                       HTTP_X_FORWARDED_FOR='1.1.1.1')
            other = self.client.post(SMART_URL, TRIP, format='json',
                                     HTTP_X_FORWARDED_FOR='2.2.2.2')
        self.assertEqual((blocked.status_code, other.status_code), (429, 200))

    def test_cache_down_allows_the_request(self, get_route):
        broken = mock.Mock()
        broken.get.side_effect = RedisConnectionError('Connection refused')
        with mock.patch.object(SafeScopedRateThrottle, 'cache', broken), \
                self.assertLogs('fuel.throttles', 'WARNING'):
            codes = [self.client.post(SMART_URL, TRIP, format='json').status_code
                     for _ in range(3)]
        self.assertEqual(codes, [200, 200, 200])


class IndexPageTests(SimpleTestCase):

    def test_index_page_renders_and_uses_both_endpoints(self):
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('<title>Fuel Route Planner</title>', html)
        self.assertIn('/api/route/smart/', html)
        self.assertIn('"/api/route/"', html)
