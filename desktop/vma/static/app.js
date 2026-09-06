/* Visual Memory Agent dashboard — vanilla JS, no build step. */
"use strict";

const $ = (sel) => document.querySelector(sel);
let uiSocket = null;
let currentRuns = [];
let chatBuffers = {};   // run_id -> [{who, text}]
let pairingAddresses = [];

/* ------------------------------ tabs ---------------------------------- */
document.querySelectorAll("nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "runs") refreshRuns();
    if (btn.dataset.tab === "models") renderModelEditors();
  });
});

/* --------------------------- status polling ---------------------------- */
async function refreshStatus() {
  try {
    const st = await (await fetch("/api/status")).json();
    renderStatus(st);
  } catch (e) { /* server briefly down */ }
}

async function refreshPairingInfo() {
  try {
    const info = await (await fetch("/api/pairing/info")).json();
    const browserUrl = location.origin;
    const browserHost = location.hostname;
    const browserIsLocal = ["localhost", "127.0.0.1", "::1"].includes(browserHost);
    pairingAddresses = browserIsLocal
      ? (info.lan_urls || []).concat(info.local_url ? [info.local_url] : [])
      : [browserUrl];
    const box = $("#pairing-addresses");
    box.innerHTML = pairingAddresses.length
      ? pairingAddresses.map((url, i) =>
          i === 0 && browserIsLocal
            ? `<code class="primary">${escapeHtml(url)}</code> <span class="muted small">← use this one</span>`
            : `<code>${escapeHtml(url)}</code>`).join("<br>")
      : `<code>${escapeHtml(info.local_url || browserUrl)}</code>`;
    $("#btn-copy-address").disabled = !pairingAddresses.length;
  } catch (e) {
    pairingAddresses = [location.origin];
    $("#pairing-addresses").innerHTML = `<code>${escapeHtml(location.origin)}</code>`;
  }
}

async function generatePairingCode(force = false) {
  const response = await fetch(`/api/pairing/code${force ? "?force=true" : ""}`);
  if (!response.ok) return;
  const data = await response.json();
  $("#pairing-code").textContent = data.code || "—";
  $("#pairing-expiry").textContent = `expires in ${Math.round((data.expires_in_s || 600) / 60)} minutes`;
  const qr = $("#pairing-qr");
  if (qr) qr.src = `/api/pairing/qr.svg?t=${Date.now()}`;
}

async function refreshPairingRequests() {
  const list = $("#pairing-requests-list");
  if (!list) return;
  try {
    const requests = await (await fetch("/api/pair/requests")).json();
    if (!Array.isArray(requests) || !requests.length) {
      list.textContent = "none";
      return;
    }
    list.innerHTML = "";
    for (const req of requests) {
      const row = document.createElement("div");
      row.className = "pairing-request-row";
      const label = document.createElement("span");
      label.textContent = `${req.device_name} · ${req.age_s}s ago`;
      const approve = document.createElement("button");
      approve.textContent = "Approve";
      approve.onclick = async () => {
        const r = await fetch("/api/pair/approve", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ request_id: req.request_id }),
        });
        if (r.ok) {
          const data = await r.json();
          $("#pairing-code").textContent = data.code || "—";
          $("#pairing-expiry").textContent = "approved — give this code to the phone";
          await refreshPairingRequests();
        }
      };
      const deny = document.createElement("button");
      deny.textContent = "Deny";
      deny.className = "secondary";
      deny.onclick = async () => {
        await fetch("/api/pair/deny", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ request_id: req.request_id }),
        });
        await refreshPairingRequests();
      };
      row.append(label, approve, deny);
      list.append(row);
    }
  } catch {
    list.textContent = "none";
  }
}

async function loadRelayConfig() {
  const urlInput = $("#relay-url-input");
  const tokenInput = $("#relay-token-input");
  if (!urlInput) return;
  try {
    const cfg = await (await fetch("/api/config/relay")).json();
    if (document.activeElement !== urlInput) urlInput.value = cfg.relay_url || "";
    tokenInput.placeholder = cfg.relay_reg_token_set
      ? "registration token (set — leave blank to keep)"
      : "registration token";
    const status = $("#relay-config-status");
    if (status) {
      status.textContent = cfg.relay_url
        ? (cfg.connected ? `connected · ${cfg.status}` : `not connected · ${cfg.status}`)
        : "online mode off (LAN only)";
    }
  } catch { /* server not up yet */ }
}

