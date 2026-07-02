"use strict";
// The face-agnostic core of tx remote, shared verbatim by mobile.html and desktop.html: token
// plumbing, feed state, text/markdown rendering, the details sheet, history paging, the reply +
// attach pipeline, the ask-the-assistant flow, and the live stream. Loaded BEFORE each face's
// inline script (top-level lets here are visible there — scripts share the global lexical
// environment), and the DOM ids it wires (#list, #turns, #composer, #askbar, #gate, …) exist in
// both faces' markup.
//
// Each face must define (this file calls them late-bound):
//   render()        — repaint the conversation list + whatever face chrome depends on it
//   renderThread()  — repaint the open thread
//   openThread(id)  — navigate to a conversation (mobile pushes the thread screen; desktop
//                     repaints the split-pane's selected row)
// and call boot() as its last line.
const $ = (id) => document.getElementById(id);

// ---- token (only enforced when the server has TX_REMOTE_TOKEN set) ------------------------------
// Remembered per browser; a 401 clears to the gate. EventSource can't set headers, so the token
// rides a query param on every request.
let token = "";
try { token = localStorage.getItem("tx-remote-token") || ""; } catch { /* private mode */ }
const withToken = (url) => token ? `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}` : url;
function showGate() {
  document.body.classList.add("gated");
  disconnectStream();
  $("gateInput").focus();
}
$("gateInput").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  token = $("gateInput").value.trim();
  try { localStorage.setItem("tx-remote-token", token); } catch { /* private mode */ }
  document.body.classList.remove("gated");
  boot();
});

// ---- state ---------------------------------------------------------------------------------------
let feed = null;      // the /api/inbox payload
let sel = null;       // open thread's session id
let shownFor = null;  // which thread the turns pane last rendered (drives autoscroll on switch)
const picked = new Set();   // sessions selected via the row circles — the ask-bar's "about" set
// Earlier dialogue, paged in as the user scrolls up. Belongs to ONE thread (id); prepended before
// the live feed's turns on render. `boundary` is the oldest ts the FEED covered when paging began
// — pages fetch strictly before it, and the live window only slides forward, so no overlap.
let older = { id: null, turns: [], more: true, loading: false, boundary: 0 };
function resetOlder() { older = { id: null, turns: [], more: true, loading: false, boundary: 0 }; }


