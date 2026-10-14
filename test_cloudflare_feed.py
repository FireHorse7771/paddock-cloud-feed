"""Offline corruption, scope and exact readback guards; no real writes."""
from io import BytesIO
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import zipfile
from cloud_once import export_evidence
from test_cloud_once import CloudFeedTests
from cloudflare_feed import check_access, publish_evidence, RelayError, verified_evidence, strict_json, SITE


class CloudflareRelayTests(unittest.TestCase):
    def fixture(self, temp):
        root = Path(temp) / 'raw'
        root.mkdir()
        value = CloudFeedTests().fixture(root, ['scan_budget'])
        target = Path(temp) / 'source.zip'
        export_evidence(value, root, target)
        return value, target

    def test_native_replay_and_exact_public_readback(self):
        with tempfile.TemporaryDirectory() as temp:
            value, target = self.fixture(temp)
            calls = []
            def opener(request, timeout):
                calls.append(request)
                self.assertTrue(request.full_url.startswith(SITE + '/'))
                if request.data is not None:
                    self.assertEqual(strict_json(request.data), value)
                    self.assertEqual(request.get_header('Authorization'), 'Bearer fixture-key')
                    return BytesIO(json.dumps({'receipt_sha256': value['receipt_sha256']}).encode())
                self.assertIsNone(request.get_header('Authorization'))
                return BytesIO(json.dumps(dict(value, stale=True)).encode())
            result = publish_evidence(target, 'fixture-key', opener)
            self.assertEqual(len(calls), 2)
            self.assertTrue(result['publication_verified'])
            self.assertFalse(result['complete_scan'])
            self.assertTrue(result['public_stale'])
            self.assertEqual(result['raw_price_blocks'], 2)
            self.assertEqual(result['observed_at'], value['observed_at'])

    def repack(self, target, mutate, repin=False):
        with zipfile.ZipFile(target) as source:
            files = {name: source.read(name) for name in source.namelist()}
        mutate(files)
        if repin:
            manifest = json.loads(files['manifest.json'])
            manifest['files'] = {name: {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                                 for name, raw in files.items() if name != 'manifest.json'}
            receipt = next(raw for name, raw in files.items() if name.endswith('/receipt.json'))
            manifest['receipt_sha256'] = hashlib.sha256(receipt).hexdigest()
            files['manifest.json'] = json.dumps(manifest).encode()
        broken = target.parent / 'broken.zip'
        with zipfile.ZipFile(broken, 'w') as output:
            for name, raw in files.items():
                output.writestr(name, raw)
        return broken

    def test_manifest_corruption_and_path_escape_fail_before_network(self):
        for mode in ('raw', 'escape', 'extra'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                _, target = self.fixture(temp)
                def mutate(files):
                    if mode == 'raw':
                        name = next(name for name in files if name.endswith('.txt'))
                        files[name] += b'corrupt'
                    elif mode == 'escape':
                        files['../escape.txt'] = b'escape'
                    else:
                        files['private-file.txt'] = b'forbidden'
                broken = self.repack(target, mutate)
                with self.assertRaises(ValueError):
                    publish_evidence(broken, 'fixture', lambda *a, **k: self.fail('Network attempted'))
                self.assertFalse((Path(temp).parent / 'escape.txt').exists())

    def test_rehashed_semantic_price_corruption_rejected_by_native_verifier(self):
        with tempfile.TemporaryDirectory() as temp:
            _, target = self.fixture(temp)
            def mutate(files):
                name = next(name for name in files if name.endswith('/receipt.json'))
                value = json.loads(files[name])
                value['events'][0]['runners'][0]['decimal_odds'] = 9.0
                files[name] = json.dumps(value).encode()
            broken = self.repack(target, mutate, repin=True)
            with self.assertRaisesRegex(AssertionError, 'Raw price/runner mismatch'):
                publish_evidence(broken, 'fixture', lambda *a, **k: self.fail('Network attempted'))

    def test_receipt_time_or_runner_readback_difference_fails_without_secret(self):
        for field in ('observed_at', 'last_attempt_at', 'events'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                value, target = self.fixture(temp)
                def opener(request, timeout):
                    public = dict(value)
                    public[field] = [] if field == 'events' else 'different'
                    return BytesIO(json.dumps({'receipt_sha256': value['receipt_sha256']}
                                              if request.data else public).encode())
                with self.assertRaises(RelayError) as caught:
                    publish_evidence(target, 'fixture-secret', opener)
                self.assertEqual(str(caught.exception), '')

    def test_transport_error_hides_all_exception_details(self):
        with tempfile.TemporaryDirectory() as temp:
            _, target = self.fixture(temp)
            def opener(*args, **kwargs):
                raise OSError('fixture-secret-transport')
            with self.assertRaises(RelayError) as caught:
                publish_evidence(target, 'fixture-secret', opener)
            self.assertEqual(str(caught.exception), '')

    def test_quote_only_scope_and_invalid_probes(self):
        calls = []
        def opener(request, timeout):
            calls.append(request)
            if not request.data:
                self.assertIsNone(request.get_header('Authorization'))
                return BytesIO(b'{"events":[],"observed_at":null}')
            self.assertEqual(request.data, b'{}')
            feed = request.full_url.endswith('/api/feed')
            auth = bool(request.get_header('Authorization'))
            status = (400 if auth else 401) if feed else 404
            error = ('Receipt rejected' if auth else 'Unauthorized') if feed else 'Unknown route'
            raise urllib.error.HTTPError(request.full_url, status, '', {},
                                         BytesIO(json.dumps({'error': error}).encode()))
        result = check_access('fixture', opener)
        self.assertEqual(len(calls), 7)
        self.assertTrue(result['quote_only_scope_verified'])
        self.assertFalse(result['publication_verified'])

    def test_missing_key_duplicate_json_and_duplicate_zip_rejected(self):
        with self.assertRaises(ValueError):
            publish_evidence('unused', '', lambda *a, **k: self.fail('Network attempted'))
        with self.assertRaises(ValueError):
            strict_json(b'{"a":1,"a":2}')
        with tempfile.TemporaryDirectory() as temp:
            _, target = self.fixture(temp)
            with zipfile.ZipFile(target, 'a') as bundle:
                bundle.writestr('manifest.json', b'{}')
            with self.assertRaises(ValueError):
                with verified_evidence(target):
                    self.fail('Duplicate ZIP accepted')


if __name__ == '__main__':
    unittest.main()
