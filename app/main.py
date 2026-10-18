"""cross-seed-enhanced — interface web pour piloter cross-seed."""
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import clients, config, demandes, jackett, logs, prowlarr, releases, routing, scan, schedules, xsdb
from .worker import queue

STATIC = Path(__file__).parent / "static"
JOBS = ("rss", "search", "inject", "cleanup")


@asynccontextmanager
async def lifespan(_app):
    tasks = [asyncio.create_task(queue.run()), asyncio.create_task(_scan_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="cross-seed-enhanced", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if config.UI_PASSWORD:
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, config.UI_USER) and \
                    secrets.compare_digest(pwd, config.UI_PASSWORD)
            except (ValueError, UnicodeDecodeError):
                ok = False
        if not ok:
            return JSONResponse({"detail": "Authentification requise"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="cross-seed-enhanced"'})
    return await call_next(request)


# --- état général -------------------------------------------------------------

@app.get("/api/status")
async def status():
    out = {"qbit": {"ok": False}, "xs": {"ok": False}, "files": {}}
    try:
        out["qbit"] = {"ok": True, "version": await clients.qbit_version()}
    except Exception as e:  # noqa: BLE001
        out["qbit"]["error"] = str(e)[:200]
    try:
        out["xs"] = {"ok": await clients.xs_ping()}
    except Exception as e:  # noqa: BLE001
        out["xs"]["error"] = str(e)[:200]
    out["restart_available"] = bool(config.DOCKER_URL) and not config.READONLY
    out["xs_version"] = config.XS_VERSION
    out["xs_version_source"] = config.XS_VERSION_SOURCE
    out["readonly"] = config.READONLY
    out["config_write"] = config.XS_VERSION == "6" and (not config.READONLY or config.ALLOW_CONFIG_WRITE)
    out["files"] = {
        "config": config.XS_CONFIG_JS.exists(),
        "logs": config.LOGS_DIR.is_dir(),
        "db": config.XS_DB.exists(),
        "pending_dir": xsdb.pending_dir().is_dir(),
    }
    return out


# --- releases -----------------------------------------------------------------

@app.get("/api/releases")
async def list_releases(refresh: bool = False):
    try:
        torrents = await clients.qbit_torrents(force=refresh)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"qBittorrent injoignable : {e}") from e
    settings = config.load_settings()
    names = {**await jackett.site_names(), **await prowlarr.site_names()}
    items = releases.build(torrents, settings, names)
    resolve = releases.make_resolver(settings, names)
    try:
        all_hashes = await clients.qbit_hashes()
    except Exception:  # noqa: BLE001
        all_hashes = {t["hash"].lower() for t in torrents}
    state = await asyncio.to_thread(xsdb.search_state)
    labels = await _indexer_labels(resolve)
    releases.attach_trackers(items, state, labels, all_hashes, resolve)
    active = await _active_indexers(resolve)
    releases.attach_routed(items, scan.routed_by_indexer(), active)
    scan.merge_last_search(items)
    # Indexers sur lesquels la release est déjà en seed : la recherche routée ne les interroge pas
    # (un tracker qui a repris les torrents d'un autre peut avoir deux infohash pour une même release,
    # et cross-seed injecterait alors un doublon).
    by_label = {}
    for i in labels.values():
        by_label.setdefault(releases._key(i["label"]), []).append(i["key"])
    for r in items:
        r["seeded_keys"] = [k for t in r["trackers"] if t["state"] == "seed"
                            for k in by_label.get(releases._key(t["label"]), [])]
    trackers = {}
    for r in items:
        for c in r["copies"]:
            trackers.setdefault(c["host"], c["tracker"])
    out = {"releases": items, "trackers": trackers, "torrents": len(torrents),
           "indexers": [{"key": k, "label": v} for k, v in active.items()]}
    _rel_cache.update(ts=time.time(), data=out)
    return out


_rel_cache = {"ts": 0.0, "data": None}


async def _releases_cached(max_age: int = 60) -> dict:
    """Liste des releases récente (aperçu d'une demande pendant la saisie : pas de rechargement à chaque
    frappe). Rechargée si elle a plus de `max_age` secondes."""
    if _rel_cache["data"] is None or time.time() - _rel_cache["ts"] > max_age:
        await list_releases()
    return _rel_cache["data"]


_idx_cache = {"ts": 0.0, "data": None}


async def _indexer_labels(resolve) -> dict:
    """{id cross-seed: {"label", "active"}} pour les indexers présents dans config.js (actifs ou suspendus).
    Le nom vient du site Prowlarr/Jackett, résolu comme les trackers des torrents. Cache de 5 min."""
    if _idx_cache["data"] is None or time.time() - _idx_cache["ts"] > 300:
        data = await asyncio.to_thread(xsdb.indexers)
        await prowlarr.enrich(data)
        await jackett.enrich(data)
        _idx_cache.update(ts=time.time(), data=data)
    out = {}
    for it in _idx_cache["data"]["items"]:
        if it.get("id") is None or not it.get("config"):
            continue   # retiré de config.js : ses anciennes recherches ne comptent plus
        site = (it.get("prowlarr") or it.get("jackett") or {}).get("site")
        out[it["id"]] = {"label": resolve(site) if site else prowlarr.clean_name(it["name"]),
                         "active": it["config"] == "active", "key": it["key"]}
    return out


async def _active_indexers(resolve=None) -> dict:
    """{clé d'indexer actif dans config.js: nom affiché}, y compris un indexer ajouté que cross-seed
    n'a pas encore enregistré (redémarrage en attente). Même nom que dans la colonne Trackers."""
    if resolve is None:
        settings = config.load_settings()
        resolve = releases.make_resolver(settings, {**await jackett.site_names(), **await prowlarr.site_names()})
    await _indexer_labels(resolve)   # remplit le cache des indexers
    out = {}
    for it in _idx_cache["data"]["items"]:
        if it.get("config") != "active" or it["key"] in out:
            continue
        site = (it.get("prowlarr") or it.get("jackett") or {}).get("site")
        out[it["key"]] = resolve(site) if site else prowlarr.clean_name(it["name"])
    return dict(sorted(out.items(), key=lambda kv: kv[1].lower()))


@app.post("/api/releases/history")
async def release_history(body: dict):
    r = releases.get(body.get("key", ""))
    if not r:
        raise HTTPException(404, "Release inconnue, recharge la liste")
    names = sorted({c["name"] for c in r["copies"]} | {c["path"] for c in r["copies"] if c["path"]})
    return await asyncio.to_thread(xsdb.history, names)


# --- file de recherches -------------------------------------------------------

@app.post("/api/search")
async def search(body: dict):
    keys = body.get("keys") or []
    if not releases.get(keys[0] if keys else ""):
        await list_releases()
    batch = None
    if len(keys) > 1:
        batch = queue.new_batch(f"Sélection de {len(keys)} releases", "manuel", detail="selon le groupe")
    added = 0
    for k in keys:
        r = releases.get(k)
        if r and queue.add(r, source="manuel", front=True, batch=batch, save=False):
            added += 1
    queue.save()
    return {"added": added, "skipped": len(keys) - added}


# --- demandes ciblées (onglet Chercher) -------------------------------------------

async def _demande(body: dict) -> tuple:
    data = await _releases_cached(0 if (body or {}).get("refresh") else 60)
    crit = demandes.normalize(body)
    active = {i["key"]: i["label"] for i in data["indexers"]}
    sel = demandes.select(data["releases"], crit, config.load_settings(), active)
    return crit, active, sel


@app.post("/api/demandes/preview")
async def demande_preview(body: dict):
    crit, active, sel = await _demande(body)
    delay = config.load_settings()["delay"]
    rel = sel["releases"]
    queued = queue.snapshot()["pending"]
    what, to = demandes.label(crit, active)
    return {"count": len(rel), "matched": sel["matched"], "counts": sel["counts"],
            "examples": [{"name": r["name"], "category": r["category"], "seeds": r["seeds"]} for r in rel[:6]],
            "duration": len(rel) * delay, "queued_before": queued, "delay": delay,
            "label": what, "to": to}


@app.post("/api/demandes")
async def demande_launch(body: dict):
    crit, active, sel = await _demande({**(body or {}), "refresh": True})
    if not sel["releases"]:
        raise HTTPException(400, "Aucune release ne correspond à cette demande")
    what, to = demandes.label(crit, active)
    bid = queue.new_batch(what, "demande", targets=crit["targets"], detail=to)
    added = sum(1 for r in sel["releases"]
                if queue.add(r, source="demande", front=True, targets=crit["targets"], batch=bid, save=False))
    queue.save()
    return {"added": added, "skipped": len(sel["releases"]) - added, "batch": bid}


@app.post("/api/queue/rules")
async def queue_rules(body: dict | None = None):
    only = (body or {}).get("rule")
    data = await list_releases(refresh=True)
    settings = config.load_settings()
    order = {config.rule_label(r): n for n, r in enumerate(settings["rules"])}
    cands = [r for r in data["releases"] if r["rules"] and (not only or only in r["rules"])]
    cands.sort(key=lambda r: (min(order.get(n, 99) for n in r["rules"]), r["count"], r["name"].lower()))
    if (body or {}).get("dry"):   # simple comptage, pour demander confirmation avant un gros ajout
        return {"added": 0, "matched": len(cands)}
    bid = queue.new_batch(f"Règle {only}" if only else "Règles prioritaires", "règle",
                          detail="selon le groupe")
    added = sum(1 for r in cands if queue.add(r, source=only or r["rules"][0], batch=bid, save=False))
    queue.save()
    return {"added": added, "matched": len(cands)}


# --- recherches programmées ---------------------------------------------------

async def _run_schedule(item: dict, trigger: str) -> dict:
    """Un passage d'une recherche programmée : une demande dans la file, comme un lancement à la main."""
    try:
        crit, active, sel = await _demande({**item["criteria"], "refresh": True})
        _, to = demandes.label(crit, active)
        bid = queue.new_batch(item["name"] + (" (lancée à la main)" if trigger == "manuel" else ""), "programmée",
                              targets=crit["targets"], detail=to)
        added = sum(1 for r in sel["releases"]
                    if queue.add(r, source="programmée", targets=crit["targets"], batch=bid, save=False))
        queue.save()
        return schedules.update(item["id"], {"last_run": time.time(), "last_added": added,
                                             "last_matched": len(sel["releases"]), "last_error": None})
    except Exception as e:  # noqa: BLE001
        return schedules.update(item["id"], {"last_run": time.time(), "last_error": str(e)[:300]})


async def _scan_loop():
    """Lance les recherches programmées à leur heure (jamais en lecture seule)."""
    while True:
        await asyncio.sleep(60)
        if config.READONLY:
            continue
        for item in await asyncio.to_thread(schedules.due):
            await _run_schedule(item, "auto")


async def _schedule_view(item: dict, active: dict | None = None) -> dict:
    if active is None:
        active = await _active_indexers()
    crit = demandes.normalize(item["criteria"])
    what, to = demandes.label(crit, active)
    return item | {"what": what, "to": to, "next_run": schedules.next_run(item)}


@app.get("/api/schedules")
async def list_schedules():
    active = await _active_indexers()
    items = await asyncio.to_thread(schedules.load)
    return {"items": [await _schedule_view(i, active) for i in items], "readonly": config.READONLY}


@app.post("/api/schedules")
async def create_schedule(body: dict):
    crit = body.get("criteria") if isinstance(body.get("criteria"), dict) else {}
    try:
        item = await asyncio.to_thread(schedules.create, body.get("name"), crit, body.get("every_hours"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if body.get("run_now") and not config.READONLY:
        item = await _run_schedule(item, "auto")
    return await _schedule_view(item)


@app.patch("/api/schedules/{sid}")
async def patch_schedule(sid: str, body: dict):
    try:
        item = await asyncio.to_thread(schedules.update, sid, {k: v for k, v in body.items()
                                                                 if k in ("enabled", "every_hours", "name", "criteria")})
    except KeyError as e:
        raise HTTPException(404, "Recherche programmée inconnue") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return await _schedule_view(item)


@app.delete("/api/schedules/{sid}")
async def delete_schedule(sid: str):
    return {"deleted": await asyncio.to_thread(schedules.delete, sid)}


@app.post("/api/schedules/{sid}/run")
async def run_schedule(sid: str):
    config.guard("lancement d'une recherche programmée")
    item = next((i for i in await asyncio.to_thread(schedules.load) if i["id"] == sid), None)
    if not item:
        raise HTTPException(404, "Recherche programmée inconnue")
    return await _schedule_view(await _run_schedule(item, "manuel"))


# --- scan complet de cross-seed (searchCadence) -----------------------------------

def _scan_info() -> dict:
    xs = xsdb.useful_settings().get("searchCadence")
    if config.XS_VERSION == "7":
        xs = None   # en v7, searchCadence n'est pas modifiable d'ici (interface native)
    return {"readonly": config.READONLY, "xs_version": config.XS_VERSION,
            "xs_cadence": None if xs is None else {"value": xs["value"], "disabled": xs["kind"] == "empty"}}


@app.get("/api/scan")
async def get_scan():
    return await asyncio.to_thread(_scan_info)


@app.post("/api/scan/replace")
async def scan_replace(body: dict):
    """Coupe (searchCadence: null) ou rétablit le scan complet de cross-seed dans config.js."""
    if config.XS_VERSION == "7":
        raise HTTPException(400, "cross-seed v7 : searchCadence se règle dans l'interface native de cross-seed")
    replace = bool(body.get("replace"))
    cur = await asyncio.to_thread(lambda: xsdb.useful_settings().get("searchCadence"))
    if cur is None:
        raise HTTPException(400, "searchCadence introuvable dans config.js : modifie-le à la main")
    try:
        if replace:
            if cur["kind"] == "empty":
                return {**await asyncio.to_thread(_scan_info), "changed": False}
            res = await asyncio.to_thread(xsdb.update_settings, {"searchCadence": ""})
            scan.save_state(xs_cadence_before=cur["value"])
        else:
            if cur["kind"] != "empty":
                return {**await asyncio.to_thread(_scan_info), "changed": False}
            before = scan.load_state().get("xs_cadence_before") or "1 day"
            res = await asyncio.to_thread(xsdb.update_settings, {"searchCadence": before})
    except PermissionError as e:
        raise HTTPException(403, "config.js est en lecture seule : vérifie le montage dans docker-compose.yml") from e
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e)) from e
    return {**await asyncio.to_thread(_scan_info), **res}


@app.get("/api/queue")
async def get_queue():
    return queue.snapshot()


@app.post("/api/queue/{action}")
async def queue_action(action: str):
    if action == "pause":
        queue.paused = True
        queue.save()
    elif action == "resume":
        queue.paused = False
        queue._wake.set()  # noqa: SLF001
        queue.save()
    elif action == "clear-pending":
        return {"removed": queue.clear_pending()}
    elif action == "clear-done":
        return {"removed": queue.clear_done()}
    else:
        raise HTTPException(404, "Action inconnue")
    return {"paused": queue.paused}


@app.get("/api/queue/batch/{bid}")
async def queue_batch(bid: str, outcome: str | None = None):
    if bid not in queue.batches:
        raise HTTPException(404, "Demande inconnue")
    return {"items": queue.batch_items(bid, outcome)}


@app.delete("/api/queue/batch/{bid}")
async def queue_batch_cancel(bid: str):
    if bid not in queue.batches:
        raise HTTPException(404, "Demande inconnue")
    return {"removed": queue.cancel_batch(bid)}


@app.delete("/api/queue/{item_id}")
async def queue_remove(item_id: int):
    return {"removed": queue.remove(item_id)}


# --- injections en attente ----------------------------------------------------

PENDING_RE = re.compile(r"^\[(?P<type>[^\]]+)\]\[(?P<tracker>[^\]]+)\](?P<name>.+)\[(?P<hash>[0-9a-fA-F]{40})\]\.torrent$")


@app.get("/api/pending")
async def pending():
    items = []
    pdir = await asyncio.to_thread(xsdb.pending_dir)
    if pdir.is_dir():
        for p in sorted(pdir.glob("*.torrent"), key=lambda x: x.stat().st_mtime, reverse=True):
            m = PENDING_RE.match(p.name)
            info = m.groupdict() if m else {"type": "?", "tracker": "?", "name": p.stem, "hash": ""}
            info.update(file=p.name, size=p.stat().st_size,
                        mtime=datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"))
            info["errors"] = await asyncio.to_thread(logs.errors_for, info["hash"][:8]) if info["hash"] else []
            meta = await asyncio.to_thread(_torrent_meta, p)
            info["total_size"] = meta.get("size")
            items.append(info)
    await _pending_sources(items)
    writable = pdir.is_dir() and os.access(pdir, os.W_OK) and not config.READONLY
    return {"items": items, "writable": writable}


def _bdecode(data: bytes, i: int = 0):
    """Décodeur bencode minimal : (valeur, position suivante)."""
    c = data[i:i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":
        out, i = [], i + 1
        while data[i:i + 1] != b"e":
            v, i = _bdecode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        out, i = {}, i + 1
        while data[i:i + 1] != b"e":
            k, i = _bdecode(data, i)
            out[k], i = _bdecode(data, i)
        return out, i + 1
    j = data.index(b":", i)
    n = int(data[i:j])
    return data[j + 1:j + 1 + n], j + 1 + n


def _torrent_meta(path) -> dict:
    """Nom et taille totale d'un .torrent en attente ({} si illisible)."""
    try:
        info = _bdecode(path.read_bytes())[0].get(b"info", {})
        files = info.get(b"files")
        size = sum(f.get(b"length", 0) for f in files) if files else info.get(b"length")
        return {"name": info.get(b"name", b"").decode("utf-8", "replace"), "size": size}
    except (OSError, ValueError, IndexError, AttributeError):
        return {}


async def _pending_sources(items: list) -> None:
    """Pour chaque injection en attente : le torrent trouvé est-il déjà dans qBittorrent,
    et dans quel état sont les torrents locaux de même taille (sa source probable).
    cross-seed n'injecte pas tant que la source n'est pas terminée : il garde le .torrent
    et retente à chaque passage du job inject."""
    try:
        torrents = await clients._qbit_all()
    except Exception:  # noqa: BLE001  qBittorrent injoignable : pas de diagnostic
        for it in items:
            it.update(qbit=None, sources=None, reason=None)
        return
    by_hash = {(t.get("hash") or "").lower(): t for t in torrents}
    view = lambda t: {"name": t.get("name"), "progress": t.get("progress") or 0,  # noqa: E731
                      "state": t.get("state"), "category": t.get("category") or ""}
    for it in items:
        t = by_hash.get((it.get("hash") or "").lower())
        it["qbit"] = view(t) if t else None
        size = it.get("total_size")
        srcs = [view(x) for x in torrents if size and x.get("total_size") == size
                and (x.get("hash") or "").lower() != (it.get("hash") or "").lower()]
        it["sources"] = srcs
        if t:
            it["reason"] = "present"
        elif srcs and all(s["progress"] < 1 for s in srcs):
            it["reason"] = "incomplete"
        elif not srcs and size:
            it["reason"] = "nosource"
        else:
            it["reason"] = "refused"


@app.delete("/api/pending/{filename}")
async def pending_delete(filename: str):
    config.guard("suppression de fichiers en attente")
    pdir = await asyncio.to_thread(xsdb.pending_dir)
    p = (pdir / filename).resolve()
    if p.parent != pdir.resolve() or p.suffix != ".torrent" or not p.exists():
        raise HTTPException(404, "Fichier introuvable")
    try:
        p.unlink()
    except OSError as e:
        raise HTTPException(500, f"Suppression impossible : {e}") from e
    return {"deleted": filename}


@app.post("/api/jobs/{name}")
async def run_job(name: str):
    if name not in JOBS:
        raise HTTPException(404, "Job inconnu")
    try:
        return await clients.xs_job(name)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"cross-seed injoignable : {e}") from e


# --- indexers -----------------------------------------------------------------

@app.get("/api/indexers")
async def indexers():
    data = await asyncio.to_thread(xsdb.indexers)
    data["version"] = config.XS_VERSION
    data["settings"] = await asyncio.to_thread(xsdb.useful_settings)
    pauses = routing._load_pauses()   # pauses posées par XSE après un HTTP 429 (recherches routées)
    for it in data["items"]:
        end = pauses.get(it["key"])
        if end and end > time.time():
            it["xse_pause"] = datetime.fromtimestamp(end).isoformat(timespec="seconds")
    await prowlarr.enrich(data)
    await jackett.enrich(data)
    return jackett.link_sites(data)


def _v7_indexer(url: str) -> dict:
    parts = urlsplit(url)
    key = parse_qs(parts.query).get("apikey", [""])[0]
    if parts.scheme not in ("http", "https") or not parts.netloc or not key:
        raise ValueError("URL Torznab ou clé API invalide")
    return {"url": urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
            "apikey": key, "enabled": True}


async def _v7_add(url: str) -> dict:
    candidate = _v7_indexer(url)
    for item in await clients.xs_indexers_v7():
        if item.get("url", "").rstrip("/") == candidate["url"].rstrip("/"):
            if item.get("enabled"):
                return {"changed": False}
            return await clients.xs_indexer_v7("PUT", item["id"], {"id": item["id"], "enabled": True})
    return await clients.xs_indexer_v7("POST", body=candidate)


async def _v7_find(key: str) -> dict:
    from .logs import normalize_url
    for item in await clients.xs_indexers_v7():
        if normalize_url(item.get("url", "")) == key:
            return item
    raise ValueError("Indexer absent de cross-seed v7")


def _config_error(e: Exception) -> HTTPException:
    if isinstance(e, PermissionError):
        return HTTPException(403, "config.js est en lecture seule : vérifie le montage dans docker-compose.yml")
    return HTTPException(400, str(e))


@app.post("/api/indexers/add")
async def indexer_add(body: dict):
    """Ajoute un indexer Prowlarr (prowlarr_id) ou Jackett (jackett_id) au tableau torznab de config.js."""
    _idx_cache["data"] = None
    try:
        if body.get("jackett_id"):
            jid = str(body["jackett_id"])
            if not any(i["id"] == jid for i in await jackett.fetch()):
                raise ValueError("Indexer introuvable dans Jackett")
            url = xsdb.jackett_torznab_url(jid)
            if config.XS_VERSION == "7":
                return await _v7_add(url)
            return await asyncio.to_thread(xsdb.add_indexer, url)
        pid = int(body.get("prowlarr_id"))
        indexers, _ = await prowlarr.fetch()
        if not any(i["id"] == pid and i.get("protocol") == "torrent" for i in indexers):
            raise ValueError("Indexer torrent introuvable dans Prowlarr")
        url = xsdb.torznab_url(pid)
        if config.XS_VERSION == "7":
            return await _v7_add(url)
        return await asyncio.to_thread(xsdb.add_indexer, url)
    except (TypeError, ValueError, OSError, RuntimeError) as e:
        raise _config_error(e) from e


@app.post("/api/indexers/remove")
async def indexer_remove(body: dict):
    """Supprime la ligne d'un indexer du tableau torznab de config.js."""
    _idx_cache["data"] = None
    try:
        if config.XS_VERSION == "7":
            item = await _v7_find(str(body.get("key", "")))
            enabled = [i for i in await clients.xs_indexers_v7() if i.get("enabled")]
            if item.get("enabled") and len(enabled) <= 1:
                raise ValueError("Impossible de retirer le dernier indexer actif")
            return await clients.xs_indexer_v7("DELETE", item["id"])
        return await asyncio.to_thread(xsdb.remove_indexer, str(body.get("key", "")))
    except (ValueError, OSError) as e:
        raise _config_error(e) from e


@app.post("/api/indexers/toggle")
async def indexer_toggle(body: dict):
    _idx_cache["data"] = None
    try:
        if config.XS_VERSION == "7":
            item = await _v7_find(str(body.get("key", "")))
            enable = bool(body.get("enable"))
            if not enable:
                enabled = [i for i in await clients.xs_indexers_v7() if i.get("enabled")]
                if item.get("enabled") and len(enabled) <= 1:
                    raise ValueError("Impossible de suspendre le dernier indexer actif")
            return await clients.xs_indexer_v7("PUT", item["id"], {"id": item["id"], "enabled": enable})
        res = await asyncio.to_thread(xsdb.set_indexer, str(body.get("key", "")), bool(body.get("enable")))
    except PermissionError as e:
        raise HTTPException(403, "config.js est en lecture seule : vérifie le montage dans docker-compose.yml") from e
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e)) from e
    return res


@app.post("/api/xs-settings/check")
async def check_xs_settings(body: dict):
    return await asyncio.to_thread(xsdb.check_settings, body)


@app.put("/api/xs-settings")
async def put_xs_settings(body: dict):
    try:
        res = await asyncio.to_thread(xsdb.update_settings, body)
    except PermissionError as e:
        raise HTTPException(403, "config.js est en lecture seule : vérifie le montage dans docker-compose.yml") from e
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e)) from e
    res["settings"] = await asyncio.to_thread(xsdb.useful_settings)
    return res


