"""An HTTPS media fixture with explicit test CA trust, never a TLS bypass.

This context is test infrastructure. It does not modify the image, host trust
store, camera secrets, production certificate policy, or any model setting.
"""
from contextlib import contextmanager
import base64
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import time
from urllib.request import urlopen


@contextmanager
def trusted_https_fixture(root, output, *, port=18882):
    root, output = Path(root), Path(output)
    output.mkdir(exist_ok=False)
    private = Path(tempfile.mkdtemp(prefix='https-private-', dir=root))
    server = None
    try:
        cert, key = private/'source.crt', private/'source.key'
        command = ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                   '-keyout', str(key), '-out', str(cert), '-days', '1',
                   '-subj', '/CN=localhost', '-addext',
                   'subjectAltName=DNS:localhost,IP:127.0.0.1', '-addext',
                   'basicConstraints=critical,CA:TRUE']
        with (output/'certificate-creation.log').open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        key.chmod(0o600)
        shutil.copy2(cert, output/'fixture-public-ca.crt')
        encoded = base64.b64encode(cert.read_bytes()).decode('ascii')
        hook = private/'trusted_fixture_ca.py'
        hook.write_text(
            "from pathlib import Path\nimport base64\nfrom gi.repository import Gio\n"
            "p=Path('/tmp/sentry-https-test-ca.pem')\n"
            "with p.open('wb') as f: f.write(base64.b64decode("+repr(encoded)+"))\n"
            "backend=Gio.TlsBackend.get_default()\nassert backend.supports_tls()\n"
            "backend.set_default_database(Gio.TlsFileDatabase.new(str(p)))\n")
        shutil.copy2(hook, output/'executed-trust-fixture-hook.py')
        source = root/'repo/scripts/isolated_validation/http_replay_source.py'
        shutil.copy2(source, output/'executed-source-server.py')
        url = f'https://127.0.0.1:{port}/live.ts'
        command = ['taskset', '-c', '14,15', 'python3', str(source),
                   '--fixture', str(root/'fixtures/people1080p30.mp4'),
                   '--output', str(output/'receiver'), '--port', str(port),
                   '--seconds', '7200', '--certificate', str(cert), '--key', str(key)]
        (output/'command.json').write_text(json.dumps({
            'command': command, 'scope': __doc__,
            'tls_verification': 'Fixture CA explicitly trusted; no validation flags disabled',
        }, indent=2)+'\n')
        with (output/'source-controller.log').open('w') as log:
            server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True)
            deadline = time.monotonic()+15
            context = ssl.create_default_context(cafile=str(cert))
            while True:
                if server.poll() is not None:
                    raise RuntimeError('HTTPS fixture exited before preflight')
                try:
                    with urlopen(url, context=context, timeout=2) as response:
                        if response.status != 200 or not response.read(188):
                            raise RuntimeError('HTTPS fixture returned no video data')
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.1)
            (output/'preflight.json').write_text(json.dumps({
                'verified_upstream': True, 'url': url,
                'scope': 'Strict TLS plus nonempty MPEG-TS bytes; not a CV image pass',
            }, indent=2)+'\n')
            yield url, hook
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        shutil.rmtree(private)
