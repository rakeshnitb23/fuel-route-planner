import random
import unittest

from fuel.optimizer import (
    STRATEGIES,
    InfeasibleRouteError,
    NoStationNearStartError,
    plan_balanced,
    plan_fewest_stops,
    plan_fuel_stops,
    plan_lowest_cost,
)


def stop(mile, price, name=None):
    return {'name': name or f'@{mile}', 'city': 'Town', 'state': 'TX',
            'mile_marker': mile, 'price': price}


def bought(plan):
    """[(mile, gallons)] rounded, for easy comparison."""
    return [(s['mile_marker'], round(s['gallons'], 6)) for s in plan['fuel_stops']]


class LowestCostTests(unittest.TestCase):
    """The original greedy rule."""

    def test_buys_just_enough_to_reach_cheaper_stop(self):
        plan = plan_lowest_cost([stop(10, 4.00), stop(200, 3.00)], 600)
        # 1 gal to reach mile 10 + 19 gal for miles 10-200; then 40 gal to finish.
        self.assertEqual(bought(plan), [(10, 20), (200, 40)])

    def test_goes_to_first_cheaper_stop_not_cheapest(self):
        plan = plan_lowest_cost([stop(0, 4.00), stop(100, 3.50), stop(200, 3.00)], 300)
        self.assertEqual(bought(plan), [(0, 10), (100, 10), (200, 10)])

    def test_finish_in_range_buys_just_enough_to_finish(self):
        plan = plan_lowest_cost([stop(20, 3.00), stop(300, 3.50)], 400)
        self.assertEqual(bought(plan), [(20, 40)])

    def test_finish_out_of_range_fills_up_and_goes_to_cheapest_in_range(self):
        stops = [stop(0, 3.00), stop(300, 3.60), stop(450, 3.40), stop(800, 3.90)]
        plan = plan_lowest_cost(stops, 1000)
        # Fill up (50 gal) at 0, skip 300 for the cheaper 450, fill again, top up at 800.
        self.assertEqual(bought(plan), [(0, 50), (450, 45), (800, 5)])

    def test_first_stop_is_cheapest_in_first_50_miles(self):
        plan = plan_lowest_cost([stop(5, 4.00), stop(40, 3.50), stop(300, 3.60)], 700)
        self.assertEqual(plan['fuel_stops'][0]['mile_marker'], 40)

    def test_first_stop_pays_for_fuel_used_to_reach_it(self):
        plan = plan_lowest_cost([stop(30, 3.00)], 300)
        self.assertEqual(bought(plan), [(30, 30)])  # 3 gal to reach + 27 gal to finish

    def test_widens_search_when_no_station_in_first_50_miles(self):
        plan = plan_lowest_cost([stop(120, 3.00)], 400)
        self.assertEqual(bought(plan), [(120, 40)])

    def test_gap_over_range_is_infeasible(self):
        with self.assertRaises(InfeasibleRouteError) as ctx:
            plan_lowest_cost([stop(10, 3.00), stop(700, 3.00)], 900)
        self.assertEqual(ctx.exception.details,
                         {'from_mile': 10, 'from_stop': 'Town, TX', 'max_range': 500})

    def test_gap_before_finish_is_infeasible(self):
        with self.assertRaises(InfeasibleRouteError):
            plan_lowest_cost([stop(10, 3.00), stop(400, 3.00)], 1000)

    def test_no_stations_raises_no_station_near_start(self):
        with self.assertRaises(NoStationNearStartError) as ctx:
            plan_lowest_cost([], 300)
        self.assertEqual(ctx.exception.details, {'searched_miles': 300})

    def test_cost_is_gallons_times_price(self):
        plan = plan_lowest_cost([stop(10, 4.00), stop(200, 3.00)], 600)
        for s in plan['fuel_stops']:
            self.assertAlmostEqual(s['cost'], s['gallons'] * s['price'])
        self.assertAlmostEqual(plan['total_cost'], 20 * 4.00 + 40 * 3.00)

    def test_unsorted_input_is_handled(self):
        stops = [stop(200, 3.00), stop(10, 4.00)]
        self.assertEqual(bought(plan_lowest_cost(stops, 600)), [(10, 20), (200, 40)])

    def test_default_engine_is_lowest_cost(self):
        stops = [stop(0, 3.00), stop(300, 3.60), stop(450, 3.40), stop(800, 3.90)]
        self.assertEqual(plan_fuel_stops(stops, 1000), plan_lowest_cost(stops, 1000))


