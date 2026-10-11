import csv
import re
from functools import lru_cache

from django.conf import settings

CITIES_CSV = settings.BASE_DIR / 'data' / 'us-cities.csv'

STATE_CODES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'district of columbia': 'DC', 'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI',
    'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA',
    'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME',
    'maryland': 'MD', 'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN',
    'mississippi': 'MS', 'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE',
    'nevada': 'NV', 'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM',
    'new york': 'NY', 'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH',
    'oklahoma': 'OK', 'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI',
    'south carolina': 'SC', 'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX',
    'utah': 'UT', 'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA',
    'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
}

CITY_WORDS = {
    'st.': 'saint', 'st': 'saint',
    'ft.': 'fort', 'ft': 'fort',
    'mt.': 'mount', 'mt': 'mount',
}


def _clean(text):
    return re.sub(r'\s+', ' ', text.strip().lower())


def normalize(city, state):
    """Return a (city, state_code) key used to match cities everywhere."""
    city = ' '.join(CITY_WORDS.get(word, word) for word in _clean(city).split())
    state = _clean(state)
    state = STATE_CODES.get(state, state.upper())
    return city, state


@lru_cache(maxsize=1)
def city_table():
    """Offline lookup: normalize(city, state) -> (lon, lat)."""
    table = {}
    with open(CITIES_CSV, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            key = normalize(row['city'], row['state'])
            table.setdefault(key, (float(row['lon']), float(row['lat'])))
    return table


class LocationError(ValueError):
    """code: 'invalid_input' (bad format) or 'unknown_city'. field is set by the caller."""

    def __init__(self, message, code, value):
        super().__init__(message)
        self.code = code
        self.value = value
        self.field = None


def location_key(text):
    """Parse "City, ST" or "City, State Name" into the normalized (city, state_code) key."""
    city, sep, state = (text or '').rpartition(',')
    if not sep or not city.strip() or not state.strip():
        raise LocationError(
            f'Invalid location "{text}". Use "City, ST" or "City, State Name".',
            code='invalid_input', value=text,
        )
    return normalize(city, state)


def resolve_location(text):
    """Turn "City, ST" or "City, State Name" into (lon, lat). US-only via the table."""
    coords = city_table().get(location_key(text))
    if coords is None:
        raise LocationError(f'Unknown US city "{text}".', code='unknown_city', value=text)
    return coords
