import test from 'node:test';
import assert from 'node:assert/strict';
import { summarizeConnectionStats, unknownConnection } from '../src/services/connection-stats.js';

function stats(...items) { return new Map(items.map((item) => [item.id, item])); }
function pair(extra = {}) {
  return { id: 'pair', type: 'candidate-pair', state: 'succeeded', localCandidateId: 'local', remoteCandidateId: 'remote', ...extra };
}
const local = { id: 'local', type: 'local-candidate', candidateType: 'host', protocol: 'udp' };
const remote = { id: 'remote', type: 'remote-candidate', candidateType: 'srflx', protocol: 'udp' };
const inbound = (received, lost, extra = {}) => ({ id: 'audio', type: 'inbound-rtp', kind: 'audio', packetsReceived: received, packetsLost: lost, ...extra });

test('the selected transport pair takes precedence over other nominated pairs', () => {
  const result = summarizeConnectionStats(stats(local, remote,
    pair({ id: 'old', nominated: true, localCandidateId: 'old-local' }),
    { ...local, id: 'old-local', candidateType: 'relay' },
    pair({ currentRoundTripTime: .042 }),
    { id: 'transport', type: 'transport', selectedCandidatePairId: 'pair' }));
  assert.deepEqual(result.connection, { route: 'Direct', transport: 'UDP', rttMs: 42, packetLossPercent: null });
});

test('remote relay candidates are classified as relayed and TLS is surfaced', () => {
  const result = summarizeConnectionStats(stats(local, { ...remote, candidateType: 'relay', relayProtocol: 'tls' }, pair({ nominated: true })));
  assert.equal(result.connection.route, 'Relayed');
  assert.equal(result.connection.transport, 'TLS');
});

test('missing candidate or latency statistics remain unknown', () => {
  assert.deepEqual(summarizeConnectionStats(stats()).connection, unknownConnection());
  assert.equal(summarizeConnectionStats(stats(local, pair({ selected: true, currentRoundTripTime: NaN }))).connection.route, 'Unknown');
  assert.equal(summarizeConnectionStats(stats(local, remote, pair({ selected: true, currentRoundTripTime: -1 }))).connection.rttMs, null);
});

test('audio packet loss uses interval deltas, ignores video and remote sender reports', () => {
  const first = summarizeConnectionStats(stats(inbound(1000, 10)));
  assert.equal(first.connection.packetLossPercent, null);
  const next = summarizeConnectionStats(stats(inbound(1095, 15),
    inbound(0, 5000, { id: 'video', kind: 'video' }),
    inbound(0, 5000, { id: 'sender', isRemote: true })), first.counters);
  assert.equal(next.connection.packetLossPercent, 5);
});

test('late packets never produce negative loss and no traffic has no percentage', () => {
  const first = summarizeConnectionStats(stats(inbound(100, 10)));
  const late = summarizeConnectionStats(stats(inbound(120, 8)), first.counters);
  assert.equal(late.connection.packetLossPercent, 0);
  assert.equal(summarizeConnectionStats(stats(inbound(120, 8)), late.counters).connection.packetLossPercent, null);
});

test('stream replacement and reset counters establish a fresh baseline', () => {
  const first = summarizeConnectionStats(stats(inbound(100, 5)));
  assert.equal(summarizeConnectionStats(stats(inbound(10, 1)), first.counters).connection.packetLossPercent, null);
  assert.equal(summarizeConnectionStats(stats(inbound(10, 1, { id: 'replacement' })), first.counters).connection.packetLossPercent, null);
});

test('legacy mediaType audio reports are accepted and malformed counters ignored', () => {
  const first = summarizeConnectionStats(stats(inbound(10, 0, { kind: undefined, mediaType: 'audio' })));
  assert.equal(summarizeConnectionStats(stats(inbound(20, 0, { kind: undefined, mediaType: 'audio' })), first.counters).connection.packetLossPercent, 0);
  assert.equal(summarizeConnectionStats(stats(inbound(Infinity, 0)), first.counters).connection.packetLossPercent, null);
});
