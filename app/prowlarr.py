"""Rapprochement des indexers Prowlarr avec le tableau torznab de cross-seed."""
import re
import time
from urllib.parse import urlparse

from . import clients, config, logs, xsdb


def endpoint():
    """(adresse, clé, origine) pour joindre Prowlarr : .env, puis Réglages,
    puis déduction depuis les lignes Torznab de config.js. None si rien n'est trouvé."""
    found = xsdb.prowlarr_from_config()
    s_url, s_key, origin = config.source("prowlarr")
    url = s_url or (found[0] if found else "")
    key = s_key or (found[1] if found else "")
    if not (url and key):
        return None
    return url.rstrip("/"), key, (origin if s_url else "config.js")


async def fetch():
    """Indexers et états Prowlarr ; RuntimeError avec un message utile en cas d'échec."""
    ep = endpoint()
    if not ep:
        raise RuntimeError("Prowlarr non configuré : renseigne son adresse et sa clé API "
                           "dans Réglages › Sources d'indexers")
    url, key, src = ep
    try:
        return await clients.prowlarr_indexers(url, key)
    except Exception as e:  # noqa: BLE001
        hint = " : renseigne son adresse dans Réglages › Sources d'indexers" if src == "config.js" else ""
        raise RuntimeError(f"Prowlarr injoignable à {url} (adresse lue dans {src}){hint}. "
                           f"Détail : {(str(e) or type(e).__name__)[:150]}") from e


def _site(i: dict) -> str:
    """Identifie le site réel : deux déclarations Prowlarr du même tracker ont la même URL de base."""
    base = next((f.get("value") for f in i.get("fields", []) if f.get("name") == "baseUrl" and f.get("value")), None)
    url = base or (i.get("indexerUrls") or [None])[0]
    host = urlparse(url).hostname if url else None
    return (host or i.get("definitionName") or str(i.get("id"))).lower().removeprefix("www.")


async def enrich(data: dict) -> dict:
    """Ajoute à la réponse /api/indexers : nom et état Prowlarr de chaque indexer, doublons de site,
    et la liste des indexers torrent de Prowlarr absents de config.js."""
    px = {"configured": endpoint() is not None, "error": None, "absent": []}
    data["prowlarr"] = px
    if not px["configured"]:
        return data
    try:
        indexers, statuses = await fetch()
    except RuntimeError as e:
        px["error"] = str(e)
        return data
    try:
        url_of = xsdb.torznab_url
        url_of(0)
    except ValueError as e:
        px["error"] = str(e)
        return data
    failing = {s.get("indexerId"): s.get("disabledTill") for s in statuses if s.get("disabledTill")}
    by_key = {}
    for i in indexers:
        if i.get("protocol") != "torrent":
            continue
        by_key[logs.normalize_url(url_of(i["id"]))] = {
            "id": i["id"], "name": i.get("name") or f"Prowlarr n°{i['id']}",
            "enabled": bool(i.get("enable", True)), "site": _site(i),
            "failing_until": failing.get(i["id"]),
        }
    in_config = [it for it in data["items"] if it.get("config")]
    for it in data["items"]:
        p = by_key.get(it["key"])
        if p:
            it["prowlarr"] = dict(p)
            it["name"] = p["name"]
    # même site déclaré plusieurs fois dans config.js
    for it in in_config:
        p = it.get("prowlarr")
        if p:
            p["same_site"] = [o["name"] for o in in_config
                              if o is not it and o.get("prowlarr", {}).get("site") == p["site"]]
    config_keys = {it["key"] for it in in_config}
    for key, p in sorted(by_key.items(), key=lambda kv: kv[1]["name"].lower()):
        if key in config_keys:
            continue
        p = dict(p, key=key, same_site=[o["name"] for o in in_config
                                        if o.get("prowlarr", {}).get("site") == p["site"]])
        px["absent"].append(p)
    return data


# --- noms de trackers -----------------------------------------------------------

_names_cache = {"ts": 0.0, "names": {}}


def domain(host: str) -> str:
    """Domaine principal : tracker.hdf.world -> hdf.world (une IP reste telle quelle)."""
    host = (host or "").lower()
    if not host or re.fullmatch(r"[\d.]+", host):
        return host
    return ".".join(host.split(".")[-2:])


def clean_name(name: str) -> str:
    """« The Old School (API) » -> « The Old School »."""
    return re.sub(r"\s*\((?:API|RSS)\)\s*$", "", name or "", flags=re.I).strip()


async def site_names() -> dict:
    """{domaine: nom Prowlarr} pour nommer les trackers des torrents. Cache de 5 min ;
    Prowlarr absent ou injoignable : on garde le dernier résultat (ou rien)."""
    if time.time() - _names_cache["ts"] < 300:
        return _names_cache["names"]
    names = _names_cache["names"]
    if endpoint():
        try:
            indexers, _ = await fetch()
            names = {}
            for i in sorted(indexers, key=lambda x: x.get("id", 0)):
                if i.get("protocol") == "torrent":
                    names.setdefault(domain(_site(i)), clean_name(i.get("name")))
        except RuntimeError:
            pass
    _names_cache.update(ts=time.time(), names=names)
    return names
