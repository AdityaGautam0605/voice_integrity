const nonnegative = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0;

export const unknownConnection = () => ({ route: 'Unknown', transport: null, rttMs: null, packetLossPercent: null });

// Keep the counters across samples: packet loss describes the last interval,
// while the browser's inbound counters describe the entire connection.
export function summarizeConnectionStats(report, previous = null) {
  const values = [...report.values()];
  const transport = values.find((item) => item.type === 'transport' && item.selectedCandidatePairId);
  const pair = report.get(transport?.selectedCandidatePairId)
    || values.find((item) => item.type === 'candidate-pair' && item.state === 'succeeded' && (item.selected || item.nominated));
  const local = report.get(pair?.localCandidateId);
  const remote = report.get(pair?.remoteCandidateId);
  const relayed = local?.candidateType === 'relay' || remote?.candidateType === 'relay';
  const route = relayed ? 'Relayed' : local?.candidateType && remote?.candidateType ? 'Direct' : 'Unknown';
  const inbound = values.filter((item) => item.type === 'inbound-rtp' && !item.isRemote
    && (item.kind === 'audio' || item.mediaType === 'audio'));
  const counters = new Map();
  let received = 0;
  let lost = 0;
  let comparable = false;
  for (const item of inbound) {
    if (!nonnegative(item.packetsReceived) || !Number.isFinite(item.packetsLost)) continue;
    counters.set(item.id, { received: item.packetsReceived, lost: item.packetsLost });
    const last = previous?.get(item.id);
    if (last && item.packetsReceived >= last.received) {
      comparable = true;
      received += item.packetsReceived - last.received;
      // Late packets can reduce packetsLost; that is not negative loss.
      lost += Math.max(0, item.packetsLost - last.lost);
    }
  }
  return {
    connection: {
      route,
      transport: (local?.relayProtocol || remote?.relayProtocol || local?.protocol || remote?.protocol)?.toUpperCase() || null,
      rttMs: nonnegative(pair?.currentRoundTripTime) ? pair.currentRoundTripTime * 1000 : null,
      packetLossPercent: comparable && received + lost > 0 ? lost / (received + lost) * 100 : null,
    },
    counters,
  };
}
