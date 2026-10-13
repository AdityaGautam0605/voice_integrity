// Call signaling authenticates with a room token in its first message.
// Analysis sockets still require the same-origin operator cookie.
export function signalingUrl(path, pageUrl, origin = '') {
  if (!/^\/v1\/calls\/[A-Za-z0-9_-]+\/signal$/.test(path)) {
    throw new Error('Invalid call signaling path');
  }
  const page = new URL(pageUrl);
  const url = origin ? new URL(origin) : new URL(`/api${path}`, page);
  if (origin) {
    if (!['https:', 'wss:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
      throw new Error('Call signaling requires a secure backend origin without a path');
    }
    url.pathname = path;
  }
  url.protocol = ['https:', 'wss:'].includes(url.protocol) ? 'wss:' : 'ws:';
  return url.href;
}
