import test from 'node:test';
import assert from 'node:assert/strict';
import { quickTunnelOrigin, tunnelConfig } from '../scripts/tunnel.mjs';

const fixture = { VIF_METERED_DOMAIN: 'demo.metered.live', VIF_METERED_API_KEY: 'fixture-credential-api-key' };

test('tunnel configuration selects the public backend without VPS credentials', () => {
  const result = tunnelConfig({ ...fixture, VIF_ONLINE: '0', VIF_TURN_PROVIDER: 'coturn', VIF_FRONTEND_DIST: '/private' });
  assert.equal(result.VIF_ONLINE, '1');
  assert.equal(result.VIF_TURN_PROVIDER, 'metered');
  assert.ok(result.VIF_FRONTEND_DIST.endsWith('/dist'));
  assert.ok(result.VIF_OPERATOR_CODE.length >= 16);
  assert.equal(result.VIF_BACKEND, 'stub');
});

test('tunnel rejects foreign provider hosts and incomplete credentials', () => {
  for (const domain of ['demo.metered.live.evil.test', 'https://demo.metered.live', 'localhost', 'your-app.metered.live']) {
    assert.throws(() => tunnelConfig({ ...fixture, VIF_METERED_DOMAIN: domain }));
  }
  for (const key of ['', 'short', 'replace-with-an-api-key', 'whitespace in a credential']) {
    assert.throws(() => tunnelConfig({ ...fixture, VIF_METERED_API_KEY: key }));
  }
  assert.throws(() => tunnelConfig({ ...fixture, VIF_OPERATOR_CODE: 'short' }));
});

test('only Cloudflare-generated HTTPS origins are read from tunnel logs', () => {
  assert.equal(quickTunnelOrigin('INF | https://words-for-a-demo.trycloudflare.com |\n'), 'https://words-for-a-demo.trycloudflare.com');
  for (const value of ['https://developers.cloudflare.com', 'http://example.trycloudflare.com',
    'https://example.trycloudflare.com.evil.test', 'https://example.trycloudflare.co']) {
    assert.equal(quickTunnelOrigin(value), null);
  }
});
