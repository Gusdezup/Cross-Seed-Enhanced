"""Clients torrent, en lecture seule : XSE n'y envoie aucune action (cross-seed s'occupe des injections).

Chaque client renvoie ses torrents au format de l'API qBittorrent (torrents/info), le seul que le
reste de l'app connaisse : hash, name, size (sélection), total_size, progress (0 à 1), category, tags,
tracker, content_path, state, added_on. La liste fusionnée ajoute client (enregistrement retenu) et
clients (tous les clients où ce hash est présent).

Clients configurés, par ordre de priorité :
  1. TORRENT_CLIENT_1, TORRENT_CLIENT_2… du .env (syntaxe torrentClients de cross-seed) ;
  2. torrentClients de cross-seed (config.js en v6, réglages en base en v7) ;
  3. QBT_URL et QBT_APIKEY (format historique, un seul qBittorrent).
"""
import asyncio
import time

import httpx

from . import config, xsdb

TTL = 20            # cache des listes de torrents, par client
VERSION_TTL = 300   # cache des versions (pastilles de l'en-tête)


class Unavailable(RuntimeError):
    """Aucun client torrent n'a répondu."""


def _err(e: Exception) -> str:
    """Message d'erreur lisible, sans identifiants (les URL utilisées n'en contiennent jamais)."""
    if isinstance(e, httpx.ConnectError):
        return "connexion impossible"
    if isinstance(e, httpx.TimeoutException):
        return "délai dépassé"
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    if isinstance(e, OSError):
        return e.strerror or str(e) or type(e).__name__
    return (str(e) or type(e).__name__)[:200]


class Client:
    """Base des adaptateurs. transport : httpx.MockTransport dans les tests."""
    kind = ""

    def __init__(self, spec: dict, transport=None):
        self.spec = spec
        self.url = config.client_url(spec)
        self.http = httpx.AsyncClient(timeout=60, transport=transport)

    async def torrents(self) -> list:
        raise NotImplementedError

    async def version(self) -> str:
        raise NotImplementedError


# --- qBittorrent -------------------------------------------------------------------

class QBittorrent(Client):
    """Clé API (qBittorrent ≥ 5.2, jeton Bearer) si fournie ; sinon identifiants de l'URL
    (/api/v2/auth/login, cookie de session) ; sinon aucun (authentification contournée pour XSE)."""
    kind = "qbittorrent"

    async def _login(self) -> None:
        r = await self.http.post(f"{self.url}/api/v2/auth/login", headers={"Referer": self.url},
                                 data={"username": self.spec["user"], "password": self.spec["password"]})
        if r.status_code == 403:
            raise RuntimeError("adresse de XSE bannie par qBittorrent (trop d'échecs de connexion)")
        if r.status_code >= 400 or r.text.strip() != "Ok.":
            raise RuntimeError("identifiants refusés par qBittorrent")

    async def _get(self, path: str, timeout: float = 60) -> httpx.Response:
        key = self.spec.get("apikey")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        r = await self.http.get(f"{self.url}{path}", headers=headers, timeout=timeout)
        if r.status_code in (401, 403) and not key and self.spec["user"]:
            await self._login()   # session absente ou expirée
            r = await self.http.get(f"{self.url}{path}", timeout=timeout)
        if r.status_code in (401, 403):
            raise RuntimeError("authentification refusée par qBittorrent (clé API ou identifiants)")
        r.raise_for_status()
        return r

    async def torrents(self) -> list:
        return (await self._get("/api/v2/torrents/info")).json()

    async def version(self) -> str:
        return (await self._get("/api/v2/app/version", timeout=8)).text.strip()


ADAPTERS = {"qbittorrent": QBittorrent}


# --- clients configurés ------------------------------------------------------------

_LOCAL = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
_conf_cache = {"ts": 0.0, "data": None}


