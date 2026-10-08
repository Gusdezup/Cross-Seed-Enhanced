"use strict";

// ---------- utilitaires ----------
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const init = { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch { /* vide */ }
  if (!r.ok) throw new Error((data && data.detail) || `Erreur HTTP ${r.status}`);
  return data;
}

let toastTimer;
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("bad", bad);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 3500);
}

function fmtSize(b) {
  if (!b) return "";
  const u = ["o", "Ko", "Mo", "Go", "To"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(i >= 3 ? 1 : 0)} ${u[i]}`;
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function fmtDuration(s) {
  if (s == null) return "";
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}`;
}

// Couleur stable par tracker, calculée depuis son nom
const colorCache = new Map();
const lightMode = window.matchMedia("(prefers-color-scheme: light)");
function trackerColor(name) {
  const key = `${name}|${lightMode.matches}`;
  if (!colorCache.has(key)) {
    let h = 0;
    for (const ch of String(name).toLowerCase().replace(/\s*\(api\)\s*$/, "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    colorCache.set(key, lightMode.matches ? `hsl(${h % 360} 65% 38%)` : `hsl(${h % 360} 70% 66%)`);
  }
  return colorCache.get(key);
}

// ---------- état ----------
const state = {
  releases: [], trackers: {}, torrents: 0, selected: new Set(), expanded: null, limit: 200,
  settings: null, queue: null, tab: "releases",
};

// ---------- onglets ----------
function showTab(name) {
  if (!$(`#tab-${name}`)) name = "releases";
  state.tab = name;
  $$(".tab").forEach((t) => (t.hidden = t.id !== `tab-${name}`));
  $$(".rail a").forEach((a) => a.classList.toggle("on", a.dataset.tab === name));
  if (name === "queue") loadQueue();
  if (name === "pending") loadPending();
  if (name === "indexers") loadIndexers();
  if (name === "logs") startLogs();
  if (name === "settings") renderSettings();
  if (name !== "logs") stopLogs();
}
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

// ---------- santé ----------
async function loadHealth() {
  try {
    const s = await api("/api/status");
    state.restartAvailable = !!s.restart_available;
    // Lecture seule (instance de dev) : bandeau + boutons neutralisés par CSS (body.ro, body.ro-cfg)
    state.readonly = !!s.readonly;
    const cfgWrite = s.config_write !== false;
    state.configWrite = cfgWrite;
    document.body.classList.toggle("ro", state.readonly);
    document.body.classList.toggle("ro-cfg", !cfgWrite);
    const rb = $("#ro-banner");
    rb.hidden = !state.readonly;
    if (state.readonly) rb.textContent = "Lecture seule (XSE_READONLY) : aucune action sur cross-seed (jobs, recherches, redémarrage), la file de recherche ne part pas. "
      + (cfgWrite ? "Modifications de config.js autorisées (XSE_ALLOW_CONFIG_WRITE)." : "config.js n'est pas modifiable.");
    const q = $("#pill-qbit"), x = $("#pill-xs");
    q.className = `pill ${s.qbit.ok ? "ok" : "ko"}`;
    q.lastChild.textContent = s.qbit.ok ? `qBittorrent ${s.qbit.version}` : "qBittorrent injoignable";
    q.title = s.qbit.error || "";
    if (!state.xsRestarting) {   // pendant un redémarrage, la pastille affiche « redémarre… »
      x.className = `pill ${s.xs.ok ? "ok" : "ko"}`;
      x.lastChild.textContent = s.xs.ok ? "cross-seed en ligne" : "cross-seed injoignable";
      x.title = s.xs.error || "";
    }
  } catch { /* le serveur lui-même ne répond pas */ }
}

// ---------- jobs ----------
const JOB_LABELS = { rss: "Scan RSS", search: "Scan complet", inject: "Injection" };
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-job]");
  if (!b) return;
  b.disabled = true;
  try {
    const r = await api(`/api/jobs/${b.dataset.job}`, { method: "POST" });
    if (r.status < 400) toast(`${JOB_LABELS[b.dataset.job]} lancé(e) par cross-seed`);
    else if (r.status === 409) toast(`${JOB_LABELS[b.dataset.job]} déjà en cours`);
    else toast(`cross-seed a répondu ${r.status} : ${typeof r.body === "string" ? r.body : JSON.stringify(r.body)}`, true);
    if (b.dataset.job === "inject") setTimeout(loadPending, 8000);
  } catch (err) { toast(err.message, true); }
  b.disabled = false;
});

// ---------- releases ----------
// Préférences d'affichage propres à ce navigateur (tri, colonnes masquées)
const REL_COLS = [
  ["rules", "Priorité"], ["cat", "Catégorie"], ["size", "Taille"], ["added", "Ajoutée"], ["last", "Dernière recherche"], ["copies", "Trackers"],
];
const SORT_DEFAULT_DIR = { rules: "asc", name: "asc", category: "asc", size: "desc", added: "desc", last: "asc", seeds: "asc" };
function prefGet(k, dflt) { try { const v = localStorage.getItem(`xse.${k}`); return v ? JSON.parse(v) : dflt; } catch { return dflt; } }
function prefSet(k, v) { try { localStorage.setItem(`xse.${k}`, JSON.stringify(v)); } catch { /* stockage indisponible */ } }
state.relSort = prefGet("relSort", { key: "seeds", dir: "asc" });
if (!(state.relSort.key in SORT_DEFAULT_DIR)) state.relSort = { key: "seeds", dir: "asc" };
state.relHidden = new Set(prefGet("relHidden", []));

function fmtDay(iso) {
  if (!iso) return "";
  const d = new Date(typeof iso === "number" ? iso * 1000 : iso);
  if (isNaN(d)) return String(iso);
  return d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit", year: "2-digit" });
}
function fmtFull(iso) {
  if (!iso) return "";
  const d = new Date(typeof iso === "number" ? iso * 1000 : iso);
  return isNaN(d) ? String(iso) : d.toLocaleString("fr-FR");
}

function applyRelColumns() {
  const t = $("table.rel");
  for (const [k] of REL_COLS) t.classList.toggle(`hide-${k}`, state.relHidden.has(k));
  $("#rel-cols-menu").innerHTML = REL_COLS.map(([k, label]) =>
    `<label><input type="checkbox" data-col="${k}" ${state.relHidden.has(k) ? "" : "checked"}> ${esc(label)}</label>`).join("");
  for (const th of $$("table.rel th[data-sort]")) {
    const on = th.dataset.sort === state.relSort.key;
    th.classList.toggle("sorted", on);
    th.classList.toggle("desc", on && state.relSort.dir === "desc");
  }
}
async function loadReleases(refresh = false) {
  $("#rel-summary").textContent = "Chargement depuis qBittorrent…";
  try {
    const d = await api(`/api/releases${refresh ? "?refresh=1" : ""}`);
    state.releases = d.releases;
    state.trackers = d.trackers;
    state.torrents = d.torrents;
    const sel = $("#rel-tracker"), cur = sel.value;
    const labels = [...new Set(Object.values(d.trackers))].sort((a, b) => a.localeCompare(b));
    sel.innerHTML = `<option value="">Tous les trackers</option>` + labels.map((l) => `<option>${esc(l)}</option>`).join("");
    sel.value = labels.includes(cur) ? cur : "";
    const csel = $("#rel-cat"), ccur = csel.value;
    const cats = [...new Set(d.releases.map((r) => r.category))].sort((a, b) => a.localeCompare(b));
    csel.innerHTML = `<option value="">Toutes les catégories</option>` +
      cats.map((c) => `<option value="${esc(c)}">${c ? esc(c) : "(sans catégorie)"}</option>`).join("");
    csel.value = cats.includes(ccur) ? ccur : "";
    for (const k of [...state.selected]) if (!state.releases.some((r) => r.key === k)) state.selected.delete(k);
    $("#cnt-releases").textContent = d.releases.length;
    renderReleases();
    if (state.tab === "settings" && state.settings) { renderRules(); renderAliases(); }
  } catch (err) {
    $("#rel-summary").textContent = err.message;
    $("#rel-body").innerHTML = `<tr><td colspan="99" class="empty">${esc(err.message)}. Vérifie QBT_URL et QBT_APIKEY.</td></tr>`;
  }
}