function escapeHtml(text) {
  return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// Agent markdown, rendered inline (the bubble keeps pre-wrap, so line layout survives).
function renderMarkdown(text) {
  let s = escapeHtml(text);
  s = s.replace(/```[^\n`]*\n([\s\S]*?)```\n?/g, (_, body) => `<span class="mdblock">${body.replace(/\n$/, "")}</span>`);
  s = s.replace(/^(#{1,4})\s+(.+)$/gm, (_, _h, title) => `<span class="mdhead">${title}</span>`);
  s = s.replace(/^&gt;\s?(.*)$/gm, '<span class="mdquote">▎$1</span>');
  s = s.replace(/`([^`\n]+)`/g, '<code class="mdcode">$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/(^|[^*\w])\*([^\s*][^*\n]*?[^\s*]|[^\s*])\*(?!\*)/g, "$1<i>$2</i>");
  s = s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  return s;
}
function stripMarkdown(text) {
  return String(text)
    .replace(/```[^\n`]*/g, "").replace(/[`*]+/g, "").replace(/^#{1,4}\s+/g, "")
    .replace(/\[([^\]]+)\]\((?:https?:[^)\s]+)\)/g, "$1");
}
const initial = (name) => (name || "?").replace(/[^a-zA-Z0-9]/g, "").slice(0, 2) || "?";
const typingHtml = (extra) => `<span class="typing${extra ? " " + extra : ""}"><span></span><span></span><span></span></span>`;

// `[img:<abs path>]` in a turn (the attach protocol — the agent Reads the path) renders as the
// actual image, served back by basename from the uploads dir. Runs on already-escaped HTML.
function renderImages(html) {
  return html.replace(/\[img:([^\]]+)\]/g, (_, path) => {
    const name = path.split("/").pop();
    return `<img class="imgmsg" src="/uploads/${encodeURIComponent(name)}" alt="attached image" />`;
  });
}

// A sent ask-assistant message starts with `<tx-about session='…' chat-id='…'/>` envelopes; in the
// bubble they render as small session-name chips instead of raw XML. Runs on already-escaped HTML
// (escapeHtml leaves single quotes alone, so the attribute quoting survives).
function renderAboutTags(html) {
  return html.replace(/&lt;tx-about session='([^']*)'(?: chat-id='[^']*')?\/&gt;\s*/g,
    (_, name) => `<span class="aboutchip">${name}</span>`);
}

// Fuzzy search over the conversation list: name prefix beats name substring beats an in-order
// character subsequence of the name, then the same over tags+preview. Score 0 hides the row;
// ties keep the Signal recency order.
function fuzzyScore(item, q) {
  const name = item.name.toLowerCase();
  const hay = `${name} ${(item.tags || []).join(" ").toLowerCase()} ${(item.preview || "").toLowerCase()}`;
  const subsequence = (text) => {
    let i = 0;
    for (const ch of text) if (ch === q[i]) i++;
    return i === q.length;
  };
  if (name.startsWith(q)) return 100;
  if (name.includes(q)) return 80;
  if (subsequence(name)) return 60;
  if (hay.includes(q)) return 40;
  if (subsequence(hay)) return 20;
  return 0;
}

// A centred time separator marks a >10 min gap instead of per-bubble stamps.
function timeSep(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const label = sameDay ? time : `${d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })} ${time}`;
  return `<div class="tsep">${label}</div>`;
}

// ---- session details sheet -------------------------------------------------------------------
// Tap the thread's avatar or name → a bottom sheet with the session's vitals, fetched fresh from
// /api/session: model + context usage (with a fill bar against the window), state/age, cwd +
// git branch, tags, lineage, chats, transcript size, and the launch prompt.
const fmtTokens = (n) => n >= 1000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}k` : String(n ?? "—");

function sheetRow(key, value, mono) {
  if (value === null || value === undefined || value === "") return "";
  return `<div class="row"><span class="k">${key}</span><span class="v${mono ? " mono" : ""}">${value}</span></div>`;
}

async function openSheet(id) {
  document.body.classList.add("sheet-open");
  $("sheet").innerHTML = `<div class="grab"></div><div id="sheetLoading">loading…</div>`;
  let d;
  try {
    const response = await fetch(withToken(`/api/session?id=${encodeURIComponent(id)}`), { cache: "no-store" });
    if (response.status === 401) { showGate(); return; }
    d = await response.json();
  } catch { d = null; }
  if (!d || !d.ok) { $("sheet").innerHTML = `<div class="grab"></div><div id="sheetLoading">failed to load details</div>`; return; }

  const pct = d.context_tokens && d.context_window ? Math.min(100, Math.round(d.context_tokens / d.context_window * 100)) : null;
  const barClass = pct === null ? "" : pct > 80 ? "hot" : pct > 55 ? "warn" : "";
  const context = d.context_tokens
    ? `${fmtTokens(d.context_tokens)} / ${fmtTokens(d.context_window)} (${pct}%)` +
      `<div class="ctxbar"><i class="${barClass}" style="width:${pct}%"></i></div>`
    : null;
  const chips = (d.tags || []).map((tag) => {
    const color = (d.tag_colors && d.tag_colors[tag]) || "var(--dim)";
    return `<span class="chip" style="--chip:${color}">${escapeHtml(tag)}</span>`;
  }).join("");

  $("sheet").innerHTML = `<div class="grab"></div>` +
    `<div class="shead s-${d.state}"><span class="avatar">${escapeHtml(initial(d.name))}</span>` +
    `<span class="sname">${escapeHtml(d.name)}</span></div>` +
    sheetRow("state", `${escapeHtml(d.state)}${d.activity_rel ? ` · active ${escapeHtml(d.activity_rel)} ago` : ""}`) +
    sheetRow("model", d.model ? escapeHtml(d.model) : null, true) +
    sheetRow("context", context) +
    sheetRow("last turn out", d.last_output_tokens ? `${fmtTokens(d.last_output_tokens)} tokens` : null) +
    sheetRow("tags", chips ? `<span class="chips">${chips}</span>` : null) +
    sheetRow("cwd", escapeHtml(d.cwd || ""), true) +
    sheetRow("branch", d.branch ? escapeHtml(d.branch) : null, true) +
    sheetRow("parent", d.parent ? escapeHtml(d.parent) : null) +
    sheetRow("attached", (d.attached || []).map(escapeHtml).join("<br>") || "detached") +
    sheetRow("chats", d.chats > 1 ? `${d.chats} (rollovers/forks)` : String(d.chats)) +
    sheetRow("started", d.created_rel ? `${escapeHtml(d.created_rel)} ago` : null) +
    sheetRow("engine", d.version ? `${escapeHtml(d.engine || "claude")} · Claude Code ${escapeHtml(d.version)}` : escapeHtml(d.engine || "")) +
    sheetRow("launch prompt", d.cmd ? escapeHtml(d.cmd) : null, true);
}
$("sheetWrap").addEventListener("click", (event) => {
  if (!event.target.closest("#sheet")) document.body.classList.remove("sheet-open");
});

// Page in earlier dialogue when the reader nears the top. The boundary is fixed on first page
// (the feed's oldest timestamp at that moment); each page fetches strictly before what we hold,
// and the scroll position is re-anchored so the content doesn't jump under the reader.
async function loadOlder() {
  const item = feed && feed.items.find((candidate) => candidate.id === sel);
  if (!item || older.loading) return;
  if (older.id === item.id && !older.more) return;
  if (older.id !== item.id) {
    resetOlder();
    older.id = item.id;
    older.boundary = (item.turns.find((turn) => turn.ts) || {}).ts || 0;
    if (!older.boundary) { older.more = false; return; }
  }
  const before = (older.turns.find((turn) => turn.ts) || {}).ts || older.boundary;
  older.loading = true;
  try {
    const response = await fetch(withToken(`/api/history?id=${encodeURIComponent(sel)}&before=${before}`), { cache: "no-store" });
    if (response.status === 401) { showGate(); return; }
    const result = await response.json();
    if (!result.ok) { older.more = false; return; }
    older.turns = result.turns.concat(older.turns);
    older.more = result.more && result.turns.length > 0;
    const turns = $("turns");
    const prevHeight = turns.scrollHeight, prevTop = turns.scrollTop;
    renderThread();
    turns.scrollTop = prevTop + (turns.scrollHeight - prevHeight);
  } catch {
    /* transient — the next scroll-to-top retries */
  } finally {
    older.loading = false;
  }
}

// POST one reply into the open session; on success clear the composer and append the optimistic
// bubble (the real turn arrives on the next SSE push). Shared by plain text and image sends.
async function postReply(text) {
  try {
    const response = await fetch(withToken("/api/reply"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: sel, text }),
    });
    if (response.status === 401) { showGate(); return false; }
    const result = await response.json();
    if (!result.ok) { $("hint").className = "show err"; $("hint").textContent = `send failed: ${result.error || "unknown"}`; return false; }
    $("input").value = "";
    $("send").disabled = true;
    autogrow();                       // collapse the grown pill back to one line
    const item = feed && feed.items.find((candidate) => candidate.id === sel);
    if (item) { item.turns.push({ who: "you", text, ts: Date.now() / 1000 }); renderThread(); }
    return true;
  } catch {
    $("hint").className = "show err"; $("hint").textContent = "send failed: server unreachable";
    return false;
  }
}

// A picked image stages in the composer (thumbnail + ✕) and goes out WITH the message: on send
// it uploads to the host, then the reply is `[caption] [img:<abs path>]` — the thread shows the
// picture; the agent opens the path with its Read tool.
let pendingImage = null;   // { name, dataUrl } staged in the composer

function updateSend() { $("send").disabled = !$("input").value.trim() && !pendingImage; }

function stageImage(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    pendingImage = { name: file.name, dataUrl: reader.result };
    $("attachImg").src = pendingImage.dataUrl;
    $("attach").hidden = false;
    updateSend();
  };
  reader.readAsDataURL(file);
}

