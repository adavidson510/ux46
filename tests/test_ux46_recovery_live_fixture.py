"""Real process lifecycle, only for a temporary no-provider UX46 installation."""
import json
import http.client
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from ux46_local import initialize
from ux46_recovery import ServiceManager, installation, process_identity, Client, Coordinator


class OwnProcessTests(unittest.TestCase):
    def test_dead_console_recovers_outside_service_without_touching_other_installation(self):
        self.exercise(dead=True)

    def test_ui_launched_coordinator_survives_stopping_its_requesting_console(self):
        self.exercise(dead=False)

    def exercise(self, dead):
        with tempfile.TemporaryDirectory(prefix='ux46-recovery-fixture-') as temporary:
            root=Path(temporary); config=initialize(root);config['agent']='none'
            (root/'config.json').write_text(json.dumps(config))
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            env=dict(os.environ,UX46_HOME=str(root))
            owned=subprocess.Popen([sys.executable,str(ROOT/'tools/ux46'),'run','--port',str(port)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
            def ready():
                for _ in range(100):
                    try:
                        with urlopen(f'http://127.0.0.1:{port}/api/bootstrap',timeout=.3) as response:return json.load(response)
                    except OSError:time.sleep(.05)
                self.fail('Temporary console did not start')
            try:
                self.assertFalse(ready()['runtime_started'])
                old_identity=process_identity(owned.pid)
                if dead:
                    # Stop only the fixture process that this test launched.
                    owned.send_signal(signal.SIGINT);owned.communicate(timeout=8)
                    self.assertIsNone(process_identity(owned.pid))
                    result=subprocess.run([sys.executable,str(ROOT/'tools/ux46'),'--doctor','--recover-all','--request-id','fixture_recovery_123','--json'],env=env,capture_output=True,text=True,timeout=30)
                    self.assertEqual(result.returncode,0,result.stderr+result.stdout)
                    receipt=json.loads(result.stdout)
                else:
                    client = Client({'port':port},time.monotonic()+5)
                    try:
                        status, answer = client.request('POST','/api/recovery/start',{'mode':'all','request_id':'fixture_recovery_123'})
                        self.assertEqual(status,202)
                    except (OSError, http.client.HTTPException, json.JSONDecodeError):
                        # Stopping the requesting service may lose its HTTP
                        # response, including an empty or truncated JSON body.
                        # Read the fixed receipt; never resend POST.
                        pass
                    for _ in range(150):
                        receipt = Coordinator(root).receipt('fixture_recovery_123')
                        if receipt and receipt['state'] in {'complete','partial','failed'}: break
                        time.sleep(.05)
                    owned.communicate(timeout=8)
                self.assertEqual(receipt['state'],'complete',receipt)
                self.assertFalse(ready()['runtime_started'])
                record=installation(root)['services'][0]
                self.assertNotEqual(record['identity'],old_identity)
                self.assertIsNone(unrelated.poll())
                again=subprocess.run([sys.executable,str(ROOT/'tools/ux46'),'doctor','--recover-all','--request-id','fixture_recovery_123','--json'],env=env,capture_output=True,text=True,timeout=5)
                self.assertEqual(json.loads(again.stdout),receipt)
                self.assertEqual(installation(root)['services'][0]['identity'],record['identity'])
                self.assertEqual(json.loads((root/'registry.json').read_text())['projects'],[])
            finally:
                if owned.poll() is None:owned.send_signal(signal.SIGINT);owned.communicate(timeout=8)
                if (root/'control/console.json').exists():
                    manager=ServiceManager(root,time.monotonic()+8)
                    for service in installation(root)['services']:manager.stop(service)
                unrelated.terminate();unrelated.wait(timeout=5)


if __name__=='__main__':unittest.main()