function filteredReleases() {
  const q = $("#rel-q").value.trim().toLowerCase();
  const f = $("#rel-filter").value, tr = $("#rel-tracker").value, cat = $("#rel-cat").value;
  const catOn = $("#rel-cat").selectedIndex > 0;
  const terms = q.split(/\s+/).filter(Boolean);
  let list = state.releases.filter((r) => {
    if (f === "rules" && !r.rules.length) return false;
    if (f === "single" && r.seeds !== 1) return false;
    if (f === "available" && !r.available) return false;
    if (f === "noorig" && r.has_original) return false;
    if (f === "dup" && new Set(r.copies.map((c) => c.tracker)).size === r.copies.length) return false;
    if (tr && !r.copies.some((c) => c.tracker === tr)) return false;
    if (catOn && r.category !== cat) return false;
    if (terms.length) {
      const hay = (r.name + " " + r.copies.map((c) => c.tracker).join(" ")).toLowerCase();
      if (!terms.every((t) => hay.includes(t))) return false;
    }
    return true;
  });
  const { key, dir } = state.relSort;
  const val = {
    name: (r) => r.name.toLowerCase(),
    category: (r) => (r.category || "").toLowerCase(),
    // releases prioritaires d'abord, puis par nom de règle
    rules: (r) => (r.rules.length ? r.rules[0].toLowerCase() : "\uffff"),
    size: (r) => r.size,
    added: (r) => r.added_on,
    last: (r) => r.last_search || "",
    seeds: (r) => r.seeds,
  }[key] || ((r) => r.seeds);
  const sign = dir === "desc" ? -1 : 1;
  return list.sort((a, b) => {
    const x = val(a), y = val(b);
    // Valeurs vides (jamais cherchée, sans catégorie) : toujours en tête en ordre croissant
    const c = x < y ? -1 : x > y ? 1 : 0;
    return sign * c || a.name.localeCompare(b.name);
  });
}

const TSTATE = {
  seed: { icon: "✓", text: "déjà en seed" },
  available: { icon: "+", text: "trouvée par cross-seed, pas encore injectée" },
  nomatch: { icon: "–", text: "cherchée, rien trouvé" },
  never: { icon: "·", text: "jamais cherchée sur cet indexer" },
};
const MATCH_TEXT = {
  MATCH: "correspondance exacte", MATCH_SIZE_ONLY: "correspondance par la taille",
  MATCH_PARTIAL: "correspondance partielle (injectée seulement si matchMode le permet)",
};
function tchip(t) {
  const s = TSTATE[t.state];
  let tip = `${t.label} : ${s.text}`;
  if (t.state === "seed") tip += t.origin ? " (torrent d'origine)" : " (ajoutée par cross-seed)";
  if (t.match) tip += ` — ${MATCH_TEXT[t.match] || t.match}`;
  if (t.last_search) tip += ` — dernière recherche le ${fmtFull(t.last_search)}`;
  return `<span class="tchip ${t.state}" style="--c:${trackerColor(t.label)}" title="${esc(tip)}"><i>${s.icon}</i>${esc(t.label)}</span>`;
}
function chipsHtml(r) {
  return r.trackers.filter((t) => t.state !== "never").map(tchip).join("");
}

function renderReleases() {
  const list = filteredReleases();
  const shown = list.slice(0, state.limit);
  const ruleCount = state.releases.filter((r) => r.rules.length).length;
  $("#rel-summary").textContent =
    `${list.length} release${list.length > 1 ? "s" : ""} affichée${list.length > 1 ? "s" : ""} sur ${state.releases.length} ` +
    `(${state.torrents} torrents terminés, ${ruleCount} prioritaires).`;
  const body = $("#rel-body");
  if (!shown.length) {
    body.innerHTML = `<tr><td colspan="99" class="empty">Aucune release ne correspond à ce filtre.</td></tr>`;
  } else {
    body.innerHTML = shown.map((r) => {
      const flag = r.mode === "path"
        ? `<span class="rflag" title="Aucun torrent d'origine dans qBittorrent : toutes les copies sont des cross-seeds">· sans torrent d'origine</span>` : "";
      const row = `<tr data-key="${esc(r.key)}">
        <td class="c-check"><input type="checkbox" ${state.selected.has(r.key) ? "checked" : ""} aria-label="Sélectionner"></td>
        <td><span class="rname" title="Afficher le détail">${esc(r.name)}</span>${flag}</td>
        <td class="c-rules">${r.rules.map((n) => `<span class="tag rule">${esc(n)}</span>`).join("")}</td>
        <td class="c-cat">${esc(r.category || "—")}</td>
        <td class="c-size">${fmtSize(r.size)}</td>
        <td class="c-added" title="${esc(fmtFull(r.added_on))}">${fmtDay(r.added_on)}</td>
        ${r.last_search
          ? `<td class="c-last" title="${esc(fmtFull(r.last_search))}">${fmtDay(r.last_search)}</td>`
          : `<td class="c-last never">jamais</td>`}
        <td class="c-copies"><div class="copies">${chipsHtml(r)}</div></td>
        <td class="c-act"><button class="small" data-search>Chercher</button></td>
      </tr>`;
      return row + (state.expanded === r.key ? detailRow(r) : "");
    }).join("");
  }
  $("#rel-more").hidden = list.length <= state.limit;
  $("#rel-all").checked = shown.length > 0 && shown.every((r) => state.selected.has(r.key));
  updateSelbar();
}

function detailRow(r) {
  const byHash = new Map(r.copies.map((c) => [c.hash, c]));
  const rows = r.trackers.map((t) => {
    const copies = t.copies.map((h) => byHash.get(h)).filter(Boolean).map((c) =>
      `${c.cross_seed ? "ajoutée par cross-seed" : "torrent d'origine"} · ${esc(c.category || "sans catégorie")} · ` +
      `<span class="mono" title="${esc(c.hash)}">${esc(c.hash.slice(0, 10))}…</span>`).join("<br>");
    const extra = t.state === "available" && t.match ? ` <span class="muted">(${esc(MATCH_TEXT[t.match] || t.match)})</span>` : "";
    return `<tr>
      <td>${tchip(t)}</td>
      <td class="tstate ${t.state}">${TSTATE[t.state].text}${extra}</td>
      <td>${t.last_search ? fmtDate(t.last_search) : "—"}</td>
      <td>${copies || ""}</td>
    </tr>`;
  }).join("");
  const target = r.mode === "hash" ? `hash ${r.payload.infoHash.slice(0, 10)}…` : r.payload.path;
  return `<tr class="detail" data-detail="${esc(r.key)}"><td></td><td colspan="98">
    <h4>État par tracker</h4>
    <table class="mini"><tr><th>Tracker</th><th>État</th><th>Dernière recherche</th><th>Copie dans qBittorrent</th></tr>${rows}</table>
    <h4 style="margin-top:10px">Cible envoyée à cross-seed</h4><div class="mono">${esc(target)}</div>
  </td></tr>`;
}

function updateSelbar() {
  const n = state.selected.size;
  $("#rel-selbar").hidden = n === 0;
  $("#rel-selcount").textContent = `${n} release${n > 1 ? "s" : ""} sélectionnée${n > 1 ? "s" : ""}`;
}

async function enqueue(keys) {
  try {
    const r = await api("/api/search", { method: "POST", body: { keys } });
    if (r.added) toast(r.added === 1 ? "Recherche ajoutée en tête de file" : `${r.added} recherches ajoutées en tête de file`);
    if (r.skipped) toast(`${r.added} ajoutée(s), ${r.skipped} déjà dans la file`, !r.added);
    loadQueue();
  } catch (err) { toast(err.message, true); }
}

$("#rel-body").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-key]");
  if (!tr) return;
  const key = tr.dataset.key;
  if (e.target.matches("input[type=checkbox]")) {
    e.target.checked ? state.selected.add(key) : state.selected.delete(key);
    updateSelbar();
  } else if (e.target.closest("[data-search]")) {
    enqueue([key]);
  } else if (e.target.closest(".rname")) {
    state.expanded = state.expanded === key ? null : key;
    renderReleases();
  }
});
$("#rel-all").addEventListener("change", (e) => {
  for (const r of filteredReleases().slice(0, state.limit)) e.target.checked ? state.selected.add(r.key) : state.selected.delete(r.key);
  renderReleases();
});
$("#rel-search-sel").addEventListener("click", () => { enqueue([...state.selected]); state.selected.clear(); renderReleases(); });
$("#rel-clear-sel").addEventListener("click", () => { state.selected.clear(); renderReleases(); });
$("#rel-more").addEventListener("click", () => { state.limit += 200; renderReleases(); });
$("#rel-refresh").addEventListener("click", () => loadReleases(true));
let qTimer;
$("#rel-q").addEventListener("input", () => { clearTimeout(qTimer); qTimer = setTimeout(() => { state.limit = 200; renderReleases(); }, 150); });
$("table.rel thead").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (!th) return;
  const key = th.dataset.sort;
  state.relSort = state.relSort.key === key
    ? { key, dir: state.relSort.dir === "asc" ? "desc" : "asc" }
    : { key, dir: SORT_DEFAULT_DIR[key] || "asc" };
  prefSet("relSort", state.relSort);
  state.limit = 200;
  applyRelColumns();
  renderReleases();
});
$("#rel-cols-menu").addEventListener("change", (e) => {
  const k = e.target.dataset.col;
  if (!k) return;
  e.target.checked ? state.relHidden.delete(k) : state.relHidden.add(k);
  prefSet("relHidden", [...state.relHidden]);
  applyRelColumns();
});
document.addEventListener("click", (e) => { if (!e.target.closest("#rel-cols")) $("#rel-cols").open = false; });
applyRelColumns();
["#rel-filter", "#rel-tracker", "#rel-cat"].forEach((s) => $(s).addEventListener("change", () => { state.limit = 200; renderReleases(); }));

