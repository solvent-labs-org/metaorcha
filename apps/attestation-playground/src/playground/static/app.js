"use strict";

const $ = (id) => document.getElementById(id);
const RUN_KEY = "playground.run_id";
let currentEnvelope = null;

async function refreshMode() {
  const resp = await fetch("/mode");
  const info = await resp.json();
  const badge = $("mode-badge");
  badge.textContent = `mode: ${info.mode}${info.kafka ? " · kafka" : ""}`;
  badge.classList.toggle("real", info.mode === "real");
}

function addFeedItem(event) {
  if (event.type !== "live_step" && event.type !== "step" && event.type !== "run_failed") {
    return;
  }
  const li = document.createElement("li");
  if (event.type === "live_step") {
    li.innerHTML = `<span class="live">live</span> <span class="tool">${event.tool}</span>` +
      ` ok=${event.success} ${event.latency_ms}ms`;
  } else if (event.type === "step") {
    li.innerHTML = `<span class="tool">${event.tool}</span> ok=${event.success} ${event.latency_ms}ms` +
      `<br><span class="hash">chain[${event.step_index}] ${event.chain_hash.slice(0, 24)}…</span>`;
  } else if (event.type === "run_failed") {
    li.innerHTML = `<span style="color:#f85149">run failed: ${event.error}</span>`;
  }
  $("step-feed").appendChild(li);
}

async function loadEnvelope(runId) {
  const resp = await fetch(`/runs/${runId}/envelope`);
  if (!resp.ok) return;
  currentEnvelope = await resp.json();
  $("envelope-view").textContent = JSON.stringify(currentEnvelope, null, 2);
  $("signer-line").textContent =
    `signer: ${currentEnvelope.signer.did}\npubkey: ${currentEnvelope.signer.public_key_b64}`;
  $("download-btn").disabled = false;
  $("copy-btn").disabled = false;
}

async function startRun() {
  $("step-feed").textContent = "";
  $("envelope-view").textContent = "running…";
  $("download-btn").disabled = true;
  $("copy-btn").disabled = true;
  currentEnvelope = null;
  const body = { prompt: $("prompt").value };
  const sel = $("mode-select").value;
  if (sel) body.mode = sel;
  const resp = await fetch("/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const err = await resp.json();
    addFeedItem({ type: "run_failed", error: err.detail || resp.statusText });
    return;
  }
  const { run_id } = await resp.json();
  localStorage.setItem(RUN_KEY, run_id);
  attachToRun(run_id);
}

// Subscribe to a run's event stream. The server replays buffered events
// first, so this also restores the full feed after a browser refresh.
function attachToRun(runId) {
  const source = new EventSource(`/runs/${runId}/events`);
  source.onmessage = (msg) => {
    const event = JSON.parse(msg.data);
    addFeedItem(event);
    if (event.type === "envelope_ready") {
      source.close();
      loadEnvelope(runId);
    } else if (event.type === "run_failed") {
      source.close();
    }
  };
  source.onerror = () => {
    source.close();
    // run unknown to the server (e.g. playground restarted) — forget it
    fetch(`/runs/${runId}/envelope`).then((r) => {
      if (r.status === 404) localStorage.removeItem(RUN_KEY);
    });
  };
}

function showVerdict(result) {
  const el = $("verify-result");
  const checks = Object.entries(result.checks)
    .map(([name, ok]) => `  ${ok ? "ok    " : "FAILED"} ${name}`)
    .join("\n");
  el.className = `verdict ${result.ok ? "valid" : "invalid"}`;
  el.textContent = `${result.ok ? "VALID" : "INVALID"} — ${result.reason}\n${checks}` +
    (result.signer_did ? `\nsigner: ${result.signer_did}` : "") +
    (result.step_count != null ? `\nsteps: ${result.step_count}` : "");
}

async function verifyInput() {
  let envelope;
  try {
    envelope = JSON.parse($("verify-input").value);
  } catch {
    showVerdict({ ok: false, reason: "input is not valid JSON", checks: {}, signer_did: null, step_count: null });
    return;
  }
  const resp = await fetch("/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(envelope),
  });
  showVerdict(await resp.json());
}

async function loadGolden(which) {
  const resp = await fetch("/golden");
  const data = await resp.json();
  $("verify-input").value = JSON.stringify(data[which], null, 2);
  await verifyInput();
}

$("run-btn").addEventListener("click", startRun);
$("verify-btn").addEventListener("click", verifyInput);
$("golden-valid-btn").addEventListener("click", () => loadGolden("valid"));
$("golden-tampered-btn").addEventListener("click", () => loadGolden("tampered"));
$("copy-btn").addEventListener("click", () => {
  if (currentEnvelope) $("verify-input").value = JSON.stringify(currentEnvelope, null, 2);
});
$("download-btn").addEventListener("click", () => {
  if (!currentEnvelope) return;
  const blob = new Blob([JSON.stringify(currentEnvelope, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${currentEnvelope.run_id}.attestation.json`;
  a.click();
  URL.revokeObjectURL(a.href);
});

refreshMode();

// Re-attach to an in-progress (or finished) run after a browser refresh.
const storedRunId = localStorage.getItem(RUN_KEY);
if (storedRunId) attachToRun(storedRunId);
