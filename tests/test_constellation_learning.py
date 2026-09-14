import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_constellation_email import Store,Principal,lesson
from constellation_store import encoded,Conflict

class Learning(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'knowledge.db')
        self.owner=Principal('owner',('*',),True);self.peer=Principal('peer',('alpha',),True)
    def capture(self,id,claim,projects=['alpha'],**fields):
        p=lesson(id,projects);p['record'].update(claim=claim,**fields);self.store.capture(self.owner,p)
    def brief(self,q,principal=None,**args):
        return self.store.learning_view(principal or self.owner,'brief',dict(query=q,**args))
    def test_whole_words_prevent_ai_matching_failure(self):
        self.capture('failure','Failure of a database snapshot',terms=['backup'],rationale='Check files',applies='Migration')
        self.assertEqual(self.brief('AI')['items'],[])
    def test_cross_project_retrieval_is_scoped_not_hard_filtered(self):
        self.capture('backup','Check backups after migrations',subjects=['snapshot'],learning={'trigger':'Moving the database','action':'Verify destination files','check':'Restore and read'},origin='agent-discovery')
        result=self.brief('database migrations backups',project='orbit')
        self.assertEqual(result['items'][0]['id'],'backup')
        self.assertEqual(result['items'][0]['learning']['check'],'Restore and read')
        self.assertFalse(result['items'][0]['match']['project_match'])
        self.assertNotIn('excerpt',json.dumps(result['items'][0]['sources']))
    def test_one_hop_surfaces_connection_without_scope_leak(self):
        self.capture('target','Public exchange',terms=['shelf'])
        self.capture('seed','Adaptive interface',terms=['reshape'],links=[{'target':'target','type':'informs','state':'proposed','reason':'A place to share the pattern'}])
        self.capture('private','Adaptive interface secret',projects=['secret'],terms=['reshape'])
        r=self.brief('reshape',principal=self.peer)
        self.assertEqual({x['id'] for x in r['items']},{'seed','target'})
        self.assertEqual(r['items'][1]['match']['via']['type'],'informs')
        self.assertNotIn('Adaptive interface secret',json.dumps(r))
    def test_failed_reuse_qualifies_future_advice_without_erasing_it(self):
        self.capture('method','Migration method',terms=['migration'])
        self.store.feedback(self.peer,{'key':'failed-use','id':'method','revision':1,'use_id':'task-1','verdict':'failed','reason':'Invalid for copied completion marker','suggestion':'Verify the new destination first'})
        result=self.brief('migration')['items'][0]
        self.assertEqual(result['outcome']['state'],'reported-failure')
        self.assertTrue(any('Failure reported' in w for w in result['warnings']))
        self.assertEqual(result['outcome']['corrections'][0]['by'],'peer')
        self.assertEqual(result['outcome']['reports'][0]['reason'],'Invalid for copied completion marker')
        self.assertEqual(self.store.get(self.owner,'method')['revision'],1)
        self.assertEqual(self.store.learning_view(self.owner,'review',{})['items'][0]['id'],'method')
        r=self.store.get(self.owner,'method');r['claim']='Revised migration method'
        self.store.capture(self.owner,{'key':'revise','base_revision':1,'record':r})
        outcome=self.brief('migration')['items'][0]['outcome']
        self.assertEqual(outcome['state'],'not-yet-applied');self.assertEqual(outcome['earlier_revision_reports'],1)
    def test_bounded_complete_brief_and_deterministic_order(self):
        for i in range(8):self.capture('r'+str(i),'Snapshot handling '+str(i),terms=['snapshot'],learning={'trigger':'a'*500,'action':'b'*500,'check':'c'*500})
        a=self.brief('snapshot',limit=5,budget_bytes=2500);b=self.brief('snapshot',limit=5,budget_bytes=2500)
        self.assertEqual(a['items'],b['items']);self.assertLessEqual(len(encoded(a)),2500)
        self.assertTrue(a['measurement']['truncated']);self.assertEqual(len(encoded(a)),a['measurement']['returned_bytes'])
    def test_retirement_review_dates_and_adoption_observations(self):
        self.capture('old','Backup lesson',terms=['backup'],review_after=1)
        self.capture('gone','Backup lesson',terms=['backup'],state='retired')
        result=self.brief('backup')['items'];self.assertEqual([x['id'] for x in result],['old'])
        self.assertTrue(any('review date' in w for w in result[0]['warnings']))
        self.brief('notpresent')
        h=self.store.health(self.owner);row=next(x for x in h['adoption'] if x['principal']=='owner')
        self.assertEqual(row['briefs_in_window'],2);self.assertEqual(row['empty_briefs_in_window'],1)
        self.assertEqual([x['principal'] for x in self.store.health(self.peer)['adoption']],['peer'])
    def test_late_feedback_names_the_actual_published_revision(self):
        self.capture('method','Old method',terms=['migration'])
        old=self.store.get(self.owner,'method');new=dict(old,claim='Revised method')
        self.store.capture(self.owner,{'key':'revision-two','base_revision':1,'record':new})
        self.store.feedback(self.peer,{'key':'late','id':'method','revision':1,'use_id':'late-use','verdict':'failed','reason':'Used the earlier method'})
        self.assertEqual(self.store.get(self.peer,'method',1)['claim'],'Old method')
        self.assertEqual(self.brief('migration')['items'][0]['outcome']['earlier_revision_reports'],1)
        current=self.store.get(self.owner,'method');current['projects']=['alpha','secret']
        self.store.capture(self.owner,{'key':'restricted','base_revision':2,'record':current})
        from constellation_store import Unavailable
        with self.assertRaises(Unavailable):self.store.get(self.peer,'method',1)
        self.assertEqual(self.store.health(self.peer)['feedback'],0)

    def test_catalog_pages_authorized_records_only(self):
        for i in range(3):self.capture('a'+str(i),'Entry '+str(i))
        self.capture('z','Secret entry',projects=['secret'])
        first=self.store.learning_view(self.peer,'catalog',{'limit':2});self.assertTrue(first['more'])
        last=self.store.learning_view(self.peer,'catalog',{'limit':2,'after':first['after']})
        self.assertEqual([x['id'] for x in last['items']],['a2']);self.assertFalse(last['more'])

    def report(self,id,verdict,principal=None,use='use'):
        return self.store.feedback(principal or self.peer,{'key':id+'-'+verdict+'-'+use,'id':id,'revision':1,'use_id':use,'verdict':verdict,'reason':'Observed in fixture'})

    def test_usefulness_breaks_relevance_ties_without_popularity_takeover(self):
        for id in ('a-new','z-helpful'):self.capture(id,'Migration check',terms=['migration'])
        self.report('z-helpful','helped')
        self.assertEqual(self.brief('migration',limit=1)['items'][0]['id'],'z-helpful')
        adjustment=self.brief('migration')['items'][0]['outcome']['ranking_adjustment']
        for i in range(10):self.report('z-helpful','helped',use='repeat'+str(i))
        self.assertEqual(self.brief('migration')['items'][0]['outcome']['ranking_adjustment'],adjustment)
        self.capture('precise','Migration destination snapshot checks',terms=['migration','destination','snapshot'])
        self.assertEqual(self.brief('migration destination snapshot',limit=1)['items'][0]['id'],'precise')
        self.assertEqual(self.store.lookup(self.owner,'migration destination snapshot')['items'][0]['id'],'precise')

    def test_failed_revision_is_held_but_recoverable_and_revisable(self):
        self.capture('bad','Migration check',terms=['migration'])
        self.report('bad','failed');self.report('bad','failed',use='another')
        self.assertEqual(len(self.brief('migration')['items']),1) # One reporter cannot hold it alone.
        self.report('bad','failed',principal=self.owner)
        self.assertEqual(self.brief('migration')['items'],[])
        self.assertEqual(self.store.lookup(self.owner,'migration')['items'],[])
        self.assertTrue(self.brief('migration',include_flagged=True)['items'][0]['outcome']['held_for_review'])
        self.assertTrue(self.store.get(self.owner,'bad')['outcome']['held_for_review'])
        self.assertEqual(self.store.learning_view(self.owner,'catalog',{})['items'][0]['id'],'bad')
        current=self.store.get(self.owner,'bad');current.update(claim='Corrected migration check',rationale='New evidence fixes missing condition')
        self.store.capture(self.owner,{'key':'fix','base_revision':1,'record':current})
        self.assertFalse(self.brief('migration')['items'][0]['outcome']['held_for_review'])

    def test_unused_rare_lesson_is_reviewed_not_removed(self):
        self.capture('rare','Rare recovery safeguard',terms=['recovery'],evidence='observed',state='supported')
        with patch('constellation_learning.time.time',return_value=__import__('time').time()+91*86400):
            review=self.store.learning_view(self.owner,'review',{})
            self.assertIn('No reported application',' '.join(review['items'][0]['warnings']))
            self.assertEqual(len(self.brief('recovery')['items']),1)
        self.assertEqual(self.store.get(self.owner,'rare')['state'],'supported')

    def test_historical_source_requires_opt_in_and_preserves_exact_evidence(self):
        self.capture('method','Earlier claim');old=self.store.get(self.owner,'method')
        source_id=old['sources'][0]['id'];old_excerpt=self.store.source(self.owner,[{'id':'method','revision':1,'source_id':source_id}])['sources'][0]['excerpt']
        new=dict(old,claim='Corrected claim');new['sources'][0]['excerpt']='Changed source'
        self.store.capture(self.owner,{'key':'new','base_revision':1,'record':new})
        ref={'id':'method','revision':1,'source_id':source_id}
        with self.assertRaises(Conflict):self.store.source(self.peer,[ref])
        self.assertEqual(self.store.source(self.peer,[dict(ref,historical=True)])['sources'][0]['excerpt'],old_excerpt)

if __name__=='__main__':unittest.main()
