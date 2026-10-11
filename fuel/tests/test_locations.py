from django.test import SimpleTestCase

from fuel.locations import LocationError, normalize, resolve_location


class NormalizeTests(SimpleTestCase):

    def test_lowercase_trim_collapse_spaces(self):
        self.assertEqual(normalize('  New   York ', ' ny '), ('new york', 'NY'))

    def test_abbreviations(self):
        self.assertEqual(normalize('St. Louis', 'MO'), ('saint louis', 'MO'))
        self.assertEqual(normalize('St Paul', 'MN'), ('saint paul', 'MN'))
        self.assertEqual(normalize('Ft Worth', 'TX'), ('fort worth', 'TX'))
        self.assertEqual(normalize('Mt. Vernon', 'IL'), ('mount vernon', 'IL'))

    def test_full_state_name_to_code(self):
        self.assertEqual(normalize('Los Angeles', 'California'), ('los angeles', 'CA'))
        self.assertEqual(normalize('Charleston', 'west virginia'), ('charleston', 'WV'))


class ResolveLocationTests(SimpleTestCase):

    def test_known_city_returns_lon_lat(self):
        lon, lat = resolve_location('New York, NY')
        self.assertAlmostEqual(lon, -74.0, delta=0.5)
        self.assertAlmostEqual(lat, 40.7, delta=0.5)

    def test_input_variants_resolve_to_same_place(self):
        self.assertEqual(resolve_location('St. Louis, MO'), resolve_location('saint louis, Missouri'))

    def test_unknown_city(self):
        with self.assertRaises(LocationError) as ctx:
            resolve_location('Atlantis, NY')
        self.assertEqual(ctx.exception.code, 'unknown_city')

    def test_non_us_city(self):
        with self.assertRaises(LocationError) as ctx:
            resolve_location('Toronto, ON')
        self.assertEqual(ctx.exception.code, 'unknown_city')

    def test_missing_state_is_invalid_input(self):
        for text in ['New York', '', ', NY', 'Boston, ']:
            with self.subTest(text=text), self.assertRaises(LocationError) as ctx:
                resolve_location(text)
            self.assertEqual(ctx.exception.code, 'invalid_input')