async function saveRelayConfig(disable = false) {
  const url = disable ? "" : ($("#relay-url-input")?.value || "").trim();
  const token = ($("#relay-token-input")?.value || "").trim();
  const body = disable ? { relay_url: "" } : { relay_url: url };
  if (token) body.relay_reg_token = token;   // empty input keeps the saved token
  const r = await fetch("/api/config/relay", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const status = $("#relay-config-status");
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    if (status) status.textContent = `save failed: ${err.detail || r.status}`;
    return;
  }
  if (!disable) $("#relay-token-input").value = "";   // don't keep the secret on screen
  await loadRelayConfig();
  refreshStatus();
}

async function copyPairingAddress() {
  const address = pairingAddresses[0];
  if (!address) return;
  try {
    await navigator.clipboard.writeText(address);
  } catch {
    const input = document.createElement("textarea");
    input.value = address;
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
  }
  $("#btn-copy-address").textContent = "Copied";
  setTimeout(() => $("#btn-copy-address").textContent = "Copy address", 1500);
}

/* --------------------- system state (always visible) ------------------- */
/* The banner answers "is the system watching me right now" without reading
   anything else. Mapping is deliberately coarse and fails toward visible. */
function renderSystemState(st) {
  const chip = $("#system-state");
  const label = $("#system-state-label");
  let cls = "state-disconnected", text = "DISCONNECTED", detail = "";
  if (st.run_state === "running") {
    cls = "state-observing"; text = "OBSERVING";
    detail = `run "${st.run ? st.run.name : "?"}" — camera frames are being processed`;
  } else if (st.run_state === "paused") {
    cls = "state-paused"; text = "PAUSED"; detail = "run paused by user";
  } else if (st.run_state === "degraded") {
    cls = "state-disconnected"; text = "DISCONNECTED";
    detail = "run active but the phone is offline";
  } else if (st.run_state === "idle") {
    cls = "state-disconnected"; text = "DISCONNECTED"; detail = "no active run";
  }
  const pipelineError = st.pipeline && st.pipeline.last_error;
  const modelError = st.models && ((st.models.vision && st.models.vision.error) ||
    (st.models.reasoning && st.models.reasoning.error));
  if (pipelineError || modelError) {
    cls = "state-error"; text = "ERROR";
    detail = pipelineError || String(modelError);
  }
  chip.className = "state-chip " + cls;
  label.textContent = text;
  chip.title = text + (detail ? ` — ${detail}` : "");
}