// ---------- file de recherche ----------
const SKIP_WORDS = [
  [/temporarily disabled indexers/g, "indexers en pause"],
  [/timestamps/g, "déjà cherchée récemment"],
  [/searchLimit/g, "limite de recherches"],
  [/excludeOlder/g, "torrent trop ancien"],
  [/, and |, | and /g, ", "],
];
function explainSkip(s) { return SKIP_WORDS.reduce((acc, [rx, fr]) => acc.replace(rx, fr), s); }

function resultHtml(i) {
  if (i.status === "pending") return `<span class="res-none">En attente</span>`;
  if (i.status === "running") return `<span class="res-none">${i.routed ? "Recherche routée en cours…" : "Recherche envoyée, lecture des logs…"}</span>`;
  if (i.status === "error" || i.status === "timeout") return `<span class="res-bad">${esc(i.error)}</span>`;
  const r = i.result || {};
  if (r.routed) return routedResultHtml(r);
  if (r.refused) {
    const why = /cross seed/i.test(r.refused) ? "ce torrent est lui-même un cross-seed" : r.refused;
    return `<span class="res-bad" title="${esc(r.refused)}">Refusée par cross-seed : ${esc(why)}</span>`;
  }
  const parts = [];
  if (r.injected.length) parts.push(`<span class="res-hit">${r.injected.length} injecté${r.injected.length > 1 ? "s" : ""} :</span> ` +
    r.injected.map((t) => `<span class="chip" style="--c:${trackerColor(t)}">${esc(t)}</span>`).join(" "));
  if (r.failed.length) parts.push(`<span class="res-bad">échec d'injection sur ${esc(r.failed.join(", "))}</span>`);
  if (!parts.length) {
    if (r.skipped && !r.found) parts.push(`<span class="res-warn">Sautée : ${esc(explainSkip(r.skipped))}</span>`);
    else parts.push(`<span class="res-none">${r.found ? `${r.found} trouvé(s), rien de nouveau` : "Rien trouvé"}</span>`);
  }
  return parts.join(" ");
}

const seededOrPaused = (r) => (r.paused || []).length + (r.seeded || []).length > 0;
function routedResultHtml(r) {
  const chips = (list) => [...new Set(list)].map((t) => `<span class="chip" style="--c:${trackerColor(t)}">${esc(t)}</span>`).join(" ");
  const parts = [];
  if (r.injected.length) parts.push(`<span class="res-hit">${r.injected.length} injecté${r.injected.length > 1 ? "s" : ""} :</span> ${chips(r.injected)}`);
  if (r.failed.length) parts.push(`<span class="res-bad">échec d'injection :</span> ${chips(r.failed)}`);
  if (r.exists.length) parts.push(`<span class="res-none">déjà en seed :</span> ${chips(r.exists)}`);
  if (!parts.length && !r.routed.length && seededOrPaused(r)) parts.push(`<span class="res-none">${(r.paused || []).length ? "Non cherchée : indexers en pause ou déjà en seed" : "Rien à chercher : déjà en seed sur tous les indexers routés"}</span>`);
  if (!parts.length) parts.push(`<span class="res-none">${r.candidates ? `${r.candidates} résultat${r.candidates > 1 ? "s" : ""} examiné${r.candidates > 1 ? "s" : ""}, aucun ne correspond` : "Rien trouvé"}</span>`);
  const paused = r.paused || [];
  const asked = r.routed.length;
  const seeded = r.seeded || [];
  const tip = `Recherche routée sur : ${r.routed.join(", ") || "aucun"}`
    + (seeded.length ? `\n\nDéjà en seed, non interrogés : ${seeded.join(", ")}` : "")
    + (paused.length ? `\n\nEn pause, non interrogés :\n${paused.join("\n")}` : "")
    + (r.errors.length ? `\n\nErreurs :\n${r.errors.join("\n")}` : "");
  let meta = `${asked} indexer${asked > 1 ? "s" : ""}`;
  if (paused.length) meta += ` · ${paused.length} en pause`;
  if (r.errors.length) meta += ` · ⚠ ${r.errors.length}`;
  return `<span class="res-line">${parts.join(" ")} <span class="res-meta" title="${esc(tip)}">${esc(meta)}</span></span>`;
}

async function loadQueue() {
  try { state.queue = await api("/api/queue"); } catch { return; }
  const q = state.queue;
  const cnt = $("#cnt-queue");
  cnt.textContent = q.pending || "";
  cnt.classList.toggle("hot", q.pending > 0);
  if (state.tab !== "queue") return;
  $("#q-pause").textContent = q.paused ? "Reprendre" : "Pause";
  renderQueueRules();
  let st;
  if (state.readonly) st = `Lecture seule : la file ne part pas, ${q.pending} recherche(s) en attente.`;
  else if (q.paused) st = `En pause, ${q.pending} recherche(s) en attente.`;
  else if (q.pending) st = `${q.pending} en attente. Prochaine recherche dans ${fmtDuration(q.next_in)}, fin estimée dans ${fmtDuration(q.eta_seconds)} (une toutes les ${q.delay} s).`;
  else st = `File vide. Une recherche est envoyée toutes les ${q.delay} s quand la file se remplit.`;
  $("#q-state").textContent = st;
  const order = { running: 0, pending: 1 };
  const items = [...q.items].sort((a, b) =>
    (order[a.status] ?? 2) - (order[b.status] ?? 2) || ((order[a.status] ?? 2) === 2 ? b.done - a.done : 0));
  const LBL = { pending: "en attente", running: "en cours", done: "terminée", error: "erreur", timeout: "sans réponse" };
  $("#q-body").innerHTML = items.length ? items.map((i) => `<tr>
      <td><span class="st ${i.status}">${LBL[i.status]}</span></td>
      <td><div class="rname">${esc(i.name)}</div>${i.mode === "path" ? `<div class="rmeta"><span class="tag path" title="Aucun torrent d'origine dans qBittorrent : toutes les copies sont des cross-seeds">fichier trouvé uniquement en cross-seed</span></div>` : ""}</td>
      <td>${i.source === "manuel" ? "manuel" : `<span class="tag rule">${esc(i.source)}</span>`}</td>
      <td>${resultHtml(i)}</td>
      <td class="c-act">${i.status === "pending" ? `<button class="small danger" data-rm="${i.id}">Retirer</button>` : ""}</td>
    </tr>`).join("") : `<tr><td colspan="5" class="empty">Rien dans la file. Clique « Chercher » sur une release, ou ajoute les releases d'une règle prioritaire avec « Ajouter à la file ».</td></tr>`;
}

$("#q-body").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-rm]");
  if (!b) return;
  await api(`/api/queue/${b.dataset.rm}`, { method: "DELETE" });
  loadQueue();
});
// Liste des règles actives à côté du bouton : toutes, ou une seule (ex. une règle « Catégorie radarr »)
async function renderQueueRules() {
  if (!state.settings) { try { state.settings = await api("/api/settings"); } catch { return; } }
  const sel = $("#q-rule"), cur = sel.value;
  const labels = [...new Set(state.settings.rules.filter((r) => r.enabled).map(ruleLabel))];
  const html = `<option value="">Règle : toutes</option>` +
    labels.map((l) => `<option value="${esc(l)}"${l === cur ? " selected" : ""}>Règle : ${esc(l)}</option>`).join("");
  if (sel.dataset.html !== html) { sel.innerHTML = html; sel.dataset.html = html; }
}
$("#q-rules").addEventListener("click", async (e) => {
  e.target.disabled = true;
  // règles modifiées mais pas enregistrées : on enregistre d'abord, sinon le serveur lance les anciennes
  if (state.settingsDirty && !(await saveSettings())) { e.target.disabled = false; return; }
  const rule = $("#q-rule").value;
  try {
    const dry = await api("/api/queue/rules", { method: "POST", body: { ...(rule ? { rule } : {}), dry: true } });
    if (dry.matched > 20 && !confirm(`${dry.matched} releases correspondent ${rule ? `à « ${rule} »` : "aux règles prioritaires"}.\n\nLes ajouter à la file ? À une recherche toutes les ${state.queue ? state.queue.delay : "?"} s, il faudra environ ${fmtDuration(dry.matched * (state.queue ? state.queue.delay : 60))}.`)) {
      e.target.disabled = false;
      return;
    }
    const r = await api("/api/queue/rules", { method: "POST", body: rule ? { rule } : {} });
    const what = rule ? `« ${rule} »` : "aux règles";
    toast(r.matched ? `${r.added} release(s) ajoutée(s) à la file (${r.matched} correspondent ${rule ? "à " : ""}${what})` : `Aucune release ne correspond ${rule ? "à " : ""}${what}`);
    loadQueue();
  } catch (err) { toast(err.message, true); }
  e.target.disabled = false;
});
$("#q-pause").addEventListener("click", async () => {
  await api(`/api/queue/${state.queue && state.queue.paused ? "resume" : "pause"}`, { method: "POST" });
  loadQueue();
});
$("#q-clear-pending").addEventListener("click", async () => {
  if (!confirm("Retirer toutes les recherches en attente ?")) return;
  const r = await api("/api/queue/clear-pending", { method: "POST" });
  toast(`${r.removed} recherche(s) retirée(s)`);
  loadQueue();
});
$("#q-clear-done").addEventListener("click", async () => {
  await api("/api/queue/clear-done", { method: "POST" });
  loadQueue();
});

