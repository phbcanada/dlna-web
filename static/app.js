/*
 * Copyright (c) 2026 Paul H. Breslin
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Lesser General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public License
 * along with this program. If not, see <https://www.gnu.org/licenses/>.
 */

// app.js -- vanilla JS, no build step. Talks to the Flask REST API and
// listens on /api/stream (SSE) for live updates so the queue/status panels
// never need polling once the page is open.

const el = (id) => document.getElementById(id);

// ---------------------------------------------------------------------
// Small fetch helpers
// ---------------------------------------------------------------------
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body = null;
  try { body = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) {
    const msg = (body && body.error) ? body.error : `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return body;
}
const apiGet = (path) => api(path);
const apiPost = (path, data) => api(path, { method: "POST", body: JSON.stringify(data || {}) });

// ---------------------------------------------------------------------
// Toasts
// ---------------------------------------------------------------------
function toast(message, isError = false) {
  const stack = el("toast-stack");
  const t = document.createElement("div");
  t.className = "toast" + (isError ? " error" : "");
  t.textContent = message;
  stack.appendChild(t);
  setTimeout(() => t.remove(), 5000);
}

// ---------------------------------------------------------------------
// Console / log panel
// ---------------------------------------------------------------------
function formatTs(ts) {
  const d = new Date(ts * 1000);
  return d.toTimeString().slice(0, 8);
}

function appendLogLine(entry) {
  const body = el("console-body");
  const line = document.createElement("div");
  line.className = `log-line ${entry.level}`;
  line.innerHTML =
    `<span class="ts">${formatTs(entry.ts)}</span>` +
    `<span class="lvl">${entry.level}</span>` +
    `<span class="msg"></span>`;
  line.querySelector(".msg").textContent = `${entry.logger}: ${entry.message}`;
  body.appendChild(line);
  // Cap DOM growth; keep it snappy on a Pi.
  while (body.children.length > 400) body.removeChild(body.firstChild);
  body.scrollTop = body.scrollHeight;
}

el("console-toggle").addEventListener("click", () => {
  const consoleEl = el("console");
  const splitterEl = el("console-splitter");
  const collapsing = !consoleEl.classList.contains("collapsed");
  if (collapsing) {
    // Remember the height so re-expanding restores it rather than
    // snapping back to the default.
    lastConsoleHeight = consoleEl.getBoundingClientRect().height || lastConsoleHeight;
    consoleEl.classList.add("collapsed");
    splitterEl.classList.add("hidden");
  } else {
    consoleEl.classList.remove("collapsed");
    splitterEl.classList.remove("hidden");
    setConsoleHeight(lastConsoleHeight);
  }
});

// ---------------------------------------------------------------------
// Console resize (drag the splitter above it)
// ---------------------------------------------------------------------
const CONSOLE_MIN_HEIGHT = 90;          // header + a couple of visible lines
const CONSOLE_DEFAULT_HEIGHT = 240;

let lastConsoleHeight = CONSOLE_DEFAULT_HEIGHT;

// The console's max height is however much room is left after the topbar,
// the splitter, and the browse/queue panels' own minimum height (set in
// CSS on .main-grid) -- read from the actual layout rather than a flat
// percentage of the viewport, so dragging it as tall as it'll go can never
// itself push the page past the viewport (which was the original bug).
function getConsoleMaxHeight() {
  const topbarH = el("topbar").getBoundingClientRect().height;
  const splitterH = el("console-splitter").getBoundingClientRect().height;
  const mainGridFloor = parseFloat(getComputedStyle(el("main-grid")).minHeight) || 200;
  return Math.max(CONSOLE_MIN_HEIGHT, window.innerHeight - topbarH - splitterH - mainGridFloor);
}

function setConsoleHeight(px) {
  const maxH = getConsoleMaxHeight();
  const clamped = Math.min(Math.max(px, CONSOLE_MIN_HEIGHT), maxH);
  el("console").style.height = `${clamped}px`;
  lastConsoleHeight = clamped;
}

(function initConsoleResize() {
  const splitterEl = el("console-splitter");
  const consoleEl = el("console");
  let dragging = false;
  let dragStartY = 0;
  let dragStartHeight = 0;

  setConsoleHeight(CONSOLE_DEFAULT_HEIGHT);

  splitterEl.addEventListener("pointerdown", (e) => {
    if (consoleEl.classList.contains("collapsed")) return;
    dragging = true;
    dragStartY = e.clientY;
    dragStartHeight = consoleEl.getBoundingClientRect().height;
    splitterEl.setPointerCapture(e.pointerId);
    splitterEl.classList.add("dragging");
    document.body.style.userSelect = "none";
  });

  splitterEl.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    // Dragging the handle up (smaller clientY) grows the console.
    const delta = dragStartY - e.clientY;
    setConsoleHeight(dragStartHeight + delta);
  });

  const stopDrag = () => {
    if (!dragging) return;
    dragging = false;
    splitterEl.classList.remove("dragging");
    document.body.style.userSelect = "";
  };
  splitterEl.addEventListener("pointerup", stopDrag);
  splitterEl.addEventListener("pointercancel", stopDrag);

  // Re-clamp on window resize so a previously-dragged height can't leave
  // the console taller than the (now smaller) viewport allows.
  window.addEventListener("resize", () => {
    if (!consoleEl.classList.contains("collapsed")) {
      setConsoleHeight(consoleEl.getBoundingClientRect().height);
    }
  });
})();

// ---------------------------------------------------------------------
// Renderer picker dropdown -- the media server is fixed at deploy time
// (see the static "MEDIA SERVER" chip above), so this is renderer-only.
// ---------------------------------------------------------------------
function openPicker() {
  el("picker-title").textContent = "Renderers";
  el("device-picker").classList.remove("hidden");
  el("picker-manual-url").value = "";
  rescanPicker();
}

function closePicker() {
  el("device-picker").classList.add("hidden");
}

el("renderer-chip").addEventListener("click", () => openPicker());
el("picker-close").addEventListener("click", closePicker);
el("picker-rescan").addEventListener("click", rescanPicker);

async function rescanPicker() {
  const list = el("picker-list");
  list.innerHTML = `<div class="picker-empty">Scanning\u2026</div>`;
  try {
    const data = await apiGet("/api/renderers/discover");
    const items = data.renderers;
    if (!items || items.length === 0) {
      list.innerHTML = `<div class="picker-empty">Nothing found on the network. Devices that are asleep or powered off won't appear -- try again once it's on, or paste its URL below.</div>`;
      return;
    }
    list.innerHTML = "";
    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "picker-item";
      const name = item.friendly_name || item.desc_url;
      row.innerHTML = `<span class="name"></span><span class="url"></span>`;
      row.querySelector(".name").textContent = name;
      row.querySelector(".url").textContent = item.desc_url;
      row.addEventListener("click", () => selectRenderer(item.desc_url));
      list.appendChild(row);
    });
  } catch (e) {
    list.innerHTML = `<div class="picker-empty">Scan failed: ${e.message}</div>`;
  }
}

el("picker-manual-go").addEventListener("click", () => {
  const url = el("picker-manual-url").value.trim();
  if (url) selectRenderer(url);
});

async function selectRenderer(descUrl) {
  try {
    await apiPost("/api/renderers/select", { desc_url: descUrl });
    toast("Connected to renderer.");
    await refreshQueue();
    closePicker();
    await refreshStatus();
  } catch (e) {
    toast(`Could not connect: ${e.message}`, true);
  }
}

function setChip(kind, connected, label) {
  const led = el(`${kind}-led`);
  const value = el(`${kind}-value`);
  led.classList.remove("on", "off-state");
  if (connected) {
    led.classList.add("on");
  } else {
    led.classList.add("off-state");
  }
  value.textContent = label;
}

// ---------------------------------------------------------------------
// Browse panel
// ---------------------------------------------------------------------
function renderBreadcrumb(breadcrumb) {
  const bc = el("breadcrumb");
  bc.innerHTML = "";
  breadcrumb.forEach((crumb, i) => {
    const isLast = i === breadcrumb.length - 1;
    const span = document.createElement("span");
    span.className = "crumb";
    span.textContent = crumb.title || "Root";
    if (!isLast) {
      span.addEventListener("click", () => navigateBackTo(breadcrumb.length - 1 - i));
    } else {
      span.style.color = "var(--text)";
      span.style.cursor = "default";
    }
    bc.appendChild(span);
    if (!isLast) {
      const sep = document.createElement("span");
      sep.className = "sep";
      sep.textContent = "/";
      bc.appendChild(sep);
    }
  });
}

async function navigateBackTo(steps) {
  try {
    for (let i = 0; i < steps; i++) {
      await apiPost("/api/browse/back");
    }
    await refreshBrowse();
  } catch (e) {
    toast(`Navigation failed: ${e.message}`, true);
  }
}

// Icon per file's broad media category (from DIDL-Lite's upnp:class,
// classified server-side -- see dlnabrowser.py's classify_media_type()).
// Display only, same as everywhere else in this app: never gates what
// can be queued, just which glyph shows next to it.
const MEDIA_TYPE_ICONS = {
  audio: "\u{1F3B5}",        // musical note
  video: "\u{1F3AC}",        // clapper board
  image: "\u{1F5BC}\uFE0F",  // framed picture
  other: "\u{1F4C4}",        // page facing up
};

function iconForMediaType(mediaType) {
  return MEDIA_TYPE_ICONS[mediaType] || MEDIA_TYPE_ICONS.other;
}

function setAddFolderButtonState(fileCount, limit) {
  const btn = el("add-folder-btn");
  if (fileCount === null || fileCount === undefined) {
    btn.disabled = true;
    btn.title = "";
    return;
  }
  if (fileCount === 0) {
    btn.disabled = true;
    btn.title = "This folder has no tracks directly in it.";
  } else if (fileCount > limit) {
    btn.disabled = true;
    btn.title = `This folder has ${fileCount} tracks, which exceeds the ${limit}-track queue limit.`;
  } else {
    btn.disabled = false;
    btn.title = "";
  }
}

function renderBrowseList(data) {
  const items = data.items;
  const list = el("browse-list");
  list.innerHTML = "";
  setAddFolderButtonState(data.file_count, data.queue_track_limit);
  if (!items || items.length === 0) {
    list.innerHTML = `<div class="list-empty">This folder is empty.</div>`;
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = `row ${item.type}`;
    if (item.type === "folder") {
      row.innerHTML =
        `<span class="row-icon">\u{1F4C1}</span>` +
        `<span class="row-title"></span>`;
      row.querySelector(".row-title").textContent = item.title;
      row.querySelector(".row-title").addEventListener("click", () => enterFolder(item));

      // Direct-children-only track count, hidden entirely (not just
      // disabled) above the queue limit -- queueing an oversized folder
      // from a listing isn't offered at all, same rule "Queue All" enforces
      // for the currently-browsed folder.
      if (item.track_count > 0 && item.track_count <= data.queue_track_limit) {
        const queueBtn = document.createElement("button");
        queueBtn.className = "row-queue-folder";
        const trackWord = item.track_count === 1 ? "track" : "tracks";
        queueBtn.title = `Queue ${item.track_count} ${trackWord} from "${item.title}"`;
        queueBtn.innerHTML = `${item.track_count} \u2795\uFE0F`;
        queueBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          queueFolder(item);
        });
        row.appendChild(queueBtn);
      }
    } else {
      row.innerHTML =
        `<span class="row-icon">${iconForMediaType(item.media_type)}</span>` +
        `<span class="row-title"></span>`;
      row.querySelector(".row-title").textContent = item.title;
      row.querySelector(".row-title").addEventListener("click", () => addToQueue(item));
    }
    list.appendChild(row);
  });
}

async function queueFolder(item) {
  try {
    const data = await apiPost("/api/queue/add_folder", { id: item.id, title: item.title });
    toast(`Queued ${data.added} track(s) from "${item.title}".`);
  } catch (e) {
    toast(`Could not queue "${item.title}": ${e.message}`, true);
  }
}

async function enterFolder(item) {
  try {
    const data = await apiPost("/api/browse/enter", { id: item.id, title: item.title });
    renderBreadcrumb(data.breadcrumb);
    renderBrowseList(data);
  } catch (e) {
    toast(`Could not open folder: ${e.message}`, true);
  }
}

async function refreshBrowse() {
  try {
    const data = await apiGet("/api/browse/current");
    renderBreadcrumb(data.breadcrumb);
    renderBrowseList(data);
  } catch (e) {
    el("browse-list").innerHTML = `<div class="list-empty">Connect a media server to browse your music.</div>`;
    el("breadcrumb").innerHTML = "";
    setAddFolderButtonState(null, null);
  }
}

async function addToQueue(item) {
  try {
    await apiPost("/api/queue/add", { id: item.id, title: item.title, uri: item.uri });
  } catch (e) {
    toast(`Could not queue "${item.title}": ${e.message}`, true);
  }
}

el("add-folder-btn").addEventListener("click", async () => {
  try {
    const data = await apiPost("/api/queue/add_current_folder");
    toast(`Queued ${data.added} track(s).`);
  } catch (e) {
    toast(`Could not queue folder: ${e.message}`, true);
  }
});

// ---------------------------------------------------------------------
// Queue panel
// ---------------------------------------------------------------------
function renderQueue(snapshot) {
  const list = el("queue-list");
  list.innerHTML = "";
  // Independent of whether the queue itself is empty, so this must run
  // before the early-return below.
  el("shuffle-btn").classList.toggle("active", !!snapshot.shuffle);
  if (!snapshot.queue || snapshot.queue.length === 0) {
    list.innerHTML = `<div class="list-empty">Queue is empty. Browse and click "+ Queue" to add tracks.</div>`;
    return;
  }
  let activeRow = null;
  snapshot.queue.forEach((track, idx) => {
    const row = document.createElement("div");
    row.className = "row queue-row" + (idx === snapshot.current_idx ? " active" : "");
    row.innerHTML =
      `<span class="row-index"></span>` +
      `<span class="row-icon">${idx === snapshot.current_idx ? "\u25B6" : "\u{1F3B5}"}</span>` +
      `<span class="row-title"></span>` +
      `<button class="row-remove" title="Remove from queue">\u{1F5D1}\uFE0F</button>`;
    row.querySelector(".row-index").textContent = idx + 1;
    row.querySelector(".row-title").textContent = track.title;
    row.querySelector(".row-title").addEventListener("click", () => playAt(idx));
    row.querySelector(".row-remove").addEventListener("click", (e) => {
      e.stopPropagation();
      removeFromQueue(idx);
    });
    list.appendChild(row);
    if (idx === snapshot.current_idx) {
      activeRow = row;
    }
  });

  if (activeRow) {
    // block: "nearest" is a no-op if the row is already fully visible --
    // safe to call on every render rather than only when the current
    // track actually changed, no manual visibility check needed.
    activeRow.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

async function removeFromQueue(idx) {
  try {
    // Render immediately from the response (same pattern as playAt) so
    // the row disappears and the highlight/now-playing update without
    // waiting on the SSE round-trip.
    const data = await apiPost("/api/queue/remove", { index: idx });
    renderQueue(data);
  } catch (e) {
    toast(`Could not remove track: ${e.message}`, true);
  }
}

async function playAt(idx) {
  try {
    // The endpoint already returns the fresh snapshot -- render it directly
    // instead of waiting on the SSE round-trip, so the highlight moves the
    // instant the click registers.
    const data = await apiPost("/api/queue/play_at", { index: idx });
    renderQueue(data);
  } catch (e) {
    toast(`Could not play track: ${e.message}`, true);
  }
}

async function refreshQueue() {
  try {
    const data = await apiGet("/api/queue");
    renderQueue(data);
  } catch (e) { /* ignore */ }
}

el("queue-clear-btn").addEventListener("click", async () => {
  try { await apiPost("/api/queue/clear"); } catch (e) { toast(e.message, true); }
});

// ---------------------------------------------------------------------
// Transport controls
// ---------------------------------------------------------------------
el("prev-btn").addEventListener("click", () => apiPost("/api/queue/prev").catch((e) => toast(e.message, true)));
el("next-btn").addEventListener("click", () => apiPost("/api/queue/next").catch((e) => toast(e.message, true)));
el("stop-btn").addEventListener("click", () => apiPost("/api/queue/stop").catch((e) => toast(e.message, true)));
el("playpause-btn").addEventListener("click", () => apiPost("/api/queue/toggle").catch((e) => toast(e.message, true)));

el("shuffle-btn").addEventListener("click", async () => {
  const currentlyEnabled = el("shuffle-btn").classList.contains("active");
  try {
    // Render immediately from the response, same pattern as playAt/
    // removeFromQueue, so the button's state flips the instant the
    // click registers rather than waiting on the SSE round-trip.
    const data = await apiPost("/api/queue/shuffle", { enabled: !currentlyEnabled });
    renderQueue(data);
  } catch (e) {
    toast(`Could not toggle shuffle: ${e.message}`, true);
  }
});

// ---------------------------------------------------------------------
// Playback tick (position, duration, LED bar, transport state)
// ---------------------------------------------------------------------
const LED_SEGMENTS = 40;
let ledBarBuilt = false;

function buildLedBar() {
  const bar = el("led-bar");
  bar.innerHTML = "";
  for (let i = 0; i < LED_SEGMENTS; i++) {
    const seg = document.createElement("span");
    seg.className = "seg";
    bar.appendChild(seg);
  }
  ledBarBuilt = true;
}

function toSeconds(hms) {
  if (!hms) return 0;
  const parts = hms.split(":").map((p) => parseInt(p, 10) || 0);
  if (parts.length !== 3) return 0;
  return parts[0] * 3600 + parts[1] * 60 + parts[2];
}

function applyTick(tick) {
  if (!ledBarBuilt) buildLedBar();

  el("now-playing-title").textContent = tick.connected
    ? (tick.title && tick.title !== "None" ? tick.title : "Nothing playing")
    : "Renderer offline";

  el("position-code").textContent = tick.position || "00:00:00";
  el("duration-code").textContent = tick.duration || "00:00:00";
  el("transport-state").textContent = tick.connected ? (tick.transport_state || "UNKNOWN") : "OFFLINE";

  const cur = toSeconds(tick.position);
  const total = toSeconds(tick.duration);
  const pct = total > 0 ? cur / total : 0;
  const filledCount = Math.round(pct * LED_SEGMENTS);
  const segs = el("led-bar").children;
  for (let i = 0; i < segs.length; i++) {
    segs[i].classList.toggle("filled", i < filledCount);
  }

  const playing = tick.transport_state === "PLAYING" || tick.transport_state === "TRANSITIONING";
  el("playpause-btn").textContent = playing ? "\u23F8" : "\u25B6";

  setChip(
    "renderer",
    tick.connected,
    tick.connected ? el("renderer-value").dataset.name || "Connected" : "Offline"
  );
}

// ---------------------------------------------------------------------
// Overall status bootstrap
// ---------------------------------------------------------------------
async function refreshStatus() {
  try {
    const data = await apiGet("/api/status");

    if (data.server.connected) {
      const label = (data.server.friendly_name && data.server.friendly_name !== "None")
        ? data.server.friendly_name
        : new URL(data.server.desc_url).hostname;
      setChip("server", true, label);
    } else {
      setChip("server", false, "Not connected");
    }

    if (data.renderer.friendly_name) {
      el("renderer-value").dataset.name = data.renderer.friendly_name;
      setChip("renderer", data.renderer.connected, data.renderer.friendly_name);
    } else {
      setChip("renderer", false, "Not connected");
    }

    renderQueue(data.queue);
  } catch (e) {
    console.error("status refresh failed", e);
  }
}

// ---------------------------------------------------------------------
// Playlist save/load modal
// ---------------------------------------------------------------------
let modalMode = null; // "save" | "load"

function openModal(mode) {
  modalMode = mode;
  el("modal-title").textContent = mode === "save" ? "Save queue as playlist" : "Load playlist";
  el("modal-input").value = "";
  el("modal-list").innerHTML = "";
  el("modal-backdrop").classList.remove("hidden");
  el("modal-input").focus();

  if (mode === "load") {
    apiGet("/api/playlists").then((data) => {
      const listEl = el("modal-list");
      (data.playlists || []).forEach((name) => {
        const item = document.createElement("div");
        item.className = "item";
        item.textContent = name;
        item.addEventListener("click", () => { el("modal-input").value = name; });
        listEl.appendChild(item);
      });
    }).catch(() => {});
  }
}

function closeModal() {
  el("modal-backdrop").classList.add("hidden");
  modalMode = null;
}

el("playlist-save-btn").addEventListener("click", () => openModal("save"));
el("playlist-load-btn").addEventListener("click", () => openModal("load"));
el("modal-cancel").addEventListener("click", closeModal);

el("modal-confirm").addEventListener("click", async () => {
  const name = el("modal-input").value.trim();
  if (!name) { closeModal(); return; }
  try {
    if (modalMode === "save") {
      await apiPost("/api/playlists/save", { name });
      toast(`Saved playlist "${name}".`);
    } else {
      await apiPost("/api/playlists/load", { name });
      toast(`Loaded playlist "${name}".`);
    }
  } catch (e) {
    toast(e.message, true);
  }
  closeModal();
});

// ---------------------------------------------------------------------
// Live event stream
// ---------------------------------------------------------------------
function connectStream() {
  const src = new EventSource("/api/stream");

  src.onmessage = (evt) => {
    let payload;
    try { payload = JSON.parse(evt.data); } catch (e) { return; }

    switch (payload.type) {
      case "log":
        appendLogLine(payload.data);
        break;
      case "queue_status":
        renderQueue(payload.data);
        break;
      case "playback_tick":
        applyTick(payload.data);
        break;
      case "now_playing":
        if (!payload.data.ok) {
          toast(`Could not play "${payload.data.title}" -- renderer may be offline.`, true);
        }
        break;
      case "renderer_status":
        el("renderer-value").dataset.name = payload.data.friendly_name || "Connected";
        setChip("renderer", payload.data.connected, payload.data.friendly_name || (payload.data.connected ? "Connected" : "Offline"));
        toast(
          payload.data.connected
            ? `Renderer "${payload.data.friendly_name}" is online.`
            : `Renderer "${payload.data.friendly_name}" went offline.`,
          !payload.data.connected
        );
        break;
      case "library_status":
        applyLibraryStatus(payload.data);
        if (payload.data.ready) revealApp();
        break;
      default:
        break;
    }
  };

  src.onerror = () => {
    // EventSource auto-reconnects; just note it in the console hint.
    el("console-hint").textContent = "stream reconnecting\u2026";
  };
  src.onopen = () => {
    el("console-hint").textContent = "";
    // Resync immediately on every (re)connection, not just the first one.
    // If the backend process was restarted while this tab was open, this
    // is what catches it even in the worst case -- the reconnect landing
    // *after* the new process already finished indexing, so no "not
    // ready"/"ready" events were ever seen by this tab to trigger a
    // reveal in the first place. checkLibraryStatus() is a harmless
    // no-op if nothing has actually changed.
    checkLibraryStatus();
    // Also resync general app status (queue, renderer, position). If the
    // connection was dropped for a while -- e.g. a mobile browser
    // suspending a backgrounded tab -- any queue_status events published
    // during that gap are gone for good; there's no replay for a
    // reconnecting client. The "Now Playing" bar self-corrects on its own
    // (it's polled every second regardless of what changed), but the
    // queue panel only ever updates in response to a change event, so
    // without this it can be left showing stale content indefinitely.
    refreshStatus();
  };
}

// A tab returning to the foreground is the actual signal that matters
// for the "left the queue stale after a while backgrounded" case (e.g.
// iPad Safari, switching apps for a stretch while tracks keep playing) --
// more direct than relying on the SSE connection's own reconnect timing,
// since some mobile browsers can suspend JS execution for a backgrounded
// tab without necessarily tearing the connection down in a way this page
// notices promptly. refreshStatus() is cheap and safe to call redundantly.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    refreshStatus();
  }
});

// ---------------------------------------------------------------------
// Indexing overlay -- shown until the startup library crawl finishes
// (state.library_ready on the backend). Driven by an initial fetch plus
// live "library_status" SSE events, with a polling fallback in case the
// stream is slow to connect or an event gets missed.
// ---------------------------------------------------------------------
let libraryRevealed = false;
let libraryPollTimer = null;

function applyLibraryStatus(data) {
  const overlay = el("indexing-overlay");
  const card = overlay.querySelector(".indexing-card");

  const srv = data.server || {};
  if (srv.connected) {
    const label = (srv.friendly_name && srv.friendly_name !== "None")
      ? srv.friendly_name
      : (srv.desc_url ? new URL(srv.desc_url).hostname : "Connected");
    setChip("server", true, label);
  } else {
    setChip("server", false, "Not connected");
  }

  if (data.ready) {
    // Deliberately NOT hiding the overlay here -- revealApp() does that,
    // and only after the real content (refreshStatus/refreshBrowse) has
    // actually finished loading. Hiding it eagerly here would create a
    // brief window with the overlay gone but the browse panel still
    // empty, since those calls are async and haven't resolved yet.
    return;
  }

  // The backend is (re)indexing -- e.g. it just restarted while this tab
  // was already open and revealed. Un-latch so a subsequent "ready"
  // signal can properly re-trigger revealApp() instead of being silently
  // dropped by its one-shot guard, and make sure the poll fallback is
  // running in case the live SSE "ready" event lands before this tab's
  // stream reconnects (missing it entirely).
  libraryRevealed = false;
  startLibraryStatusPolling();

  overlay.classList.remove("hidden");

  if (data.error) {
    card.classList.add("error");
    el("indexing-title").textContent = "Configuration problem";
    el("indexing-detail").textContent = data.error;
    el("indexing-progress").textContent = "";
    return;
  }

  card.classList.remove("error");
  if (srv.connected) {
    el("indexing-title").textContent = "Indexing your library\u2026";
    el("indexing-detail").textContent = `Reading ${srv.friendly_name || "the media server"}\u2019s folder tree\u2026`;
    const p = data.progress || {};
    el("indexing-progress").textContent =
      `${p.folders_scanned || 0} folders scanned, ${p.tracks_found || 0} tracks found`;
  } else {
    el("indexing-title").textContent = "Starting up\u2026";
    el("indexing-detail").textContent = "Connecting to the media server\u2026";
    el("indexing-progress").textContent = "";
  }
}

async function checkLibraryStatus() {
  try {
    const data = await apiGet("/api/library_status");
    applyLibraryStatus(data);
    return data;
  } catch (e) {
    return null;
  }
}

async function revealApp() {
  if (libraryRevealed) return;
  libraryRevealed = true;
  if (libraryPollTimer) {
    clearInterval(libraryPollTimer);
    libraryPollTimer = null;
  }
  await refreshStatus();
  await refreshBrowse();
  // Only now -- content is actually loaded, so there's no gap between
  // the overlay disappearing and the real browse panel appearing.
  el("indexing-overlay").classList.add("hidden");
}

function startLibraryStatusPolling() {
  if (libraryPollTimer) return;
  libraryPollTimer = setInterval(async () => {
    const data = await checkLibraryStatus();
    if (data && data.ready) await revealApp();
  }, 1500);
}

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------
(async function init() {
  buildLedBar();
  try {
    const logsData = await apiGet("/api/logs");
    (logsData.logs || []).forEach(appendLogLine);
  } catch (e) { /* ignore */ }

  // Safe to open immediately: /api/stream is reachable even while the
  // library is still indexing, so the console shows live crawl progress
  // and the "library_status" event can reveal the app as soon as it's
  // ready without waiting on the poll interval below.
  connectStream();

  const initial = await checkLibraryStatus();
  if (initial && initial.ready) {
    await revealApp();
  } else {
    startLibraryStatusPolling();
  }
})();