function renderStatus(st) {
  renderSystemState(st);
  const rs = $("#run-state");
  rs.textContent = st.run_state;
  rs.className = "big " + st.run_state;
  $("#run-name").textContent = st.run ? st.run.name : "";
  $("#run-stats").textContent = st.run
    ? `${st.run.stats.observations} obs · ${st.run.stats.frames_accepted}/${st.run.stats.frames_total} processed · no-change: ${st.run.stats.frames_nochange} · errors: ${st.run.stats.frames_error || 0} · dup: ${st.run.stats.frames_duplicates || 0} · avg VLM: ${st.run.stats.avg_vlm_ms ? Math.round(st.run.stats.avg_vlm_ms) + " ms" : "—"}`
    : "";

  const ph = st.sensor || {};
  const ps = $("#phone-state");
  ps.textContent = ph.connected ? `connected (${ph.transport})` : "disconnected";
  ps.className = "big " + (ph.connected ? "connected" : "");
  $("#phone-info").textContent = ph.connected
    ? `${ph.device_id} · last seq ${ph.last_seq}` : "";
  const relay = st.relay || {};
  $("#relay-info").textContent = relay.configured
    ? `Relay: ${relay.status}${relay.channel_id ? ` · ${relay.channel_id}` : ""}` +
      (relay.hosted ? " · hosted in-app" : "")
    : "Relay: disabled";
  const disc = st.discovery || {};
  const di = $("#discovery-info");
  if (di) {
    di.textContent = disc.advertised
      ? `Discoverable on this Wi-Fi as “${disc.name || disc.instance}” — phones can find this desktop in the app`
      : "Not discoverable on the LAN (zeroconf unavailable) — pair by address/QR";
  }
  if (ph.connected && ph.auth) {
    const authNote = ph.auth === "cr+e2e"
      ? "auth: challenge-response · end-to-end encrypted"
      : ph.auth === "cr" ? "auth: challenge-response"
      : ph.auth === "bearer" ? "auth: bearer token (re-pair to upgrade)" : "";
    if (authNote) $("#phone-info").textContent += ` · ${authNote}`;
  }

  const p = st.pipeline;
  $("#pipeline-stats").textContent = p
    ? `frame inference: ${p.vlm_ms_last ?? "—"} ms last · ~${p.vlm_latency_ms_ema ?? "—"} ms avg · send every ${p.recommended_interval_ms} ms · processed ${p.processed_total}` +
      (st.run && st.run.stats.vlm_ms_p95 ? ` · p50 ${st.run.stats.vlm_ms_p50} / p95 ${st.run.stats.vlm_ms_p95} ms` : "")
    : "no active run";
  const stats = st.run ? st.run.stats : null;
  $("#storage-stats").textContent = stats
    ? `${stats.media_files} images stored · ${(stats.media_bytes / 1e6).toFixed(1)} MB · ${stats.embeddings}/${stats.observations} observations embedded`
    : "no active run";
  $("#last-obs").textContent = p && p.last_observation ? `last: ${p.last_observation}` : "";

  const v = st.models.vision, r = st.models.reasoning;
  const fmt = (m) => !m ? "?" :
    m.error ? `error: ${m.error}` :
    m.loaded ? `${(m.size_vram_bytes / 1e9).toFixed(1)}GB in VRAM (${Math.round(m.gpu_fraction * 100)}% GPU)` :
    "not loaded";
  $("#model-status").innerHTML =
    `VLM: ${v ? v.name : "?"} → ${fmt(v)}<br>LLM: ${r ? r.name : "?"} → ${fmt(r)}`;

  $("#cloud-warning").classList.toggle("hidden", !(st.cloud_used && (st.cloud_used.vision || st.cloud_used.reasoning)));

  $("#btn-stop").disabled = !st.run;
  $("#btn-start").disabled = !!st.run;
}

/* ------------------------------ run controls --------------------------- */
$("#btn-start").addEventListener("click", async () => {
  const name = $("#run-name-input").value.trim() || new Date().toISOString().slice(0, 16);
  await fetch("/api/runs", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }) });
  refreshStatus();
});
$("#btn-stop").addEventListener("click", async () => {
  await fetch("/api/runs/current/stop", { method: "POST" });
  refreshStatus();
});
async function modelAction(path) {
  const response = await fetch(path, { method: "POST" });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch {}
    alert(`Model action failed: ${detail}`);
  }
  await refreshStatus();
}
$("#btn-preload-vision").addEventListener("click", () => modelAction("/api/models/vision/preload"));
$("#btn-preload-reasoning").addEventListener("click", () => modelAction("/api/models/reasoning/preload"));
$("#btn-unload-vision").addEventListener("click", () => modelAction("/api/models/vision/unload"));
$("#btn-unload-reasoning").addEventListener("click", () => modelAction("/api/models/reasoning/unload"));
$("#btn-copy-address").addEventListener("click", copyPairingAddress);
$("#btn-new-code").addEventListener("click", () => generatePairingCode(true));
$("#btn-save-relay").addEventListener("click", () => saveRelayConfig(false));
$("#btn-disable-relay").addEventListener("click", () => saveRelayConfig(true));

/* ------------------------- storage policy card ------------------------- */
$("#btn-apply-encrypt").addEventListener("click", async () => {
  const enabled = $("#cfg-encrypt").checked;
  const response = await fetch("/api/security/encryption", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch {}
    alert(`Encryption change rejected: ${detail}`);
  } else {
    const d = await response.json();
    $("#btn-apply-encrypt").textContent = enabled ? "Encrypted" : "Off";
    setTimeout(() => $("#btn-apply-encrypt").textContent = "Apply", 1500);
    if (enabled) alert("Encryption is on for new data. Keep data/secret.key safe — " +
      "deleting it locks everything already encrypted.");
    else alert("New data will be written unencrypted. Already-encrypted rows stay " +
      "encrypted and need the key.");
  }
  refreshStatus();
});
$("#btn-apply-storage").addEventListener("click", async () => {
  const budgetMb = Number($("#cfg-budget").value || 0);
  const body = {
    save_frames: $("#cfg-save-frames").value,
    media_max_side: Number($("#cfg-max-side").value || 1024),
    media_retention_minutes: Number($("#cfg-retention").value || 0),
    media_budget_bytes: Math.round(budgetMb * 1e6),
    hourly_index: $("#cfg-hourly-index").checked,
  };
  const response = await fetch("/api/config/pipeline", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch {}
    alert(`Storage policy rejected: ${detail}`);
  } else {
    $("#btn-apply-storage").textContent = "Saved";
    setTimeout(() => $("#btn-apply-storage").textContent = "Apply storage policy", 1500);
  }
  refreshStatus();
});
$("#btn-wipe-media").addEventListener("click", async () => {
  const runId = $("#obs-run-select").value || ($("#chat-run-select").value);
  if (!runId) { alert("Select a run first (Observations or Chat tab)."); return; }
  if (!confirm("Delete ALL retained images of this run? Observation text stays (it can still be sensitive).")) return;
  await fetch(`/api/runs/${runId}/media`, { method: "DELETE" });
  refreshStatus();
});