// ---------- injections en attente ----------
async function loadPending() {
  let d;
  try { d = await api("/api/pending"); } catch (err) { $("#pending-list").innerHTML = `<p class="empty">${esc(err.message)}</p>`; return; }
  const cnt = $("#cnt-pending");
  cnt.textContent = d.items.length || "";
  cnt.classList.toggle("hot", d.items.length > 0);
  if (!d.items.length) {
    $("#pending-list").innerHTML = `<p class="empty">Aucune injection en attente. Tout ce que cross-seed a trouvé est dans qBittorrent.</p>`;
    return;
  }
  $("#pending-list").innerHTML = d.items.map((p) => `<div class="card">
      <div class="card-head">
        <div><span class="chip" style="--c:${trackerColor(p.tracker)}">${esc(p.tracker)}</span>
          <span class="rname">${esc(p.name)}</span></div>
        <div class="btns"><span class="muted" style="margin:0">${esc(p.type)}, trouvé le ${fmtDate(p.mtime)}</span>
          ${d.writable ? `<button class="small danger" data-del="${esc(p.file)}">Abandonner</button>` : ""}</div>
      </div>
      ${p.errors.length ? `<pre>${esc(p.errors.join("\n"))}</pre>` : ""}
    </div>`).join("") + (d.writable ? "" : state.readonly
      ? `<p class="muted">Lecture seule (XSE_READONLY) : suppression désactivée.</p>`
      : `<p class="muted">Le dossier cross-seeds est monté en lecture seule : suppression impossible depuis l'interface.</p>`);
}
$("#pending-list").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-del]");
  if (!b || !confirm(`Abandonner cette injection ?\n\n${b.dataset.del}\n\nSeul le fichier .torrent en attente est supprimé ; tes données ne sont pas touchées.`)) return;
  try {
    await api(`/api/pending/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
    toast("Injection abandonnée");
    loadPending();
  } catch (err) { toast(err.message, true); }
});

// ---------- indexers ----------
function parseLogDate(s) { return s ? new Date(s.replace(" ", "T")) : null; }
let idxData = null;
async function loadIndexers() {
  try { idxData = await api("/api/indexers"); } catch (err) { $("#idx-list").innerHTML = `<p class="empty">${esc(err.message)}</p>`; return; }
  renderIndexers();
}
function renderIndexers() {
  const d = idxData, now = new Date(), showRetired = $("#idx-retired").checked;
  let paused = 0, needRestart = false;
  const retired = d.items.filter((i) => !i.active && i.config == null).length;
  $("#idx-retired-lbl").textContent = `Afficher les indexers retirés (${retired})`;
  const fmt = (x) => x.toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  const rank = (i) => (i.config === "active" ? 0 : i.config === "suspended" ? 1 : 2);
  const list = d.items.filter((i) => i.config != null || i.active || showRetired).sort((a, b) => rank(a) - rank(b));
  const cards = list.map((i) => {
    const until = [i.retry_after ? new Date(i.retry_after) : null, parseLogDate(i.snooze_log), i.xse_pause ? new Date(i.xse_pause) : null]
      .filter((x) => x && x > now).sort((a, b) => b - a)[0];
    let cls = "", line, pending = "", btn = "";
    const rm = i.config ? `<button class="small ghost danger" data-remove="${esc(i.key)}">Retirer</button>` : "";
    if (i.config == null && i.active && i.id != null) {
      cls = "down"; line = "Retiré de config.js";
      pending = "Redémarrage de cross-seed nécessaire"; needRestart = true;
    } else if (i.config === "suspended") {
      cls = "suspended";
      line = "Suspendu dans config.js";
      if (i.active) { pending = "Redémarrage de cross-seed nécessaire"; needRestart = true; }
      btn = `<button class="small" data-toggle="${esc(i.key)}" data-enable="1">Réactiver</button>`;
    } else if (i.config === "active" || i.active) {
      if (until) { cls = "paused"; paused++; line = `En pause jusqu'au ${fmt(until)}${i.status && i.status !== "OK" ? ` (${esc(i.status)})` : ""}`; }
      else line = i.status ? "Disponible" : "Statut inconnu (base cross-seed non lue)";
      if (i.config === "active" && !i.active && i.id != null) { pending = "Réactivé : redémarrage de cross-seed nécessaire"; needRestart = true; }
      if (i.config === "active" && i.id == null) { line = "Ajouté dans config.js"; pending = "Redémarrage de cross-seed nécessaire"; needRestart = true; }
      if (i.config === "active") btn = `<button class="small ghost" data-toggle="${esc(i.key)}" data-enable="0">Suspendre</button>`;
    } else {
      cls = "down"; line = "Retiré : n'est plus dans la config de cross-seed (gardé dans sa base)";
    }
    const past = (i.config === "active" || i.active) && !until && i.status && i.status !== "OK"
      ? ` title="Dernière pause (${esc(i.status)}) terminée${i.retry_after ? ` le ${fmt(new Date(i.retry_after))}` : ""}"` : "";
    return `<div class="card ${cls}"${past}><div class="name" style="color:${trackerColor(i.name)}">${esc(i.name)}</div>
      <div class="meta">${line}</div>${pxWarnings(i.prowlarr || i.jackett, i.config ? "doublon" : "")}<div class="meta mono">${esc(i.url)}</div>
      ${pending || btn || rm ? `<div class="card-foot"><span class="pending">${pending}</span><span class="btns">${btn}${rm}</span></div>` : ""}</div>`;
  });
  const px = d.prowlarr || {};
  let pxHtml = "";
  const goSrc = `<a href="#settings" data-goto-sources>Réglages › Sources d'indexers</a>`;
  if (!px.configured) pxHtml = `<p class="muted">Prowlarr n'est pas configuré. Pour ajouter des indexers depuis Prowlarr ou Jackett, renseigne l'un ou l'autre dans ${goSrc}.</p>`;
  else if (px.error) pxHtml = `<p class="muted">${esc(px.error)}</p>`;
  else if (px.absent.length) pxHtml = `<div class="idx">${px.absent.map((p) => `<div class="card absent">
      <div class="name" style="color:${trackerColor(p.name)}">${esc(p.name)}</div>
      <div class="meta">Dans Prowlarr, pas utilisé par cross-seed</div>${pxWarnings(p, "deja")}
      <div class="card-foot"><span></span><button class="small" data-add="${p.id}">Ajouter</button></div></div>`).join("")}</div>`;
  else pxHtml = `<p class="muted">Tous tes indexers torrent Prowlarr sont déjà dans cross-seed.</p>`;
  // Jackett : section affichée seulement s'il est configuré (facultatif, en plus ou à la place de Prowlarr)
  const jk = d.jackett || {};
  let jkHtml = "";
  if (jk.error) jkHtml = `<p class="muted">${esc(jk.error)}</p>`;
  else if (jk.configured && jk.absent.length) jkHtml = `<div class="idx">${jk.absent.map((p) => `<div class="card absent">
      <div class="name" style="color:${trackerColor(p.name)}">${esc(p.name)}</div>
      <div class="meta">Dans Jackett, pas utilisé par cross-seed</div>${pxWarnings(p, "deja")}
      <div class="card-foot"><span></span><button class="small" data-add-jackett="${esc(p.id)}">Ajouter</button></div></div>`).join("")}</div>`;
  else if (jk.configured) jkHtml = `<p class="muted">Tous tes indexers Jackett sont déjà dans cross-seed.</p>`;
  else if (px.configured) jkHtml = `<p class="muted">Tu utilises aussi Jackett ? Renseigne-le dans ${goSrc}.</p>`;
  $("#idx-list").innerHTML = (d.error ? `<p class="muted">Base cross-seed : ${esc(d.error)}</p>` : "") +
    (cards.length ? `<div class="idx">${cards.join("")}</div>` : `<p class="empty">Aucun indexer trouvé.</p>`) +
    (px.configured || !jk.configured ? `<h3>Disponibles dans Prowlarr</h3>${pxHtml}` : "") +
    (jkHtml ? `<h3>Disponibles dans Jackett</h3>${jkHtml}` : "");
  // Toujours disponible ; mis en avant seulement quand une modification attend un redémarrage.
  const rb = $("#idx-restart");
  rb.hidden = !state.restartAvailable;
  rb.className = needRestart ? "primary" : "ghost";
  rb.textContent = needRestart ? "Redémarrer cross-seed pour appliquer" : "Redémarrer cross-seed";
  const cnt = $("#cnt-indexers");
  cnt.textContent = paused ? `${paused} en pause` : "";
  cnt.classList.toggle("hot", paused > 0);
}
// Avertissements venant de Prowlarr ou Jackett (désactivé, en échec, même site déclaré deux fois)
function pxWarnings(p, dupMode) {
  if (!p) return "";
  const w = [];
  const src = p.source || "Prowlarr";
  if (!p.enabled) w.push(`Désactivé dans ${src}`);
  if (p.failing_until && new Date(p.failing_until) > new Date())
    w.push(`En échec dans ${src} jusqu'au ${new Date(p.failing_until).toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}`);
  if (p.same_site && p.same_site.length)
    w.push(dupMode === "deja" ? `Même site que ${p.same_site.join(", ")}, déjà utilisé par cross-seed`
                              : `Même site que ${p.same_site.join(", ")} : doublon, un seul suffit`);
  return w.map((x) => `<div class="meta warn">${esc(x)}</div>`).join("");
}
$("#idx-list").addEventListener("click", async (e) => {
  const add = e.target.closest("[data-add], [data-add-jackett]"), rem = e.target.closest("[data-remove]");
  if (add || rem) {
    const btn = add || rem, name = btn.closest(".card").querySelector(".name").textContent;
    if (rem && !confirm(`Retirer ${name} de cross-seed ?\n\nSa ligne sera supprimée de config.js (sauvegarde faite avant). Tu pourras le rajouter depuis la liste Prowlarr ou Jackett.`)) return;
    btn.disabled = true;
    try {
      if (add) await api("/api/indexers/add", { method: "POST", body: add.dataset.addJackett
        ? { jackett_id: add.dataset.addJackett } : { prowlarr_id: +add.dataset.add } });
      else await api("/api/indexers/remove", { method: "POST", body: { key: rem.dataset.remove } });
      toast(`${name} ${add ? "ajouté à" : "retiré de"} config.js. Redémarre cross-seed pour appliquer.`);
      loadIndexers();
    } catch (err) { toast(err.message, true); btn.disabled = false; }
    return;
  }
  const b = e.target.closest("[data-toggle]");
  if (!b) return;
  const enable = b.dataset.enable === "1";
  const card = b.closest(".card"), name = card.querySelector(".name").textContent;
  if (!enable && !confirm(`Suspendre ${name} ?\n\nSa ligne sera mise en commentaire dans config.js (sauvegarde faite avant). cross-seed ne le contactera plus après redémarrage.`)) return;
  b.disabled = true;
  try {
    await api("/api/indexers/toggle", { method: "POST", body: { key: b.dataset.toggle, enable } });
    toast(`${name} ${enable ? "réactivé" : "suspendu"} dans config.js. Redémarre cross-seed pour appliquer.`);
    loadIndexers();
  } catch (err) { toast(err.message, true); b.disabled = false; }
});
async function restartXs(btn) {
  if (!confirm("Redémarrer cross-seed ? Une recherche en cours sera interrompue.")) return false;
  btn.disabled = true;
  const x = $("#pill-xs");
  const setPill = (cls, text, title = "") => { x.className = `pill ${cls}`; x.lastChild.textContent = text; x.title = title; };
  // Affichage immédiat dans la barre du haut ; loadHealth n'y touche plus jusqu'à la fin.
  state.xsRestarting = true;
  setPill("busy", "cross-seed redémarre…", "Le démarrage peut prendre quelques minutes s'il indexe des dataDirs");
  let ok = false;
  try {
    await api("/api/xs-restart", { method: "POST" });
    // On attend qu'il réponde à nouveau plutôt qu'un délai fixe : le démarrage peut être long.
    const t0 = Date.now();
    while (Date.now() - t0 < 300000) {
      await new Promise((r) => setTimeout(r, 5000));
      const s = await api("/api/status").catch(() => null);
      if (s && s.xs.ok) { ok = true; break; }
    }
    if (!ok) toast("cross-seed ne répond toujours pas après 5 min : regarde ses logs", true);
    return ok;
  } catch (err) { toast(err.message, true); return false; }
  finally {
    state.xsRestarting = false;
    btn.disabled = false;
    await loadHealth();
    if (ok && state.tab === "indexers") loadIndexers();
  }
}
$("#idx-restart").addEventListener("click", (e) => restartXs(e.target));
$("#idx-retired").addEventListener("change", () => idxData && renderIndexers());

