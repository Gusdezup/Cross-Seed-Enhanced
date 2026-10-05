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


def _domain(host: str) -> str:
    return host if re.fullmatch(r"[\d.]+", host or "") else ".".join((host or "").split(".")[-2:])


def build(torrents: list, settings: dict, site_names: dict | None = None) -> list:
    """site_names : {domaine: nom} venant de Prowlarr, utilisé quand aucun nom n'est saisi dans les réglages."""
    aliases = settings.get("tracker_aliases", {})
    site_names = site_names or {}
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
                "tracker": aliases.get(host) or site_names.get(_domain(host)) or default_label(host),
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
        })
    releases.sort(key=lambda r: r["name"].lower())
    _cache["by_key"] = {r["key"]: r for r in releases}
    return releases


def get(key: str):
    return _cache["by_key"].get(key)