/* ---------------------------- push-to-talk ----------------------------- */
/* Hold the mic button, release to transcribe locally (whisper, CPU) and put
   the text into the chat input. Continuous listening does not exist. */
(function setupVoice() {
  const btn = $("#btn-voice");
  let recorder = null, chunks = [], startTs = 0;
  async function start() {
    if (!navigator.mediaDevices || !window.MediaRecorder) { chatStatus("voice: browser recording unsupported"); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (ev) => chunks.push(ev.data);
      recorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        submitRecording(new Blob(chunks, { type: recorder.mimeType || "audio/webm" }));
      };
      recorder.start();
      startTs = Date.now();
      btn.classList.add("recording");
      chatStatus("recording… release to transcribe");
    } catch (e) { chatStatus("voice: microphone permission denied"); }
  }
  function stop() {
    btn.classList.remove("recording");
    if (recorder && recorder.state === "recording") recorder.stop();
    recorder = null;
  }
  async function submitRecording(blob) {
    if ((Date.now() - startTs) < 400) { chatStatus("held too briefly"); return; }
    chatStatus("transcribing locally…");
    const asNote = $("#voice-as-note") && $("#voice-as-note").checked;
    try {
      if (asNote) {
        // Voice note: transcribe AND commit as an observation linked to
        // nearby/similar observations (phone app uses the same endpoint).
        const noteResp = await fetch("/api/voice/note", { method: "POST", body: blob });
        const noteData = await noteResp.json();
        if (!noteResp.ok) { chatStatus(`voice note: ${noteData.detail || noteResp.status}`); return; }
        if (!noteData.saved) { chatStatus("voice note: nothing recognized"); return; }
        const links = (noteData.links || []).length;
        chatStatus(`voice note saved as observation ${noteData.observation_id} · linked to ${links} observation(s) — transcription below`);
      }
      const response = await fetch("/api/voice/transcribe", { method: "POST", body: blob });
      const data = await response.json();
      if (!response.ok) { chatStatus(`voice: ${data.detail || response.status}`); return; }
      const text = (data.transcription && data.transcription.text) || "";
      if (!text) { chatStatus("voice: nothing recognized"); return; }
      $("#chat-input").value = text;
      chatStatus(`transcribed in ${data.transcription.elapsed_ms} ms (${data.transcription.model}) — review and send`);
    } catch (e) { chatStatus("voice: transcription request failed"); }
  }
  btn.addEventListener("mousedown", start);
  btn.addEventListener("mouseup", stop);
  btn.addEventListener("mouseleave", () => { if (recorder) stop(); });
  btn.addEventListener("touchstart", (ev) => { ev.preventDefault(); start(); });
  btn.addEventListener("touchend", (ev) => { ev.preventDefault(); stop(); });
})();

/* ------------------------------ observations --------------------------- */
$("#btn-refresh-obs").addEventListener("click", refreshObservations);

async function refreshRunsInto(select) {
  currentRuns = await (await fetch("/api/runs")).json();
  const cur = select.value;
  select.innerHTML = currentRuns.map((r) =>
    `<option value="${r.run_id}">${r.name}${r.ended_at ? "" : " ●"}</option>`).join("");
  if (cur && currentRuns.some((r) => r.run_id === cur)) select.value = cur;
}

