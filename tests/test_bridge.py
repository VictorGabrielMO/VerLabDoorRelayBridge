import json
from pathlib import Path
import subprocess
import sys
import threading
from multiprocessing.connection import Listener

import httpx
import pytest

from bridge import Bridge, Handler, Rejected, Server

TOKEN = 'a' * 64

@pytest.fixture
def bridge(tmp_path):
    (tmp_path / 'token').write_text(TOKEN)
    calls = []
    config = dict(token_file=str(tmp_path / 'token'),
                  door_id='door', dry_run=False, allowed_ips=['127.0.0.1'])
    now = [1000.]
    service = Bridge(config, dispatch=lambda: calls.append(True), clock=lambda: now[0])
    service.calls, service.now = calls, now
    return service

def payload(request_id='request'):
    return dict(request_id=request_id, door_id='door', expires_at=1010.)

def test_auth_expiry_door_and_replay(bridge):
    for auth, body, code in [
        ('Bearer bad', payload(), 401),
        ('Bearer ' + TOKEN, {**payload(), 'door_id': 'other'}, 403),
        ('Bearer ' + TOKEN, {**payload(), 'expires_at': 999}, 422),
        ('Bearer ' + TOKEN, {**payload(), 'expires_at': float('nan')}, 422),
        ('Bearer ' + TOKEN, {**payload(), 'expires_at': 2000}, 422),
    ]:
        with pytest.raises(Rejected) as exc:
            bridge.unlock(auth, body)
        assert exc.value.status == code
    assert not bridge.calls
    assert bridge.unlock('Bearer '+TOKEN, payload())['status'] == 'command_dispatched'
    with pytest.raises(Rejected) as exc:
        bridge.unlock('Bearer '+TOKEN, payload())
    assert exc.value.code == 'duplicate_request'
    with pytest.raises(Rejected) as exc:
        bridge.unlock('Bearer '+TOKEN, payload('new'))
    assert exc.value.code == 'cooldown'
    assert len(bridge.calls) == 1
    # A new process intentionally forgets BOTH the request ID and cooldown.
    restarted = Bridge(bridge.config, dispatch=lambda: bridge.calls.append(True), clock=lambda: 1001.)
    assert restarted.unlock('Bearer '+TOKEN, payload())['status'] == 'command_dispatched'
    assert len(bridge.calls) == 2

def test_dry_run_and_failed_dispatch_consumed(bridge):
    bridge.dry_run = True
    assert bridge.unlock('Bearer '+TOKEN, payload())['status'] == 'simulated'
    assert not bridge.calls
    bridge.now[0] += 6
    bridge.dry_run = False
    def fail():
        raise TimeoutError()
    bridge.dispatch = fail
    with pytest.raises(Rejected) as exc:
        bridge.unlock('Bearer '+TOKEN, payload('second'))
    assert exc.value.code == 'relay_outcome_unknown'
    with pytest.raises(Rejected) as exc:
        bridge.unlock('Bearer '+TOKEN, payload('second'))
    assert exc.value.code == 'duplicate_request'

def test_helper_uses_existing_local_authenticated_protocol(tmp_path):
    listener = Listener(('localhost', 0), authkey=b'local-secret')
    config = tmp_path / 'relay.json'
    config.write_text(json.dumps(dict(relay_port=listener.address[1], process_passwd='local-secret')))
    messages=[]
    def receive():
        with listener.accept() as connection:
            messages.append(connection.recv())
        listener.close()
    thread=threading.Thread(target=receive,daemon=True); thread.start()
    subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1] / 'relay_send.py'),str(config)],check=True,timeout=5)
    thread.join(5)
    assert messages == ['open_door']

def test_real_http_bridge(bridge):
    server=Server(('127.0.0.1',0),Handler);server.bridge=bridge
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url='http://127.0.0.1:'+str(server.server_port)
    try:
        with httpx.Client(base_url=url,trust_env=False) as client:
            assert client.post('/api/unlock',json=payload()).status_code==401
            response=client.post('/api/unlock',json=payload(),headers={'Authorization':'Bearer '+TOKEN})
            assert response.status_code==200 and not response.json()['door_open_confirmed']
            assert client.post('/api/unlock',json=payload(),headers={'Authorization':'Bearer '+TOKEN}).status_code==409
            import ipaddress
            bridge.allowed_ips={ipaddress.ip_address('192.0.2.10')}
            response=client.post('/api/unlock',json=payload('other'),headers={
                'Authorization':'Bearer '+TOKEN,'X-Forwarded-For':'192.0.2.10','X-Real-IP':'192.0.2.10'})
            assert response.status_code==403 and response.json()['error']=='ip_not_allowed'
        assert len(bridge.calls)==1
    finally:
        server.shutdown();server.server_close();thread.join(5)

@pytest.mark.parametrize('allowed',[None,[],['*'],['192.0.2.0/24'],['invalid'], '127.0.0.1', [1]])
def test_invalid_allowlist_fails_startup(bridge,allowed):
    with pytest.raises(ValueError):
        Bridge({**bridge.config,'allowed_ips':allowed})

def test_cooldown_expiry_and_request_pruning(bridge):
    bridge.unlock('Bearer '+TOKEN, payload())
    bridge.now[0] = 1006.
    bridge.unlock('Bearer '+TOKEN, payload('second'))
    assert len(bridge.calls) == 2
    with pytest.raises(Rejected) as exc:
        bridge.unlock('Bearer '+TOKEN, payload())
    assert exc.value.code == 'duplicate_request'
    bridge.now[0] = 1307.
    bridge.unlock('Bearer '+TOKEN, {**payload('third'), 'expires_at': 1317.})
    assert set(bridge.requests) == {'third'}
