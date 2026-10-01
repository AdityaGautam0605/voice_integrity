export function socketUrl(path) {
  const url = new URL(`/api${path}`, window.location.href);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.href;
}

export function connectToStream(sessionId, onMessage, onError, onClose) {
  const socket = new WebSocket(socketUrl(`/v1/stream/${encodeURIComponent(sessionId)}`));
  socket.binaryType = 'arraybuffer';

  socket.onmessage = (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (error) {
      onError?.(error);
      return;
    }
    onMessage?.(data);
  };
  socket.onerror = (event) => onError?.(event);
  socket.onclose = (event) => onClose?.(event);

  return socket;
}

export function opened(socket) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { socket.close(); reject(new Error('Connection timed out')); }, 10000);
    socket.addEventListener('open', () => { clearTimeout(timeout); resolve(); }, { once: true });
    socket.addEventListener('error', () => { clearTimeout(timeout); reject(new Error('Connection failed')); }, { once: true });
    socket.addEventListener('close', () => { clearTimeout(timeout); reject(new Error('Connection closed')); }, { once: true });
  });
}
