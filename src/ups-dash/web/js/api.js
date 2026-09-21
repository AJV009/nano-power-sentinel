/* API client.  All paths relative so Tailscale Serve can front this at any
   prefix with HTTPS and a hostname. */

export async function get(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

export async function put(path, body) {
  const res = await fetch(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return { ok: res.ok, status: res.status, data: await res.json() };
}

export async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return { ok: res.ok, status: res.status, data: await res.json() };
}

/* Live stream.  A phone changing networks kills the connection silently, so
   reconnect is not optional -- and while disconnected the UI must say so
   rather than quietly showing a frozen snapshot as if it were current. */
export function stream(onSnapshot, onLink) {
  let src = null;
  let retry = 1000;

  const open = () => {
    src = new EventSource("api/stream");
    src.onopen = () => { retry = 1000; onLink(true); };
    src.onmessage = (ev) => {
      try { onSnapshot(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
    };
    src.onerror = () => {
      onLink(false);
      try { src.close(); } catch (e) { /* ignore */ }
      setTimeout(open, retry);
      retry = Math.min(retry * 2, 15000);
    };
  };
  open();
  return () => { try { src.close(); } catch (e) { /* ignore */ } };
}
