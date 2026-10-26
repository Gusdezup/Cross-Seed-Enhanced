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
import xmlrpc.client
from urllib.parse import unquote

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


def _qstate(done: bool, kind: str) -> str:
    """État au format qBittorrent 5 : suffixe UP (terminé) ou DL (en cours)."""
    return f"{kind}{'UP' if done else 'DL'}"


# --- rTorrent ----------------------------------------------------------------------

class RTorrent(Client):
    """XML-RPC sur HTTP (/RPC2, httprpc de ruTorrent ; Basic ou Digest) ou SCGI direct
    (scgi://hôte:port, scgi:///chemin/du/socket, sans authentification)."""
    kind = "rtorrent"
    FIELDS = ("d.hash=", "d.name=", "d.size_bytes=", "d.completed_bytes=", "d.custom1=", "d.directory=",
              "d.is_multi_file=", "d.state=", "d.is_active=", "d.complete=", "d.hashing=", "d.custom=addtime")

    def __init__(self, spec: dict, transport=None):
        super().__init__(spec, transport)
        self.auth = httpx.BasicAuth(spec["user"], spec["password"]) if spec["user"] else None

    async def _http_call(self, body: bytes) -> bytes:
        headers = {"Content-Type": "text/xml"}
        r = await self.http.post(self.url, content=body, headers=headers, auth=self.auth)
        if r.status_code == 401 and self.spec["user"] \
                and r.headers.get("www-authenticate", "").lower().startswith("digest") \
                and not isinstance(self.auth, httpx.DigestAuth):
            self.auth = httpx.DigestAuth(self.spec["user"], self.spec["password"])
            r = await self.http.post(self.url, content=body, headers=headers, auth=self.auth)
        if r.status_code == 401:
            raise RuntimeError("authentification refusée par rTorrent")
        r.raise_for_status()
        return r.content

    async def _scgi_call(self, body: bytes) -> bytes:
        s = self.spec
        if s["host"]:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(s["host"], s["port"] or 5000), 10)
        else:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(s["path"]), 10)
        try:
            head = b"CONTENT_LENGTH\x00%d\x00SCGI\x001\x00REQUEST_METHOD\x00POST\x00REQUEST_URI\x00/RPC2\x00" % len(body)
            writer.write(b"%d:%s," % (len(head), head) + body)
            await writer.drain()
            data = await asyncio.wait_for(reader.read(), 60)
        finally:
            writer.close()
        for sep in (b"\r\n\r\n", b"\n\n"):   # en-têtes de type CGI avant le XML
            if sep in data and not data.lstrip().startswith(b"<?xml"):
                return data.split(sep, 1)[1]
        return data

    async def call(self, method: str, *params):
        body = xmlrpc.client.dumps(params, method).encode()
        raw = await (self._scgi_call(body) if self.spec["scheme"] == "scgi" else self._http_call(body))
        try:
            return xmlrpc.client.loads(raw, use_builtin_types=True)[0][0]
        except xmlrpc.client.Fault as e:
            raise RuntimeError(f"rTorrent : {e.faultString}") from e
        except Exception as e:  # noqa: BLE001  réponse qui n'est pas du XML-RPC
            raise RuntimeError("réponse XML-RPC illisible (adresse du point d'entrée ?)") from e

    async def torrents(self) -> list:
        rows = await self.call("d.multicall2", "", "main", *self.FIELDS)
        calls = [{"methodName": "t.multicall", "params": [r[0], "", "t.url="]} for r in rows]
        trs = await self.call("system.multicall", calls) if calls else []
        out = []
        for row, tr in zip(rows, trs):
            h, name, size, done_b, label, directory, multi, started, active, complete, hashing, addtime = row
            urls = [u[0] for u in (tr[0] if isinstance(tr, list) and tr else []) if u and u[0]]
            done = bool(complete)
            if hashing:
                state = _qstate(done, "checking")
            elif not started:
                state = _qstate(done, "stopped")
            elif not active:
                state = _qstate(done, "paused")
            else:
                state = "uploading" if done else "downloading"
            out.append({
                "hash": h.lower(), "name": name, "size": size, "total_size": size,
                "progress": 1.0 if done else (done_b / size if size else 0.0),
                # étiquette ruTorrent, encodée en URL dans custom1
                "category": unquote(label or ""), "tags": "",
                "tracker": next((u for u in urls if not u.startswith("dht://")), ""),
                "content_path": directory if multi else f"{directory.rstrip('/')}/{name}",
                "state": state, "added_on": int(addtime) if str(addtime).strip().isdigit() else 0,
            })
        return out

    async def version(self) -> str:
        return str(await self.call("system.client_version"))


# --- Transmission ------------------------------------------------------------------

