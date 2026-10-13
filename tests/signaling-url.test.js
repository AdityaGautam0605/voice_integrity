import test from 'node:test';
import assert from 'node:assert/strict';
import { signalingUrl } from '../src/services/signaling-url.js';
const path = '/v1/calls/test-room/signal';
test('production pairing goes directly to the token-authenticated backend', () => {
  assert.equal(signalingUrl(path, 'https://frontend.example/call', 'https://backend.example'), 'wss://backend.example/v1/calls/test-room/signal');
});
test('local and same-origin pairing retain the API proxy', () => {
  assert.equal(signalingUrl(path, 'http://localhost:5173/call'), 'ws://localhost:5173/api/v1/calls/test-room/signal');
  assert.equal(signalingUrl(path, 'https://frontend.example/call'), 'wss://frontend.example/api/v1/calls/test-room/signal');
});
test('direct pairing cannot route analysis or accept insecure or credential-bearing origins', () => {
  assert.throws(() => signalingUrl('/v1/stream/session', 'https://frontend.example', 'https://backend.example'));
  for (const origin of ['http://backend.example', 'https://user:secret@backend.example', 'https://backend.example/api', 'https://backend.example?token=secret']) {
    assert.throws(() => signalingUrl(path, 'https://frontend.example', origin));
  }
});
