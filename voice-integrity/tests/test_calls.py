import time
from contextlib import asynccontextmanager

import numpy as np
import pytest
from fastapi.testclient import TestClient

from vif.serve import server
from vif.serve.calls import ROOM_TTL_S


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('VIF_API_TOKEN', raising=False)
    monkeypatch.setenv('VIF_VAD', 'energy')
    monkeypatch.setenv('VIF_DEMO_SCENARIOS', '1')
    server.bootstrap(backend='stub', keys_dir=tmp_path/'keys', data_dir=tmp_path/'data')
    app = server.create_app()

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await app.state.call_rooms.close()

    app.router.lifespan_context = lifespan
    with TestClient(app) as connection:
        yield connection
    for session in server.state.sessions.values():
        session.close()
    server.state.sessions.clear()
    server.state.verdicts.clear()
    server.state.audit.close()
    server.state.vault.close()


def join(ws, room, role):
    ws.send_json({'role': role, 'token': room['credentials'][role]})
    assert ws.receive_json()['type'] == 'joined'


def test_pair_relay_and_hangup(client):
    room = client.post('/v1/calls').json()
    path = f"/v1/calls/{room['call_id']}/signal"
    with client.websocket_connect(path) as operator:
        join(operator, room, 'operator')
        with client.websocket_connect(path) as caller:
            join(caller, room, 'caller')
            assert operator.receive_json()['type'] == caller.receive_json()['type'] == 'ready'
            operator.send_json({'type': 'offer', 'data': {'sdp': 'test'}})
            assert caller.receive_json() == {'type': 'offer', 'data': {'sdp': 'test'}}
            caller.send_json({'type': 'answer', 'data': {'sdp': 'answer'}})
            assert operator.receive_json()['type'] == 'answer'
            caller.send_json({'type': 'ice', 'data': {'candidate': 'local'}})
            assert operator.receive_json()['data']['candidate'] == 'local'
            caller.send_json({'type': 'hangup'})
            assert operator.receive_json()['type'] == 'hangup'
    assert not client.app.state.call_rooms.rooms


def test_room_auth_duplicate_and_cross_room_tokens(client):
    room = client.post('/v1/calls').json()
    other = client.post('/v1/calls').json()
    path = f"/v1/calls/{room['call_id']}/signal"
    with client.websocket_connect(path) as invalid:
        invalid.send_json({'role': 'operator', 'token': other['credentials']['operator']})
        assert invalid.receive_json()['type'] == 'error'
    with client.websocket_connect(path) as operator:
        join(operator, room, 'operator')
        with client.websocket_connect(path) as duplicate:
            duplicate.send_json({'role': 'operator', 'token': room['credentials']['operator']})
            assert 'already connected' in duplicate.receive_json()['detail']
        assert room['call_id'] in client.app.state.call_rooms.rooms


def test_expiry_and_create_auth(client, monkeypatch):
    room = client.post('/v1/calls').json()
    client.app.state.call_rooms.rooms[room['call_id']].created = time.monotonic() - ROOM_TTL_S - 1
    with client.websocket_connect(f"/v1/calls/{room['call_id']}/signal") as ws:
        ws.send_json({'role': 'caller', 'token': room['credentials']['caller']})
        assert ws.receive_json()['type'] == 'error'
    monkeypatch.setenv('VIF_API_TOKEN', 'secret')
    assert client.post('/v1/calls').status_code == 401


def test_scenarios_require_explicit_stub_mode(client, monkeypatch):
    assert client.post('/v1/session', json={'demo_scenario': 'INVALID'}).status_code == 422
    monkeypatch.setenv('VIF_DEMO_SCENARIOS', '0')
    assert client.post('/v1/session', json={'demo_scenario': 'RED'}).status_code == 403
    monkeypatch.setenv('VIF_DEMO_SCENARIOS', '1')
    monkeypatch.setattr(server.state, 'detector', object())
    assert client.post('/v1/session', json={'demo_scenario': 'RED'}).status_code == 403


@pytest.mark.parametrize(('band', 'action'), [('GREEN', 'PROCEED'), ('AMBER', 'CHALLENGE'), ('RED', 'GATE_ACTION')])
def test_scenario_scores_signs_once_and_audits(client, band, action):
    info = client.post('/v1/session', json={'demo_scenario': band}).json()
    session_id = info['session_id']
    path = f'/v1/stream/{session_id}'
    with client.websocket_connect(path) as ws:
        with client.websocket_connect(path) as duplicate:
            assert duplicate.receive_json()['type'] == 'error'
        assert session_id in server.state.sessions
        for _ in range(35):
            ws.send_bytes(np.zeros(512, dtype='<f4').tobytes())
        for _ in range(160):
            ws.send_bytes((.4*np.sin(np.arange(512)*.1)).astype('<f4').tobytes())
        ws.send_text('end')
        messages = []
        while True:
            message = ws.receive_json()
            messages.append(message)
            if message['type'] == 'verdict':
                break
    scores = [m for m in messages if m['type'] == 'score']
    assert scores and scores[0]['risk'] == band
    if band == 'AMBER':
        assert scores[0]['challenge']['phrase']
    verdict = messages[-1]
    assert verdict['payload']['action'] == action
    assert verdict['payload']['model_version'] == f'stub-rehearsal-{band.lower()}-1'
    assert client.post('/v1/verdict/verify', json=verdict).json()['valid']
    audit = client.get('/v1/audit').json()
    assert audit['chain_valid'] and audit['count'] == 1
    assert session_id not in server.state.sessions
