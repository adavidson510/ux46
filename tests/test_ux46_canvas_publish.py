import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'skills/ux46-canvas/scripts'))
from board import BoardStore, Conflict
from publish import publish


class LedgerPublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=BoardStore(Path(self.tmp.name)/'boards.db')
        self.board={'title':'Fixture','reporter':'Fixture agent','sections':[
            {'title':'Now','items':[{'label':'First slice','state':'doing'}]}]}
    def tearDown(self):self.tmp.cleanup()
    def test_changed_then_unchanged_preserves_revision_and_timestamp(self):
        self.assertEqual(publish(self.store,'fixture','test/ledger',0,self.board),{'changed':True,'version':1})
        first=self.store.read('fixture','test/ledger')
        self.assertEqual(publish(self.store,'fixture','test/ledger',1,self.board),{'changed':False,'version':1})
        self.assertEqual(first,self.store.read('fixture','test/ledger'))
        self.board['sections'][0]['items'][0]['state']='done'
        self.assertEqual(publish(self.store,'fixture','test/ledger',1,self.board),{'changed':True,'version':2})
    def test_stale_projection_preserves_human_edit(self):
        publish(self.store,'fixture','test/ledger',0,self.board)
        self.store.save('fixture','test/ledger',1,dict(self.board,title='Human title'))
        with self.assertRaises(Conflict):publish(self.store,'fixture','test/ledger',1,self.board)
        self.assertEqual(self.store.read('fixture','test/ledger')['board']['title'],'Human title')
    def test_invalid_projection_preserves_last_valid_board(self):
        publish(self.store,'fixture','test/ledger',0,self.board)
        with self.assertRaises(ValueError):publish(self.store,'fixture','test/ledger',1,{'title':'Bad'})
        self.assertEqual(self.store.read('fixture','test/ledger')['version'],1)

if __name__=='__main__':unittest.main()