@app.post("/api/xs-restart")
async def xs_restart():
    try:
        await clients.xs_restart()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e)) from e
    return {"restarted": True}


# --- logs ---------------------------------------------------------------------

@app.get("/api/logs/stream")
async def log_stream(request: Request, kind: str = "info", lines: int = 300):
    if kind not in logs.TYPES:
        raise HTTPException(400, "Type de log inconnu")
    lines = max(50, min(lines, 5000))

    async def gen():
        initial = await asyncio.to_thread(logs.tail_entries, kind, lines)
        yield f"event: init\ndata: {json.dumps(initial)}\n\n"
        offset, ident = logs.position(kind)
        pending = ""
        ticks = 0
        while not await request.is_disconnected():
            await asyncio.sleep(1)
            ticks += 1
            text, offset, ident = await asyncio.to_thread(logs.read_from, kind, offset, ident)
            if text:
                pending += text
                complete, _, pending = pending.rpartition("\n")
                entries = logs.group_entries(complete)
                if entries:
                    yield f"event: lines\ndata: {json.dumps(entries)}\n\n"
            elif ticks % 15 == 0:
                yield ": keepalive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --- réglages -----------------------------------------------------------------

@app.get("/api/settings")
async def get_settings():
    return config.public_settings(config.load_settings())


