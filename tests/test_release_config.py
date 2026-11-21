import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ReleaseConfigTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows paths are required')
    def test_configured_installation_and_workspace_are_used(self):
        with tempfile.TemporaryDirectory(prefix='sscnc-config-') as folder:
            installation = Path(folder, 'Simulator')
            workspace = Path(folder, 'Workpieces')
            workspace.mkdir()
            env = dict(os.environ, SSCNC_INSTALL_DIR=str(installation), SSCNC_WORKSPACE=str(workspace))
            probe = '''
import json
from pathlib import Path
from constants import TARGET_PATH, WORKSPACE
from driver import Driver
from simulator_launcher import LAUNCHER_PATH
output = Driver._output_path(str(Path(WORKSPACE, 'new.wp')))
print(json.dumps([TARGET_PATH, LAUNCHER_PATH, str(output)]))
'''
            result = subprocess.run([sys.executable, '-B', '-c', probe], env=env, cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [
                str(installation / 'FANUC' / 'FANUC0iMF' / 'sscnc.exe'),
                str(installation / 'CNCLogin.exe'),
                str(workspace / 'new.wp'),
            ])
