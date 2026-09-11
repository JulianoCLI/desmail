// API 2.4.0; native fetch (Node 18+).
const BASE = "http://127.0.0.1:8000";
export const API_VERSION = "2.4.0";
const TIMEOUT_MS = 60000; // 60s para create (Turnstile pode demorar).

async function request(path, data) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const r = await fetch(BASE + path, data === undefined
      ? {signal: ctrl.signal}
      : {method: "POST", headers: {"Content-Type": "application/json"},
         body: JSON.stringify(data), signal: ctrl.signal});
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  } finally {
    clearTimeout(timer);
  }
}

export function createEmail(config = {}) {
  return request("/email/create", {provider: "google", domain: "gmail.com", server: "1", ...config});
}

export function getStatus(session_id) {
  return request(`/email/status?${new URLSearchParams({session_id})}`);
}

export async function checkEmail(session_id, tries = 10, delay = 15000) {
  let result;
  for (let i = 0; i < tries; i++) {
    result = await request(`/email/check?${new URLSearchParams({session_id})}`);
    if (!["ok", "empty"].includes(result.state)) throw new Error(result.error || result.state);
    if (!Array.isArray(result.messages)) throw new Error("messages invalidas");
    if (result.messages.length || i === tries - 1) return result;
    await new Promise(resolve => setTimeout(resolve, delay));
  }
  throw new Error("tries deve ser positivo");
}

export async function getBody(session_id, mid) {
  const result = await request(`/email/body?${new URLSearchParams({session_id, mid})}`);
  if (result.error) throw new Error(result.error);
  return result; // state=in_progress: repetir depois; best nunca equivale a verified.
}

export function openMessage(session_id, mid, revalidate = false) {
  return request("/email/open", {session_id, mid, revalidate});
}

export function releaseEmail(session_id) {
  // Ciclo criar > usar > confirmar > apagar: libera a sessao da memoria
  // imediatamente e agenda a exclusao no provedor em segundo plano.
  // O proximo createEmail nao espera essa limpeza.
  return request("/email/release", {session_id});
}