@app.put("/api/settings")
async def put_settings(body: dict):
    for r in body.get("rules", []):
        try:
            re.compile(config.rule_pattern(r) if r.get("type") else r.get("pattern", ""))
        except re.error as e:
            raise HTTPException(400, f"Expression invalide pour « {r.get('name') or r.get('value')} » : {e}") from e
    body.pop("sources", None)   # les sources passent par /api/sources
    try:
        return config.public_settings(config.save_settings(body))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


# --- sources d'indexers (Prowlarr, Jackett) ---------------------------------------

def _sources_info() -> dict:
    """État de chaque source pour l'onglet Réglages, sans jamais renvoyer de clé API."""
    saved = config.load_settings()["sources"]
    found = {"prowlarr": xsdb.prowlarr_from_config(), "jackett": xsdb.jackett_from_config()}
    out = {}
    for n in config.SOURCES:
        env_url, env_key = config.env_source(n)
        ep = (prowlarr if n == "prowlarr" else jackett).endpoint()
        out[n] = {
            "url": saved[n]["url"], "has_apikey": bool(saved[n]["apikey"]),
            "env": bool(env_url or env_key), "env_url": env_url,
            "detected_url": found[n][0] if found[n] else "",
            "effective": {"url": ep[0], "origin": ep[2]} if ep else None,
        }
    return out