// ---------- logs ----------
const logState = { es: null, kind: "info", entries: [] };
const SUCCESS_RX = /\bMATCH(_PARTIAL|_SIZE_ONLY)?\b.*- injected/;
const TRACKER_RX = [
  / on (.+?) by MATCH/, /Querying (.+?) at http/, /no match for (.+?) torrent /,
  /Snatched .+ from (.+)$/m, /on temporarily disabled indexers \[(.+?)\]/, /^(.+?) was rate limited/,
];

function highlight(entry) {
  const m = entry.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\w+):\s*(\[[^\]]+\])?\s*([\s\S]*)$/);
  if (!m) return esc(entry);
  let msg = esc(m[4]);
  const names = new Set();
  for (const rx of TRACKER_RX) { const t = m[4].match(rx); if (t && t[1].length < 40) names.add(t[1]); }
  for (const n of [...names].sort((a, b) => b.length - a.length)) {
    msg = msg.split(esc(n)).join(`<span class="trk" style="--c:${trackerColor(n)}">${esc(n)}</span>`);
  }
  const lv = m[2].toLowerCase();
  return `<span class="ts">${m[1].slice(5)}</span> <span class="lv-${lv}">${m[2]}</span> ${m[3] ? `<span class="ctx">${esc(m[3])}</span> ` : ""}${msg}`;
}

function logLineHtml(e) {
  const cls = SUCCESS_RX.test(e) ? " hit" : /\berror:|failed to inject/.test(e) ? " err" : "";
  return `<div class="ll${cls}">${highlight(e)}</div>`;
}

function logFilter(e) {
  if ($("#log-success").checked && !SUCCESS_RX.test(e)) return false;
  const q = $("#log-q").value.trim().toLowerCase();
  return !q || e.toLowerCase().includes(q);
}

function renderLogs() {
  const v = $("#logview");
  v.innerHTML = logState.entries.filter(logFilter).slice(-2000).map(logLineHtml).join("") ||
    `<p class="empty">Aucune ligne ne correspond.</p>`;
  if ($("#log-follow").checked) v.scrollTop = v.scrollHeight;
}

function startLogs() {
  stopLogs();
  logState.entries = [];
  $("#logview").innerHTML = `<p class="empty">Connexion aux logs…</p>`;
  const es = new EventSource(`/api/logs/stream?kind=${logState.kind}&lines=600`);
  logState.es = es;
  es.addEventListener("init", (ev) => { logState.entries = JSON.parse(ev.data); renderLogs(); });
  es.addEventListener("lines", (ev) => {
    const add = JSON.parse(ev.data);
    logState.entries.push(...add);
    if (logState.entries.length > 5000) logState.entries.splice(0, logState.entries.length - 5000);
    const v = $("#logview");
    if (v.querySelector(".empty")) v.innerHTML = "";
    v.insertAdjacentHTML("beforeend", add.filter(logFilter).map(logLineHtml).join(""));
    if ($("#log-follow").checked) v.scrollTop = v.scrollHeight;
  });
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) $("#logview").insertAdjacentHTML("beforeend", `<p class="empty">Flux interrompu. Rouvre l'onglet pour reconnecter.</p>`); };
}
function stopLogs() { if (logState.es) { logState.es.close(); logState.es = null; } }

$$(".seg [data-kind]").forEach((b) => b.addEventListener("click", () => {
  $$(".seg [data-kind]").forEach((x) => x.classList.toggle("on", x === b));
  logState.kind = b.dataset.kind;
  startLogs();
}));
$("#log-success").addEventListener("change", renderLogs);
let lqTimer;
$("#log-q").addEventListener("input", () => { clearTimeout(lqTimer); lqTimer = setTimeout(renderLogs, 200); });
$("#log-clear").addEventListener("click", () => { logState.entries = []; renderLogs(); });