function clearAttach() {
  pendingImage = null;
  $("attachImg").src = "";
  $("attach").hidden = true;
  updateSend();
}

async function uploadPending() {
  const response = await fetch(withToken("/api/upload"), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: pendingImage.name, data: pendingImage.dataUrl }),
  });
  if (response.status === 401) { showGate(); return null; }
  const result = await response.json();
  if (!result.ok) {
    $("hint").className = "show err";
    $("hint").textContent = `upload failed: ${result.error || "unknown"}`;
    return null;
  }
  return result.path;
}

async function sendReply() {
  const input = $("input");
  const caption = input.value.trim();
  if ((!caption && !pendingImage) || !sel) return;
  input.disabled = true;
  try {
    let text = caption;
    if (pendingImage) {
      $("hint").className = "show"; $("hint").textContent = "uploading image…";
      const path = await uploadPending();
      if (path === null) return;
      text = `${caption ? caption + " " : ""}[img:${path}]`;
    }
    if (await postReply(text)) {
      clearAttach();
      $("hint").className = ""; $("hint").textContent = "";
    }
  } catch {
    $("hint").className = "show err"; $("hint").textContent = "send failed: server unreachable";
  } finally {
    input.disabled = false;
    updateSend();
  }
}

// ---- ask the tx-assistant about the selected sessions --------------------------------------------
// The row circles pick the "about" set; the bar names its FIXED recipient (the tx-assistant) and
// the picked sessions, so what you send and to whom is never ambiguous. The server prefixes the
// message with one `<tx-about session chat-id/>` envelope per picked session.
function renderAskbar() {
  if (feed) {
    for (const id of [...picked]) {                 // a selected session exited — drop it
      if (!feed.items.some((item) => item.id === id)) picked.delete(id);
    }
  }
  const names = feed ? feed.items.filter((item) => picked.has(item.id)).map((item) => item.name) : [];
  $("askbar").hidden = names.length === 0;
  $("askAbout").textContent = names.length ? `about ${names.join(", ")}` : "";
  updateAskSend();
}