def _from_cross_seed() -> tuple:
    """(clients, entrées ignorées) lus dans la config de cross-seed, adaptés au réseau de XSE :
    - le qBittorrent du port de QBT_URL prend l'adresse et la clé API de QBT_URL (ce qui marchait déjà) ;
    - localhost devient TORRENT_CLIENT_HOST, à défaut l'hôte de QBT_URL (cross-seed dans le réseau
      d'un autre conteneur, Gluetun par exemple)."""
    legacy = config.legacy_client() if config.QBT_URL_SET else None
    out, bad = [], []
    for entry in xsdb.torrent_client_entries():
        try:
            c = config.parse_client(entry)
        except ValueError as e:
            bad.append(str(e))
            continue
        if legacy and c["type"] == "qbittorrent" and c["port"] == legacy["port"] \
                and (c["host"] in _LOCAL or c["host"] == legacy["host"]):
            c.update(scheme=legacy["scheme"], host=legacy["host"], path=legacy["path"],
                     apikey=legacy["apikey"])
        elif c["host"] in _LOCAL and (config.CLIENT_HOST or legacy):
            c["host"] = config.CLIENT_HOST or legacy["host"]
        out.append(c)
    return out, bad


def configured() -> dict:
    """{"clients": [spec avec id et name], "origin": str, "ignored": [messages]}. Cache de 60 s
    (config.js peut changer). Les noms : .env, puis Réglages, puis un nom automatique."""
    if _conf_cache["data"] is not None and time.time() - _conf_cache["ts"] < 60:
        return _conf_cache["data"]
    ignored = []
    if config.ENV_CLIENTS:
        specs, origin = [dict(c) for c in config.ENV_CLIENTS], ".env (TORRENT_CLIENT_N)"
    else:
        specs, ignored = _from_cross_seed()
        origin = "config de cross-seed (torrentClients)"
        if not specs:
            specs, origin = [config.legacy_client()], ".env (QBT_URL)"
    seen, uniq = set(), []
    for c in specs:   # un même client déclaré deux fois n'est lu qu'une fois
        c["id"] = config.client_id(c)
        if c["id"] not in seen:
            seen.add(c["id"])
            uniq.append(c)
    _name(uniq, config.load_settings().get("client_names", {}))
    data = {"clients": uniq, "origin": origin, "ignored": ignored}
    _conf_cache.update(ts=time.time(), data=data)
    return data


def default_name(c: dict, specs: list) -> str:
    """Nom du type (« qBittorrent ») ; complété du port, ou de l'hôte et du port, s'il y a plusieurs
    clients de ce type."""
    label = config.CLIENT_TYPES[c["type"]]
    same = [s for s in specs if s["type"] == c["type"]]
    if len(same) < 2:
        return label
    if not c["host"]:
        return f"{label} {c['path'].rsplit('/', 1)[-1]}"
    if len({s["host"] for s in same}) == 1 and len({s["port"] for s in same}) == len(same):
        return f"{label} :{c['port']}"
    return f"{label} {c['host']}" + (f":{c['port']}" if c["port"] else "")


def _name(specs: list, custom: dict) -> None:
    used = set()
    for c in specs:
        name = c.get("name") or custom.get(c["id"]) or default_name(c, specs)
        base, n = name, 2
        while name.lower() in used:
            name, n = f"{base} ({n})", n + 1
        used.add(name.lower())
        c["name"] = name


def reset() -> None:
    """Oublie la configuration en cache (noms modifiés dans les réglages)."""
    _conf_cache.update(ts=0.0, data=None)


# --- lecture et fusion -------------------------------------------------------------

_instances: dict = {}   # id -> (signature, adaptateur)
_state: dict = {}       # id -> {"ts", "data", "ok", "error"}
_versions: dict = {}    # id -> {"ts", "version", "ok", "error"}
_locks: dict = {}


def _signature(c: dict) -> tuple:
    return (config.client_url(c), c["user"], c["password"], c["apikey"])


def adapter(c: dict) -> Client:
    sig = _signature(c)
    cur = _instances.get(c["id"])
    if cur and cur[0] == sig:
        return cur[1]
    cls = ADAPTERS.get(c["type"])
    if cls is None:
        raise RuntimeError(f"{config.CLIENT_TYPES[c['type']]} n'est pas encore pris en charge par XSE")
    inst = cls(c)
    _instances[c["id"]] = (sig, inst)
    return inst


