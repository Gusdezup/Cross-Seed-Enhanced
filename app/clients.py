"""Accès aux API de cross-seed v6/v7, Prowlarr et Jackett (clients torrent : torrentclients.py)."""
import httpx

from . import config

_http = httpx.AsyncClient(timeout=30)


async def xs_ping() -> bool:
    r = await _http.get(f"{config.XS_URL}/api/ping",
                        headers={"X-Api-Key": config.XS_APIKEY}, timeout=8)
    return r.status_code < 400


async def xs_indexers_v7() -> list:
    r = await _http.get(f"{config.XS_URL}/api/indexer/v1",
                        headers={"X-Api-Key": config.XS_APIKEY}, timeout=15)
    r.raise_for_status()
    return r.json()


async def xs_indexer_v7(method: str, indexer_id: int | None = None, body: dict | None = None) -> dict:
    config.guard("modification des indexers cross-seed")
    url = f"{config.XS_URL}/api/indexer/v1"
    if indexer_id is not None:
        url += f"/{indexer_id}"
    r = await _http.request(method, url, json=body, headers={"X-Api-Key": config.XS_APIKEY}, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"cross-seed a répondu HTTP {r.status_code} : {r.text[:200]}")
    # La réponse cross-seed contient l'apikey de l'indexer : ne jamais la relayer au navigateur.
    return {"changed": True}


async def xs_webhook(payload: dict) -> int:
    config.guard("envoi de recherche à cross-seed")
    r = await _http.post(f"{config.XS_URL}/api/webhook", data=payload,
                         headers={"X-Api-Key": config.XS_APIKEY}, timeout=300)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} : {r.text[:200]}")
    return r.status_code


async def xs_announce(name: str, link: str, tracker: str) -> int:
    """Soumet un résultat de recherche à cross-seed (comme autobrr) : 200 trouvé (injecté ou déjà présent),
    202 trouvé mais source incomplète, 204 aucune correspondance."""
    config.guard("envoi d'un résultat à cross-seed")
    r = await _http.post(f"{config.XS_URL}/api/announce",
                         json={"name": name, "guid": link, "link": link, "tracker": tracker},
                         headers={"X-Api-Key": config.XS_APIKEY}, timeout=180)
    if r.status_code >= 400:
        raise RuntimeError(f"announce HTTP {r.status_code} : {r.text[:200]}")
    return r.status_code


async def torznab_get(url: str) -> httpx.Response:
    """Requête Torznab vers Prowlarr ou Jackett (URL complète, clé API comprise)."""
    return await _http.get(url, timeout=60, follow_redirects=True)


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
