"""File de recherches : une recherche cross-seed toutes les `delay` secondes, résultat lu dans les logs."""
import asyncio
import itertools
import json
import time

import httpx

from . import clients, config, logs

_ids = itertools.count(1)


class SearchQueue:
    def __init__(self):
        self.items: list = []
        self.paused = False
        self.last_sent = 0.0
        self._wake = asyncio.Event()
        self._load()

    # --- persistance (la file survit à un redémarrage du conteneur) ---
    def _path(self):
        return config.DATA_DIR / "queue.json"

    def _load(self):
        global _ids
        try:
            data = json.loads(self._path().read_text())
        except (OSError, ValueError):
            return
        self.paused = bool(data.get("paused"))
        for i in data.get("items", []):
            if i.get("status") == "running":
                i["status"] = "pending"   # interrompue par un redémarrage : on la relance
            self.items.append(i)
        _ids = itertools.count(max((i["id"] for i in self.items), default=0) + 1)

    def save(self):
        finished = [i["id"] for i in self.items if i["status"] not in ("pending", "running")]
        drop = set(finished[:-300])
        if drop:
            self.items = [i for i in self.items if i["id"] not in drop]
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self._path().with_suffix(".tmp")
            tmp.write_text(json.dumps({"paused": self.paused, "items": self.items}, ensure_ascii=False))
            tmp.replace(self._path())
        except OSError:
            pass

    # --- gestion de la file ---
    def _active_keys(self) -> set:
        return {i["key"] for i in self.items if i["status"] in ("pending", "running")}

    def add(self, release: dict, source: str, front: bool = False) -> bool:
        if release["key"] in self._active_keys() or not any(release["payload"].values()):
            return False
        item = {
            "id": next(_ids), "key": release["key"], "name": release["name"],
            "payload": release["payload"], "mode": release["mode"], "source": source,
            "status": "pending", "added": time.time(), "sent": None, "done": None,
            "result": None, "error": None,
        }
        if front:
            pos = next((n for n, i in enumerate(self.items) if i["status"] == "pending"
                        and i["source"] != "manuel"), len(self.items))
            self.items.insert(pos, item)
        else:
            self.items.append(item)
        self._wake.set()
        self.save()
        return True

    def remove(self, item_id: int) -> bool:
        before = len(self.items)
        self.items = [i for i in self.items if not (i["id"] == item_id and i["status"] == "pending")]
        self.save()
        return len(self.items) < before

    def clear_pending(self) -> int:
        n = sum(1 for i in self.items if i["status"] == "pending")
        self.items = [i for i in self.items if i["status"] != "pending"]
        self.save()
        return n

    def clear_done(self) -> int:
        n = sum(1 for i in self.items if i["status"] in ("done", "error", "timeout"))
        self.items = [i for i in self.items if i["status"] not in ("done", "error", "timeout")]
        self.save()
        return n

    def snapshot(self) -> dict:
        delay = config.load_settings()["delay"]
        pending = sum(1 for i in self.items if i["status"] == "pending")
        wait = max(0, int(self.last_sent + delay - time.time()))
        return {
            "paused": self.paused or config.READONLY, "delay": delay, "pending": pending,
            "next_in": wait if pending and not (self.paused or config.READONLY) else None,
            "eta_seconds": (wait + max(0, pending - 1) * delay) if pending else 0,
            "items": [{k: v for k, v in i.items() if k != "payload"} | {"target": next(iter(i["payload"].values()))}
                      for i in self.items[-400:]],
        }

    # --- boucle de traitement ---
    async def run(self):
        while True:
            item = next((i for i in self.items if i["status"] == "pending"), None)
            if self.paused or config.READONLY or item is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
                continue
            delay = config.load_settings()["delay"]
            wait = self.last_sent + delay - time.time()
            if wait > 0:
                await asyncio.sleep(min(wait, 2))
                continue
            await self._send(item)

    async def _send(self, item: dict):
        item["status"] = "running"
        item["sent"] = time.time()
        offset, ident = logs.position("info")
        try:
            await clients.xs_webhook(item["payload"])
        except httpx.TransportError:
            # cross-seed injoignable (redémarrage en cours…) : la recherche reste en attente
            item.update(status="pending", sent=None)
            self.last_sent = time.time()
            self.save()
            return
        except Exception as e:  # noqa: BLE001
            item.update(status="error", error=str(e), done=time.time())
            self.last_sent = time.time()
            self.save()
            return
        self.last_sent = time.time()
        self.save()
        asyncio.create_task(self._follow(item, offset, ident))

    async def _follow(self, item: dict, offset: int, ident):
        """Lit les logs info jusqu'à trouver le résumé « Found N torrents for … »."""
        buf = ""
        deadline = time.time() + 600
        while time.time() < deadline:
            await asyncio.sleep(3)
            try:
                text, offset, ident = logs.read_from("info", offset, ident)
            except OSError:
                continue
            buf = (buf + text)[-2_000_000:]
            res = logs.analyse(buf, item["payload"])
            if res is not None:
                item.update(status="done", result=res, done=time.time())
                self.save()
                return
        item.update(status="timeout", done=time.time(),
                    error="Aucun résultat trouvé dans les logs après 10 minutes")
        self.save()


queue = SearchQueue()