// ---------- réglages ----------
const escRe = (x) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const ruleWords = (v) => String(v || "").trim().split(/[\s,;]+/).map((w) => w.replace(/^-+/, "")).filter(Boolean);
const ruleCats = (v) => String(v || "").split(",").map((c) => c.trim()).filter(Boolean);
function rulePattern(r) {
  const v = String(r.value || "");
  if (r.type === "category") { const c = ruleCats(v); return c.length ? `^(?:${c.map(escRe).join("|")})$` : ""; }
  if (r.type === "group") { const g = ruleWords(v); return g.length ? `-(?:${g.map(escRe).join("|")})(\\.\\w{2,4})?$` : ""; }
  if (r.type === "contains") return ruleWords(v).map((w) => `(?=.*${escRe(w)})`).join("");
  if (r.type === "starts") return v.trim() ? `^${escRe(v.trim())}` : "";
  return v.trim();
}
const RULE_TYPES = {
  group: { label: "Groupe de release", ph: "ex. HDForever (plusieurs : HDForever, NEO)",
    hint: "Le nom se termine par -GROUPE, extension .mkv comprise. Plusieurs groupes séparés par une virgule." },
  contains: { label: "Contient les mots", ph: "ex. 1080p remux",
    hint: "Tous les mots doivent apparaître dans le nom, dans n'importe quel ordre." },
  starts: { label: "Commence par", ph: "ex. Star.Wars",
    hint: "Le nom commence exactement par ce texte (points compris)." },
  category: { label: "Catégorie", ph: "ex. radarr (plusieurs : radarr, sonarr)",
    hint: "La release est dans cette catégorie qBittorrent (celle du torrent d'origine). Plusieurs catégories séparées par une virgule." },
  regex: { label: "Expression régulière", ph: "pour les utilisateurs avancés",
    hint: "Expression régulière Python, insensible à la casse." },
};
function ruleMatches(r) {
  const pat = rulePattern(r);
  if (!pat) return { n: 0, ex: [], empty: true };
  let rx;
  try { rx = new RegExp(pat, "i"); } catch { return null; }
  const hits = state.releases.filter((rel) => (r.type === "category" ? rx.test(rel.category || "")
    : rel.copies.some((c) => rx.test(c.name))));
  return { n: hits.length, ex: hits.slice(0, 3).map((h) => h.name) };
}
// Même étiquette que config.rule_label côté serveur (colonne Priorité, origine dans la file)
function ruleLabel(r) {
  const v = String(r.value || "").trim();
  if (r.type === "category") return v ? `Catégorie ${ruleCats(v).join(", ")}` : "Catégorie";
  return v || "Règle";
}
function ruleInfoHtml(r) {
  const m = ruleMatches(r), t = RULE_TYPES[r.type] || RULE_TYPES.regex;
  let res;
  if (m === null) res = `<span class="res-bad">Expression invalide</span>`;
  else if (m.empty) res = `<span class="res-none">Renseigne une valeur</span>`;
  else if (!m.n) res = `<span class="res-warn">Aucune release ne correspond</span>`;
  else res = `<span class="res-hit">${m.n} release${m.n > 1 ? "s" : ""}</span>, par exemple : ${m.ex.map((x) => `<span class="mono">${esc(x)}</span>`).join(" ; ")}`;
  return `${esc(t.hint)}<br>${res}`;
}

