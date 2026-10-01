import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from ux46_account_usage import project_usage
from ux46_account_gateway import AccountUsageAPI
from test_ux46_access_gateway import GateCase, ORIGIN, USER

class Projection(unittest.TestCase):
    def test_retains_allowance_and_credits_without_account_identifiers(self):
        value=project_usage({'accountId':'private-id','ordinaryUsageAllowed':False,'rateLimits':{'primary':{'usedPercent':100,'windowDurationMins':10080,'resetsAt':200},'credits':{'hasCredits':True,'balance':'secret-balance'}},'rateLimitResetCredits':{'availableCount':3,'credits':[{'id':'redemption-token'}]}},100)
        self.assertEqual(value['buckets'][0]['windows'][0]['used_percent'],100)
        self.assertTrue(value['buckets'][0]['credits_available'])
        self.assertEqual(value['available_resets'],3)
        for secret in ['private-id','secret-balance','redemption-token']:self.assertNotIn(secret,json.dumps(value))
    def test_unknown_and_invalid_values_are_not_zero_allowance(self):
        self.assertEqual(project_usage({})['state'],'unavailable')
        row=project_usage({'rateLimits':{'primary':{'usedPercent':float('nan'),'windowDurationMins':-1}}})['buckets'][0]['windows'][0]
        self.assertNotIn('used_percent',row);self.assertNotIn('minutes',row)

class UsageGateway(GateCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        p=Path(cls.tmp.name)/'usage.json';p.write_text(json.dumps({'local_agent':'local','agents':{'local':['fixture-probe'],'aster':['aster-probe']}}))
        cls.server.account_usage_api=AccountUsageAPI(p)
    def setUp(self):
        super().setUp();self.server.account_usage_api.cache.clear()
    def test_authenticated_selected_agent_probe_is_deduplicated(self):
        with patch('ux46_account_gateway.subprocess.run') as run:
            run.return_value.stdout=b'{"state":"reported","buckets":[],"checked_at":100}'
            for _ in range(2):
                status,headers,body=self.ask('GET','/api/agents/aster/api/account-usage')
                self.assertEqual(status,200);self.assertEqual(json.loads(body)['source'],'saved_login')
            self.assertEqual(run.call_count,1);self.assertEqual(run.call_args.args[0],['aster-probe'])
    def test_usage_ui_is_served_by_the_independent_gateway(self):
        prior=getattr(self.server,"workspace_ui",None)
        try:
            ui=Path(self.tmp.name)/'usage-ui';ui.mkdir(exist_ok=True)
            (ui/'efficiency.js').write_text('/* new allowance view */')
            self.server.workspace_ui=ui
            status,_,body=self.ask('GET','/efficiency.js')
            self.assertEqual(status,200);self.assertIn(b'new allowance view',body)
        finally:self.server.workspace_ui=prior
    def test_unauthenticated_request_cannot_run_probe(self):
        with patch('ux46_account_gateway.subprocess.run') as run:
            status,_,_=self.ask('GET','/api/account-usage',identity=False)
            self.assertEqual(status,403);run.assert_not_called()
