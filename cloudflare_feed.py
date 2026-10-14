"""Mirror one native, independently verified public ZIP; never collect twice."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import urllib.error
import urllib.request
import zipfile
from verify_receipt import verify

SITE = 'https://paddock-race-desk.pages.dev'
MAX_BYTES = 8 * 1024 * 1024
USER_AGENT = 'PADDOCK-Cloudflare-Relay/1'


class RelayError(Exception):
    """No remote exception text or credentials in logs."""


def strict_json(raw):
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError('Duplicate JSON key')
            result[name] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


@contextmanager
def verified_evidence(path):
    """Bounded allowlist extraction, exact manifest, then unchanged raw verifier."""
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Invalid ZIP size or path')
    with zipfile.ZipFile(path) as bundle:
        infos = bundle.infolist()
        names = [info.filename for info in infos]
        if len(infos) > 64 or len(set(names)) != len(names):
            raise ValueError('Invalid ZIP member count')
        if sum(info.file_size for info in infos) > MAX_BYTES:
            raise ValueError('Evidence size limit exceeded')
        allowed = re.compile(r'(?:[a-f0-9]{64}\.txt|scan_\d{8}T\d{6}/(?:receipt|failures)\.json|audit\.json|manifest\.json)')
        for info in infos:
            if (not allowed.fullmatch(info.filename) or info.is_dir() or
                    stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1):
                raise ValueError('Invalid ZIP member')
        manifest = strict_json(bundle.read('manifest.json'))
        if manifest['schema_version'] != 1 or set(manifest['files']) != set(names) - {'manifest.json'}:
            raise ValueError('Manifest member mismatch')
        data = {}
        for name, meta in manifest['files'].items():
            raw = bundle.read(name)
            if len(raw) != meta['bytes'] or hashlib.sha256(raw).hexdigest() != meta['sha256']:
                raise ValueError('Manifest digest mismatch')
            data[name] = raw
    receipts = [name for name in data if name.endswith('/receipt.json')]
    if len(receipts) != 1:
        raise ValueError('One receipt required')
    receipt_name = receipts[0]
    value = strict_json(data[receipt_name])
    if 'receipt_sha256' in value:
        raise ValueError('Unexpected archived digest')
    digest = hashlib.sha256(data[receipt_name]).hexdigest()
    if digest != manifest['receipt_sha256']:
        raise ValueError('Receipt digest mismatch')
    value['receipt_sha256'] = digest
    expected = {'audit.json', receipt_name, receipt_name.replace('/receipt.json', '/failures.json')}
    expected.update(event['dom_sha256'] + '.txt' for event in value['events'])
    if set(data) != expected:
        raise ValueError('Unexpected evidence file')
    with tempfile.TemporaryDirectory(prefix='paddock-cf-verified-') as temporary:
        archive = Path(temporary)
        for name, raw in data.items():
            target = archive / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(raw)
        audit = verify(value, archive)
        if strict_json(data['audit.json']) != {k: v for k, v in audit.items() if k != 'archive_run'}:
            raise ValueError('Independent audit mismatch')
        yield value, archive, audit


def read_public(opener):
    request = urllib.request.Request(SITE + '/pokerbet-current.json', headers={'User-Agent': USER_AGENT})
    with opener(request, timeout=30) as response:
        raw = response.read(700001)
    if len(raw) > 700000:
        raise ValueError('Public receipt size limit')
    return strict_json(raw)


def check_access(key, opener=urllib.request.urlopen):
    if not key:
        raise ValueError('Missing new repository secret')
    try:
        before = read_public(opener)
        for path, credential, status, message in (
                ('/api/feed', '', 401, 'Unauthorized'),
                ('/api/feed', key, 400, 'Receipt rejected'),
                ('/api/cloud-source-evidence', key, 404, 'Unknown route'),
                ('/api/cloud-client-source', key, 404, 'Unknown route'),
                ('/api/asset/research-day.json', key, 404, 'Unknown route')):
            headers = {'Content-Type': 'application/json', 'User-Agent': USER_AGENT}
            if credential:
                headers['Authorization'] = 'Bearer ' + credential
            request = urllib.request.Request(SITE + path, data=b'{}', headers=headers)
            try:
                with opener(request, timeout=30):
                    raise ValueError('Unexpected accepted probe')
            except urllib.error.HTTPError as response:
                if response.code != status or strict_json(response.read(4096)) != {'error': message}:
                    raise ValueError('Unexpected access scope') from None
        after = read_public(opener)
        fields = ('receipt_sha256', 'events', 'observed_at', 'last_attempt_at', 'completed_at')
        if {k: before.get(k) for k in fields} != {k: after.get(k) for k in fields}:
            raise ValueError('Feed changed during access probe')
    except Exception:
        raise RelayError() from None
    return {'cloudflare_access_verified': True, 'quote_only_scope_verified': True,
            'feed_unchanged': True, 'publication_verified': False, 'actual_bets_placed': False}


def publish_evidence(path, key, opener=urllib.request.urlopen):
    if not key:
        raise ValueError('Missing new repository secret')
    with verified_evidence(path) as (value, archive, audit):
        # Re-run the native verifier immediately before the authenticated write.
        verify(value, archive)
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf8')
        if len(raw) > 700000:
            raise ValueError('Receipt size limit')
        request = urllib.request.Request(SITE + '/api/feed', data=raw, headers={
            'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'User-Agent': USER_AGENT})
        try:
            with opener(request, timeout=30) as response:
                result = strict_json(response.read(4096))
            if result.get('receipt_sha256') != value['receipt_sha256']:
                raise ValueError('Writer receipt mismatch')
            public = read_public(opener)
            # Public runtime may append freshness fields, but all original fields must match.
            if any(public.get(name) != item for name, item in value.items()):
                raise ValueError('Public receipt mismatch')
        except Exception:
            raise RelayError() from None
        return {'site': SITE, 'source_verified': True, 'publication_verified': True,
                'receipt_sha256': value['receipt_sha256'], 'observed_at': value['observed_at'],
                'last_attempt_at': value['last_attempt_at'], 'completed_at': value['completed_at'],
                'events': audit['events'], 'raw_price_blocks': audit['raw_price_blocks'],
                'complete_scan': audit['complete_scan'], 'public_stale': public.get('stale'),
                'actual_bets_placed': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check-access', action='store_true')
    mode.add_argument('--publish', action='store_true')
    parser.add_argument('--evidence', type=Path)
    options = parser.parse_args()
    key = os.environ.pop('PADDOCK_CF_FEED_WRITE_KEY', '')
    try:
        result = check_access(key) if options.check_access else publish_evidence(options.evidence, key)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'ok': False, 'error_type': type(error).__name__,
                          'publication_verified': False, 'actual_bets_placed': False}))
        raise SystemExit(1) from None
