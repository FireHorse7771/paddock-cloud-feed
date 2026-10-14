"""Optional legacy receiver, executed after verified primary delivery only."""
import argparse,json,os
from pathlib import Path
from cloudflare_feed import verified_evidence
from cloud_once import publish

def mirror(evidence,key):
    with verified_evidence(evidence) as (value,archive,audit):
        publish(value,archive,key)
        return dict(destination='legacy_sites_optional',publication_verified=True,
            receipt_sha256=value['receipt_sha256'],events=audit['events'],actual_bets_placed=False)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--evidence',required=True,type=Path);a=p.parse_args()
    key=os.environ.pop('PADDOCK_FEED_WRITE_KEY','')
    if not key:
        print(json.dumps({'optional_mirror':'not_configured','primary_delivery_unchanged':True}));raise SystemExit(0)
    try:print(json.dumps(mirror(a.evidence,key)))
    except Exception as exc:
        print(json.dumps({'optional_mirror':'failed','error_type':type(exc).__name__,
            'publication_verified':False,'primary_delivery_unchanged':True,'actual_bets_placed':False}))
        raise SystemExit(1) from None
