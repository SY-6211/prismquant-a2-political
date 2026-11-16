// Transport lifecycle is independent of the page. No credentials or full bodies in SSE.
export function validCursor(value) {
  return typeof value === 'string' && /^[0-9a-f]{32}:(0|[1-9][0-9]{0,18})$/.test(value)
    && BigInt(value.split(':')[1]) <= 9223372036854775807n;
}
export function newerCursor(value, previous) {
  if (!validCursor(value)) return false;
  return !previous || value.split(':')[0] !== previous.split(':')[0]
    || BigInt(value.split(':')[1]) > BigInt(previous.split(':')[1]);
}

export function connectUpdates({base, api, onChange, onSync, onStatus, onReset,
  EventSourceClass = globalThis.EventSource, fallbackMs = 60000}) {
  const controller = new AbortController();
  let stream, position, stopped = false, paused = false, starting = false, connected = false, recovering = false;
  let lastActivity = Date.now(), serial = Promise.resolve();
  const alive = () => !stopped && !paused && !controller.signal.aborted;
  const sync = () => onSync(controller.signal);
  function enqueue(job) {
    serial = serial.then(async () => { if (alive()) await job(); }).catch(() => {
      if (!alive()) return;
      stream?.close(); stream = null; connected = false; recovering = true;
      onStatus('fallback'); // Keep the last successfully applied cursor for replay.
    });
  }
  function open() {
    if (!alive() || !EventSourceClass) return;
    const current = new EventSourceClass(`${base}/api/v1/content-stream?after=${encodeURIComponent(position)}`);
    stream = current; lastActivity = Date.now();
    const currentAlive = () => alive() && stream === current;
    for (const type of ['ready', 'reset', 'content', 'heartbeat', 'unavailable']) {
      current.addEventListener(type, event => {
        if (!currentAlive()) return;
        lastActivity = Date.now();
        if (type === 'heartbeat') return;
        if (type === 'unavailable') {
          current.close(); stream = null; connected = false; recovering = true; onStatus('fallback'); return;
        }
        enqueue(async () => {
          if (!currentAlive()) return;
          const data = JSON.parse(event.data);
          if (!validCursor(event.lastEventId)) throw new Error('Invalid update cursor');
          if (type === 'reset') {
            onReset(data.reason); await sync(); position = event.lastEventId;
          } else if (type === 'ready') {
            if (recovering) await sync();
            if (!position || newerCursor(event.lastEventId, position)) position = event.lastEventId;
          } else if (newerCursor(event.lastEventId, position)) {
            await onChange(data, controller.signal, event.lastEventId);
            position = event.lastEventId;
          }
          if (currentAlive()) { connected = true; recovering = false; onStatus('live'); }
        });
      });
    }
    current.onerror = () => {
      if (!currentAlive()) return;
      // Recreate with our applied cursor, not the browser's received-but-unapplied ID.
      // Drain already queued work before reopening, preventing an error/replay race.
      connected = false; recovering = true; onStatus('reconnecting');
      enqueue(async () => { if (stream === current) { current.close(); stream = null; } });
    };
  }
  async function start() {
    if (!alive() || starting) return;
    starting = true;
    try {
      if (!position) {
        const info = await api('/api/v1/content-stream/state', {}, controller.signal);
        if (!validCursor(info.cursor)) throw new Error('Invalid update state');
        if (!alive()) return;
        position = info.cursor;
        // Read cursor before canonical list: commits during the initial load replay.
        open(); await sync();
      } else open();
      if (!EventSourceClass) onStatus('fallback');
    } catch { if (alive()) { onStatus('fallback'); await sync().catch(() => {}); } }
    finally { starting = false; }
  }
  const retry = setInterval(() => {
    if (!alive()) return;
    if (stream && Date.now()-lastActivity > 45000) {
      stream.close(); stream = null; connected = false; recovering = true; onStatus('reconnecting');
    }
    if (!stream) enqueue(start);
  }, 3000);
  const poll = setInterval(() => { if (alive()) enqueue(sync); }, fallbackMs);
  onStatus('connecting'); start();
  return { stop() { stopped = true; controller.abort(); stream?.close(); clearInterval(retry); clearInterval(poll); },
    pause() { paused = true; connected = false; recovering = true; stream?.close(); stream = null; },
    resume() { if (!stopped) { paused = false; onStatus('reconnecting'); enqueue(start); } },
    sync() { if (alive()) enqueue(sync); }, get connected() { return connected; } };
}
