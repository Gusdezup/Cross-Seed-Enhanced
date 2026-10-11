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
  settings: null, queue: null, tab: "search", sub: "queue", openItems: new Set(), indexers: [], openBatches: new Set(), batchItems: {},
};

// ---------- onglets ----------
// Trois pages : 1 Chercher, 2 Suivre l'avancement (recherches, injections en attente, logs), 3 Réglages.
// Les anciennes adresses (#releases, #queue, #pending, #logs, #indexers) mènent au bon endroit.
const OLD_TABS = { releases: ["search"], queue: ["follow", "queue"], pending: ["follow", "pending"],
  logs: ["follow", "logs"], indexers: ["settings", null, "set-indexers"] };
function showTab(name) {
  let [tab, sub, anchor] = OLD_TABS[name] || [name];
  if (!$(`#tab-${tab}`)) tab = "search";
  state.tab = tab;
  $$(".tab").forEach((t) => (t.hidden = t.id !== `tab-${tab}`));
  $$(".rail a").forEach((a) => a.classList.toggle("on", a.dataset.tab === tab));
  if (tab === "search") showCsub(state.csub || "new");
  if (tab === "follow") showSub(sub || state.sub);
  else stopLogs();
  if (tab === "settings") {
    anchor = anchor || state.pendingAnchor;
    state.pendingAnchor = null;
    renderSettings().then(() => anchor && setTimeout(() => goTo(anchor), 80));
  }
}
function showSub(sub) {
  state.sub = sub;
  $$("#f-seg [data-sub]").forEach((b) => b.classList.toggle("on", b.dataset.sub === sub));
  for (const n of ["queue", "pending", "logs"]) $(`#sub-${n}`).hidden = n !== sub;
  if (sub === "queue") loadQueue();
  if (sub === "pending") loadPending();
  if (sub === "logs") startLogs(); else stopLogs();
}
function showCsub(sub) {
  state.csub = sub;
  $$("#c-seg [data-csub]").forEach((b) => b.classList.toggle("on", b.dataset.csub === sub));
  $("#csub-new").hidden = sub !== "new";
  $("#csub-prog").hidden = sub !== "prog";
  $("#c-seg").hidden = !!state.editing && sub === "new";   // pendant une modification : bandeau « Annuler » à la place
  if (sub === "new") renderSearch(); else loadSchedules();
}
function goTo(id) { const el = document.getElementById(id); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); }
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
document.addEventListener("click", (e) => {
  const sub = e.target.closest("#f-seg [data-sub]");
  if (sub) showSub(sub.dataset.sub);
  const csub = e.target.closest("#c-seg [data-csub]");
  if (csub) showCsub(csub.dataset.csub);
  const g = e.target.closest("[data-goto]");
  if (g && state.tab === "settings") { e.preventDefault(); goTo(g.dataset.goto); }
  else if (g) state.pendingAnchor = g.dataset.goto;   // le lien mène à #settings, showTab défile ensuite
});

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
  ["cat", "Catégorie"], ["size", "Taille"], ["added", "Ajoutée"], ["last", "Dernière recherche"], ["copies", "Trackers"],
];
const SORT_DEFAULT_DIR = { rules: "asc", name: "asc", category: "asc", size: "desc", added: "desc", last: "asc", seeds: "asc" };
function prefGet(k, dflt) { try { const v = localStorage.getItem(`xse.${k}`); return v ? JSON.parse(v) : dflt; } catch { return dflt; } }
function prefSet(k, v) { try { localStorage.setItem(`xse.${k}`, JSON.stringify(v)); } catch { /* stockage indisponible */ } }
state.relSort = prefGet("relSort", { key: "seeds", dir: "asc" });
if (!(state.relSort.key in SORT_DEFAULT_DIR) || state.relSort.key === "rules") state.relSort = { key: "seeds", dir: "asc" };
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
  const rs = $("#rel-sort");
  if ([...rs.options].some((o) => o.value === state.relSort.key)) rs.value = state.relSort.key;
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
    state.indexers = d.indexers || [];
    state.trackerLabels = [...new Set(Object.values(d.trackers))].sort((a, b) => a.localeCompare(b));
    for (const k of [...state.selected]) if (!state.releases.some((r) => r.key === k)) state.selected.delete(k);
    $("#cnt-releases").textContent = d.releases.length;
    if (state.tab === "settings" && state.settings) { renderRules(); renderGroups(); renderAliases(); }
    renderSearch();
  } catch (err) {
    $("#rel-summary").textContent = err.message;
    $("#rel-body").innerHTML = `<tr><td colspan="99" class="empty">${esc(err.message)}. Vérifie QBT_URL et QBT_APIKEY.</td></tr>`;
  }
}

