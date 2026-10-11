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
import zipfile
from source_browser import collect,utc
from verify_receipt import verify

SITE='https://paddock-hk-lab.cuddly-calf-2261.chatgpt.site'


class PublicationError(Exception):
    """A remote write may have happened; verification failed without disclosing details."""


def collection_diagnostic(cards,courses,discovered,failures):
    """Report counts and fixed classifications, never exception text or credentials."""
    reasons={}
    for failure in failures:
        message=failure.get('reason','') if isinstance(failure,dict) else ''
        code='other'
        for prefix,label in [('ValueError: Incomplete or additional markets','incomplete_market'),
                             ('ValueError: One explicit WIN','missing_win_market'),
                             ('TimeoutError:','page_timeout'),('ValueError:','invalid_card')]:
            if message.startswith(prefix):code=label;break
        if isinstance(failure,str):
            code=failure if failure in ('scan_budget','scope_limit') else 'page_failure'
        reasons[code]=reasons.get(code,0)+1
    return dict(stage='collection_completed',courses=courses,discovered_events=discovered,
                accepted_events=len(cards),rejected_entries=len(failures),failure_counts=reasons,
                publication_state='not_attempted',actual_bets_placed=False)


def export_evidence(value,archive,destination,max_bytes=8*1024*1024):
    """Export only independently verified public observations, never a directory dump."""
    audit=verify(value,archive)
    archive=Path(archive).resolve()
    run=Path(audit['archive_run']).resolve()
    files={}
    names={event['dom_sha256']+'.txt' for event in value['events']}
    for name in sorted(names):
        path=archive/name
        if path.is_symlink() or path.resolve().parent!=archive:raise ValueError('Invalid evidence path')
        files[name]=path.read_bytes()
    for name in ('receipt.json','failures.json'):
        path=run/name
        if path.is_symlink() or not path.resolve().is_relative_to(archive):raise ValueError('Invalid evidence path')
        files[run.name+'/'+name]=path.read_bytes()
    public_audit={key:item for key,item in audit.items() if key!='archive_run'}
    files['audit.json']=json.dumps(public_audit,ensure_ascii=False,sort_keys=True).encode('utf8')
    manifest={'schema_version':1,'receipt_sha256':value['receipt_sha256'],'files':{
        name:{'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)} for name,raw in files.items()}}
    files['manifest.json']=json.dumps(manifest,sort_keys=True).encode('utf8')
    if sum(map(len,files.values()))>max_bytes:raise ValueError('Evidence size limit exceeded')
    destination=Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    # Never overwrite an earlier observation or publish an incomplete ZIP.
    with destination.open('xb') as target:
        with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED) as bundle:
            for name,raw in files.items():bundle.writestr(name,raw)
    return {'evidence_sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
            'evidence_bytes':destination.stat().st_size,'evidence_files':len(files)}


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


async def run(do_publish=False,evidence_path=None):
    # A publishing credential must never reach Chromium's child environment.
    key=os.environ.pop('PADDOCK_FEED_WRITE_KEY','')
    if do_publish and not key:raise ValueError('Missing repository secret; nothing published')
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
        print(json.dumps(collection_diagnostic(cards,courses,discovered,failures)),flush=True)
        value=package_receipt(cards,courses,discovered,failures,attempt,archive)
        audit=verify(value,archive)
        print(json.dumps(dict(stage='source_verified',events=audit['events'],publication_state='not_attempted')),flush=True)
        evidence=export_evidence(value,archive,evidence_path) if evidence_path else {}
        if do_publish:publish(value,archive,key)
        print(json.dumps(dict(source_verified=audit['verified'],events=audit['events'],complete_scan=audit['complete_scan'],publication_verified=do_publish,publication_state='verified' if do_publish else 'not_requested',observed_at=value['observed_at'],receipt_sha256=value['receipt_sha256'],actual_bets_placed=False,**evidence)))


if __name__=='__main__':
    args=argparse.ArgumentParser();args.add_argument('--publish',action='store_true');args.add_argument('--evidence',type=Path);options=args.parse_args()
    try:asyncio.run(run(options.publish,options.evidence))
    except Exception as error:
        # Do not print exception args, request headers, response body or secret.
        print(json.dumps({'ok':False,'error_type':type(error).__name__,'publication_verified':False,'publication_state':'unknown' if isinstance(error,PublicationError) else 'not_attempted'}))
        raise SystemExit(1) from None
