import os
import plistlib
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from spliff import services


class BackgroundServices(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / 'A project with spaces'
        self.project.mkdir()
        self.db = self.base / 'catalog.sqlite'
        self.db.touch()
        self.config = services.configuration(self.project, self.db, self.base / 'home', port=19876)
        self.config['profile'].parent.mkdir(parents=True)
        self.config['profile'].write_text('''config_version: 1
control_plane:
  tunnel_id: "tunnel_preserved"
  api_key: "env:CONTROL_PLANE_API_KEY"
health:
  listen_addr: "127.0.0.1:0"
mcp:
  commands:
    - channel: main
      command: "old-command mcp --search-only"
''')
        self.config['tunnel'].parent.mkdir(parents=True, exist_ok=True)
        self.config['tunnel'].touch()
        self.config['python'].touch()

    def test_prepared_agents_share_catalog_and_require_no_terminal_or_embedded_key(self):
        output = self.base / 'prepared'
        services.prepare(self.config, output)
        dashboard, tunnel = [plistlib.loads((output / (label + '.plist')).read_bytes()) for label in services.LABELS]
        self.assertTrue(dashboard['RunAtLoad'] and tunnel['KeepAlive'])
        self.assertEqual(dashboard['WorkingDirectory'], str(self.project))
        self.assertEqual(dashboard['ProgramArguments'][dashboard['ProgramArguments'].index('--db') + 1], str(self.db))
        self.assertNotIn('CONTROL_PLANE_API_KEY', tunnel['EnvironmentVariables'])
        mcp = shlex.split(self.config['mcp'])
        self.assertEqual(mcp[mcp.index('--db') + 1], str(self.db))
        self.assertIn('--allow-downloads', mcp)
        self.assertNotIn('--search-only', mcp)
        profile = (output / 'tunnel.yaml').read_text()
        self.assertIn('tunnel_preserved', profile)
        self.assertNotIn('env:CONTROL_PLANE_API_KEY', profile)
        self.assertIn('file:', profile)
        self.assertNotIn('--search-only', profile)
        self.assertEqual((output / 'tunnel.yaml').stat().st_mode & 0o777, 0o600)

    def test_secret_file_rejects_symlinks_and_weak_permissions(self):
        key = self.base / 'key'
        services.write_private(key, b'local-secret-test')
        services.validate_key_file(key)
        link = self.base / 'linked-key'
        link.symlink_to(key)
        with self.assertRaises((OSError, ValueError)):
            services.write_private(link, b'overwrite')
        self.assertEqual(key.read_bytes(), b'local-secret-test')
        key.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'owner-only'):
            services.validate_key_file(key)

    def test_refuses_ambiguous_profile_before_replacing_mcp_commands(self):
        with self.config['profile'].open('a') as output:
            output.write('      command: "second-channel"\n')
        with self.assertRaisesRegex(ValueError, 'needs review'):
            services.render_profile(self.config)

    def test_partial_bootstrap_failure_restores_prior_agent_files(self):
        self.config['agents_dir'].mkdir(parents=True)
        old_agents = []
        for agent in self.config['agents']:
            old = dict(agent, ThrottleInterval=99)
            path = self.config['agents_dir'] / (agent['Label'] + '.plist')
            path.write_bytes(plistlib.dumps(old))
            old_agents.append((path, old))
        self.config['support'].mkdir(parents=True)
        services.write_private(self.config['support'] / 'runtime-key', b'local-secret-test')
        bootstraps = []
        def launch(*args, check=True):
            if args[0] == 'bootstrap':
                bootstraps.append(args)
                if len(bootstraps) == 2:
                    raise ValueError('simulated tunnel launch failure')
            return SimpleNamespace(returncode=0, stdout='')
        with patch.object(services, 'validate_files'), patch.object(services, 'manual_tunnels', return_value=[]), \
                patch.object(services.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
                patch.object(services, 'launchctl', side_effect=launch):
            with self.assertRaisesRegex(ValueError, 'restored'):
                services.install(self.config, no_input=True)
        for path, old in old_agents:
            self.assertEqual(plistlib.loads(path.read_bytes()), old)
        self.assertEqual(len(bootstraps), 4)
        self.assertFalse((self.config['support'] / 'settings.json').exists())

    def test_manual_tunnel_prevents_installing_duplicate(self):
        result = SimpleNamespace(returncode=0, stdout=f'123 {self.config["tunnel"]} run --profile spliff\n')
        with patch.object(services.subprocess, 'run', return_value=result):
            self.assertEqual(services.manual_tunnels(self.config), [123])
        with patch.object(services, 'validate_files'), patch.object(services, 'manual_tunnels', return_value=[123]):
            with self.assertRaisesRegex(ValueError, 'Control\\+C'):
                services.install(self.config, no_input=True)
        self.assertFalse(self.config['agents_dir'].exists())

    def test_takeover_never_stops_existing_tunnel_before_credentials_are_ready(self):
        with patch.object(services, 'validate_files'), \
                patch.object(services, 'manual_tunnels', return_value=[123]), \
                patch.dict(os.environ, {'CONTROL_PLANE_API_KEY': ''}), \
                patch.object(services.os, 'kill') as kill:
            with self.assertRaisesRegex(ValueError, 'interactively'):
                services.install(self.config, no_input=True, takeover=True)
        kill.assert_not_called()

    def test_nested_coalition_state_does_not_override_running_job(self):
        self.config['support'].mkdir(parents=True)
        (self.config['support'] / 'health.url').write_text('http://127.0.0.1:12345')
        output = 'job = {\n\tstate = running\n\tpid = 123\n\tresource coalition = {\n\t\tstate = active\n\t}\n}\n'
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with patch.object(services, 'launchctl', return_value=SimpleNamespace(returncode=0, stdout=output)), \
                patch.object(services, 'urlopen', return_value=response), \
                patch.object(services.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
            result = services.status(self.config)
        self.assertEqual(result['services'][0]['state'], 'running')
        self.assertEqual(result['ready'], {'dashboard': True, 'tunnel': True})

    def test_nested_running_state_cannot_make_waiting_job_ready(self):
        output = 'job = {\n\tstate = waiting\n\tcoalition = {\n\t\tstate = running\n\t}\n}\n'
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with patch.object(services, 'launchctl', return_value=SimpleNamespace(returncode=0, stdout=output)), \
                patch.object(services, 'urlopen', return_value=response):
            result = services.status(self.config)
        self.assertFalse(result['ready']['dashboard'])