async function refreshObservations() {
  const select = $("#obs-run-select");
  const runId = select.value;
  const box = $("#observations-list");
  if (!runId) { box.textContent = "no run selected"; return; }
  const minImp = Number($("#obs-min-importance").value || 0);
  const obs = await (await fetch(`/api/runs/${runId}/observations?min_importance=${minImp}`)).json();
  if (!obs.length) { box.textContent = "no observations"; return; }
  box.innerHTML = obs.reverse().map((o) => {
    const imp = o.importance || 0;
    const p = o.payload || {};
    const vlm = p.vlm || {};
    const issues = (vlm.issues || []).map((i) => `⚠ [${i.severity}] ${i.description}`).join("<br>");
    const actions = (vlm.actions || []).map((a) => `→ ${a.description}`).join("<br>");
    const img = o.frame_id && imp >= 2
      ? `<img src="/api/runs/${runId}/media/obs${String(o.id).padStart(6, "0")}_i${imp}" alt="" onerror="this.remove()">`
      : "";
    return `<div class="obs-item">
      <span class="imp i${imp}">imp ${imp}</span>
      <div class="t">${o.local_ts || o.ts} · ${vlm.scene || p.scene || "?"} · conf ${o.confidence ?? "?"}</div>
      <div>${o.summary || ""}</div>
      ${actions ? `<div class="muted small">${actions}</div>` : ""}
      ${issues ? `<div class="small" style="color:var(--warn)">${issues}</div>` : ""}
      ${img}
    </div>`;
  }).join("");
}

/* --------------------------------- runs -------------------------------- */
$("#btn-refresh-runs").addEventListener("click", refreshRuns);

async function refreshRuns() {
  const runs = await (await fetch("/api/runs")).json();
  const box = $("#runs-list");
  if (!runs.length) { box.innerHTML = "<div class='muted'>no runs yet</div>"; return; }
  box.innerHTML = "";
  for (const r of runs) {
    const row = document.createElement("div");
    row.className = "run-row";
    row.innerHTML = `
      <div>
        <b>${r.name}</b> ${r.ended_at ? "" : "<span style='color:var(--ok)'>● active</span>"}
        <div class="muted small">${r.created_at || ""} · ${(r.size_bytes / 1e6).toFixed(1)} MB ·
          ${Object.entries(r.cloud_used || {}).filter(([, v]) => v).map(([k]) => k + ":cloud").join(" ") || "local"}</div>
      </div>
      <div class="row">
        <button class="secondary" data-act="open">Open in chat</button>
        <button class="secondary" data-act="delete">Delete</button>
      </div>`;
    row.querySelector('[data-act="open"]').addEventListener("click", () => {
      document.querySelector('nav [data-tab="chat"]').click();
      $("#chat-run-select").value = r.run_id;
    });
    row.querySelector('[data-act="delete"]').addEventListener("click", async () => {
      if (!confirm(`Delete run "${r.name}" and all its media permanently?`)) return;
      await fetch(`/api/runs/${r.run_id}`, { method: "DELETE" });
      refreshRuns();
    });
    box.appendChild(row);
  }
}

/* --------------------------------- chat -------------------------------- */
$("#chat-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text) return;
  const runId = $("#chat-run-select").value;
  if (!runId) { chatStatus("select a run first"); return; }
  input.value = "";
  addChat(runId, "user", text);
  chatStatus("thinking…");
  sendUi({ type: "chat", run_id: runId, text });
});

$("#btn-report").addEventListener("click", () => {
  const runId = $("#chat-run-select").value;
  if (!runId) { chatStatus("select a run first"); return; }
  chatStatus("generating report…");
  sendUi({ type: "generate_report", run_id: runId, kind: "chronological" });
});

function addChat(runId, who, text) {
  chatBuffers[runId] = chatBuffers[runId] || [];
  chatBuffers[runId].push({ who, text });
  renderChat(runId);
}
function chatAppend(runId, piece) {
  chatBuffers[runId] = chatBuffers[runId] || [];
  const msgs = chatBuffers[runId];
  if (!msgs.length || msgs[msgs.length - 1].who !== "assistant-stream")
    msgs.push({ who: "assistant-stream", text: "" });
  msgs[msgs.length - 1].text += piece;
  renderChat(runId);
}
function chatStatus(text) { $("#chat-status").textContent = text; }

