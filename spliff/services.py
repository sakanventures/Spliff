"""Per-user macOS launch agents for the dashboard and the existing MCP tunnel."""
import argparse
import getpass
import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import signal
import time
from pathlib import Path
from urllib.request import urlopen

PREFIX = 'com.sakanventures.spliff'
LABELS = (PREFIX + '.dashboard', PREFIX + '.tunnel')
SERVICE_PATH = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'


def configuration(project, db, home=None, port=8765):
    project = Path(project).resolve()
    home = Path(home or Path.home())
    db = Path(db).resolve()
    support = home / 'Library/Application Support/Spliff/Services'
    python = project / '.venv/bin/python'
    tunnel = project / '.venv/bin/tunnel-client'
    profile = project / '.venv/spliff-tunnel/spliff.yaml'
    mcp = shlex.join([str(python), '-m', 'spliff', '--db', str(db), 'mcp',
                      '--allow-downloads', '--allow-discogs', '--dashboard-port', str(port)])
    if ',' in mcp:
        raise ValueError('The tunnel command cannot use project or catalog paths containing commas.')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Choose a dashboard port from 1024 to 65535.')
    commands = [
        [str(python), '-m', 'spliff', '--db', str(db), 'serve', '--port', str(port)],
        [str(tunnel), 'run', '--profile-file', str(support / 'tunnel.yaml'),
         '--health.listen-addr', '127.0.0.1:0',
         '--health.url-file', str(support / 'health.url'),
         '--pid.file', str(support / 'tunnel.pid')],
    ]
    agents = []
    for label, command in zip(LABELS, commands):
        agents.append({'Label': label, 'ProgramArguments': command,
            'WorkingDirectory': str(project), 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 30, 'LimitLoadToSessionType': 'Aqua',
            'ProcessType': 'Interactive', 'Umask': 0o077,
            'EnvironmentVariables': {'PATH': SERVICE_PATH, 'PYTHONPATH': str(project),
                                     'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1'},
            'StandardOutPath': str(support / (label.rsplit('.', 1)[1] + '.log')),
            'StandardErrorPath': str(support / (label.rsplit('.', 1)[1] + '.error.log'))})
    return {'project': project, 'db': db, 'support': support, 'python': python,
            'tunnel': tunnel, 'profile': profile, 'port': port,
            'agents_dir': home / 'Library/LaunchAgents', 'agents': agents, 'mcp': mcp}


def render_profile(config, key_file=None):
    # Preserve the existing tunnel ID and other settings. The installed client
    # resolves YAML credential references before applying CLI overrides, so it
    # needs a service profile referencing the saved key, not the Terminal env var.
    source = config['profile'].read_text()
    source, keys = re.subn(r'(?m)^(\s+api_key:)\s*[^\n]*$',
        lambda match: match[1] + ' ' + json.dumps('file:' + str(key_file or config['support'] / 'runtime-key')),
        source)
    source, commands = re.subn(r'(?m)^(\s+command:)\s*[^\n]*$',
        lambda match: match[1] + ' ' + json.dumps(config['mcp']), source)
    if keys != 1 or commands != 1:
        raise ValueError('The existing tunnel profile needs review: expected one API key reference and one MCP command.')
    return source.encode()


