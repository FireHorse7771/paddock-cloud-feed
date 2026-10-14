import asyncio,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock
import cloudflare_primary
import sites_optional_mirror
from cloudflare_feed import publish_evidence
from test_cloudflare_feed import CloudflareRelayTests
from io import BytesIO

class PrimaryDeliveryTests(unittest.TestCase):
 def test_source_delegates_once_without_legacy_publish(self):
  mocked=AsyncMock()
  with patch.object(cloudflare_primary.cloud_once,'run',mocked):
   asyncio.run(cloudflare_primary.collect_only('evidence.zip'))
  mocked.assert_awaited_once_with(do_publish=False,evidence_path=Path('evidence.zip'))
 def test_no_writer_key_can_reach_source_browser(self):
  async def source(**kwargs):
   self.assertFalse(any(k in os.environ for k in ('PADDOCK_FEED_WRITE_KEY','PADDOCK_CF_FEED_WRITE_KEY','PADDOCK_CF_DAILY_WRITE_KEY')))
  with patch.dict(os.environ,{'PADDOCK_FEED_WRITE_KEY':'fixture','PADDOCK_CF_FEED_WRITE_KEY':'fixture','PADDOCK_CF_DAILY_WRITE_KEY':'fixture'}),patch.object(cloudflare_primary.cloud_once,'run',source):
   asyncio.run(cloudflare_primary.collect_only('evidence.zip'))
 def test_legacy_failure_cannot_block_prior_primary_publication(self):
  with tempfile.TemporaryDirectory() as tmp:
   value,target=CloudflareRelayTests().fixture(tmp)
   def opener(request,timeout):
    return BytesIO(json.dumps({'receipt_sha256':value['receipt_sha256']} if request.data is not None else dict(value,stale=False)).encode())
   result=publish_evidence(target,'fixture-cf-key',opener);self.assertTrue(result['publication_verified'])
   with patch.object(sites_optional_mirror,'publish',side_effect=RuntimeError('fixture old host down')):
    with self.assertRaises(RuntimeError):sites_optional_mirror.mirror(target,'fixture-old-key')
   self.assertTrue(result['publication_verified'])
 def test_old_mirror_reuses_exact_native_verified_evidence(self):
  with tempfile.TemporaryDirectory() as tmp:
   value,target=CloudflareRelayTests().fixture(tmp)
   with patch.object(sites_optional_mirror,'publish') as publisher:
    result=sites_optional_mirror.mirror(target,'fixture-old-key')
    self.assertEqual(publisher.call_args.args[0],value);self.assertEqual(publisher.call_args.args[2],'fixture-old-key')
    self.assertEqual(result['receipt_sha256'],value['receipt_sha256'])
 def test_workflow_keeps_primary_before_optional_legacy(self):
  text=(Path(__file__).parent/'.github/workflows/source-check.yml').read_text('utf8')
  self.assertLess(text.index('Publish verified quotes to primary Cloudflare'),text.index('Optional mirror to previous Sites'))
  primary=text[text.index('Read public Pokerbet cards once'):text.index('Optional mirror to previous Sites')]
  self.assertNotIn('PADDOCK_FEED_WRITE_KEY',primary)
  optional=text[text.index('Optional mirror to previous Sites'):text.index('Preserve verified public evidence')]
  self.assertIn('continue-on-error: true',optional)
  self.assertIn('retention-days: 1',text);self.assertIn('timeout-minutes: 10',text)
if __name__=='__main__':unittest.main()
