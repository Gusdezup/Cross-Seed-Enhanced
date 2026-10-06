"""cross-seed-enhanced — interface web pour piloter cross-seed."""
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import clients, config, jackett, logs, prowlarr, releases, xsdb
from .worker import queue

STATIC = Path(__file__).parent / "static"
JOBS = ("rss", "search", "inject", "cleanup")


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(queue.run())
    yield
    task.cancel()


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
    out["readonly"] = config.READONLY
    out["config_write"] = not config.READONLY or config.ALLOW_CONFIG_WRITE
    out["files"] = {
        "config": config.XS_CONFIG_JS.exists(),
        "logs": config.LOGS_DIR.is_dir(),
        "db": config.XS_DB.exists(),
        "pending_dir": config.PENDING_DIR.is_dir(),
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
    items = releases.build(torrents, settings, {**await jackett.site_names(), **await prowlarr.site_names()})
    trackers = {}
    for r in items:
        for c in r["copies"]:
            trackers.setdefault(c["host"], c["tracker"])
    return {"releases": items, "trackers": trackers, "torrents": len(torrents)}


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
    added = 0
    for k in keys:
        r = releases.get(k)
        if r and queue.add(r, source="manuel", front=True):
            added += 1
    return {"added": added, "skipped": len(keys) - added}


@app.post("/api/queue/rules")
async def queue_rules(body: dict | None = None):
    only = (body or {}).get("rule")
    data = await list_releases(refresh=True)
    settings = config.load_settings()
    order = {config.rule_label(r): n for n, r in enumerate(settings["rules"])}
    cands = [r for r in data["releases"] if r["rules"] and (not only or only in r["rules"])]
    cands.sort(key=lambda r: (min(order.get(n, 99) for n in r["rules"]), r["count"], r["name"].lower()))
    added = sum(1 for r in cands if queue.add(r, source=r["rules"][0]))
    return {"added": added, "matched": len(cands)}


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


@app.delete("/api/queue/{item_id}")
async def queue_remove(item_id: int):
    return {"removed": queue.remove(item_id)}


# --- injections en attente ----------------------------------------------------

PENDING_RE = re.compile(r"^\[(?P<type>[^\]]+)\]\[(?P<tracker>[^\]]+)\](?P<name>.+)\[(?P<hash>[0-9a-fA-F]{40})\]\.torrent$")


@app.get("/api/pending")
async def pending():
    items = []
    if config.PENDING_DIR.is_dir():
        for p in sorted(config.PENDING_DIR.glob("*.torrent"), key=lambda x: x.stat().st_mtime, reverse=True):
            m = PENDING_RE.match(p.name)
            info = m.groupdict() if m else {"type": "?", "tracker": "?", "name": p.stem, "hash": ""}
            info.update(file=p.name, size=p.stat().st_size,
                        mtime=datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"))
            info["errors"] = await asyncio.to_thread(logs.errors_for, info["hash"][:8]) if info["hash"] else []
            items.append(info)
    writable = config.PENDING_DIR.is_dir() and os.access(config.PENDING_DIR, os.W_OK) and not config.READONLY
    return {"items": items, "writable": writable}


@app.delete("/api/pending/{filename}")
async def pending_delete(filename: str):
    config.guard("suppression de fichiers en attente")
    p = (config.PENDING_DIR / filename).resolve()
    if p.parent != config.PENDING_DIR.resolve() or p.suffix != ".torrent" or not p.exists():
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
    data["settings"] = await asyncio.to_thread(xsdb.useful_settings)
    await prowlarr.enrich(data)
    await jackett.enrich(data)
    return jackett.link_sites(data)


def _config_error(e: Exception) -> HTTPException:
    if isinstance(e, PermissionError):
        return HTTPException(403, "config.js est en lecture seule : vérifie le montage dans docker-compose.yml")
    return HTTPException(400, str(e))


@app.post("/api/indexers/add")
async def indexer_add(body: dict):
    """Ajoute un indexer Prowlarr (prowlarr_id) ou Jackett (jackett_id) au tableau torznab de config.js."""
    try:
        if body.get("jackett_id"):
            jid = str(body["jackett_id"])
            if not any(i["id"] == jid for i in await jackett.fetch()):
                raise ValueError("Indexer introuvable dans Jackett")
            return await asyncio.to_thread(lambda: xsdb.add_indexer(xsdb.jackett_torznab_url(jid)))
        pid = int(body.get("prowlarr_id"))
        indexers, _ = await prowlarr.fetch()
        if not any(i["id"] == pid and i.get("protocol") == "torrent" for i in indexers):
            raise ValueError("Indexer torrent introuvable dans Prowlarr")
        return await asyncio.to_thread(lambda: xsdb.add_indexer(xsdb.torznab_url(pid)))
    except (TypeError, ValueError, OSError, RuntimeError) as e:
        raise _config_error(e) from e


@app.post("/api/indexers/remove")
async def indexer_remove(body: dict):
    """Supprime la ligne d'un indexer du tableau torznab de config.js."""
    try:
        return await asyncio.to_thread(xsdb.remove_indexer, str(body.get("key", "")))
    except (ValueError, OSError) as e:
        raise _config_error(e) from e


@app.post("/api/indexers/toggle")
async def indexer_toggle(body: dict):
    try:
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
