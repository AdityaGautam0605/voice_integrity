const WS_BASE = "ws://localhost:5173/api";

export function connectToStream(sessionId, onMessage, onError, onClose) {
  const socket = new WebSocket(`${WS_BASE}/v1/stream/${sessionId}`);

  socket.binaryType = "arraybuffer";

  socket.onopen = () => {
    console.log("WebSocket connected");
  };

  socket.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      console.log("Backend message:", data);

      if (onMessage) {
        onMessage(data);
      }
    } catch (error) {
      console.error("Invalid backend message:", error);
    }
  };

  socket.onerror = (error) => {
    console.error("WebSocket error:", error);

    if (onError) {
      onError(error);
    }
  };

  socket.onclose = () => {
    console.log("WebSocket closed");

    if (onClose) {
      onClose();
    }
  };

  return socket;
}