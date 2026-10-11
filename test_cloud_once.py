"""Offline fixtures only; no source browsing or real remote writes."""
from io import BytesIO
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
import urllib.error
from cloud_once import package_receipt,publish,PublicationError,export_evidence,collection_diagnostic,check_access,AccessProbeError
from source_browser import parse_event,utc
from verify_receipt import verify

class CloudFeedTests(unittest.TestCase):
    def test_scoped_access_uses_only_invalid_payloads_and_preserves_feed(self):
        calls=[]
        def opener(request,timeout):
            calls.append(request)
            self.assertEqual(request.get_header('User-agent'),'PADDOCK-Cloud-Browser/1')
            if request.data is None:
                self.assertIsNone(request.get_header('Authorization'))
                return BytesIO(b'{"receipt_sha256":"fixture","events":[],"observed_at":null}')
            self.assertEqual(request.data,b'{}')
            authorized=request.get_header('Authorization')=='Bearer fixture'
            feed=request.full_url.endswith('/api/feed')
            error='Receipt rejected' if authorized and feed else 'Unauthorized'
            status=400 if authorized and feed else 401
            raise urllib.error.HTTPError(request.full_url,status,'fixture',{},BytesIO(json.dumps({'error':error}).encode()))
        result=check_access('fixture',opener)
        self.assertTrue(result['access_verified']);self.assertTrue(result['feed_unchanged'])
        self.assertFalse(result['publication_verified']);self.assertEqual(len(calls),7)

    def test_access_probe_stops_on_wrong_scope_or_changed_feed_without_leaking_error(self):
        for failure in ('wrong_key','broad_key','changed_feed','transport'):
            with self.subTest(failure=failure):
                calls=[]
                def opener(request,timeout):
                    calls.append(request)
                    if failure=='transport':raise OSError('fixture-secret')
                    if request.data is None:return BytesIO(json.dumps({'receipt_sha256':len(calls) if failure=='changed_feed' else 'fixture'}).encode())
                    authorized=bool(request.get_header('Authorization')) and failure!='wrong_key'
                    status=400 if authorized and (request.full_url.endswith('/api/feed') or failure=='broad_key') else 401
                    payload={'error':'Receipt rejected' if status==400 else 'Unauthorized'}
                    raise urllib.error.HTTPError(request.full_url,status,'fixture',{},BytesIO(json.dumps(payload).encode()))
                with self.assertRaises(AccessProbeError) as caught:check_access('fixture-secret',opener)
                self.assertEqual(str(caught.exception),'')

    def test_missing_access_key_stops_before_network(self):
        with self.assertRaises(ValueError):check_access('',lambda *a,**k:self.fail('Network attempted'))

    def test_empty_source_cannot_be_packaged(self):
        with tempfile.TemporaryDirectory() as temp:
            archive=Path(temp)
            with self.assertRaises(ValueError):package_receipt([],0,0,[],utc(),archive)
            self.assertEqual(list(archive.iterdir()),[])

    def test_diagnostic_never_exports_failure_text(self):
        failures=[{'reason':'ValueError: Incomplete or additional markets fixture-secret'},
                  {'reason':'TimeoutError: fixture-secret'},'scan_budget','fixture-secret']
        result=collection_diagnostic([],2,4,failures)
        self.assertEqual(result['accepted_events'],0)
        self.assertEqual(result['failure_counts'],{'incomplete_market':1,'page_timeout':1,'scan_budget':1,'page_failure':1})
        self.assertNotIn('fixture-secret',json.dumps(result))
        self.assertEqual(result['publication_state'],'not_attempted')

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

    def test_evidence_is_replayable_and_allowlisted(self):
        with tempfile.TemporaryDirectory() as temp:
            archive=Path(temp)/'raw';archive.mkdir();value=self.fixture(archive,['scan_budget'])
            (archive/'private-key.txt').write_text('fixture-secret-never-export')
            target=Path(temp)/'evidence.zip';result=export_evidence(value,archive,target)
            with zipfile.ZipFile(target) as bundle:
                self.assertNotIn('private-key.txt',bundle.namelist())
                audit=json.loads(bundle.read('audit.json'));self.assertNotIn('archive_run',audit)
                manifest=json.loads(bundle.read('manifest.json'))
                for name,info in manifest['files'].items():
                    raw=bundle.read(name);self.assertEqual(hashlib.sha256(raw).hexdigest(),info['sha256'])
                    self.assertEqual(len(raw),info['bytes']);self.assertNotIn(b'fixture-secret',raw)
                replay=Path(temp)/'replay';bundle.extractall(replay)
                self.assertTrue(verify(value,replay)['verified'])
            self.assertEqual(result['evidence_sha256'],hashlib.sha256(target.read_bytes()).hexdigest())
            before=target.read_bytes()
            with self.assertRaises(FileExistsError):export_evidence(value,archive,target)
            self.assertEqual(target.read_bytes(),before)

    def test_invalid_or_oversized_evidence_is_not_exported(self):
        with tempfile.TemporaryDirectory() as temp:
            archive=Path(temp)/'raw';archive.mkdir();value=self.fixture(archive)
            target=Path(temp)/'evidence.zip'
            with self.assertRaises(ValueError):export_evidence(value,archive,target,max_bytes=10)
            self.assertFalse(target.exists())
            (archive/(value['events'][0]['dom_sha256']+'.txt')).write_bytes(b'corrupt')
            with self.assertRaises(AssertionError):export_evidence(value,archive,target)
            self.assertFalse(target.exists())

if __name__=='__main__':unittest.main()
