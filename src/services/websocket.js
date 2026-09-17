export function socketUrl(path) {
  const url = new URL(`/api${path}`, window.location.href);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.href;
}
export function opened(socket) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { socket.close(); reject(new Error('Connection timed out')); }, 10000);
    socket.addEventListener('open', () => { clearTimeout(timeout); resolve(); }, { once: true });
    socket.addEventListener('error', () => { clearTimeout(timeout); reject(new Error('Connection failed')); }, { once: true });
    socket.addEventListener('close', () => { clearTimeout(timeout); reject(new Error('Connection closed')); }, { once: true });
  });
}