class Transmission(Client):
    """RPC JSON (/transmission/rpc) : Basic, et jeton X-Transmission-Session-Id (relance sur 409)."""
    kind = "transmission"
    FIELDS = ["hashString", "name", "totalSize", "sizeWhenDone", "percentDone", "labels", "trackers",
              "downloadDir", "status", "addedDate"]
    STATES = {0: "stopped", 1: "checking", 2: "checking", 3: "queued", 4: "downloading", 5: "queued",
              6: "uploading"}

    def __init__(self, spec: dict, transport=None):
        super().__init__(spec, transport)
        self.sid = ""
        self.auth = httpx.BasicAuth(spec["user"], spec["password"]) if spec["user"] or spec["password"] else None

    async def rpc(self, method: str, arguments: dict | None = None, timeout: float = 60) -> dict:
        for _ in range(2):
            r = await self.http.post(self.url, json={"method": method, "arguments": arguments or {}},
                                     headers={"X-Transmission-Session-Id": self.sid}, auth=self.auth,
                                     timeout=timeout)
            if r.status_code == 409:   # jeton de session absent ou périmé
                self.sid = r.headers.get("X-Transmission-Session-Id", "")
                continue
            break
        if r.status_code == 401:
            raise RuntimeError("authentification refusée par Transmission")
        r.raise_for_status()
        body = r.json()
        if body.get("result") != "success":
            raise RuntimeError(f"Transmission : {body.get('result')}")
        return body.get("arguments") or {}

    async def torrents(self) -> list:
        args = await self.rpc("torrent-get", {"fields": self.FIELDS})
        out = []
        for t in args.get("torrents") or []:
            labels = [str(x) for x in t.get("labels") or []]
            done = (t.get("percentDone") or 0) >= 1
            kind = self.STATES.get(t.get("status"), "unknown")
            out.append({
                "hash": (t.get("hashString") or "").lower(), "name": t.get("name") or "",
                "size": t.get("sizeWhenDone") or 0, "total_size": t.get("totalSize") or 0,
                "progress": t.get("percentDone") or 0,
                # labels depuis Transmission 4.0 : le premier sert de catégorie
                "category": labels[0] if labels else "", "tags": ",".join(labels),
                "tracker": next((x.get("announce") for x in t.get("trackers") or [] if x.get("announce")), ""),
                "content_path": f"{(t.get('downloadDir') or '').rstrip('/')}/{t.get('name') or ''}",
                "state": kind if kind in ("uploading", "downloading") else _qstate(done, kind),
                "added_on": t.get("addedDate") or 0,
            })
        return out

    async def version(self) -> str:
        return str((await self.rpc("session-get", {"fields": ["version"]}, timeout=8)).get("version", ""))


# --- Deluge ------------------------------------------------------------------------

class Deluge(Client):
    """API JSON de la WebUI (/json) : auth.login (mot de passe de l'URL), puis connexion au démon
    (le premier hôte de la WebUI) si la WebUI n'y est pas déjà connectée. Catégorie : plugin Label."""
    kind = "deluge"
    FIELDS = ["name", "total_size", "total_wanted", "progress", "label", "trackers", "save_path", "state",
              "time_added"]
    STATES = {"Seeding": "uploading", "Downloading": "downloading", "Paused": "stopped", "Checking": "checking",
              "Queued": "queued", "Error": "error", "Moving": "moving", "Allocating": "allocating"}

    def __init__(self, spec: dict, transport=None):
        super().__init__(spec, transport)
        self.ready, self.n = False, 0

    async def _raw(self, method: str, *params, timeout: float = 60):
        self.n += 1
        r = await self.http.post(self.url, json={"method": method, "params": list(params), "id": self.n},
                                 timeout=timeout)
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise RuntimeError(f"Deluge : {(body['error'] or {}).get('message') or body['error']}")
        return body.get("result")

    async def _connect(self) -> None:
        if not await self._raw("auth.login", self.spec["password"]):
            raise RuntimeError("mot de passe refusé par Deluge")
        if not await self._raw("web.connected"):
            hosts = await self._raw("web.get_hosts") or []
            if not hosts:
                raise RuntimeError("aucun démon Deluge déclaré dans la WebUI")
            await self._raw("web.connect", hosts[0][0])
            if not await self._raw("web.connected"):
                raise RuntimeError("la WebUI Deluge n'arrive pas à se connecter au démon")
        self.ready = True

    async def call(self, method: str, *params, timeout: float = 60):
        if not self.ready:
            await self._connect()
        try:
            return await self._raw(method, *params, timeout=timeout)
        except RuntimeError:   # session expirée ou démon déconnecté : on se reconnecte une fois
            self.ready = False
            await self._connect()
            return await self._raw(method, *params, timeout=timeout)

    async def torrents(self) -> list:
        data = await self.call("core.get_torrents_status", {}, self.FIELDS) or {}
        out = []
        for h, t in data.items():
            progress = (t.get("progress") or 0) / 100
            done = progress >= 1
            kind = self.STATES.get(t.get("state"), "unknown")
            out.append({
                "hash": h.lower(), "name": t.get("name") or "",
                "size": t.get("total_wanted") or t.get("total_size") or 0, "total_size": t.get("total_size") or 0,
                "progress": progress, "category": t.get("label") or "", "tags": "",
                "tracker": next((x.get("url") for x in t.get("trackers") or [] if x.get("url")), ""),
                "content_path": f"{(t.get('save_path') or '').rstrip('/')}/{t.get('name') or ''}",
                "state": kind if kind in ("uploading", "downloading", "error") else _qstate(done, kind),
                "added_on": int(t.get("time_added") or 0),
            })
        return out

    async def version(self) -> str:
        return str(await self.call("daemon.info", timeout=8))


