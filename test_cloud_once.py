"""Offline fixtures only; no source browsing or real remote writes."""
from io import BytesIO
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from cloud_once import package_receipt,publish,PublicationError
from source_browser import parse_event,utc
from verify_receipt import verify

class CloudFeedTests(unittest.TestCase):
    def fixture(self,archive,failures=None,discovered=1):
        raw='Кінні перегони\nІрландія. Нейс\nСЬОГОДНІ\n18:10\nRACE 1\nПереможець\n1\nFixture Horse A\nFixture Jockey A\n2.50\n2\nFixture Horse B\nFixture Jockey B\n3.75\nКупон\n'
        observed=utc();card=parse_event(raw,'/uk/events/race-1-123456',observed)
        card['dom_sha256']=hashlib.sha256(raw.encode()).hexdigest()
        (archive/(card['dom_sha256']+'.txt')).write_bytes(raw.encode('utf8'))
        return package_receipt([card],[],discovered,failures or [],observed,archive)

    def test_complete_and_partial_sources(self):
        for failures,discovered in [([],1),(['scan_budget'],1),(['/uk/sports/horseracing/ireland/test'],1),(['/uk/events/race-2-234567'],2)]:
            with self.subTest(failures=failures),tempfile.TemporaryDirectory() as temp:
                archive=Path(temp);result=verify(self.fixture(archive,failures,discovered),archive)
                self.assertTrue(result['verified']);self.assertEqual(result['complete_scan'],not failures)

    def test_corruption_stops_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            archive=Path(temp);value=self.fixture(archive);value['events'][0]['runners'][0]['decimal_odds']=2.75
            with self.assertRaises(AssertionError):publish(value,archive,'fixture',lambda *a,**k:self.fail('Network attempted'))

    def test_missing_key_stops_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            archive=Path(temp);value=self.fixture(archive)
            with self.assertRaises(ValueError):publish(value,archive,'',lambda *a,**k:self.fail('Network attempted'))

    def test_matching_write_and_public_readback(self):
        with tempfile.TemporaryDirectory() as temp:
            archive=Path(temp);value=self.fixture(archive);calls=[]
            def opener(request,timeout):
                calls.append(request);payload={'receipt_sha256':value['receipt_sha256']} if len(calls)==1 else value
                return BytesIO(json.dumps(payload).encode())
            publish(value,archive,'fixture-key',opener)
            self.assertEqual(len(calls),2)
            self.assertEqual(calls[0].get_header('Authorization'),'Bearer fixture-key')
            self.assertIsNone(calls[1].get_header('Authorization'))

    def test_unknown_remote_state_hides_secret(self):
        for mode in ('writer','readback','transport'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as temp:
                archive=Path(temp);value=self.fixture(archive);calls=[]
                def opener(request,timeout):
                    calls.append(request)
                    if mode=='transport':raise OSError('fixture-secret-must-not-escape')
                    payload=value if len(calls)==2 else {'receipt_sha256':value['receipt_sha256']}
                    if (mode=='writer' and len(calls)==1) or (mode=='readback' and len(calls)==2):payload={'receipt_sha256':'wrong'}
                    return BytesIO(json.dumps(payload).encode())
                with self.assertRaises(PublicationError) as caught:publish(value,archive,'fixture-secret-must-not-escape',opener)
                self.assertEqual(str(caught.exception),'')

if __name__=='__main__':unittest.main()
