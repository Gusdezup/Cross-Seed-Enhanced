"""Rapprochement des indexers Jackett avec le tableau torznab de cross-seed.

Même principe que prowlarr.py. Jackett n'expose sans session admin que l'API Torznab :
la liste des indexers configurés vient de l'agrégat « all » (t=indexers), qui ne sert
qu'à lister — cross-seed reçoit toujours une URL par indexer, jamais celle de l'agrégat.
"""
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from . import clients, config, logs, prowlarr, xsdb

# Indexers virtuels de Jackett : agrégat et filtres (« all », « !status:failing », « tag:fr »…)
_VIRTUAL = re.compile(r"^(all|!.*|.*:.*)$")


def endpoint():
    """(adresse, clé, origine) pour joindre Jackett : le .env est prioritaire,
    sinon déduction depuis les lignes Torznab Jackett de config.js. None si rien n'est trouvé."""
    found = xsdb.jackett_from_config()
    url = config.JACKETT_URL or (found[0] if found else "")
    key = config.JACKETT_APIKEY or (found[1] if found else "")
    if not (url and key):
        return None
    src = ".env" if config.JACKETT_URL else "config.js"
    return url.rstrip("/"), key, src


def _parse(xml_text: str) -> list:
    root = ET.fromstring(xml_text)
    if root.tag == "error":
        raise RuntimeError(f"Jackett : {root.get('description') or root.get('code')}")
    out = []
    for i in root.iter("indexer"):
        iid = i.get("id") or ""
        if not iid or _VIRTUAL.match(iid):
            continue
        out.append({
            "id": iid,
            "name": (i.findtext("title") or iid).strip(),
            "link": (i.findtext("link") or "").strip(),
            "type": (i.findtext("type") or "").strip(),
        })
    return out


async def fetch() -> list:
    """Indexers configurés dans Jackett ; RuntimeError avec un message utile en cas d'échec."""
    ep = endpoint()
    if not ep:
        raise RuntimeError("Jackett introuvable : aucune ligne Torznab Jackett dans config.js "
                           "et JACKETT_URL / JACKETT_APIKEY vides dans le .env")
    url, key, src = ep
    try:
        return _parse(await clients.jackett_indexers(url, key))
    except Exception as e:  # noqa: BLE001
        hint = " : renseigne JACKETT_URL dans le .env" if src == "config.js" else ""
        raise RuntimeError(f"Jackett injoignable à {url} (adresse lue dans {src}){hint}. "
                           f"Détail : {(str(e) or type(e).__name__)[:150]}") from e


def _site(i: dict) -> str:
    host = urlparse(i["link"]).hostname if i.get("link") else None
    return (host or i["id"]).lower().removeprefix("www.")


async def enrich(data: dict) -> dict:
    """Ajoute à la réponse /api/indexers : nom Jackett des indexers de config.js
    et la liste des indexers Jackett absents de config.js."""
    jk = {"configured": endpoint() is not None, "error": None, "absent": []}
    data["jackett"] = jk
    if not jk["configured"]:
        return data
    try:
        indexers = await fetch()
        url_of = xsdb.jackett_torznab_url
        url_of("test")
    except (RuntimeError, ValueError) as e:
        jk["error"] = str(e)
        return data
    by_key = {}
    for i in indexers:
        by_key[logs.normalize_url(url_of(i["id"]))] = {
            "id": i["id"], "name": i["name"], "enabled": True, "site": _site(i),
            "private": i["type"] == "private", "source": "Jackett",
        }
    config_keys = {it["key"] for it in data["items"] if it.get("config")}
    for it in data["items"]:
        p = by_key.get(it["key"])
        if p:
            it["jackett"] = dict(p)
            it["name"] = p["name"]
    for key, p in sorted(by_key.items(), key=lambda kv: kv[1]["name"].lower()):
        if key not in config_keys:
            jk["absent"].append(dict(p, key=key))
    return data


def link_sites(data: dict) -> dict:
    """Doublons de site, toutes sources confondues : un même tracker déclaré via Prowlarr
    et via Jackett (ou deux fois via la même source) n'a besoin que d'une ligne dans config.js."""
    def site(p):
        return prowlarr.domain(p.get("site", "")) if p else ""

    def src(it):
        return it.get("prowlarr") or it.get("jackett")

    in_config = [it for it in data["items"] if it.get("config") and src(it)]
    for it in in_config:
        p = src(it)
        p["same_site"] = [o["name"] for o in in_config if o is not it and site(src(o)) == site(p)]
    for block in (data.get("prowlarr"), data.get("jackett")):
        for p in (block or {}).get("absent", []):
            p["same_site"] = [o["name"] for o in in_config if site(src(o)) == site(p)]
    return data


# --- noms de trackers -----------------------------------------------------------

_names_cache = {"ts": 0.0, "names": {}}


async def site_names() -> dict:
    """{domaine: nom Jackett}, en complément des noms Prowlarr. Cache de 5 min ;
    Jackett absent ou injoignable : on garde le dernier résultat (ou rien)."""
    if time.time() - _names_cache["ts"] < 300:
        return _names_cache["names"]
    names = _names_cache["names"]
    if endpoint():
        try:
            names = {}
            for i in await fetch():
                names.setdefault(prowlarr.domain(_site(i)), prowlarr.clean_name(i["name"]))
        except RuntimeError:
            pass
    _names_cache.update(ts=time.time(), names=names)
    return names