function renderRules() {
  const rules = state.settings.rules;
  $("#rules").innerHTML = rules.map((r, i) => `<div class="rule" data-i="${i}">
      <div class="order"><button class="ghost" data-up ${i === 0 ? "disabled" : ""} aria-label="Monter">▲</button>
        <button class="ghost" data-down ${i === rules.length - 1 ? "disabled" : ""} aria-label="Descendre">▼</button></div>
      <select data-f="type" aria-label="Type de règle">${Object.entries(RULE_TYPES).map(([k, t]) =>
        `<option value="${k}"${(r.type || "regex") === k ? " selected" : ""}>${t.label}</option>`).join("")}</select>
      <input type="text" data-f="value" value="${esc(r.value ?? r.pattern ?? "")}" placeholder="${esc((RULE_TYPES[r.type] || RULE_TYPES.regex).ph)}"
        class="${r.type === "regex" ? "mono" : ""}" ${r.type === "category" ? 'list="rule-cats"' : ""} aria-label="Valeur">
      <span class="rule-end"><label class="check"><input type="checkbox" data-f="enabled" ${r.enabled ? "checked" : ""}> active</label>
        <button class="small danger" data-rm-rule>Supprimer</button></span>
      <div class="rule-info">${ruleInfoHtml(r)}</div>
    </div>`).join("") || `<p class="muted">Aucune règle.</p>`;
  const cats = [...new Set(state.releases.map((rel) => rel.category).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  $("#rules").insertAdjacentHTML("beforeend",
    `<datalist id="rule-cats">${cats.map((c) => `<option value="${esc(c)}">`).join("")}</datalist>`);
}

// ---------- routage par catégorie ----------
function routeInfoHtml(r) {
  const cat = String(r.category || "").trim().toLowerCase();
  if (!cat) return `<span class="res-none">Renseigne une catégorie</span>`;
  const n = state.releases.filter((rel) => (rel.category || "").toLowerCase() === cat).length;
  const rel = n ? `<span class="res-hit">${n} release${n > 1 ? "s" : ""}</span>` : `<span class="res-warn">Aucune release dans cette catégorie</span>`;
  const idx = r.indexers.length ? `cherchée${n > 1 ? "s" : ""} sur ${r.indexers.length} indexer${r.indexers.length > 1 ? "s" : ""}`
    : `<span class="res-warn">aucun indexer coché : recherche normale sur tous les indexers</span>`;
  return `${rel}, ${idx}`;
}
function renderRoutes() {
  const routes = state.settings.routes || (state.settings.routes = []);
  if (!idxData) { $("#routes").innerHTML = `<p class="muted">Chargement des indexers…</p>`; loadIndexers().then(() => { if (idxData) renderRoutes(); else $("#routes").innerHTML = `<p class="muted">Liste des indexers indisponible.</p>`; }); return; }
  const active = idxData.items.filter((i) => i.config === "active");
  $("#routes").innerHTML = routes.map((r, n) => {
    const others = r.indexers.filter((k) => !active.some((i) => i.key === k)).map((k) => {
      const it = idxData.items.find((i) => i.key === k);
      return { key: k, name: it ? it.name : k, off: true };
    });
    const boxes = [...active, ...others].map((i) => `<label class="check${i.off ? " off" : ""}"${i.off ? ' title="Suspendu ou retiré de config.js : ignoré"' : ""}>
        <input type="checkbox" data-key="${esc(i.key)}" ${r.indexers.includes(i.key) ? "checked" : ""}>
        <span style="color:${i.off ? "inherit" : trackerColor(i.name)}">${esc(i.name)}</span>${i.off ? " (inactif)" : ""}</label>`).join("");
    return `<div class="route" data-i="${n}">
      <input type="text" data-f="category" list="rule-cats" value="${esc(r.category)}" placeholder="Catégorie, ex. radarr" aria-label="Catégorie">
      <div class="route-idx">${boxes || `<span class="muted">Aucun indexer actif dans config.js</span>`}</div>
      <button class="small danger" data-rm-route>Supprimer</button>
      <div class="rule-info">${routeInfoHtml(r)}</div>
    </div>`;
  }).join("") || `<p class="muted">Aucun routage : toutes les recherches passent par cross-seed, sur tous ses indexers.</p>`;
}
$("#routes").addEventListener("input", (e) => {
  const row = e.target.closest(".route");
  if (!row) return;
  const r = state.settings.routes[+row.dataset.i];
  if (e.target.dataset.f === "category") r.category = e.target.value;
  else if (e.target.dataset.key) {
    const k = e.target.dataset.key;
    r.indexers = e.target.checked ? [...new Set([...r.indexers, k])] : r.indexers.filter((x) => x !== k);
  } else return;
  state.settingsDirty = true;
  row.querySelector(".rule-info").innerHTML = routeInfoHtml(r);
});
$("#routes").addEventListener("click", (e) => {
  const row = e.target.closest(".route");
  if (!row || !e.target.closest("[data-rm-route]")) return;
  state.settings.routes.splice(+row.dataset.i, 1);
  state.settingsDirty = true;
  renderRoutes();
});
$("#route-add").addEventListener("click", () => {
  state.settings.routes.push({ category: "", indexers: [] });
  state.settingsDirty = true;
  renderRoutes();
  $('#routes .route:last-of-type [data-f="category"]').focus();
});

// ---------- scan planifié ----------
function scanForm() {
  return { enabled: $("#scan-enabled").checked, every_hours: +$("#scan-every").value,
    limit: +$("#scan-limit").value, recent_days: +$("#scan-recent").value };
}
const fmtWhen = (sec) => new Date(sec * 1000).toLocaleString("fr-FR", { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
function scanStatusText(info) {
  const s = info.state || {}, sc = state.settings.scan, parts = [];
  if (s.last_run) {
    const how = s.last_trigger === "manuel" ? "lancé à la main" : "automatique";
    if (s.last_error) parts.push(`Dernier passage (${how}) le ${fmtWhen(s.last_run)} : erreur, ${s.last_error}.`);
    else parts.push(`Dernier passage (${how}) le ${fmtWhen(s.last_run)} : ${s.last_added} release(s) mise(s) en file sur ${s.last_eligible} éligible(s), ${s.last_recent} sautée(s) car cherchée(s) récemment.`);
  } else parts.push("Aucun passage pour l'instant.");
  if (info.readonly) parts.push("Lecture seule : le scan automatique ne se lance pas.");
  else if (!sc.enabled) parts.push("Scan automatique désactivé.");
  else if (info.next_run) parts.push(info.next_run * 1000 <= Date.now() ? "Prochain passage : dans la minute." : `Prochain passage : ${fmtWhen(info.next_run)}.`);
  return parts.join(" ");
}
function scanXsNote(info, pendingRestart) {
  const x = info.xs_cadence;
  $("#scan-replace").disabled = !x || state.configWrite === false;
  if (!x) return "searchCadence introuvable dans config.js : à modifier à la main.";
  $("#scan-replace").checked = x.disabled;
  const now = x.disabled ? "Dans config.js : searchCadence: null, le scan complet de cross-seed est coupé."
    : `Dans config.js : searchCadence = ${x.value}, cross-seed fait son propre scan complet en plus de celui-ci.`;
  return pendingRestart ? `${now} Redémarrage de cross-seed nécessaire pour l'appliquer.` : now;
}
async function renderScan() {
  const sc = state.settings.scan;
  $("#scan-enabled").checked = sc.enabled;
  $("#scan-every").value = sc.every_hours;
  $("#scan-limit").value = sc.limit;
  $("#scan-recent").value = sc.recent_days;
  try {
    const info = await api("/api/scan");
    $("#scan-status").textContent = scanStatusText(info);
    $("#scan-xs-note").textContent = scanXsNote(info, !$("#scan-xs-restart").hidden);
  } catch (err) { $("#scan-status").textContent = err.message; }
}
["#scan-enabled", "#scan-every", "#scan-limit", "#scan-recent"].forEach((s) =>
  $(s).addEventListener("input", () => { state.settingsDirty = true; }));
$("#scan-run").addEventListener("click", async (e) => {
  e.target.disabled = true;
  if (state.settingsDirty && !(await saveSettings())) { e.target.disabled = false; return; }
  try {
    const info = await api("/api/scan/run", { method: "POST" });
    toast(`${info.state.last_added} release(s) mise(s) en file`);
    $("#scan-status").textContent = scanStatusText(info);
    loadQueue();
  } catch (err) { toast(err.message, true); }
  e.target.disabled = false;
});
$("#scan-replace").addEventListener("change", async (e) => {
  const replace = e.target.checked;
  const msg = replace ? "Couper le scan complet de cross-seed (searchCadence: null dans config.js) ?\n\nUne sauvegarde de config.js est faite. Le RSS et l'announce ne changent pas."
    : "Rétablir le scan complet de cross-seed (ancienne valeur de searchCadence) ?";
  if (!confirm(msg)) { e.target.checked = !replace; return; }
  try {
    const r = await api("/api/scan/replace", { method: "POST", body: { replace } });
    if (r.changed) { $("#scan-xs-restart").hidden = !state.restartAvailable; toast(`config.js modifié (sauvegarde : ${r.backup})`); }
    $("#scan-xs-note").textContent = scanXsNote(r, r.changed);
  } catch (err) { e.target.checked = !replace; toast(err.message, true); }
});
$("#scan-xs-restart").addEventListener("click", async (e) => {
  if (await restartXs(e.target)) { e.target.hidden = true; renderScan(); }
});

function renderAliases() {
  const aliases = state.settings.tracker_aliases || {};
  const hosts = Object.keys(state.trackers).filter(Boolean).sort();
  $("#aliases").innerHTML = hosts.length ? hosts.map((h) =>
    `<code>${esc(h)}</code><input type="text" data-host="${esc(h)}" value="${esc(aliases[h] || "")}" placeholder="${esc(state.trackers[h])}">`).join("")
    : `<p class="muted">Aucun tracker détecté (charge d'abord l'onglet Releases).</p>`;
}

async function renderSettings() {
  if (!state.settings) state.settings = await api("/api/settings");
  $("#set-delay").value = state.settings.delay;
  renderRules();
  renderRoutes();
  renderScan();
  renderAliases();
  renderSources();
  renderXsForm();
}

// ---------- sources d'indexers ----------
const SRC_META = {
  prowlarr: { label: "Prowlarr", ph: "http://prowlarr:9696" },
  jackett: { label: "Jackett", ph: "http://jackett:9117" },
};
const SRC_ORIGIN = { ".env": "via .env", "config.js": "via config.js", "Réglages": "via Réglages" };
async function renderSources(info) {
  try { info = info || await api("/api/sources"); }
  catch (err) { $("#sources").innerHTML = `<p class="muted">${esc(err.message)}</p>`; return; }
  $("#sources").innerHTML = Object.entries(SRC_META).map(([n, m]) => {
    const s = info[n], eff = s.effective, fromJs = eff && eff.origin === "config.js";
    let status;
    if (s.env) status = `Défini dans le .env${s.env_url ? ` (${esc(s.env_url)})` : ""}. Pour le changer, modifie le .env.`;
    else if (fromJs) status = "Rien à saisir : adresse et clé lues dans tes lignes torznab de config.js. Remplis les champs seulement pour utiliser une autre adresse.";
    else if (eff) status = "Adresse et clé enregistrées ici.";
    else status = "Non configuré : renseigne l'adresse et la clé API, puis teste.";
    const dis = s.env ? "disabled" : "";
    const urlPh = s.env ? s.env_url : fromJs ? `${eff.url} (lue dans config.js)` : m.ph;
    const keyPh = s.env ? "définie dans le .env" : s.has_apikey ? "enregistrée (laisser vide pour la garder)"
      : fromJs ? "lue dans config.js" : "";
    const badge = eff ? `<span class="badge" data-badge>Vérification…</span>` : `<span class="badge">Non configuré</span>`;
    return `<div class="card" data-src="${n}" data-eff="${esc(eff ? eff.url : "")}" data-origin="${esc(eff ? SRC_ORIGIN[eff.origin] || eff.origin : "")}">
      <div class="src-head"><span class="name">${m.label}</span>${badge}</div><div class="meta">${status}</div>
      <label class="field">Adresse<input type="text" data-f="url" value="${esc(s.url)}" placeholder="${esc(urlPh)}" autocomplete="off" ${dis}></label>
      <label class="field">Clé API<input type="password" data-f="apikey" placeholder="${esc(keyPh)}" autocomplete="new-password" ${dis}></label>
      <div class="src-test"><button class="small ghost" data-test>Tester</button><span data-result></span></div></div>`;
  }).join("");
  // Vérifie tout de suite la connexion des sources configurées (adresse et clé effectives)
  $$("#sources [data-badge]").forEach(async (bd) => {
    const card = bd.closest("[data-src]");
    try {
      const r = await api("/api/sources/test", { method: "POST", body: { name: card.dataset.src, url: card.dataset.eff, apikey: "" } });
      bd.className = "badge ok";
      bd.textContent = `Connecté ${card.dataset.origin} · ${r.indexers} indexer${r.indexers > 1 ? "s" : ""}`;
    } catch (err) { bd.className = "badge ko"; bd.textContent = "Injoignable"; bd.title = err.message; }
  });
}
$("#sources").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-test]");
  if (!b) return;
  const card = b.closest("[data-src]"), out = card.querySelector("[data-result]");
  const url = card.querySelector('[data-f="url"]').value.trim() || card.dataset.eff;
  b.disabled = true; out.className = ""; out.textContent = "Test…";
  try {
    const r = await api("/api/sources/test", { method: "POST",
      body: { name: card.dataset.src, url, apikey: card.querySelector('[data-f="apikey"]').value } });
    out.className = "ok-msg"; out.textContent = `OK : ${r.indexers} indexer${r.indexers > 1 ? "s" : ""} trouvé${r.indexers > 1 ? "s" : ""}`;
  } catch (err) { out.className = "ko-msg"; out.textContent = err.message; }
  b.disabled = false;
});
$("#src-save").addEventListener("click", async (e) => {
  const body = {};
  $$("#sources [data-src]").forEach((card) => {
    const url = card.querySelector('[data-f="url"]'), key = card.querySelector('[data-f="apikey"]');
    if (url.disabled) return;
    body[card.dataset.src] = { url: url.value.trim(), apikey: key.value.trim() || null };
  });
  e.target.disabled = true;
  try {
    await renderSources(await api("/api/sources", { method: "PUT", body }));
    toast("Sources enregistrées");
  } catch (err) { toast(err.message, true); }
  e.target.disabled = false;
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("[data-goto-sources]")) return;
  setTimeout(() => $("#sources-h").scrollIntoView({ behavior: "smooth" }), 50);
});