ADAPTERS = {"qbittorrent": QBittorrent, "rtorrent": RTorrent, "transmission": Transmission, "deluge": Deluge}


# --- clients configurés ------------------------------------------------------------

_LOCAL = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
_conf_cache = {"ts": 0.0, "data": None}


def adapt(c: dict) -> dict:
    """Adresse d'un client lu chez cross-seed, vue depuis XSE :
    - le qBittorrent du port de QBT_URL prend l'adresse et la clé API de QBT_URL (ce qui marchait déjà) ;
    - localhost devient TORRENT_CLIENT_HOST, à défaut l'hôte de QBT_URL (cross-seed dans le réseau
      d'un autre conteneur, Gluetun par exemple)."""
    c = dict(c)
    legacy = config.legacy_client() if config.QBT_URL_SET else None
    if legacy and c["type"] == "qbittorrent" and c["port"] == legacy["port"] \
            and (c["host"] in _LOCAL or c["host"] == legacy["host"]):
        c.update(scheme=legacy["scheme"], host=legacy["host"], path=legacy["path"], apikey=legacy["apikey"])
    elif c["host"] in _LOCAL and (config.CLIENT_HOST or legacy):
        c["host"] = config.CLIENT_HOST or legacy["host"]
    return c


def _from_cross_seed() -> tuple:
    """(clients, entrées ignorées) lus dans la config de cross-seed, adaptés au réseau de XSE.
    raw_id : identifiant tel qu'écrit chez cross-seed (pour retirer le client de config.js)."""
    out, bad = [], []
    for entry in xsdb.torrent_client_entries():
        try:
            c = config.parse_client(entry)
        except ValueError as e:
            bad.append(str(e))
            continue
        raw = config.client_id(c)
        c = adapt(c)
        c.update(raw_id=raw, source="cross-seed")
        out.append(c)
    return out, bad


def configured() -> dict:
    """{"clients": [spec avec id et name], "origin": str, "ignored": [messages]}. Cache de 60 s
    (config.js peut changer). Les noms : .env, puis Réglages, puis un nom automatique."""
    if _conf_cache["data"] is not None and time.time() - _conf_cache["ts"] < 60:
        return _conf_cache["data"]
    ignored = []
    if config.ENV_CLIENTS:
        specs, origin = [dict(c, source="env") for c in config.ENV_CLIENTS], ".env (TORRENT_CLIENT_N)"
    else:
        specs, ignored = _from_cross_seed()
        origin = "config de cross-seed (torrentClients)"
        if not specs:
            specs, origin = [dict(config.legacy_client(), source="legacy")], ".env (QBT_URL)"
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
        "readonly": bool(c.get("readonly")), "source": c.get("source", ""), "raw_id": c.get("raw_id", ""),
    } for c, v in zip(specs, versions)], "editable": editable()}


def inject_target() -> str:
    """Nom du client où cross-seed injecte les correspondances trouvées par les données (dataDirs) :
    le premier client de torrentClients qui n'est pas en lecture seule ("" si aucun)."""
    for entry in xsdb.torrent_client_entries():
        try:
            c = config.parse_client(entry)
        except ValueError:
            continue
        if c["readonly"]:
            continue
        raw = config.client_id(c)
        known = next((k for k in configured()["clients"] if k.get("raw_id") == raw or k["id"] == raw), None)
        return known["name"] if known else config.client_url(adapt(c))
    return ""


def editable() -> bool:
    """Clients modifiables depuis l'interface : torrentClients de config.js (v6), écriture permise."""
    return config.XS_VERSION == "6" and (not config.READONLY or config.ALLOW_CONFIG_WRITE)


async def test(entry: str) -> dict:
    """Essaie un client avant de l'écrire dans config.js : version et nombre de torrents, vus depuis XSE."""
    c = adapt(config.parse_client(entry))
    c["id"] = config.client_id(c)
    inst = ADAPTERS[c["type"]](c)
    try:
        version = await asyncio.wait_for(inst.version(), 15)
        count = len(await asyncio.wait_for(inst.torrents(), 60))
        return {"ok": True, "version": version, "torrents": count, "url": config.client_url(c)}
    except Exception as e:  # noqa: BLE001
        msg = "délai dépassé" if isinstance(e, asyncio.TimeoutError) else _err(e)
        return {"ok": False, "error": msg, "url": config.client_url(c)}
    finally:
        await inst.http.aclose()
