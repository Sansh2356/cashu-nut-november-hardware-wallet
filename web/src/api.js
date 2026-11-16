// GET without a body, POST with one. Errors carry the server's {error, hint, in_flight}.
// Every call carries this run's token, which the server put in the page.
const TOKEN = document.querySelector('meta[name="nutsigner-token"]')?.content ?? "";
export async function api(path, body) {
  const headers = { "X-NutSigner-Token": TOKEN };
  const r = await fetch("/api/" + path, body === undefined ? { headers } : {
    method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const out = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw Object.assign(new Error(out.error), out);
  return out;
}

export const sat = (n) => `${n.toLocaleString()} sat`;