// Releases correspondant aux critères de l'étape 1 (mêmes règles que demandes.py côté serveur)
function matchesSearch(r) {
  if (search.groups.length || search.categories.length) {
    const g = search.groups.length ? groupFor(r.category, (state.settings && state.settings.groups) || []) : null;
    if (!has(search.categories, r.category || "") && !(g && has(search.groups, g.name))) return false;
  }
  const terms = search.text.toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length && !terms.every((t) => r.name.toLowerCase().includes(t))) return false;
  if (search.rule && !r.rules.includes(search.rule)) return false;
  if (search.maxSeeds !== "" && r.seeds > +search.maxSeeds) return false;
  if (search.state === "available" && !r.available) return false;
  if (search.state === "noorig" && r.has_original) return false;
  if (search.state === "dup" && new Set(r.copies.map((c) => c.tracker)).size === r.copies.length) return false;
  if (search.onTracker && !r.copies.some((c) => c.tracker === search.onTracker)) return false;
  return true;
}
function filteredReleases() {
  const list = state.releases.filter(matchesSearch);
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
  $("#rel-summary").textContent = `${list.length} sur ${state.releases.length} releases ` +
    `(${state.torrents} torrents terminés)`;
  const body = $("#rel-body");
  if (!shown.length) {
    body.innerHTML = `<tr><td colspan="99" class="empty">Aucune release ne correspond à ces critères.</td></tr>`;
  } else {
    body.innerHTML = shown.map((r) => {
      const flag = r.mode === "path"
        ? `<span class="rflag" title="Aucun torrent d'origine dans qBittorrent : toutes les copies sont des cross-seeds">· sans torrent d'origine</span>` : "";
      const row = `<tr data-key="${esc(r.key)}">
        <td class="c-check"><input type="checkbox" ${state.selected.has(r.key) ? "checked" : ""} aria-label="Sélectionner"></td>
        <td><span class="rname" title="Afficher le détail">${esc(r.name)}</span>${flag}</td>
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
  $("#rel-selcount").textContent = `${n} release${n > 1 ? "s" : ""} cochée${n > 1 ? "s" : ""} : seules celles-ci seront cherchées`;
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
    previewSoon();
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
  previewSoon(0);
});
$("#rel-clear-sel").addEventListener("click", () => { state.selected.clear(); renderReleases(); previewSoon(0); });
$("#rel-more").addEventListener("click", () => { state.limit += 200; renderReleases(); });
$("#rel-refresh").addEventListener("click", () => loadReleases(true));
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
$("#rel-sort").addEventListener("change", (e) => {
  state.relSort = { key: e.target.value, dir: SORT_DEFAULT_DIR[e.target.value] || "asc" };
  prefSet("relSort", state.relSort);
  state.limit = 200;
  applyRelColumns();
  renderReleases();
});

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
  if ((r.matched || []).length) parts.push(`<span class="res-warn" title="cross-seed a répondu « correspondance trouvée », mais ses logs ne disent pas s'il l'a injectée ou s'il l'avait déjà">trouvé, non confirmé :</span> ${chips(r.matched)}`);
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
  // mention des indexers toujours sur sa propre ligne : même hauteur pour toutes les lignes
  return `<div class="res-line">${parts.join(" ")}</div><div class="res-sub"><span class="res-meta" title="${esc(tip)}">${esc(meta)}</span></div>`;
}

const QLBL = { pending: "en attente", running: "en cours", done: "terminée", error: "erreur", timeout: "sans réponse" };
function queueRow(i) {
  const src = ["manuel", "demande", "scan", "programmée"].includes(i.source) ? i.source : `<span class="tag rule">${esc(i.source)}</span>`;
  const open = state.openItems.has(i.id);
  return `<tr data-item="${i.id}">
      <td><span class="st ${i.status}">${QLBL[i.status]}</span></td>
      <td><div class="rname qname" title="Afficher le détail">${esc(i.name)}</div>${i.mode === "path" ? `<div class="rmeta"><span class="tag path" title="Aucun torrent d'origine dans qBittorrent : toutes les copies sont des cross-seeds">fichier trouvé uniquement en cross-seed</span></div>` : ""}</td>
      <td>${src}</td>
      <td>${resultHtml(i)}</td>
      <td class="c-when">${queueWhen(i)}</td>
      <td class="c-act">${i.status === "pending" ? `<button class="small danger" data-rm="${i.id}">Retirer</button>` : ""}</td>
    </tr>` + (open ? `<tr class="detail qdetail"><td colspan="6">${queueDetail(i)}</td></tr>` : "");
}
// Détail d'une recherche de la file (clic sur son nom)
function queueDetail(i) {
  const at = (sec) => (sec ? new Date(sec * 1000).toLocaleString("fr-FR") : "—");
  const label = (k) => (state.indexers.find((x) => x.key === k) || {}).label || k;
  const list = (arr) => (arr && arr.length ? [...new Set(arr)].map(esc).join(", ") : "—");
  const rows = [["Ajoutée à la file", at(i.added)], ["Envoyée", at(i.sent)], ["Terminée", at(i.done)]];
  rows.push(["Cible", (i.targets || []).length ? list(i.targets.map(label)) : "selon le groupe"]);
  const r = i.result || {};
  if (r.routed) {
    rows.push(["Trackers interrogés", list(r.routed)]);
    if ((r.seeded || []).length) rows.push(["Non interrogés : déjà en seed", list(r.seeded)]);
    if ((r.paused || []).length) rows.push(["Non interrogés : en pause", list(r.paused)]);
    rows.push(["Résultats examinés", String(r.candidates ?? 0)]);
    rows.push(["Injectés", list(r.injected)]);
    if ((r.matched || []).length) rows.push(["Trouvés, non confirmés", list(r.matched)]);
    if ((r.exists || []).length) rows.push(["Déjà présents", list(r.exists)]);
    if ((r.failed || []).length) rows.push(["Échec d'injection", list(r.failed)]);
    if ((r.errors || []).length) rows.push(["Erreurs", r.errors.map(esc).join("<br>")]);
  } else if (i.result) {
    rows.push(["Recherche", "cross-seed, sur tous ses indexers"]);
    rows.push(["Torrents trouvés", String(r.found ?? "—")]);
    rows.push(["Injectés", list(r.injected)]);
    if ((r.failed || []).length) rows.push(["Échec d'injection", list(r.failed)]);
    if (r.skipped) rows.push(["Sautée", esc(explainSkip(r.skipped))]);
    if (r.refused) rows.push(["Refusée", esc(r.refused)]);
  }
  if (i.error) rows.push(["Erreur", esc(i.error)]);
  rows.push(["Envoyé à cross-seed", `<span class="mono">${esc(i.target || "")}</span>`]);
  return `<dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
}
// Mise à jour de la file sans gêner la lecture : rien n'est redessiné si rien n'a changé,
// ni pendant une sélection de texte dans la zone (copier-coller)
function setHtml(el, html) {
  if (el._html === html) return;
  const sel = window.getSelection();
  if (sel && !sel.isCollapsed && el.contains(sel.anchorNode)) return;
  el.innerHTML = html;
  el._html = html;
}
// Date de la recherche : fin si elle est terminée, départ si elle est en cours ; rien si elle attend encore
function queueWhen(i) {
  const at = i.done || i.sent;
  if (!at || i.status === "pending") return "";
  const d = new Date(at * 1000), today = new Date().toDateString() === d.toDateString();
  const txt = today ? d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  return `<span title="${esc(d.toLocaleString("fr-FR"))}">${i.status === "running" ? `depuis ${txt}` : txt}</span>`;
}
const sortQueue = (items) => {
  const order = { running: 0, pending: 1 };
  return [...items].sort((a, b) =>
    (order[a.status] ?? 2) - (order[b.status] ?? 2) || ((order[a.status] ?? 2) === 2 ? b.done - a.done : 0));
};

function batchStatus(b, q) {
  if (b.finished) {
    const when = fmtDate(new Date(b.finished * 1000).toISOString());
    return b.cancelled ? `annulée le ${when}` : `terminée le ${when}`;
  }
  if (state.readonly) return "lecture seule : la file ne part pas";
  if (q.paused) return "file en pause";
  const end = b.eta_seconds != null ? `, fin estimée dans ${fmtDuration(b.eta_seconds)}` : "";
  if (b.running || !b.starts_in) return `en cours${end}`;
  return `démarre dans ${fmtDuration(b.starts_in)}${end}`;
}
function batchHtml(b, q) {
  const pct = b.total ? Math.round((100 * b.done) / b.total) : 0;
  const live = !b.finished;
  const stats = [`${b.done}/${b.total}`];
  if (b.injected) stats.push(`<span class="res-hit">${b.injected} injecté${b.injected > 1 ? "s" : ""}</span>`);
  if (b.errors) stats.push(`<span class="res-bad">${b.errors} erreur${b.errors > 1 ? "s" : ""}</span>`);
  if (b.dropped) stats.push(`${b.dropped} retirée${b.dropped > 1 ? "s" : ""} avant de partir`);
  stats.push(esc(batchStatus(b, q)));
  const open = state.openBatches.has(b.id);
  const items = state.batchItems[b.id];
  const detail = !open ? "" : `<div class="b-items">${items ? `<div class="table-wrap"><table class="queue">
      <thead><tr><th>Statut</th><th>Release</th><th>Origine</th><th>Résultat</th><th class="c-when">Date</th><th></th></tr></thead>
      <tbody>${sortQueue(items).map(queueRow).join("") || `<tr><td colspan="6" class="empty">Plus aucune recherche de cette demande dans la file.</td></tr>`}</tbody></table></div>`
    : `<p class="muted">Chargement…</p>`}</div>`;
  return `<div class="batch${live ? "" : " finished"}" data-batch="${esc(b.id)}">
    <div class="b-head">
      <div class="b-title">${esc(b.label)} <span class="b-to">→ ${esc(b.detail || "selon le groupe")}</span></div>
      <div class="btns">
        <button class="small ghost" data-bdetail>${open ? "Masquer" : "Détail"}</button>
        ${b.pending ? `<button class="small danger" data-bcancel>Annuler</button>` : ""}
      </div>
    </div>
    <div class="bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><i style="width:${pct}%"></i></div>
    <div class="b-meta">${stats.join(" · ")}</div>
    ${detail}
  </div>`;
}

async function loadQueue() {
  try { state.queue = await api("/api/queue"); } catch { return; }
  const q = state.queue;
  for (const cnt of [$("#cnt-queue"), $("#cnt-queue2")]) {
    cnt.textContent = q.pending || "";
    cnt.classList.toggle("hot", q.pending > 0);
  }
  if (state.tab !== "follow" || state.sub !== "queue") return;
  $("#q-pause").textContent = q.paused ? "Reprendre" : "Pause";
  let st;
  if (state.readonly) st = `Lecture seule : la file ne part pas, ${q.pending} recherche(s) en attente.`;
  else if (q.paused) st = `En pause, ${q.pending} recherche(s) en attente.`;
  else if (q.pending) st = `${q.pending} en attente. Prochaine recherche dans ${fmtDuration(q.next_in)}, fin estimée dans ${fmtDuration(q.eta_seconds)} (une toutes les ${q.delay} s).`;
  else st = `File vide. Une recherche est envoyée toutes les ${q.delay} s quand la file se remplit.`;
  $("#q-state").textContent = st;
  // demandes ouvertes : leur détail est rechargé avec la file
  await Promise.all([...state.openBatches].map(async (id) => {
    if (!q.batches.some((b) => b.id === id)) { state.openBatches.delete(id); return; }
    try { state.batchItems[id] = (await api(`/api/queue/batch/${id}`)).items; } catch { /* demande supprimée */ }
  }));
  const live = q.batches.filter((b) => !b.finished), done = q.batches.filter((b) => b.finished).slice(0, 10);
  setHtml($("#q-batches"), live.concat(done).map((b) => batchHtml(b, q)).join(""));
  const singles = sortQueue(q.items);
  $("#q-singles").hidden = !singles.length && q.batches.length > 0;
  setHtml($("#q-body"), singles.length ? singles.map(queueRow).join("")
    : `<tr><td colspan="6" class="empty">Rien dans la file. Lance une recherche depuis <a href="#search">Chercher</a>.</td></tr>`);
}
// Clic sur le nom d'une recherche : ouvre ou ferme son détail (pas si l'on vient de sélectionner du texte)
function toggleItem(e) {
  const n = e.target.closest(".qname");
  if (!n) return false;
  const sel = window.getSelection();
  if (sel && !sel.isCollapsed) return true;
  const id = +n.closest("tr[data-item]").dataset.item;
  state.openItems.has(id) ? state.openItems.delete(id) : state.openItems.add(id);
  loadQueue();
  return true;
}

$("#q-batches").addEventListener("click", async (e) => {
  if (toggleItem(e)) return;
  const card = e.target.closest("[data-batch]");
  if (!card) return;
  const id = card.dataset.batch;
  if (e.target.closest("[data-bdetail]")) {
    state.openBatches.has(id) ? state.openBatches.delete(id) : state.openBatches.add(id);
    loadQueue();
  } else if (e.target.closest("[data-bcancel]")) {
    if (!confirm("Annuler cette demande ?\n\nLes recherches pas encore parties sont retirées de la file ; celle en cours se termine.")) return;
    try {
      const r = await api(`/api/queue/batch/${id}`, { method: "DELETE" });
      toast(`${r.removed} recherche(s) retirée(s)`);
    } catch (err) { toast(err.message, true); }
    loadQueue();
  } else if (e.target.closest("[data-rm]")) {
    await api(`/api/queue/${e.target.closest("[data-rm]").dataset.rm}`, { method: "DELETE" });
    loadQueue();
  }
});
$("#q-body").addEventListener("click", async (e) => {
  if (toggleItem(e)) return;
  const b = e.target.closest("[data-rm]");
  if (!b) return;
  await api(`/api/queue/${b.dataset.rm}`, { method: "DELETE" });
  loadQueue();
});
$("#q-pause").addEventListener("click", async () => {
  await api(`/api/queue/${state.queue && state.queue.paused ? "resume" : "pause"}`, { method: "POST" });
  loadQueue();
});
$("#q-clear-pending").addEventListener("click", async () => {
  if (!confirm("Retirer toutes les recherches en attente, demandes comprises ?")) return;
  const r = await api("/api/queue/clear-pending", { method: "POST" });
  toast(`${r.removed} recherche(s) retirée(s)`);
  loadQueue();
});
$("#q-clear-done").addEventListener("click", async () => {
  await api("/api/queue/clear-done", { method: "POST" });
  loadQueue();
});

// ---------- page 1, Chercher : « des releases (critères) → vers des trackers » ----------
// Les choix sont gardés dans ce navigateur. Les releases cochées dans la liste remplacent les critères.
const SEARCH_DEFAULT = { groups: [], categories: [], mode: "group", targets: [], text: "", rule: "", state: "",
  onTracker: "", fresh: "all", days: 30, maxSeeds: "", limit: "" };
const search = Object.assign({}, SEARCH_DEFAULT, prefGet("search", {}));
if (!["group", "manual"].includes(search.mode)) search.mode = search.targets.length ? "manual" : "group";
search.rule = "";   // critère retiré : les règles ne servent plus qu'à l'ordre de passage
Object.assign(search, { when: "now", every: 24, name: "" });   // étape 3 : pas mémorisée d'une visite à l'autre
// Modification d'une recherche programmée : { id, name, backup } (backup = critères de Chercher à restaurer)
state.editing = null;
const saveSearch = () => {
  if (state.editing) return;   // pendant une modification, on ne touche pas aux choix mémorisés
  const { when, every, name, ...rest } = search;
  prefSet("search", rest);
};
const has = (arr, v) => arr.some((x) => lc(x) === lc(v));
const toggle = (arr, v) => (has(arr, v) ? arr.filter((x) => lc(x) !== lc(v)) : [...arr, v]);

function scopeCount(pred) { return state.releases.filter(pred).length; }
function chipBtn(attrs, label, on, extra = "", style = "") {
  return `<button type="button" class="pchip${on ? " on" : ""}${extra}" ${attrs} aria-pressed="${on}"${style ? ` style="${style}"` : ""}>${label}</button>`;
}
function fillSelect(sel, values, current, first) {
  sel.innerHTML = `<option value="">${first}</option>` + values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  sel.value = values.includes(current) ? current : "";
  return sel.value;
}

function renderSearch() {
  if (!state.settings) { api("/api/settings").then((s) => { state.settings = s; renderSearch(); }).catch(() => {}); return; }
  const groups = state.settings.groups || [];
  const idx = activeIndexers();
  // choix devenus invalides (groupe supprimé, indexer retiré) : oubliés
  search.groups = search.groups.filter((n) => groups.some((g) => lc(g.name) === lc(n)));
  if (idx.length) search.targets = search.targets.filter((k) => idx.some((i) => i.key === k));

  // 1. releases
  const none = !search.groups.length && !search.categories.length;
  let html = chipBtn('data-scope="all"', `Toutes <small>${state.releases.length}</small>`, none);
  if (groups.length) {
    html += `<span class="chips-sep">Groupes</span>`;
    html += groups.map((g) => chipBtn(`data-group="${esc(g.name)}"`,
      `${esc(g.name)} <small>${scopeCount((r) => groupFor(r.category, groups) === g)}</small>`, has(search.groups, g.name), " grp")).join("");
  }
  html += `<span class="chips-sep">Catégories</span>`;
  html += knownCategories().map((c) => chipBtn(`data-cat="${esc(c)}"`,
    `${esc(catName(c))} <small>${scopeCount((r) => lc(r.category) === lc(c))}</small>`, has(search.categories, c))).join("");
  $("#s-scope").innerHTML = html;

  // affiner
  if (state.trackerLabels) search.onTracker = fillSelect($("#s-ontracker"), state.trackerLabels, search.onTracker, "Peu importe");
  $("#s-text").value = search.text;
  $("#s-state").value = search.state;
  $("#s-fresh").value = search.fresh;
  $("#s-days").value = search.days;
  $("#s-days-wrap").hidden = search.fresh !== "days";
  $("#s-maxseeds").value = search.maxSeeds;
  $("#s-limit").value = search.limit;
  refineSummary();

  // 2. trackers : selon le groupe de chaque release, ou choisis à la main
  $$("#s-mode [data-mode]").forEach((b) => { const on = b.dataset.mode === search.mode; b.classList.toggle("on", on); b.setAttribute("aria-checked", on); });
  const manual = search.mode === "manual";
  $("#s-targets").hidden = !manual;
  if (manual) {
    $("#s-targets").innerHTML = idx.map((i) => chipBtn(`data-target="${esc(i.key)}"`, esc(i.label),
      search.targets.includes(i.key), " trk", `--c:${trackerColor(i.label)}`)).join("")
      || `<span class="muted">Aucun indexer actif dans config.js.</span>`;
  }
  $("#s-targets-hint").innerHTML = manual
    ? (search.targets.length ? "Toutes les releases choisies sont cherchées sur les trackers cochés." : "Coche un ou plusieurs trackers.")
    : groups.length
      ? `Un film part vers les trackers de son groupe, un album vers ceux du sien… Groupes définis dans <a href="#settings" data-goto="set-groups">Réglages</a>.`
      : `Aucun groupe défini : cross-seed cherche sur tous ses indexers. Crée tes groupes dans <a href="#settings" data-goto="set-groups">Réglages</a>.`;
  // 3. quand
  $$("#s-when [data-when]").forEach((b) => { const on = b.dataset.when === search.when; b.classList.toggle("on", on); b.setAttribute("aria-checked", on); });
  const repeat = search.when === "repeat";
  $("#s-repeat").hidden = !repeat;
  $("#s-when-hint").hidden = !repeat;
  const ev = +search.every || 24;
  $("#s-every-d").value = Math.floor(ev / 24);
  $("#s-every-h").value = ev % 24;
  $("#s-name").value = search.name;
  $("#s-now-wrap").hidden = !!state.editing;
  $("#s-now").disabled = !!state.readonly;
  if (state.readonly) $("#s-now").checked = false;
  $("#s-editing").hidden = !state.editing;
  if (state.editing) $("#s-editing-name").textContent = `« ${state.editing.name} »`;
  renderReleases();
  previewSoon(0);
}

function refineSummary() {
  const n = [search.state, search.onTracker, search.fresh !== "all", search.maxSeeds !== "", search.limit !== ""].filter(Boolean).length;
  $("#s-refine-sum").textContent = n ? `(${n} filtre${n > 1 ? "s" : ""})` : "";
}

function criteria() {
  const c = { targets: search.mode === "manual" ? search.targets : [] };
  if (state.selected.size) { c.keys = [...state.selected]; return c; }
  Object.assign(c, { groups: search.groups, categories: search.categories, text: search.text,
    state: search.state, on_tracker: search.onTracker, fresh: { mode: search.fresh, days: +search.days || 30 } });
  if (search.maxSeeds !== "") c.max_seeds = +search.maxSeeds;
  if (search.limit !== "") c.limit = +search.limit;
  return c;
}

let previewTimer, previewSeq = 0, lastPreview = null;
function previewSoon(ms = 250) { clearTimeout(previewTimer); previewTimer = setTimeout(preview, ms); }
async function preview() {
  const seq = ++previewSeq;
  const go = $("#s-go");
  if (search.mode === "manual" && !search.targets.length) {
    $("#s-summary").innerHTML = "<b>Choisis au moins un tracker</b>";
    $("#s-why").textContent = "";
    go.disabled = true; go.textContent = goLabel(0);
    return;
  }
  go.disabled = true;
  let d;
  try { d = await api("/api/demandes/preview", { method: "POST", body: criteria() }); }
  catch (err) { if (seq === previewSeq) { $("#s-summary").textContent = err.message; $("#s-why").textContent = ""; } return; }
  if (seq !== previewSeq) return;   // une saisie plus récente a relancé l'aperçu
  lastPreview = d;
  const n = d.count;
  const to = search.mode === "manual" ? d.to : "selon le groupe";
  const what = state.selected.size ? `${n} release${n > 1 ? "s" : ""} cochée${n > 1 ? "s" : ""}` : `${n} release${n > 1 ? "s" : ""}`;
  $("#s-summary").innerHTML = n ? `<b>${what}</b> → ${esc(to)}` : `<b>Rien à chercher</b> → ${esc(to)}`;
  const why = [];
  if (search.when === "repeat") why.push(n ? `aujourd'hui ; puis ${everyText(+search.every || 24)}` : `rien aujourd'hui ; vérifié ${everyText(+search.every || 24)}`);
  else if (n) why.push(`environ ${fmtDuration(d.duration)}`);
  if (d.matched > n) why.push(`${d.matched - n} de plus au-delà de la limite`);
  const c = d.counts;
  if (c.seeded) why.push(`${c.seeded} déjà en seed sur la cible`);
  if (c.recent) why.push(`${c.recent} déjà cherchée${c.recent > 1 ? "s" : ""}`);
  if (c.no_target) why.push(`${c.no_target} sans tracker cible`);
  if (c.xs) why.push(`${c.xs} sans groupe : cross-seed, tous ses indexers`);
  $("#s-why").textContent = why.join(" · ");
  const repeat = search.when === "repeat";
  go.disabled = repeat ? state.selected.size > 0 : !n;
  go.textContent = goLabel(n);
  if (repeat && state.selected.size) $("#s-why").textContent = "Des releases cochées ne se programment pas : décoche-les et utilise des critères.";
}
function goLabel(n) {
  if (state.editing) return "Enregistrer les modifications";
  if (search.when === "repeat") return "Programmer";
  return n ? `Lancer ${n} recherche${n > 1 ? "s" : ""}` : "Lancer";
}

function searchChanged() { state.limit = 200; saveSearch(); renderSearch(); }
$("#s-scope").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.scope === "all") { search.groups = []; search.categories = []; }
  else if (b.dataset.group !== undefined) search.groups = toggle(search.groups, b.dataset.group);
  else if (b.dataset.cat !== undefined) search.categories = toggle(search.categories, b.dataset.cat);
  searchChanged();
});
$("#s-mode").addEventListener("click", (e) => {
  const b = e.target.closest("[data-mode]");
  if (!b) return;
  search.mode = b.dataset.mode;
  searchChanged();
});
$("#s-targets").addEventListener("click", (e) => {
  const b = e.target.closest("[data-target]");
  if (!b) return;
  const k = b.dataset.target;
  search.targets = search.targets.includes(k) ? search.targets.filter((x) => x !== k) : [...search.targets, k];
  searchChanged();
});
const REFINE = { "#s-text": "text", "#s-state": "state", "#s-ontracker": "onTracker", "#s-fresh": "fresh",
  "#s-days": "days", "#s-maxseeds": "maxSeeds", "#s-limit": "limit" };
