import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from ux46_devices import DeviceStore, Conflict


class Devices(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=DeviceStore(Path(self.temp.name)/'devices.sqlite3')

    def check(self,browser='browser-a',window='window-a',desktop='writing',**extra):
        return self.store.check_in(dict(browser=browser,window=window,desktop=desktop,platform='mac',browser_kind='chrome',**extra))

    def test_remembered_browser_and_smart_names(self):
        a=self.check();b=self.check('browser-b')
        self.assertEqual([r['name'] for r in b['devices']],['Mac','Mac 2'])
        self.assertEqual(a['current_device'],self.check()['current_device'])
        current=a['devices'][0]
        self.store.rename(dict(browser='browser-a',device=current['id'],revision=1,name='MacBook Pro'))
        self.assertEqual(next(r for r in self.check()['devices'] if r['id']==current['id'])['name'],'MacBook Pro')

    def test_associate_browsers_preserves_two_window_desktops(self):
        a=self.check();b=self.check('browser-b','window-b','research')
        result=self.store.associate(dict(browser='browser-b',device=a['current_device'],previous=b['current_device']))
        self.assertEqual(len(result['devices']),1)
        self.assertEqual({r['id'] for r in result['devices'][0]['desktops']},{'writing','research'})
        self.assertEqual(len(result['devices'][0]['browsers']),2)
        self.assertEqual(self.check('browser-b')['current_device'],a['current_device'])

    def test_same_browser_multiple_windows_and_stale_presence(self):
        with patch('ux46_devices.time.time',return_value=1000):
            self.check();self.check(window='second-window',desktop='research')
        with patch('ux46_devices.time.time',return_value=1101):
            result=self.store.view('browser-a');self.assertFalse(result['devices'][0]['online'])
        with patch('ux46_devices.time.time',return_value=1102):
            result=self.check(local=True)
            self.assertTrue(result['devices'][0]['online'])
            self.assertEqual(result['devices'][0]['desktops'],[{'id':'writing','local':True}])

    def test_rename_and_association_reject_stale_edits(self):
        a=self.check();b=self.check('browser-b');id=a['current_device']
        self.store.rename(dict(browser='browser-a',device=id,revision=1,name='My Mac'))
        with self.assertRaises(Conflict):self.store.rename(dict(browser='browser-a',device=id,revision=1,name='Old edit'))
        self.store.associate(dict(browser='browser-a',device=b['current_device'],previous=id))
        with self.assertRaises(Conflict):self.store.associate(dict(browser='browser-a',device=b['current_device'],previous=id))

    def test_invalid_input_and_failed_association_preserve_records(self):
        a=self.check();before=self.store.view('browser-a')
        with self.assertRaises(ValueError):self.store.associate(dict(browser='browser-a',device='absent',previous=a['current_device']))
        with self.assertRaises(ValueError):self.store.rename(dict(browser='browser-a',device=a['current_device'],revision=1,name=42))
        with self.assertRaises(ValueError):self.check(browser='../escape')
        self.assertEqual(self.store.view('browser-a')['devices'],before['devices'])

    def test_presence_is_bounded_and_survives_reopen(self):
        for i in range(270):self.check(window='window-'+str(i))
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM windows').fetchone()[0],256)
        self.assertEqual(len(DeviceStore(self.store.path).view('browser-a')['devices']),1)

if __name__=='__main__':unittest.main()
