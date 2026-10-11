import csv
import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand
from django.db import transaction

from fuel.locations import STATE_CODES, city_table, normalize
from fuel.models import TruckStop

DEFAULT_CSV = settings.BASE_DIR / 'data' / 'fuel-prices-for-be-assessment.csv'
REPORT_PATH = settings.BASE_DIR / 'reports' / 'load_stops.json'
US_STATES = set(STATE_CODES.values())


class Command(BaseCommand):
    help = 'Load truck stops from the fuel prices CSV (cheapest price per OPIS ID).'

    def add_arguments(self, parser):
        parser.add_argument('--csv', default=DEFAULT_CSV, help='Path to the fuel prices CSV.')

    def handle(self, *args, **options):
        cheapest = {}
        with open(options['csv'], newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                opis_id = int(row['OPIS Truckstop ID'])
                price = Decimal(row['Retail Price'])
                if opis_id not in cheapest or price < cheapest[opis_id][0]:
                    cheapest[opis_id] = (price, row)

        cities = city_table()
        stops, unmatched = [], []
        for opis_id, (price, row) in cheapest.items():
            coords = cities.get(normalize(row['City'], row['State']))
            if coords is None:
                unmatched.append({
                    'opis_id': opis_id,
                    'name': row['Truckstop Name'].strip(),
                    'city': row['City'].strip(),
                    'state': row['State'].strip().upper(),
                })
                continue
            stops.append(TruckStop(
                opis_id=opis_id,
                name=row['Truckstop Name'].strip(),
                address=row['Address'].strip(),
                city=row['City'].strip(),
                state=row['State'].strip().upper(),
                price=price.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP),
                location=Point(*coords, srid=4326),
            ))

        with transaction.atomic():
            TruckStop.objects.all().delete()
            TruckStop.objects.bulk_create(stops, batch_size=1000)

        report = self.build_report(cheapest, stops, unmatched)
        REPORT_PATH.parent.mkdir(exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, indent=2) + '\n')

        s = report['summary']
        self.stdout.write(f"Unique OPIS IDs: {s['unique_opis_ids']}")
        self.stdout.write(
            f"Unmatched (skipped): {s['unmatched']} ({s['unmatched_pct']}%) "
            f"- non-US: {s['unmatched_non_us']}, US: {s['unmatched_us']} ({s['unmatched_us_pct']}%)"
        )
        self.stdout.write(self.style.SUCCESS(f"Loaded: {s['loaded']}"))
        self.stdout.write(f'Report: {REPORT_PATH.relative_to(settings.BASE_DIR)}')

    def build_report(self, cheapest, stops, unmatched):
        us = [u for u in unmatched if u['state'] in US_STATES]
        non_us = [u for u in unmatched if u['state'] not in US_STATES]
        us_total = sum(1 for _, row in cheapest.values() if row['State'].strip().upper() in US_STATES)

        def pct(part, whole):
            return round(100 * part / whole, 2) if whole else 0.0

        return {
            'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'summary': {
                'unique_opis_ids': len(cheapest),
                'loaded': len(stops),
                'unmatched': len(unmatched),
                'unmatched_pct': pct(len(unmatched), len(cheapest)),
                'unmatched_non_us': len(non_us),
                'unmatched_us': len(us),
                'us_stations': us_total,
                'unmatched_us_pct': pct(len(us), us_total),
            },
            'unmatched_us': sorted(us, key=lambda u: (u['state'], u['city'])),
            'unmatched_non_us': sorted(non_us, key=lambda u: (u['state'], u['city'])),
        }
