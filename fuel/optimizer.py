"""Greedy fuel-stop optimizer. Pure Python: no Django imports."""

START_WINDOW_MILES = 50
BALANCED_MIN_SAVING = 0.05  # dollars per gallon


class OptimizerError(Exception):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


class NoStationNearStartError(OptimizerError):
    pass


class InfeasibleRouteError(OptimizerError):
    pass


def plan_fuel_stops(stops, total_miles, max_range=500, mpg=10,
                    min_saving=0.0, chase_cheaper=True):
    """
    stops: dicts with at least 'price' and 'mile_marker', sorted by mile marker.
    min_saving: a stop only counts as cheaper if it saves at least this much per gallon.
    chase_cheaper: False skips the cheaper-stop rule (fill up and go to the cheapest in range).
    Returns {'fuel_stops': [stop + gallons + cost], 'total_gallons', 'total_cost'}.
    """
    stops = sorted(stops, key=lambda s: (s['mile_marker'], s['price']))
    tank = max_range / mpg

    # Empty tank: first stop is the cheapest one in the first 50 miles, widening by 50.
    i = None
    window = START_WINDOW_MILES
    while i is None:
        limit = min(window, max_range, total_miles)
        candidates = [k for k, s in enumerate(stops) if s['mile_marker'] <= limit]
        if candidates:
            i = min(candidates, key=lambda k: (stops[k]['price'], stops[k]['mile_marker']))
        elif limit >= min(max_range, total_miles):
            raise NoStationNearStartError(
                f'No fuel station within {limit:.0f} miles of the start.',
                searched_miles=round(limit, 1),
            )
        window += START_WINDOW_MILES

    pos = stops[i]['mile_marker']
    fuel = -pos / mpg  # the first stop also pays for the fuel used to reach it
    plan = []

    while True:
        current = stops[i]
        ahead = [
            k for k in range(i + 1, len(stops))
            if stops[k]['mile_marker'] - pos <= max_range
        ]
        cheaper = None
        if chase_cheaper:
            cheaper = next(
                (k for k in ahead if stops[k]['price'] < current['price'] - min_saving), None
            )

        if cheaper is not None:
            # Buy just enough to reach the first cheaper stop.
            leg = (stops[cheaper]['mile_marker'] - pos) / mpg
            buy = max(0.0, leg - fuel)
            nxt = cheaper
        elif total_miles - pos <= max_range:
            # Finish is in range: buy just enough to finish.
            leg = (total_miles - pos) / mpg
            buy = max(0.0, leg - fuel)
            nxt = None
        elif ahead:
            # Fill up and go to the cheapest stop in range (farthest on ties).
            nxt = min(ahead, key=lambda k: (stops[k]['price'], -stops[k]['mile_marker']))
            leg = (stops[nxt]['mile_marker'] - pos) / mpg
            buy = tank - fuel
        else:
            from_stop = f"{current['city']}, {current['state']}"
            raise InfeasibleRouteError(
                f'No fuel station within {max_range} miles after mile {pos:.0f} ({from_stop}).',
                from_mile=round(pos, 1), from_stop=from_stop, max_range=max_range,
            )

        if buy > 0:
            plan.append({**current, 'gallons': buy, 'cost': buy * current['price']})
        fuel += buy - leg

        if nxt is None:
            break
        i = nxt
        pos = stops[i]['mile_marker']

    total_gallons = sum(s['gallons'] for s in plan)
    assert abs(total_gallons - total_miles / mpg) < 1e-6, 'gallons invariant violated'
    return {
        'fuel_stops': plan,
        'total_gallons': total_gallons,
        'total_cost': sum(s['cost'] for s in plan),
    }


def plan_lowest_cost(stops, total_miles, max_range=500, mpg=10):
    """Lowest possible total fuel cost. May use many stops with small purchases."""
    return plan_fuel_stops(stops, total_miles, max_range, mpg)


def plan_balanced(stops, total_miles, max_range=500, mpg=10):
    """About 30% fewer stops for about 0.3% more cost: only move on for a 5+ cent saving."""
    return plan_fuel_stops(stops, total_miles, max_range, mpg, min_saving=BALANCED_MIN_SAVING)


def plan_fewest_stops(stops, total_miles, max_range=500, mpg=10):
    """Fewest stops: always fill up and go to the cheapest stop in range. Costs about 2% more."""
    return plan_fuel_stops(stops, total_miles, max_range, mpg, chase_cheaper=False)


STRATEGIES = {
    'lowest_cost': plan_lowest_cost,
    'balanced': plan_balanced,
    'fewest_stops': plan_fewest_stops,
}