let refineTimer;
for (const [sel, k] of Object.entries(REFINE)) {
  $(sel).addEventListener("input", (e) => {
    search[k] = e.target.value.trim();
    $("#s-days-wrap").hidden = search.fresh !== "days";
    refineSummary();
    saveSearch();
    clearTimeout(refineTimer);
    refineTimer = setTimeout(() => { state.limit = 200; renderReleases(); }, 150);
    previewSoon();
  });
}
$("#s-reset").addEventListener("click", () => {
  Object.assign(search, { rule: "", state: "", onTracker: "", fresh: "all", maxSeeds: "", limit: "" });
  searchChanged();
});
$("#s-when").addEventListener("click", (e) => {
  const b = e.target.closest("[data-when]");
  if (!b || state.editing) return;   // une recherche programmée reste programmée
  search.when = b.dataset.when;
  renderSearch();
});
// fréquence : jours + heures, gardée en heures (au moins 1 h)
function readEvery() {
  const d = Math.max(0, +$("#s-every-d").value || 0), h = Math.max(0, +$("#s-every-h").value || 0);
  search.every = Math.min(720, Math.max(1, d * 24 + h));   // 1 h à 30 jours
  previewSoon();
}
$("#s-every-d").addEventListener("input", readEvery);
$("#s-every-h").addEventListener("input", readEvery);
$("#s-name").addEventListener("input", (e) => { search.name = e.target.value; });
$("#s-edit-cancel").addEventListener("click", () => endEdit(true));