def write_private(path, data):
    """Do not follow a pre-existing symlink when writing a credential or agent."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        info = os.fstat(output.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('Refusing to overwrite a file not owned by this user.')
        os.fchmod(output.fileno(), 0o600)
        os.ftruncate(output.fileno(), 0)
        output.write(data)


def validate_files(config):
    for name in ('python', 'tunnel', 'profile', 'db'):
        if not config[name].is_file():
            raise ValueError('Required file unavailable: ' + str(config[name]))
    if not shutil.which('ffmpeg', path=SERVICE_PATH) or not shutil.which('ffprobe', path=SERVICE_PATH):
        raise ValueError('Install FFmpeg before starting the background services.')


def launchctl(*arguments, check=True):
    result = subprocess.run(['/bin/launchctl', *arguments], capture_output=True, text=True, timeout=15)
    if check and result.returncode:
        raise ValueError('launchctl ' + arguments[0] + ' failed. Check the service status and log files.')
    return result


def domain():
    return 'gui/' + str(os.getuid())


def manual_tunnels(config):
    """Detect only this checkout's manually started Spliff tunnel, without printing its command."""
    result = subprocess.run(['/bin/ps', '-u', str(os.getuid()), '-o', 'pid=,command='],
                            capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise ValueError('Cannot check for an existing manual tunnel. Run the installer from your Mac.')
    found = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        try:
            executable_name, separator, remainder = fields[1].partition(' run ')
            if not separator:
                continue
            executable = Path(executable_name)
            if not executable.is_absolute():
                executable = config['project'] / executable
            if executable.resolve() != config['tunnel'].resolve():
                continue
            args = shlex.split(remainder)
            # Managed agents use --profile-file; the old manual launcher uses --profile spliff.
            if '--profile' in args and args[args.index('--profile') + 1] == 'spliff':
                found.append(int(fields[0]))
            elif '--profile-file' in args and Path(args[args.index('--profile-file') + 1]).resolve() == config['profile'].resolve():
                found.append(int(fields[0]))
        except (ValueError, IndexError, OSError):
            continue
    return found


def validate_key_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('The runtime-key must be a regular, owner-only file.')
    if not path.read_text().strip():
        raise ValueError('The saved tunnel runtime key is empty.')


def prepare(config, target):
    """Produce reviewable plists without touching login services or credentials."""
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for agent in config['agents']:
        write_private(target / (agent['Label'] + '.plist'), plistlib.dumps(agent))
    write_private(target / 'tunnel.yaml', render_profile(config))
    return {'prepared': str(target), 'catalog': str(config['db']),
            'dashboard': 'http://127.0.0.1:' + str(config['port'])}


def install(config, no_input=False, replace_key=False, takeover=False):
    validate_files(config)
    existing_tunnels = manual_tunnels(config)
    if existing_tunnels and not takeover:
        raise ValueError('The existing Spliff tunnel is still running in Terminal. Stop it with Control+C, then run the installer again.')
    # Check the OS install destination before requesting a credential.
    config['agents_dir'].mkdir(parents=True, exist_ok=True)
    probe = config['agents_dir'] / (PREFIX + '.install-check')
    write_private(probe, b'')
    probe.unlink()
    config['support'].mkdir(mode=0o700, parents=True, exist_ok=True)
    if config['support'].is_symlink():
        raise ValueError('The service configuration directory cannot be a symbolic link.')
    key_file = config['support'] / 'runtime-key'
    if (key_file.exists() or key_file.is_symlink()) and not replace_key:
        validate_key_file(key_file)
    else:
        key = os.environ.get('CONTROL_PLANE_API_KEY', '').strip()
        if not key:
            if no_input or not sys.stdin.isatty():
                raise ValueError('Run the installer interactively once to save the tunnel runtime key. Do not paste it into chat.')
            key = getpass.getpass('Tunnel runtime key (hidden, saved locally): ').strip()
        if not key or '\n' in key or '\r' in key:
            raise ValueError('Enter a nonempty, single-line tunnel runtime key.')
        write_private(key_file, key.encode())
        key = None
    write_private(config['support'] / 'tunnel.yaml', render_profile(config))
    doctor = [str(config['tunnel']), 'doctor', '--profile-file', str(config['support'] / 'tunnel.yaml')]
    checked = subprocess.run(doctor, capture_output=True, text=True, timeout=30,
                             cwd=config['project'], env={**os.environ, 'PATH': SERVICE_PATH})
    if checked.returncode:
        raise ValueError('Tunnel configuration validation failed. Services were not restarted. Run tunnel-client doctor locally to inspect it.')
    # Never take over an unrelated process already listening on the dashboard port.
    current = launchctl('print', domain() + '/' + LABELS[0], check=False)
    if current.returncode:
        import socket
        with socket.socket() as listener:
            try:
                listener.bind(('127.0.0.1', config['port']))
            except OSError as exc:
                raise ValueError('The dashboard port is already occupied. Stop the old dashboard or choose another --port.') from exc
    previous = []
    for agent in config['agents']:
        path = config['agents_dir'] / (agent['Label'] + '.plist')
        old = None
        if path.exists():
            existing = plistlib.loads(path.read_bytes())
            if existing.get('Label') != agent['Label'] or existing.get('WorkingDirectory') != str(config['project']):
                raise ValueError('An existing service uses a different project. Review it before replacing it.')
            backup = path.with_suffix('.plist.previous')
            old = path.read_bytes()
            write_private(backup, old)
        loaded = launchctl('print', domain() + '/' + agent['Label'], check=False).returncode == 0
        previous.append((agent, path, old, loaded))
    if existing_tunnels:
        # Complete all configuration/credential checks before disconnecting the old tunnel.
        for pid in set(existing_tunnels) & set(manual_tunnels(config)):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while manual_tunnels(config):
            if time.monotonic() >= deadline:
                raise ValueError('The old tunnel has not stopped yet. Try connecting again shortly.')
            time.sleep(0.2)
    installed = []
    try:
        for agent, path, _, _ in previous:
            launchctl('bootout', domain() + '/' + agent['Label'], check=False)
            write_private(path, plistlib.dumps(agent))
            launchctl('enable', domain() + '/' + agent['Label'])
            launchctl('bootstrap', domain(), str(path))
            installed.append(agent['Label'])
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        restored = True
        for agent, path, old, loaded in reversed(previous):
            try:
                launchctl('bootout', domain() + '/' + agent['Label'], check=False)
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    write_private(path, old)
                    if loaded:
                        launchctl('bootstrap', domain(), str(path))
            except (ValueError, OSError, subprocess.SubprocessError):
                restored = False
        raise ValueError('Service installation failed. ' + ('Previous agent files were restored.' if restored else
                         'Some prior services could not be restored; inspect services status.')) from exc
    write_private(config['support'] / 'settings.json', json.dumps({
        'project': str(config['project']), 'db': str(config['db']), 'port': config['port']}).encode())
    return {'installed': installed, 'catalog': str(config['db']),
            'dashboard': 'http://127.0.0.1:' + str(config['port']),
            'logs': str(config['support']), 'status': 'started; verify readiness with services status'}


def status(config):
    services = []
    for label in LABELS:
        result = launchctl('print', domain() + '/' + label, check=False)
        detail = {}
        for line in result.stdout.splitlines():
            # Only read the job's direct properties. Nested resource coalitions
            # also contain `state = active`, which is not the process state.
            match = re.fullmatch(r'\t(state|pid|last exit code) = (.*)', line)
            if match:
                detail[match[1]] = match[2]
        services.append({'label': label, 'loaded': result.returncode == 0, **detail})
    ready = {'dashboard': False, 'tunnel': False}
    try:
        with urlopen('http://127.0.0.1:' + str(config['port']) + '/api/summary', timeout=3) as response:
            ready['dashboard'] = response.status == 200 and services[0].get('state') == 'running'
    except OSError:
        pass
    health = config['support'] / 'health.url'
    if health.is_file():
        result = subprocess.run([str(config['tunnel']), 'health', '--url-file', str(health),
                                 '--pid-file', str(config['support'] / 'tunnel.pid'),
                                 '--require-control-plane-poll', '--json'],
                                capture_output=True, text=True, timeout=10)
        ready['tunnel'] = result.returncode == 0 and services[1].get('state') == 'running'
    return {'services': services, 'ready': ready, 'logs': str(config['support'])}


def main(db, arguments):
    parser = argparse.ArgumentParser(prog='spliff services')
    parser.add_argument('action', choices=('prepare', 'install', 'status', 'restart', 'stop', 'uninstall'))
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--output', type=Path, default=Path('background-services'))
    parser.add_argument('--no-input', action='store_true')
    parser.add_argument('--takeover', action='store_true', help='Gracefully stop the verified manual Spliff tunnel')
    parser.add_argument('--replace-key', action='store_true', help='Save a replacement tunnel runtime key')
    args = parser.parse_args(arguments)
    if sys.platform != 'darwin':
        raise ValueError('Background Mac services require macOS.')
    config = configuration(Path(__file__).resolve().parent.parent, db, port=args.port)
    if args.action not in ('prepare', 'install') and (config['support'] / 'settings.json').is_file():
        settings = json.loads((config['support'] / 'settings.json').read_text())
        config = configuration(settings['project'], settings['db'], port=settings['port'])
    if args.action == 'prepare':
        return prepare(config, args.output)
    if args.action == 'install':
        return install(config, args.no_input, args.replace_key, args.takeover)
    if args.action == 'status':
        return status(config)
    if args.action == 'restart':
        for label in LABELS:
            launchctl('kickstart', '-k', domain() + '/' + label)
        return status(config)
    for label in reversed(LABELS):
        launchctl('bootout', domain() + '/' + label, check=False)
        if args.action == 'uninstall':
            (config['agents_dir'] / (label + '.plist')).unlink(missing_ok=True)
    return {'status': 'stopped' if args.action == 'stop' else 'uninstalled',
            'preserved': 'Music, catalog, runtime key, and logs were retained.'}