async def _fetch(c: dict, force: bool) -> dict:
    st = _state.setdefault(c["id"], {"ts": 0.0, "data": [], "ok": None, "error": ""})
    lock = _locks.setdefault(c["id"], asyncio.Lock())
    async with lock:
        if not force and time.time() - st["ts"] < TTL:
            return st
        try:
            data = await adapter(c).torrents()
            st.update(ok=True, error="", data=data)
        except Exception as e:  # noqa: BLE001  client injoignable : ses torrents sont absents
            st.update(ok=False, error=_err(e), data=[])
        st["ts"] = time.time()
    return st


def merge(results: list) -> list:
    """Fusionne [(nom du client, torrents)] dans l'ordre des clients. Un hash présent dans plusieurs
    clients n'apparaît qu'une fois : l'enregistrement le plus avancé, à défaut celui du premier client."""
    by, order = {}, []
    for name, data in results:
        for t in data:
            h = (t.get("hash") or "").lower()
            if not h:
                continue
            cur = by.get(h)
            if cur is None:
                by[h] = {**t, "hash": h, "client": name, "clients": [name]}
                order.append(h)
                continue
            if name not in cur["clients"]:
                cur["clients"].append(name)
            if (t.get("progress") or 0) > (cur.get("progress") or 0):
                by[h] = {**t, "hash": h, "client": name, "clients": cur["clients"]}
    return [by[h] for h in order]


async def snapshot(force: bool = False) -> dict:
    """{"torrents": liste fusionnée, "clients": [{name, type, ok, error, torrents}]}.
    Un client injoignable est signalé et ses torrents manquent ; Unavailable si aucun ne répond."""
    specs = configured()["clients"]
    states = await asyncio.gather(*(_fetch(c, force) for c in specs))
    info = [{"id": c["id"], "name": c["name"], "type": c["type"], "ok": bool(st["ok"]),
             "error": st["error"], "torrents": len(st["data"])} for c, st in zip(specs, states)]
    if specs and not any(i["ok"] for i in info):
        raise Unavailable(" ; ".join(f"{i['name']} : {i['error']}" for i in info))
    return {"torrents": merge([(c["name"], st["data"]) for c, st in zip(specs, states)]), "clients": info}


async def all_torrents(force: bool = False) -> list:
    return (await snapshot(force))["torrents"]


async def _version(c: dict) -> dict:
    v = _versions.get(c["id"])
    if v and v["ok"] and time.time() - v["ts"] < VERSION_TTL:
        return v
    try:
        v = {"ok": True, "version": await asyncio.wait_for(adapter(c).version(), 10), "error": ""}
    except Exception as e:  # noqa: BLE001
        v = {"ok": False, "version": "", "error": _err(e) if not isinstance(e, asyncio.TimeoutError)
             else "délai dépassé"}
    v["ts"] = time.time()
    _versions[c["id"]] = v
    return v


async def status() -> dict:
    """État de chaque client pour l'en-tête et les réglages (jamais d'identifiants)."""
    conf = configured()
    specs = conf["clients"]
    versions = await asyncio.gather(*(_version(c) for c in specs))
    custom = config.load_settings().get("client_names", {})
    env_names = {config.client_id(c): c["name"] for c in config.ENV_CLIENTS if c["name"]}
    return {"origin": conf["origin"], "ignored": conf["ignored"], "clients": [{
        "id": c["id"], "name": c["name"], "type": c["type"], "type_label": config.CLIENT_TYPES[c["type"]],
        "url": config.client_url(c), "ok": v["ok"], "version": v["version"], "error": v["error"],
        "torrents": len((_state.get(c["id"]) or {}).get("data") or []),
        "default_name": default_name(c, specs), "custom_name": custom.get(c["id"], ""),
        "env_name": env_names.get(c["id"], ""),
    } for c, v in zip(specs, versions)]}