async function saveSchedule(btn) {
  const crit = criteria();
  delete crit.keys;
  const what = lastPreview ? lastPreview.label : "";
  const body = { name: search.name.trim() || what, every_hours: +search.every || 24, criteria: crit };
  btn.disabled = true;
  try {
    if (state.editing) {
      await api(`/api/schedules/${state.editing.id}`, { method: "PATCH", body });
      toast("Recherche programmée modifiée");
      endEdit(false);
    } else {
      await api("/api/schedules", { method: "POST", body: { ...body, run_now: $("#s-now").checked } });
      toast($("#s-now").checked ? "Recherche programmée, premier passage lancé" : "Recherche programmée");
      search.when = "now";
    }
    showCsub("prog");
    window.scrollTo(0, 0);
  } catch (err) { toast(err.message, true); }
  btn.disabled = false;
}
$("#s-go").addEventListener("click", async (e) => {
  if (search.when === "repeat" || state.editing) { saveSchedule(e.target); return; }
  const n = lastPreview ? lastPreview.count : 0;
  if (n > 100 && !confirm(`Lancer ${n} recherches ?\n\nÀ une recherche toutes les ${lastPreview.delay} s, il faudra environ ${fmtDuration(lastPreview.duration)}. Tu pourras annuler depuis « Suivre l'avancement ».`)) return;
  e.target.disabled = true;
  try {
    const r = await api("/api/demandes", { method: "POST", body: criteria() });
    toast(`${r.added} recherche${r.added > 1 ? "s" : ""} ajoutée${r.added > 1 ? "s" : ""}${r.skipped ? `, ${r.skipped} déjà en file` : ""}`);
    state.selected.clear();
    state.sub = "queue";
    location.hash = "#follow";
    window.scrollTo(0, 0);
  } catch (err) { toast(err.message, true); e.target.disabled = false; }
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
    if (ok && state.tab === "settings") loadIndexers();
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

// ---------- groupes de trackers ----------
const lc = (x) => String(x ?? "").trim().toLowerCase();
const catName = (c) => (c ? c : "sans catégorie");
// Même règle que config.group_for côté serveur
function groupFor(category, groups) {
  const c = lc(category);
  return groups.find((g) => g.categories.some((x) => lc(x) === c)) || groups.find((g) => g.others) || null;
}
function knownCategories() {
  const set = new Map();
  for (const r of state.releases) set.set(lc(r.category), r.category || "");
  for (const g of (state.settings && state.settings.groups) || []) for (const c of g.categories) if (!set.has(lc(c))) set.set(lc(c), c);
  return [...set.values()].sort((a, b) => (a === "") - (b === "") || a.localeCompare(b));
}
function activeIndexers() {
  // indexers actifs dans config.js, avec le nom affiché dans la colonne Trackers
  return state.indexers.length ? state.indexers
    : (idxData ? idxData.items.filter((i) => i.config === "active").map((i) => ({ key: i.key, label: i.name })) : []);
}
function groupInfoHtml(g) {
  const groups = state.settings.groups;
  const n = state.releases.filter((r) => groupFor(r.category, groups) === g).length;
  const rel = n ? `<span class="res-hit">${n} release${n > 1 ? "s" : ""}</span>` : `<span class="res-warn">Aucune release</span>`;
  const idx = g.indexers.length ? `cherchée${n > 1 ? "s" : ""} sur ${g.indexers.length} tracker${g.indexers.length > 1 ? "s" : ""}`
    : `<span class="res-warn">aucun tracker coché : recherche par cross-seed sur tous ses indexers</span>`;
  return `${rel}, ${idx}`;
}
function renderGroups() {
  const groups = state.settings.groups || (state.settings.groups = []);
  const idx = activeIndexers();
  if (!idx.length && !idxData) { $("#groups").innerHTML = `<p class="muted">Chargement des indexers…</p>`; loadIndexers().then(() => renderGroups()); return; }
  const cats = knownCategories();
  const othersOwner = groups.find((g) => g.others);
  $("#groups").innerHTML = groups.map((g, n) => {
    const catBoxes = cats.map((c) => {
      const owner = groups.find((x) => x !== g && x.categories.some((y) => lc(y) === lc(c)));
      const on = g.categories.some((y) => lc(y) === lc(c));
      return `<label class="pick${on ? " on" : ""}${owner ? " taken" : ""}"${owner ? ` title="Déjà dans le groupe ${esc(owner.name)}"` : ""}>
        <input type="checkbox" data-cat="${esc(c)}" ${on ? "checked" : ""} ${owner ? "disabled" : ""}>${esc(catName(c))}${owner ? ` <small>(${esc(owner.name)})</small>` : ""}</label>`;
    }).join("");
    const known = new Set(idx.map((i) => i.key));
    const extra = g.indexers.filter((k) => !known.has(k)).map((k) => ({ key: k, label: (idxData && (idxData.items.find((i) => i.key === k) || {}).name) || k, off: true }));
    const idxBoxes = idx.concat(extra).map((i) => {
      const on = g.indexers.includes(i.key);
      return `<label class="pick${on ? " on" : ""}${i.off ? " taken" : ""}" style="--c:${trackerColor(i.label)}"${i.off ? ' title="Suspendu ou retiré de config.js : ignoré"' : ""}>
        <input type="checkbox" data-key="${esc(i.key)}" ${on ? "checked" : ""}>${esc(i.label)}${i.off ? " <small>(inactif)</small>" : ""}</label>`;
    }).join("");
    return `<div class="group" data-i="${n}">
      <div class="group-head">
        <input type="text" data-f="name" value="${esc(g.name)}" placeholder="Nom du groupe, ex. Vidéo" aria-label="Nom du groupe">
        <button class="small danger" data-rm-group>Supprimer</button>
      </div>
      <div class="group-row"><span class="group-lbl">Catégories</span><div class="picks">${catBoxes}
        <input type="text" class="cat-add" data-f="newcat" list="rule-cats" placeholder="autre catégorie…" aria-label="Ajouter une catégorie"></div></div>
      <label class="check others"><input type="checkbox" data-f="others" ${g.others ? "checked" : ""} ${othersOwner && othersOwner !== g ? "disabled" : ""}>
        Toutes les autres catégories${othersOwner && othersOwner !== g ? ` (déjà le groupe ${esc(othersOwner.name)})` : " (celles rangées dans aucun groupe, y compris les nouvelles)"}</label>
      <div class="group-row"><span class="group-lbl">Trackers</span><div class="picks">${idxBoxes || `<span class="muted">Aucun indexer actif dans config.js</span>`}</div></div>
      <div class="rule-info">${groupInfoHtml(g)}</div>
    </div>`;
  }).join("") || `<p class="muted">Aucun groupe : chaque recherche passe par cross-seed, sur tous ses indexers.</p>`;
}
function groupChanged(row) {
  state.settingsDirty = true;
  renderGroups();
  const el = $(`#groups .group[data-i="${row}"] [data-f="name"]`);
  return el;
}
$("#groups").addEventListener("change", (e) => {
  const row = e.target.closest(".group");
  if (!row) return;
  const n = +row.dataset.i, g = state.settings.groups[n];
  if (e.target.dataset.cat !== undefined) {
    const c = e.target.dataset.cat;
    g.categories = e.target.checked ? [...g.categories, c] : g.categories.filter((x) => lc(x) !== lc(c));
  } else if (e.target.dataset.key) {
    const k = e.target.dataset.key;
    g.indexers = e.target.checked ? [...new Set([...g.indexers, k])] : g.indexers.filter((x) => x !== k);
  } else if (e.target.dataset.f === "others") {
    g.others = e.target.checked;
  } else if (e.target.dataset.f === "newcat") {
    const c = e.target.value.trim();
    const taken = state.settings.groups.some((x) => x.categories.some((y) => lc(y) === lc(c)));
    if (c && !taken) g.categories.push(c);
    else if (taken) toast(`La catégorie ${c} est déjà dans un groupe`, true);
  } else return;
  groupChanged(n);
});
$("#groups").addEventListener("input", (e) => {
  if (e.target.dataset.f !== "name") return;
  state.settings.groups[+e.target.closest(".group").dataset.i].name = e.target.value;
  state.settingsDirty = true;
});
$("#groups").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.dataset.f === "newcat") { e.preventDefault(); e.target.blur(); }
});
$("#groups").addEventListener("click", (e) => {
  const row = e.target.closest(".group");
  if (!row || !e.target.closest("[data-rm-group]")) return;
  const g = state.settings.groups[+row.dataset.i];
  if (!confirm(`Supprimer le groupe ${g.name || "sans nom"} ?`)) return;
  state.settings.groups.splice(+row.dataset.i, 1);
  state.settingsDirty = true;
  renderGroups();
});
$("#group-add").addEventListener("click", () => {
  state.settings.groups.push({ name: "", categories: [], others: false, indexers: [] });
  state.settingsDirty = true;
  renderGroups();
  $("#groups .group:last-of-type [data-f=\"name\"]").focus();
});

