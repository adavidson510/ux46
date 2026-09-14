import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from atlas_files import FileStore
from ux46_upload import upload_file

class Upload(unittest.TestCase):
    def test_existing_store_retains_bytes_and_real_room_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=FileStore(root/'attachments')
            source=root/'icon.png';source.write_bytes(b'\x89PNG\r\n\x1a\n'+b'test')
            result=upload_file(root/'attachments','cp','weird-gallery/start-here',source)
            self.assertEqual(result['file_agent'],'cp')
            self.assertEqual(store.get(result['file_id'],room='weird-gallery/start-here')['sha256'],result['sha256'])
            self.assertTrue(result['preview_url'])
            self.assertEqual(source.read_bytes(),b'\x89PNG\r\n\x1a\n'+b'test')
    def test_missing_store_is_not_silently_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'file.txt';source.write_text('hello')
            with self.assertRaisesRegex(ValueError,'existing attachment store'):
                upload_file(root/'missing','cp','weird-gallery/start-here',source)
            self.assertFalse((root/'missing').exists())
