import sys
from pathlib import Path
from urllib.parse import urlencode
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from ux46_visualizations import Visualizations, MAX_BYTES
from test_ux46_access_gateway import GateCase

class VisualizationBoundary(GateCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = Path(cls.tmp.name)
        cls.output = root / 'output'; cls.output.mkdir()
        kit = root / 'kit'; kit.mkdir()
        (kit / 'visualize.css').write_text('body { color: black; }')
        (kit / 'visualize.html').write_text('<!--__INLINE_VISUALIZATION_FRAGMENT__-->')
        cls.visual = cls.output / 'example.html'
        cls.visual.write_text('<button id="try">Try</button><script>window.example=1</script>')
        cls.server.visualizations = Visualizations([cls.output], kit)

    def url(self, path):
        return '/api/visualizations/render?' + urlencode({'path':str(path)})

    def test_login_and_read_only_sandbox(self):
        url = self.url(self.visual)
        self.assertEqual(self.ask(path=url,password=None)[0],401)
        self.assertEqual(self.ask(path=url,identity=None)[0],403)
        status, headers, body = self.ask(path=url)
        self.assertEqual(status,200)
        self.assertIn(b'window.example=1',body)
        self.assertIn('sandbox allow-scripts;',headers['Content-Security-Policy'])
        self.assertNotIn('allow-same-origin',headers['Content-Security-Policy'])
        self.assertIn("connect-src 'none'",headers['Content-Security-Policy'])
        self.assertEqual(self.ask('HEAD',url)[2],b'')
        self.assertEqual(self.ask('POST',url,body='x')[0],404)

    def test_paths_symlinks_size_and_missing_files(self):
        root=Path(self.tmp.name)
        secret=root/'outside.html';secret.write_text('PRIVATE OUTSIDE')
        link=self.output/'link.html';link.symlink_to(secret)
        large=self.output/'large.html';large.write_bytes(b'x'*(MAX_BYTES+1))
        for path in [secret,link,large,self.output/'missing.html',self.output/'..'/'outside.html',self.output/'example.txt']:
            status,headers,body=self.ask(path=self.url(path))
            self.assertEqual(status,404,str(path))
            self.assertNotIn(b'PRIVATE OUTSIDE',body)