function renderChat(runId) {
  const log = $("#chat-log");
  const msgs = chatBuffers[runId] || [];
  log.innerHTML = msgs.map((m) => `
    <div class="chat-msg ${m.who === "user" ? "user" : ""}">
      <div class="who">${m.who === "user" ? "You" : "Agent"}</div>${escapeHtml(m.text)}
    </div>`).join("");
  log.scrollTop = log.scrollHeight;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ------------------------------ ui websocket --------------------------- */
function connectUiSocket() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  uiSocket = new WebSocket(`${proto}://${location.host}/ws/ui`);
  uiSocket.onopen = () => sendUi({ type: "ping" });
  uiSocket.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    const runId = $("#chat-run-select").value || (msg.run_id ?? "");
    switch (msg.type) {
      case "chat_delta": chatAppend(runId, msg.text); break;
      case "chat_status": chatStatus(msg.status); break;
      case "chat_done": {
        chatBuffers[runId] = (chatBuffers[runId] || []).filter((m) => m.who !== "assistant-stream");
        addChat(runId, "assistant", msg.answer);
        if (msg.tool_trace && msg.tool_trace.length)
          chatStatus(`used ${msg.tool_trace.length} tool call(s) in ${msg.iterations} iteration(s), ${(msg.total_ms / 1000).toFixed(1)}s`);
        else chatStatus("");
        break;
      }
      case "chat_error": chatStatus("error: " + msg.message); break;
      case "report_done":
        chatBuffers[runId] = (chatBuffers[runId] || []).filter((m) => m.who !== "assistant-stream");
        addChat(runId, "assistant", msg.answer);
        chatStatus("report saved to run reports/ directory");
        break;
    }
  };
  uiSocket.onclose = () => setTimeout(connectUiSocket, 2000);
}
function sendUi(obj) { if (uiSocket && uiSocket.readyState === 1) uiSocket.send(JSON.stringify(obj)); }

