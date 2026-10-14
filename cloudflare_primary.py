"""Read public cards once without depending on the former Sites receiver.

Native collector/parser/verifier are unchanged. The existing bounded evidence
ZIP is the sole handoff; publishing keys never reach Chromium.
"""
import argparse,asyncio,json,os
from pathlib import Path
import cloud_once

async def collect_only(evidence):
    # Collection has no writer credentials, including accidental inherited ones.
    for name in ('PADDOCK_FEED_WRITE_KEY','PADDOCK_CF_FEED_WRITE_KEY','PADDOCK_CF_DAILY_WRITE_KEY'):
        os.environ.pop(name,None)
    await cloud_once.run(do_publish=False,evidence_path=Path(evidence))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',required=True,type=Path)
    args=parser.parse_args()
    try:asyncio.run(collect_only(args.evidence))
    except Exception as error:
        print(json.dumps({'ok':False,'error_type':type(error).__name__,'publication_verified':False,'actual_bets_placed':False}))
        raise SystemExit(1) from None
