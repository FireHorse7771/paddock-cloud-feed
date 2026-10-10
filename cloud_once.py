"""One bounded cloud browser scan. Publishing is an explicit separate mode."""
import argparse
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request
from source_browser import collect,utc
from verify_receipt import verify

SITE='https://paddock-hk-lab.cuddly-calf-2261.chatgpt.site'


class PublicationError(Exception):
    """A remote write may have happened; verification failed without disclosing details."""


def package_receipt(cards,courses,discovered,failures,attempt,archive):
    if not cards:raise ValueError('No complete public WIN cards; nothing published')
    completed=datetime.now(timezone.utc)
    value=dict(schema_version=1,available=True,events=cards,observed_at=min(c['observed_at'] for c in cards),last_attempt_at=attempt,completed_at=completed.isoformat(),last_attempt_ok=True,sequence=int(completed.timestamp()),automatic_updates=True,refresh_seconds=300,collection_method='cloud_public_browser_dom',courses=courses,discovered_events=discovered,accepted_events=len(cards),rejected_events=len(failures),complete_scan=not failures,status='Повний огляд' if not failures else 'Частковий огляд: не всі картки прочитані',recommendations_available=False,actual_bets_placed=False)
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf8')
    folder=Path(archive)/('scan_'+completed.strftime('%Y%m%dT%H%M%S'))
    folder.mkdir()
    (folder/'receipt.json').write_bytes(raw)
    (folder/'failures.json').write_text(json.dumps(failures,ensure_ascii=False),encoding='utf8')
    value['receipt_sha256']=hashlib.sha256(raw).hexdigest()
    return value


def publish(value,archive,key,opener=urllib.request.urlopen):
    verify(value,archive)
    if not key:raise ValueError('Missing repository secret; nothing published')
    raw=json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf8')
    request=urllib.request.Request(SITE+'/api/feed',data=raw,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json','User-Agent':'PADDOCK-Cloud-Browser/1'})
    try:
        with opener(request,timeout=30) as response:result=json.load(response)
        if result.get('receipt_sha256')!=value['receipt_sha256']:raise ValueError('Writer receipt mismatch')
        request=urllib.request.Request(SITE+'/pokerbet-current.json',headers={'User-Agent':'PADDOCK-Cloud-Browser/1'})
        with opener(request,timeout=30) as response:public=json.load(response)
        if public.get('receipt_sha256')!=value['receipt_sha256'] or public.get('events')!=value['events']:raise ValueError('Public readback mismatch')
    except Exception:
        raise PublicationError() from None
    return result


async def run(do_publish=False):
    from playwright.async_api import async_playwright
    with tempfile.TemporaryDirectory(prefix='paddock-cloud-public-') as temp:
        archive=Path(temp)
        attempt=utc()
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            try:
                context=await browser.new_context(locale='uk-UA',timezone_id='Europe/Kyiv')
                try:cards,courses,discovered,failures=await asyncio.wait_for(collect(context,archive,budget_seconds=110),timeout=170)
                finally:await context.close()
            finally:await browser.close()
        value=package_receipt(cards,courses,discovered,failures,attempt,archive)
        audit=verify(value,archive)
        if do_publish:publish(value,archive,os.environ.get('PADDOCK_FEED_WRITE_KEY',''))
        print(json.dumps(dict(source_verified=audit['verified'],events=audit['events'],complete_scan=audit['complete_scan'],publication_verified=do_publish,publication_state='verified' if do_publish else 'not_requested',observed_at=value['observed_at'],receipt_sha256=value['receipt_sha256'],actual_bets_placed=False)))


if __name__=='__main__':
    args=argparse.ArgumentParser();args.add_argument('--publish',action='store_true');options=args.parse_args()
    try:asyncio.run(run(options.publish))
    except Exception as error:
        # Do not print exception args, request headers, response body or secret.
        print(json.dumps({'ok':False,'error_type':type(error).__name__,'publication_verified':False,'publication_state':'unknown' if isinstance(error,PublicationError) else 'not_attempted'}))
        raise SystemExit(1) from None