/* ------------------------------ model editors -------------------------- */
async function renderModelEditors() {
  let models = [];
  try { models = await (await fetch("/api/ollama/models")).json(); } catch {}
  const st = await (await fetch("/api/status")).json();
  const cfg = st.config || {};
  const pc = st.pipeline_config || {};
  const setSelect = (id, v) => {
    const el = $(id);
    if (el && [...el.options].some((o) => o.value === String(v))) el.value = String(v);
  };
  setSelect("#cfg-save-frames", pc.save_frames || "important");
  setSelect("#cfg-max-side", pc.media_max_side || 1024);
  if ($("#cfg-retention")) $("#cfg-retention").value = pc.media_retention_minutes ?? 0;
  if ($("#cfg-budget")) $("#cfg-budget").value = pc.media_budget_bytes ? Math.round(pc.media_budget_bytes / 1e6) : 0;
  if ($("#cfg-hourly-index")) $("#cfg-hourly-index").checked = !!pc.hourly_index;
  if ($("#cfg-encrypt")) $("#cfg-encrypt").checked = !!(st.encryption && st.encryption.enabled);
  for (const stage of ["vision", "reasoning"]) {
    const c = cfg[stage] || {};
    const box = $(`#${stage}-model-editor`);
    box.innerHTML = `
      <div class="editor-row">
        <select id="${stage}-kind">
          <option value="ollama" ${c.kind === "ollama" ? "selected" : ""}>Ollama (local)</option>
          <option value="openai_compat" ${c.kind !== "ollama" ? "selected" : ""}>Cloud (OpenAI-compatible)</option>
        </select>
        <select id="${stage}-model">
          ${models.map((m) => `<option ${m.name === c.model ? "selected" : ""}>${m.name}</option>`).join("")}
          ${c.model && !models.some((m) => m.name === c.model) ? `<option selected>${c.model}</option>` : ""}
        </select>
      </div>
      <div class="editor-row" id="${stage}-cloud" style="${c.kind === "ollama" ? "display:none" : ""}">
        <input id="${stage}-base-url" placeholder="https://api.openai.com/v1" value="${c.base_url || ""}">
        <input id="${stage}-api-key" type="password" placeholder="API key" value="">
      </div>
      <div class="editor-row">
        <label>ctx <input id="${stage}-ctx" type="number" style="width:6em" value="${c.num_ctx || 4096}"></label>
        <label>keep_alive <input id="${stage}-keep" style="width:6em" value="${c.keep_alive || "10m"}"></label>
        <label>gpu layers <input id="${stage}-gpu" type="number" style="width:5em" placeholder="auto" title="empty = auto (Ollama places layers)" value="${c.num_gpu ?? ""}"></label>
        <button type="button" id="${stage}-gpu-reset" class="ghost" title="reset gpu layers to auto">↺</button>
        <label><input id="${stage}-think" type="checkbox" ${c.enable_thinking ? "checked" : ""}> thinking</label>
        <button data-stage="${stage}">Apply</button>
      </div>`;
    box.querySelector(`#${stage}-kind`).addEventListener("change", (ev) => {
      box.querySelector(`#${stage}-cloud`).style.display = ev.target.value === "ollama" ? "none" : "";
    });
    const applyBtn = box.querySelector("button[data-stage]");
    box.querySelector(`#${stage}-gpu-reset`).addEventListener("click", () => {
      box.querySelector(`#${stage}-gpu`).value = "";
      applyBtn.click();
    });
    applyBtn.addEventListener("click", async () => {
      const kind = box.querySelector(`#${stage}-kind`).value;
      const gpuRaw = box.querySelector(`#${stage}-gpu`).value.trim();
      const gpuNum = Number(gpuRaw);
      const body = {
        kind,
        model: box.querySelector(`#${stage}-model`).value,
        num_ctx: Number(box.querySelector(`#${stage}-ctx`).value || 4096),
        enable_thinking: box.querySelector(`#${stage}-think`).checked,
      };
      if (kind === "ollama") {
        body.keep_alive = box.querySelector(`#${stage}-keep`).value.trim();
        // empty/invalid = auto: send explicit null so the backend clears the saved value
        body.num_gpu = gpuRaw && Number.isFinite(gpuNum) ? gpuNum : null;
      }
      else {
        body.base_url = box.querySelector(`#${stage}-base-url`).value.trim();
        const key = box.querySelector(`#${stage}-api-key`).value.trim();
        if (key) body.api_key = key;
      }
      await fetch(`/api/models/${stage}`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      refreshStatus();
    });
  }

  // --- embeddings (semantic memory) editor ---
  const ec = st.embeddings || {};
  const ebox = $("#embeddings-editor");
  ebox.innerHTML = `
    <div class="editor-row">
      <label><input id="emb-enabled" type="checkbox" ${ec.enabled ? "checked" : ""}> enabled</label>
      <select id="emb-model">
        ${models.map((m) => `<option ${m.name === ec.model ? "selected" : ""}>${m.name}</option>`).join("")}
        ${ec.model && !models.some((m) => m.name === ec.model) ? `<option selected>${ec.model}</option>` : ""}
      </select>
    </div>
    <div class="editor-row">
      <span class="muted small">${ec.enabled
        ? `active — ${ec.model} (pull it first if missing)` : "disabled"}</span>
    </div>
    <div class="editor-row">
      <button id="emb-apply">Apply</button>
      <button id="emb-preload">Preload</button>
    </div>
    <div class="pairing-note">Committed observations are embedded once each. If the model is missing, semantic search switches off automatically and keyword search keeps working.</div>`;
  ebox.querySelector("#emb-apply").addEventListener("click", async () => {
    await fetch("/api/models/embeddings", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: ebox.querySelector("#emb-enabled").checked,
        model: ebox.querySelector("#emb-model").value,
      }) });
    refreshStatus();
  });
  ebox.querySelector("#emb-preload").addEventListener("click", async () => {
    const response = await fetch("/api/models/embeddings/preload", { method: "POST" });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { detail = (await response.json()).detail || detail; } catch {}
      alert(`Embedding preload failed: ${detail}`);
    } else {
      ebox.querySelector("#emb-preload").textContent = "Loaded";
      setTimeout(() => ebox.querySelector("#emb-preload").textContent = "Preload", 1500);
    }
  });
}

/* --------------------------------- boot -------------------------------- */
(async function boot() {
  await refreshStatus();
  await refreshPairingInfo();
  await generatePairingCode();
  await refreshRunsInto($("#obs-run-select"));
  await refreshRunsInto($("#chat-run-select"));
  connectUiSocket();
  setInterval(refreshStatus, 5000);
  setInterval(refreshPairingRequests, 5000);
  setInterval(loadRelayConfig, 5000);
  loadRelayConfig();
  setInterval(() => { refreshRunsInto($("#obs-run-select")); refreshRunsInto($("#chat-run-select")); }, 15000);
})();