@app.get("/api/sources")
async def get_sources():
    return await asyncio.to_thread(_sources_info)


@app.put("/api/sources")
async def put_sources(body: dict):
    try:
        await asyncio.to_thread(config.save_settings, {"sources": body})
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    prowlarr._names_cache["ts"] = 0
    jackett._names_cache["ts"] = 0
    return await asyncio.to_thread(_sources_info)


@app.post("/api/sources/test")
async def test_source(body: dict):
    """Teste une adresse et une clé avant enregistrement. Clé vide : celle déjà enregistrée."""
    name = body.get("name")
    if name not in config.SOURCES:
        raise HTTPException(400, "Source inconnue")
    url = str(body.get("url", "")).strip().rstrip("/")
    ep = (prowlarr if name == "prowlarr" else jackett).endpoint()
    key = (str(body.get("apikey") or "").strip() or config.load_settings()["sources"][name]["apikey"]
           or (ep[1] if ep else ""))
    if not (url and key):
        raise HTTPException(400, "Adresse et clé API nécessaires")
    try:
        if name == "prowlarr":
            indexers, _ = await clients.prowlarr_indexers(url, key)
            n = sum(i.get("protocol") == "torrent" for i in indexers)
        else:
            n = len(jackett._parse(await clients.jackett_indexers(url, key)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Échec : {xsdb.mask(str(e) or type(e).__name__)[:200]}") from e
    return {"ok": True, "indexers": n}


# --- interface ----------------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _versioned_index() -> str:
    """index.html avec ?v=<empreinte> sur app.js et style.css : après une mise à jour,
    le navigateur recharge forcément les nouveaux fichiers au lieu de garder l'ancienne version en cache."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "style.css"):
        v = hashlib.sha1((STATIC / name).read_bytes()).hexdigest()[:10]
        html = html.replace(f"/static/{name}", f"/static/{name}?v={v}")
    return html


_INDEX = _versioned_index()


@app.get("/")
async def index():
    return HTMLResponse(_INDEX, headers={"Cache-Control": "no-cache"})


from fastapi.responses import JSONResponse as _JSONResponse  # noqa: E402


@app.exception_handler(config.ReadOnlyError)
async def _readonly_error(request, exc):
    return _JSONResponse(status_code=403, content={"detail": str(exc)})
