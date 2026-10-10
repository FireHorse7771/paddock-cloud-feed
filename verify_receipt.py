"""Independent audit of an archived public DOM receipt before cloud transfer."""
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo


def verify(value, archive):
    archive = Path(archive)
    completed = datetime.fromisoformat(value['completed_at'])
    assert completed.tzinfo is not None and completed <= datetime.now(timezone.utc)
    run = archive / ('scan_' + completed.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S'))
    raw = (run / 'receipt.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == value['receipt_sha256'], 'Receipt hash mismatch'
    original = dict(value)
    original.pop('receipt_sha256')
    assert json.loads(raw) == original, 'Runtime receipt differs from archived observation'
    assert value['actual_bets_placed'] is False and value['recommendations_available'] is False
    assert value['last_attempt_ok'] is True and value['automatic_updates'] is True
    events = value['events']
    assert 1 <= len(events) <= 60 and value['accepted_events'] == len(events)
    # The existing producer calls *all* scan failures rejected_events, including
    # course discovery errors and scan_budget. Count failed event routes separately.
    failures = json.loads((run / 'failures.json').read_bytes())
    assert isinstance(failures, list) and value['rejected_events'] == len(failures)
    rejected_routes = set()
    course_failures = 0
    for failure in failures:
        route = failure.get('route') if isinstance(failure, dict) else failure
        assert isinstance(route, str), 'Invalid scan failure'
        if re.fullmatch(r'/uk/events/race-\d+-\d+', route):
            assert route not in rejected_routes, 'Duplicate rejected route'
            rejected_routes.add(route)
        elif isinstance(failure, str) and re.fullmatch(r'/uk/sports/horseracing/[A-Za-z0-9/_-]+', route):
            course_failures += 1
        else:
            assert isinstance(failure, str) and route in ('scan_budget', 'scope_limit'), 'Unknown scan failure'
    discovered = value['discovered_events']
    assert isinstance(discovered, int) and not isinstance(discovered, bool) and discovered >= len(events)
    assert min(discovered, 60) == len(events) + len(rejected_routes), 'Event accounting mismatch'
    assert (discovered > 60) == ('scope_limit' in failures), 'Scope limit mismatch'
    assert value['complete_scan'] == (not failures)
    event_ids, blocks = set(), 0
    for event in events:
        assert event['event_id'] not in event_ids
        event_ids.add(event['event_id'])
        assert re.fullmatch(r'[a-f0-9]{64}', event['dom_sha256'])
        blob = (archive / (event['dom_sha256'] + '.txt')).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == event['dom_sha256'], 'DOM hash mismatch'
        lines = [line.strip() for line in blob.decode('utf8').splitlines() if line.strip()]
        assert lines.count('Переможець') == 1
        win = lines.index('Переможець')
        header, rows = lines[:win], lines[win + 1:lines.index('Купон', win)]
        for key in ['meeting', 'date_label', 'displayed_time', 'race_label']:
            assert event[key] in header, key
        assert event['race_label'] == 'RACE ' + str(event['race_number'])
        route = 'race-' + str(event['race_number']) + '-' + event['event_id']
        assert event['public_url'] == 'https://pbcorp.xyz/uk/sport/events/' + route
        assert event['source_url'] == 'https://sport-iframe.thregvrvd.xyz/uk/events/' + route
        assert '/uk/events/' + route not in rejected_routes, 'Accepted event also rejected'
        observed = datetime.fromisoformat(event['observed_at'])
        assert observed.tzinfo is not None and observed <= completed
        assert event['date_label'] in ['СЬОГОДНІ', 'ЗАВТРА']
        day = observed.astimezone(ZoneInfo('Europe/Kyiv')).date()
        assert event['race_date'] == (day + timedelta(days=event['date_label'] == 'ЗАВТРА')).isoformat()
        assert re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', event['displayed_time'])
        assert event['market'] == 'WIN' and event['complete_market'] is True
        assert event['forecast_pairing_verified'] is False and event['executable_prices_verified'] is False
        assert 2 <= len(event['runners']) <= 40 and len(rows) == 4 * len(event['runners'])
        numbers, names = set(), set()
        for index, runner in enumerate(event['runners']):
            number, horse, jockey, price = rows[index * 4:index * 4 + 4]
            actual = (int(number), horse, jockey, Fraction(price.replace(',', '.')))
            expected = (runner['bookmaker_number'], runner['horse'], runner['jockey'],
                        Fraction(str(runner['decimal_odds'])))
            assert actual == expected, 'Raw price/runner mismatch'
            assert actual[3] > 1 and actual[0] not in numbers and horse not in names
            numbers.add(actual[0]); names.add(horse); blocks += 1
    return dict(verified=True, receipt_sha256=value['receipt_sha256'], events=len(events),
                raw_price_blocks=blocks, archive_run=str(run), complete_scan=value['complete_scan'],
                rejected_event_routes=len(rejected_routes), failed_course_discoveries=course_failures,
                scan_failures=len(failures), no_producer_parser=True, actual_bets_placed=False)