// ---------- scan complet de cross-seed (searchCadence) ----------
function scanXsNote(info, pendingRestart) {
  const x = info.xs_cadence;
  $("#scan-replace").disabled = !x || state.configWrite === false;
  if (!x) return "searchCadence introuvable dans config.js : à modifier à la main.";
  $("#scan-replace").checked = x.disabled;
  const now = x.disabled ? "Dans config.js : searchCadence: null, le scan complet de cross-seed est coupé."
    : `Dans config.js : searchCadence = ${x.value}, cross-seed fait son propre scan complet.`;
  return pendingRestart ? `${now} Redémarrage de cross-seed nécessaire pour l'appliquer.` : now;
}
async function renderScan() {
  try { $("#scan-xs-note").textContent = scanXsNote(await api("/api/scan"), !$("#scan-xs-restart").hidden); }
  catch (err) { $("#scan-xs-note").textContent = err.message; }
}
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

// ---------- recherches programmées : Chercher › Programmées ----------
const fmtWhen = (sec) => new Date(sec * 1000).toLocaleString("fr-FR", { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
function everyText(h) {
  if (h % 168 === 0) return h === 168 ? "toutes les semaines" : `toutes les ${h / 168} semaines`;
  if (h % 24 === 0) return h === 24 ? "tous les jours" : `tous les ${h / 24} jours`;
  if (h < 24) return `toutes les ${h} h`;
  const d = Math.floor(h / 24);
  return `tous les ${d} jour${d > 1 ? "s" : ""} ${h % 24} h`;
}
state.schedules = [];
function scheduleHtml(s, readonly) {
  const last = s.last_run
    ? (s.last_error ? `<span class="res-bad">Dernier passage le ${fmtWhen(s.last_run)} : ${esc(s.last_error)}</span>`
      : `Dernier passage le ${fmtWhen(s.last_run)} : ${s.last_added ?? 0} mise${s.last_added > 1 ? "s" : ""} en file · <a href="#follow">voir l'avancement</a>`)
    : "Pas encore lancée";
  const next = !s.enabled ? "en pause" : readonly ? "lecture seule : ne se lance pas" : `prochain passage ${fmtWhen(s.next_run)}`;
  return `<div class="sched${s.enabled ? "" : " off"}" data-sched="${esc(s.id)}">
    <div class="b-head">
      <div class="b-title">${esc(s.name)} <span class="b-to">→ ${esc(s.to)}</span></div>
      <div class="btns">
        <button class="small" data-sedit>Modifier</button>
        <button class="small ghost" data-srun ${readonly ? "disabled" : ""}>Lancer maintenant</button>
        <button class="small ghost" data-stoggle>${s.enabled ? "Pause" : "Reprendre"}</button>
        <button class="small danger" data-sdel>Supprimer</button>
      </div>
    </div>
    <div class="b-meta">${esc(s.what)}</div>
    <div class="b-meta">${everyText(s.every_hours)} · ${next}</div>
    <div class="b-meta">${last}</div>
  </div>`;
}
async function loadSchedules() {
  let d;
  try { d = await api("/api/schedules"); } catch { return; }
  state.schedules = d.items;
  const cnt = $("#cnt-prog");
  cnt.textContent = d.items.length || "";
  $("#prog-list").innerHTML = d.items.length
    ? `<div class="scheds">${d.items.map((s) => scheduleHtml(s, d.readonly)).join("")}</div>`
    : `<p class="empty">Aucune recherche programmée. Dans « Nouvelle recherche », choisis « Régulièrement » à l'étape 3.</p>`;
}
$("#prog-list").addEventListener("click", async (e) => {
  const card = e.target.closest("[data-sched]");
  if (!card) return;
  const id = card.dataset.sched, s = state.schedules.find((x) => x.id === id);
  if (!s) return;
  try {
    if (e.target.closest("[data-sedit]")) { startEdit(s); return; }
    if (e.target.closest("[data-srun]")) {
      await api(`/api/schedules/${id}/run`, { method: "POST" });
      toast(`${s.name} : passage lancé`);
      loadQueue();
    } else if (e.target.closest("[data-stoggle]")) {
      await api(`/api/schedules/${id}`, { method: "PATCH", body: { enabled: !s.enabled } });
    } else if (e.target.closest("[data-sdel]")) {
      if (!confirm(`Supprimer la recherche programmée « ${s.name} » ?\n\nLes recherches déjà en file ne sont pas retirées.`)) return;
      await api(`/api/schedules/${id}`, { method: "DELETE" });
    } else return;
  } catch (err) { toast(err.message, true); }
  loadSchedules();
});

// « Modifier » : Nouvelle recherche pré-remplie avec les critères, la cible et la fréquence
function startEdit(s) {
  const c = s.criteria || {};
  state.editing = { id: s.id, name: s.name, backup: JSON.parse(JSON.stringify(search)), selected: [...state.selected] };
  state.selected.clear();
  Object.assign(search, SEARCH_DEFAULT, {
    groups: c.groups || [], categories: c.categories || [], text: c.text || "", state: c.state || "",
    onTracker: c.on_tracker || "", fresh: (c.fresh && c.fresh.mode) || "all", days: (c.fresh && c.fresh.days) || 30,
    maxSeeds: c.max_seeds ?? "", limit: c.limit ?? "",
    mode: (c.targets || []).length ? "manual" : "group", targets: c.targets || [],
    when: "repeat", every: s.every_hours, name: s.name,
  });
  showCsub("new");
  window.scrollTo(0, 0);
}
function endEdit(restore) {
  const ed = state.editing;
  state.editing = null;
  if (ed) {
    Object.assign(search, ed.backup, { when: "now", name: "" });
    ed.selected.forEach((k) => state.selected.add(k));
  }
  if (restore) showCsub("prog");
}

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
  renderGroups();
  renderScan();
  renderAliases();
  renderSources();
  renderXsForm();
  loadIndexers();
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
  const unnamed = (state.settings.groups || []).filter((g) => !g.name.trim()).length;
  if (unnamed) { toast(`Donne un nom à chaque groupe de trackers (${unnamed} sans nom)`, true); return false; }
  const aliases = {};
  $$("#aliases input").forEach((i) => { if (i.value.trim()) aliases[i.dataset.host] = i.value.trim(); });
  try {
    state.settings = await api("/api/settings", {
      method: "PUT",
      body: { delay: +$("#set-delay").value, rules: state.settings.rules, groups: state.settings.groups, tracker_aliases: aliases },
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
showTab(location.hash.slice(1) || "search");
loadHealth();
loadReleases();
loadQueue();
loadPending();
loadIndexers();
loadSchedules();
setInterval(loadHealth, 30000);
setInterval(loadQueue, 3000);
setInterval(() => { if (!document.hidden) loadPending(); }, 60000);
