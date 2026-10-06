"""Accès aux API qBittorrent (clé API, qBit >= 5.2) et cross-seed (v6)."""
import time

import httpx

from . import config

_http = httpx.AsyncClient(timeout=30)
_torrents_cache = {"ts": 0.0, "data": []}


async def qbit_torrents(force: bool = False) -> list:
    if not force and time.time() - _torrents_cache["ts"] < 20:
        return _torrents_cache["data"]
    r = await _http.get(f"{config.QBT_URL}/api/v2/torrents/info",
                        params={"filter": "completed"},
                        headers={"Authorization": f"Bearer {config.QBT_APIKEY}"})
    r.raise_for_status()
    _torrents_cache.update(ts=time.time(), data=r.json())
    return _torrents_cache["data"]


async def qbit_version() -> str:
    r = await _http.get(f"{config.QBT_URL}/api/v2/app/version",
                        headers={"Authorization": f"Bearer {config.QBT_APIKEY}"}, timeout=8)
    r.raise_for_status()
    return r.text.strip()


async def xs_ping() -> bool:
    r = await _http.get(f"{config.XS_URL}/api/ping",
                        headers={"X-Api-Key": config.XS_APIKEY}, timeout=8)
    return r.status_code < 400


async def xs_webhook(payload: dict) -> int:
    config.guard("envoi de recherche à cross-seed")
    r = await _http.post(f"{config.XS_URL}/api/webhook", data=payload,
                         headers={"X-Api-Key": config.XS_APIKEY}, timeout=300)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} : {r.text[:200]}")
    return r.status_code


async def xs_job(name: str) -> dict:
    config.guard(f"lancement du job {name}")
    r = await _http.post(f"{config.XS_URL}/api/job", json={"name": name},
                         headers={"X-Api-Key": config.XS_APIKEY}, timeout=15)
    try:
        body = r.json()
    except ValueError:
        body = r.text[:300]
    return {"status": r.status_code, "body": body}


async def xs_restart() -> None:
    """Redémarre le conteneur cross-seed via un proxy du socket Docker limité aux redémarrages."""
    config.guard("redémarrage de cross-seed")
    if not config.DOCKER_URL:
        raise RuntimeError("Redémarrage non configuré (DOCKER_URL vide)")
    r = await _http.post(f"{config.DOCKER_URL}/containers/{config.XS_CONTAINER}/restart",
                         params={"t": 10}, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Docker a répondu {r.status_code} : {r.text[:200]}")


async def prowlarr_indexers(url: str, key: str) -> tuple:
    """Indexers déclarés dans Prowlarr et leur état (échecs en cours)."""
    h = {"X-Api-Key": key}
    r = await _http.get(f"{url}/api/v1/indexer", headers=h, timeout=20)
    r.raise_for_status()
    s = await _http.get(f"{url}/api/v1/indexerstatus", headers=h, timeout=20)
    return r.json(), (s.json() if s.status_code < 400 else [])


async def jackett_indexers(url: str, key: str) -> str:
    """Indexers configurés dans Jackett (XML Torznab t=indexers de l'agrégat « all »)."""
    r = await _http.get(f"{url}/api/v2.0/indexers/all/results/torznab/api",
                        params={"t": "indexers", "configured": "true", "apikey": key}, timeout=20)
    r.raise_for_status()
    return r.text
