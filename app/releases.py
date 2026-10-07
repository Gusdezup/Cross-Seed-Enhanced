"""Regroupe les torrents qBittorrent en releases (copies cross-seedées et hardlinks renommés)."""
import re
from urllib.parse import parse_qs, urlparse

from . import config

_cache = {"by_key": {}}


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".")


def is_cross_seed(t: dict) -> bool:
    tags = {x.strip() for x in (t.get("tags") or "").split(",")}
    cat = t.get("category") or ""
    return "cross-seed" in tags or cat.endswith(".cross-seed") or cat == "cross-seed-link"


def tracker_host(t: dict) -> str:
    url = t.get("tracker") or ""
    if not url and t.get("magnet_uri"):
        trs = parse_qs(urlparse(t["magnet_uri"]).query).get("tr", [])
        url = trs[0] if trs else ""
    return (urlparse(url).hostname or "").lower()


def default_label(host: str) -> str:
    if not host:
        return "?"
    if re.fullmatch(r"[\d.]+", host):
        return host
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else host


def compile_rules(rules: list) -> list:
    out = []
    for r in rules:
        if not r.get("enabled", True):
            continue
        try:
            out.append((config.rule_label(r), re.compile(r["pattern"], re.I)))
        except re.error:
            pass
    return out


def release_category(orig: dict | None, ts: list) -> str:
    """Catégorie de la release : celle du torrent d'origine, sinon celle d'une copie sans le suffixe .cross-seed."""
    if orig:
        return orig.get("category") or ""
    for t in ts:
        cat = t.get("category") or ""
        if cat.endswith(".cross-seed"):
            return cat[: -len(".cross-seed")]
    return ts[0].get("category") or ""


def _domain(host: str) -> str:
    return host if re.fullmatch(r"[\d.]+", host or "") else ".".join((host or "").split(".")[-2:])


def make_resolver(settings: dict, site_names: dict | None = None):
    """Nom affiché d'un tracker à partir d'un nom d'hôte : alias des réglages, puis nom Prowlarr/Jackett
    du domaine, puis nom Prowlarr/Jackett de même nom de base (tk.v3x.tw -> V3X), puis le domaine."""
    aliases = settings.get("tracker_aliases", {})
    site_names = site_names or {}
    by_base = {}
    for dom, name in site_names.items():
        by_base.setdefault(default_label(dom).lower(), name)

    def resolve(host: str) -> str:
        host = (host or "").lower()
        return (aliases.get(host) or site_names.get(_domain(host))
                or by_base.get(default_label(host).lower()) or default_label(host))
    return resolve


def build(torrents: list, settings: dict, site_names: dict | None = None) -> list:
    """site_names : {domaine: nom} venant de Prowlarr, utilisé quand aucun nom n'est saisi dans les réglages."""
    resolve = make_resolver(settings, site_names)
    rules = compile_rules(settings.get("rules", []))
    groups: dict = {}
    for t in torrents:
        key = f"{norm(t['name'])}|{t.get('size', 0)}"
        groups.setdefault(key, []).append(t)

    releases = []
    for key, ts in groups.items():
        orig = next((t for t in ts if not is_cross_seed(t)), None)
        ref = orig or ts[0]
        if orig:
            payload = {"infoHash": orig["hash"]}
        else:
            payload = {"path": ref.get("content_path") or ""}
        copies = []
        for t in ts:
            host = tracker_host(t)
            copies.append({
                "hash": t["hash"],
                "name": t["name"],
                "host": host,
                "tracker": resolve(host),
                "cross_seed": is_cross_seed(t),
                "category": t.get("category") or "",
                "state": t.get("state") or "",
                "path": t.get("content_path") or "",
                "added_on": t.get("added_on") or 0,
            })
        copies.sort(key=lambda c: (c["cross_seed"], c["added_on"]))
        matched = [name for name, rx in rules if any(rx.search(t["name"]) for t in ts)]
        releases.append({
            "key": key,
            "name": ref["name"],
            "size": ref.get("size", 0),
            "copies": copies,
            "count": len(copies),
            "has_original": orig is not None,
            "mode": "hash" if orig else "path",
            "payload": payload,
            "rules": matched,
            "added_on": min(c["added_on"] for c in copies),
            "category": release_category(orig, ts),
            "last_search": None,
            "trackers": [],
            "seeds": len({c["tracker"] for c in copies}),
            "available": 0,
        })
    releases.sort(key=lambda r: r["name"].lower())
    _cache["by_key"] = {r["key"]: r for r in releases}
    return releases


def _key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (label or "").lower())


STATE_ORDER = {"seed": 0, "available": 1, "nomatch": 2, "never": 3}


def attach_trackers(items: list, state: dict, indexers: dict, all_hashes: set, resolve) -> None:
    """Pour chaque release, l'état de chaque tracker :
    seed      — une copie est dans qBittorrent ;
    available — cross-seed a trouvé une correspondance dont le torrent n'est pas dans qBittorrent ;
    nomatch   — cherchée sur ce tracker, sans correspondance utilisable ;
    never     — indexer actif dans config.js, jamais interrogé pour cette release.
    indexers : {id cross-seed: {"label", "active"}} ; state : xsdb.search_state()."""
    searches, decisions = state.get("searches", {}), state.get("decisions", {})
    known = {_key(i["label"]): i["label"] for i in indexers.values()}

    def label_of(source: str) -> str:
        if source.startswith("name:"):
            name = source[5:]
            return known.get(_key(name), name)
        return resolve(source) if source else "?"

    for r in items:
        names = {n for c in r["copies"] for n in (c["name"], c["path"]) if n}
        tr: dict = {}

        def slot(label):
            return tr.setdefault(_key(label), {"label": label, "state": None, "origin": False,
                                               "last_search": None, "match": None, "copies": []})
        for c in r["copies"]:
            t = slot(c["tracker"])
            t["state"] = "seed"
            t["origin"] = t["origin"] or not c["cross_seed"]
            t["copies"].append(c["hash"])
        last_any = []
        for n in names:
            for idx, last in searches.get(n, []):
                info = indexers.get(idx)
                if not info:
                    continue   # indexer retiré de config.js
                t = slot(info["label"])
                if last and (not t["last_search"] or last > t["last_search"]):
                    t["last_search"] = last
                if last:
                    last_any.append(last)
                if t["state"] is None:
                    t["state"] = "nomatch"
            for source, ih, dec, _seen in decisions.get(n, []):
                if dec not in MATCHES:
                    continue
                t = slot(label_of(source))
                if ih and ih in all_hashes:
                    # torrent déjà présent : en seed (ou en cours) sur ce tracker
                    if t["state"] != "seed":
                        t["state"] = "seed"
                elif t["state"] != "seed":
                    t["state"] = "available"
                    t["match"] = dec
        for i in indexers.values():
            if i["active"] and _key(i["label"]) not in tr:
                slot(i["label"])["state"] = "never"
        for t in tr.values():
            t["state"] = t["state"] or "nomatch"
        r["trackers"] = sorted(tr.values(), key=lambda t: (STATE_ORDER[t["state"]], t["label"].lower()))
        r["seeds"] = sum(t["state"] == "seed" for t in r["trackers"])
        r["available"] = sum(t["state"] == "available" for t in r["trackers"])
        r["last_search"] = max(last_any) if last_any else None


MATCHES = {"MATCH", "MATCH_SIZE_ONLY", "MATCH_PARTIAL"}


def get(key: str):
    return _cache["by_key"].get(key)