const XS_HELP = {
  action: "inject : ajoute les cross-seeds dans qBittorrent ; save : enregistre seulement les .torrent",
  matchMode: "strict (ancien nom : safe) : fichiers identiques ; flexible (risky) : noms de fichiers différents tolérés ; partial : fichiers manquants tolérés. flexible et partial exigent linkDirs.",
  linkType: "hardlink, symlink ou reflink",
  delay: "Secondes entre deux recherches du scan complet. De 30 à 3600.",
  searchCadence: "Fréquence du scan complet. Minimum : 1 day.",
  rssCadence: "Fréquence du scan RSS. De 10 minutes à 2 hours.",
  excludeRecentSearch: "Délai avant de rechercher à nouveau un torrent sur un indexer. Au moins 3 × searchCadence.",
  excludeOlder: "Le scan complet ignore les torrents vus pour la première fois il y a plus longtemps. Entre 2 et 5 × excludeRecentSearch.",
  searchLimit: "Nombre maximal de recherches par indexer à chaque scan complet. 0 = illimité.",
  includeSingleEpisodes: "Cherche aussi les épisodes isolés",
  seasonFromEpisodes: "Part d'épisodes présents pour reconstituer une saison, de 0.5 à 1. En dessous de 1, exige matchMode partial. Vide = désactivé.",
  duplicateCategories: "Crée des catégories « .cross-seed » dans qBittorrent",
  linkCategory: "Catégorie qBittorrent des cross-seeds liés",
};
const XS_ENUMS = {
  action: ["inject", "save"],
  matchMode: ["strict", "flexible", "partial"],
  linkType: ["hardlink", "symlink", "reflink"],
};
let xsSettings = null;
async function renderXsForm() {
  try {
    const d = await api("/api/indexers");
    xsSettings = d.settings || {};
  } catch (err) { $("#xs-form").innerHTML = `<p class="muted">${esc(err.message)}</p>`; return; }
  const st = await api("/api/status").catch(() => ({}));
  $("#xs-restart").hidden = !st.restart_available;
  $("#xs-note").textContent = st.restart_available ? "" : "Redémarrage depuis l'interface non configuré : relance cross-seed depuis le NAS.";
  const keys = Object.keys(xsSettings);
  $("#xs-form").innerHTML = keys.length ? keys.map((k) => {
    const s = xsSettings[k];
    let input;
    if (s.kind === "boolean") {
      input = `<select data-xs="${k}"><option${s.value === "true" ? " selected" : ""}>true</option><option${s.value === "false" ? " selected" : ""}>false</option></select>`;
    } else if (XS_ENUMS[k]) {
      const opts = XS_ENUMS[k].includes(s.value) ? XS_ENUMS[k] : [s.value, ...XS_ENUMS[k]];
      input = `<select data-xs="${k}">${opts.map((o) => `<option${o === s.value ? " selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    } else {
      input = `<input type="text" data-xs="${k}" value="${esc(s.value)}" ${s.kind === "other" ? "disabled" : ""} ${s.kind === "empty" ? 'placeholder="non défini"' : ""}>`;
    }
    return `<label for="xs-${k}">${esc(k)}</label>${input.replace("data-xs", `id="xs-${k}" data-xs`)}<span class="help" data-help="${k}">${esc(XS_HELP[k] || "")}</span>`;
  }).join("") : `<p class="muted">config.js illisible.</p>`;
  if (keys.length) xsCheck();
}
function xsChanges() {
  const changes = {};
  $$("#xs-form [data-xs]").forEach((el) => { if (!el.disabled && el.value !== xsSettings[el.dataset.xs].value) changes[el.dataset.xs] = el.value; });
  return changes;
}
let xsCheckTimer, xsHasErrors = false;
async function xsCheck() {
  let r;
  try { r = await api("/api/xs-settings/check", { method: "POST", body: xsChanges() }); } catch { return; }
  xsHasErrors = Object.keys(r.errors).length > 0;
  $$("#xs-form [data-help]").forEach((h) => {
    const k = h.dataset.help, msg = r.errors[k] || r.warnings[k];
    h.classList.toggle("bad", !!r.errors[k]);
    h.classList.toggle("warn", !r.errors[k] && !!r.warnings[k]);
    h.textContent = msg || XS_HELP[k] || "";
  });
  $("#xs-save").disabled = xsHasErrors;
  $("#xs-note").textContent = xsHasErrors ? "Corrige les valeurs en rouge : cross-seed refuserait de démarrer." : "";
}
$("#xs-form").addEventListener("input", (e) => {
  const k = e.target.dataset.xs;
  if (!k) return;
  e.target.classList.toggle("dirty", e.target.value !== xsSettings[k].value);
  clearTimeout(xsCheckTimer);
  xsCheckTimer = setTimeout(xsCheck, 300);
});
$("#xs-save").addEventListener("click", async () => {
  const changes = xsChanges();
  if (!Object.keys(changes).length) { toast("Aucune modification"); return; }
  try {
    const r = await api("/api/xs-settings", { method: "PUT", body: changes });
    toast(r.changed ? "config.js enregistré. Redémarre cross-seed pour l'appliquer." : "Aucune modification");
    if (r.changed) $("#xs-note").textContent = `Sauvegarde : ${r.backup}. Redémarrage nécessaire.`;
    renderXsForm();
  } catch (err) { toast(err.message, true); }
});
$("#xs-restart").addEventListener("click", async (e) => { if (await restartXs(e.target)) $("#xs-note").textContent = ""; });

$("#rules").addEventListener("input", (e) => {
  const row = e.target.closest(".rule");
  if (!row) return;
  const r = state.settings.rules[+row.dataset.i];
  const f = e.target.dataset.f;
  if (!f) return;
  r[f] = f === "enabled" ? e.target.checked : e.target.value;
  state.settingsDirty = true;
  if (f === "type") {
    const val = row.querySelector('[data-f="value"]');
    val.placeholder = RULE_TYPES[r.type].ph;
    val.classList.toggle("mono", r.type === "regex");
    if (r.type === "category") val.setAttribute("list", "rule-cats"); else val.removeAttribute("list");
  }
  if (f === "type" || f === "value") row.querySelector(".rule-info").innerHTML = ruleInfoHtml(r);
});
$("#rules").addEventListener("click", (e) => {
  const row = e.target.closest(".rule");
  if (!row) return;
  const i = +row.dataset.i, rules = state.settings.rules;
  if (e.target.closest("[data-rm-rule]")) rules.splice(i, 1);
  else if (e.target.closest("[data-up]")) [rules[i - 1], rules[i]] = [rules[i], rules[i - 1]];
  else if (e.target.closest("[data-down]")) [rules[i + 1], rules[i]] = [rules[i], rules[i + 1]];
  else return;
  state.settingsDirty = true;
  renderRules();
});
$("#rule-add").addEventListener("click", () => {
  state.settings.rules.push({ type: "group", value: "", enabled: true });
  state.settingsDirty = true;
  renderRules();
  $('#rules .rule:last-of-type [data-f="value"]').focus();
});
async function saveSettings() {
  const aliases = {};
  $$("#aliases input").forEach((i) => { if (i.value.trim()) aliases[i.dataset.host] = i.value.trim(); });
  try {
    state.settings = await api("/api/settings", {
      method: "PUT",
      body: { delay: +$("#set-delay").value, rules: state.settings.rules, routes: state.settings.routes, scan: scanForm(), tracker_aliases: aliases },
    });
    state.settingsDirty = false;
    toast("Réglages enregistrés");
    await loadReleases();
    renderSettings();
    return true;
  } catch (err) { toast(err.message, true); return false; }
}
$("#set-save").addEventListener("click", saveSettings);
$("#set-delay").addEventListener("input", () => { state.settingsDirty = true; });
window.addEventListener("beforeunload", (e) => { if (state.settingsDirty) e.preventDefault(); });

// ---------- démarrage ----------
showTab(location.hash.slice(1) || "releases");
loadHealth();
loadReleases();
loadQueue();
loadPending();
loadIndexers();
setInterval(loadHealth, 30000);
setInterval(loadQueue, 3000);
setInterval(() => { if (state.tab === "pending" || !document.hidden) loadPending(); }, 60000);
