"""Fresh workspaces only read linked/new Claude transcripts; metadata survives restart."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import atlas_claude as claude


class FreshClaudeTests(unittest.TestCase):
    def test_old_transcripts_are_not_read_and_reserved_sessions_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);transcripts=root/'projects';transcripts.mkdir()
            folder=transcripts/'old';folder.mkdir()
            (folder/'old-session.jsonl').write_text('private old conversation')
            registry=root/'registry.json';registry.write_text('{"projects":[]}')
            args=claude.build_parser().parse_args(['--state-dir',str(root),'--registry',str(registry),
                '--projects-dir',str(transcripts),'--fresh-only'])
            self.assertEqual(args.permission_mode,'default')
            catalog=claude.Catalog(args)
            with patch.object(claude,'scan_session',side_effect=AssertionError('Read an old transcript')):
                self.assertEqual(catalog.rooms(),[])
            room=claude.Room('unfiled','Unfiled','','new-session','new-session','local',True,None,
                             str(root),'New work',1,'',False)
            catalog.reserve(room)
            recovered=claude.Catalog(args)
            self.assertEqual(recovered.rooms()[0].native_id,'new-session')
            self.assertEqual((root/'fresh-sessions.json').stat().st_mode & 0o777,0o600)

if __name__=='__main__':unittest.main()