function updateAskSend() { $("askSend").disabled = !$("askInput").value.trim() || picked.size === 0; }

async function sendAsk() {
  const input = $("askInput"), hint = $("askHint");
  const text = input.value.trim();
  if (!text || picked.size === 0) return;
  input.disabled = true;
  try {
    const response = await fetch(withToken("/api/ask-assistant"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: [...picked], text }),
    });
    if (response.status === 401) { showGate(); return; }
    const result = await response.json();
    if (!result.ok) { hint.className = "show err"; hint.textContent = `send failed: ${result.error || "unknown"}`; return; }
    input.value = "";
    picked.clear();
    hint.className = ""; hint.textContent = "";
    autogrow(input);
    // Follow the question to its answer: refetch (the assistant may have just been spawned and
    // not yet pushed) and open its conversation so the reply lands in front of you.
    await fetchOnce();
    if (feed && feed.items.some((item) => item.id === result.assistant_id)) {
      openThread(result.assistant_id);
    }
  } catch {
    hint.className = "show err"; hint.textContent = "send failed: server unreachable";
  } finally {
    input.disabled = false;
    updateAskSend();
  }
}

// ---- events --------------------------------------------------------------------------------------
$("list").addEventListener("click", (event) => {
  const conv = event.target.closest(".conv");
  if (!conv) return;
  if (event.target.closest(".pickwrap")) {          // the circle toggles selection, never opens
    if (picked.has(conv.dataset.id)) picked.delete(conv.dataset.id);
    else picked.add(conv.dataset.id);
    render();
    return;
  }
  openThread(conv.dataset.id);
});
$("turns").addEventListener("scroll", () => {
  if ($("turns").scrollTop < 80) loadOlder();
});
// Tap an option → answer the live dialog. The server re-verifies the dialog is still up and
// still the same tool_use before any key is sent; a stale tap becomes a harmless error hint.
$("turns").addEventListener("click", async (event) => {
  const optionButton = event.target.closest(".qopt");
  if (!optionButton || !sel) return;
  const card = optionButton.closest(".qcard");
  const item = feed && feed.items.find((candidate) => candidate.id === sel);
  optionButton.disabled = true;
  try {
    const response = await fetch(withToken("/api/answer"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: sel, tool_use_id: card.dataset.tuid, option: Number(optionButton.dataset.opt) }),
    });
    if (response.status === 401) { showGate(); return; }
    const result = await response.json();
    if (!result.ok) {
      $("hint").className = "show err";
      $("hint").textContent = result.error || "answer failed";
      return;
    }
    // Optimistic: show the chosen option as your turn; the real state flips on the next push.
    const label = optionButton.querySelector(".qlabel").textContent;
    if (item) { item.turns.push({ who: "you", text: label, ts: Date.now() / 1000 }); item.blocked = null; item.reason = "working"; renderThread(); }
  } catch {
    $("hint").className = "show err";
    $("hint").textContent = "answer failed: server unreachable";
  }
});
$("send").addEventListener("click", sendReply);
// The composer pill grows with its content, Messenger-style: height tracks scrollHeight up to
// the CSS max, after which it scrolls internally. Re-run on every edit and after a send clears it.
// Shared by the thread composer (the default) and the ask-bar's.
function autogrow(input = $("input")) {
  input.style.height = "auto";
  const capped = Math.min(input.scrollHeight, 132);
  input.style.height = capped + "px";
  input.style.overflowY = input.scrollHeight > 132 ? "auto" : "hidden";
}
$("input").addEventListener("keydown", (event) => {
  // Enter sends (the iOS "send" key fires this); Shift+Enter keeps the newline.
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendReply(); }
});
$("input").addEventListener("input", () => { updateSend(); autogrow(); });
$("askSend").addEventListener("click", sendAsk);
$("askInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendAsk(); }
});
$("askInput").addEventListener("input", () => { updateAskSend(); autogrow($("askInput")); });
$("plus").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", () => {
  stageImage($("file").files[0]);
  $("file").value = "";                 // re-picking the same photo must fire change again
});
$("attachX").addEventListener("click", clearAttach);
$("search").addEventListener("input", () => render());

// ---- live stream ----------------------------------------------------------------------------------
let stream = null;
function connectStream() {
  if (stream) return;
  stream = new EventSource(withToken("/api/stream"));
  stream.onopen = () => $("liveDot").classList.add("on");
  stream.onerror = () => $("liveDot").classList.remove("on");   // EventSource auto-reconnects
  stream.addEventListener("inbox", (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    feed = data;
    render();
  });
}
function disconnectStream() {
  if (!stream) return;
  stream.close();
  stream = null;
  $("liveDot").classList.remove("on");
}

async function fetchOnce() {
  try {
    const response = await fetch(withToken("/api/inbox"), { cache: "no-store" });
    if (response.status === 401) { showGate(); return; }
    feed = await response.json();
    render();
  } catch {
    $("listEmpty").hidden = false;
    $("listEmpty").textContent = "server unreachable";
  }
}

// Browsers throttle or freeze background tabs (a phone tab constantly) — resync on every return
// to visibility.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") { connectStream(); fetchOnce(); }
});

function boot() { fetchOnce(); connectStream(); }