class BalancedTests(unittest.TestCase):
    """Only counts a stop as cheaper if it saves at least 5 cents per gallon."""

    def test_ignores_stop_cheaper_by_less_than_5_cents(self):
        stops = [stop(0, 3.00), stop(100, 2.97)]
        self.assertEqual(bought(plan_balanced(stops, 400)), [(0, 40)])
        self.assertEqual(bought(plan_lowest_cost(stops, 400)), [(0, 10), (100, 30)])

    def test_moves_to_stop_cheaper_by_5_cents_or_more(self):
        stops = [stop(0, 3.00), stop(100, 2.90)]
        self.assertEqual(bought(plan_balanced(stops, 400)), [(0, 10), (100, 30)])

    def test_fewer_stops_slightly_higher_cost(self):
        stops = [stop(0, 3.00), stop(100, 2.98), stop(200, 2.96), stop(300, 2.94)]
        low, bal = plan_lowest_cost(stops, 400), plan_balanced(stops, 400)
        self.assertEqual(len(low['fuel_stops']), 4)
        self.assertEqual(len(bal['fuel_stops']), 2)
        self.assertGreater(bal['total_cost'], low['total_cost'])

    def test_gap_over_range_is_infeasible(self):
        with self.assertRaises(InfeasibleRouteError):
            plan_balanced([stop(10, 3.00), stop(700, 3.00)], 900)


class FewestStopsTests(unittest.TestCase):
    """Never chases a cheaper stop: fill up and go to the cheapest stop in range."""

    def test_does_not_stop_just_because_a_stop_is_cheaper(self):
        stops = [stop(0, 4.00), stop(100, 3.00)]
        self.assertEqual(bought(plan_fewest_stops(stops, 400)), [(0, 40)])
        self.assertEqual(bought(plan_lowest_cost(stops, 400)), [(0, 10), (100, 30)])

    def test_fills_up_and_jumps_to_cheapest_in_range(self):
        stops = [stop(0, 3.00), stop(100, 3.20), stop(450, 2.80), stop(800, 3.00)]
        # Fill up at 0, skip the nearer pricier stop at 100, jump to the cheapest (450).
        self.assertEqual(bought(plan_fewest_stops(stops, 1000)), [(0, 50), (450, 45), (800, 5)])
        # lowest_cost buys only enough at 0 to reach the cheaper stop at 450.
        self.assertEqual(bought(plan_lowest_cost(stops, 1000)), [(0, 45), (450, 50), (800, 5)])

    def test_fewer_stops_higher_cost(self):
        stops = [stop(0, 3.00), stop(100, 2.90), stop(200, 2.80), stop(300, 2.70)]
        low, few = plan_lowest_cost(stops, 400), plan_fewest_stops(stops, 400)
        self.assertEqual(len(few['fuel_stops']), 1)
        self.assertLess(len(few['fuel_stops']), len(low['fuel_stops']))
        self.assertGreater(few['total_cost'], low['total_cost'])

    def test_gap_over_range_is_infeasible(self):
        with self.assertRaises(InfeasibleRouteError):
            plan_fewest_stops([stop(10, 3.00), stop(700, 3.00)], 900)


class AllStrategiesTests(unittest.TestCase):

    def test_registry_has_three_strategies(self):
        self.assertEqual(set(STRATEGIES), {'lowest_cost', 'balanced', 'fewest_stops'})

    def test_gallons_invariant_on_random_trips(self):
        """total gallons == total miles / 10 for every strategy, on 300 random trips each."""
        rng = random.Random(42)
        checked = 0
        for _ in range(300):
            total = rng.uniform(50, 3000)
            stops = [stop(0, round(rng.uniform(2.5, 4.5), 3))]
            mile = 0.0
            while mile < total:
                mile += rng.uniform(1, 450)
                if mile < total:
                    stops.append(stop(mile, round(rng.uniform(2.5, 4.5), 3)))
            for name, fn in STRATEGIES.items():
                with self.subTest(strategy=name, total=total):
                    plan = fn(stops, total)
                    self.assertAlmostEqual(plan['total_gallons'], total / 10, places=6)
                    self.assertTrue(all(s['gallons'] > 0 for s in plan['fuel_stops']))
                    checked += 1
        self.assertEqual(checked, 900)
