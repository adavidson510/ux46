import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from ux46_assistant_reads import read

class Reads(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  (self.root/'email-assistant.json').write_text(json.dumps({'accounts':{'one':{'email':'one@example.test','token_file':'private'},'two':{'email':'two@example.test','token_file':'private'}}}))
 def tearDown(self):self.tmp.cleanup()
 def test_calendar_recurring_dates_declines_paging_and_partial_accounts(self):
  p=Mock();calls=[]
  def calendar(route,**kw):
   calls.append((route,kw))
   if route=='users/me/calendarList':return {'items':[{'id':'one@example.test','summary':'Work'}]}
   event={'id':'meeting','summary':'Meeting','start':{'dateTime':'2026-11-01T10:00:00-08:00'},'end':{'dateTime':'2026-11-01T11:00:00-08:00'}}
   if not kw.get('pageToken'):return {'items':[{**event,'status':'cancelled'},{**event,'attendees':[{'self':True,'responseStatus':'declined'}]},event], 'nextPageToken':'next'}
   return {'items':[{'id':'all-day','start':{'date':'2026-11-01'},'end':{'date':'2026-11-02'}}]}
  p.calendar.side_effect=calendar
  def factory(file,email):
   if email.startswith('two'):raise RuntimeError('SECRET PROVIDER BODY')
   return p
  r=read(self.root,'calendar',start='2026-11-01',days=1,factory=factory)
  self.assertFalse(r['complete']);self.assertNotIn('SECRET',json.dumps(r))
  events=r['accounts'][0]['events'];self.assertEqual([e['id'] for e in events],['all-day','meeting'])
  self.assertEqual(events[0]['end'],{'date':'2026-11-02'})
  self.assertEqual(calls[1][0],'calendars/one%40example.test/events')
  self.assertEqual(calls[1][1]['singleEvents'],'true');self.assertEqual(calls[2][1]['pageToken'],'next')
  self.assertTrue(r['accounts'][0]['complete']);self.assertIn('error',r['accounts'][1])
 def test_mail_read_does_not_modify_and_reports_truncation(self):
  p=Mock()
  p.get.side_effect=[{'messages':[{'id':'m'}],'nextPageToken':'more'},{'id':'m','threadId':'t','labelIds':['UNREAD'],'payload':{'headers':[{'name':'Subject','value':'Example'}]}}]
  r=read(self.root,'email',account='one',factory=lambda *a:p)
  self.assertFalse(r['complete']);self.assertEqual(r['accounts'][0]['messages'][0]['subject'],'Example')
  self.assertEqual([c[0] for c in p.mock_calls],['get','get'])
 def test_config_is_not_verified_and_invalid_account_never_calls_provider(self):
  factory=Mock()
  self.assertEqual(len(read(self.root,'connections')['accounts']),2)
  with self.assertRaises(ValueError):read(self.root,'calendar',account='other',factory=factory)
  factory.assert_not_called()
 def test_no_connections_and_range_limit(self):
  (self.root/'email-assistant.json').unlink()
  self.assertFalse(read(self.root,'calendar')['complete'])
  with self.assertRaises(ValueError):read(self.root,'calendar',days=32)

if __name__=='__main__':unittest.main()
