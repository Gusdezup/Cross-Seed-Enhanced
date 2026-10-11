"""File de recherches : une recherche toutes les `delay` secondes.

Une recherche part soit chez cross-seed (tous ses indexers, résultat lu dans ses logs), soit en recherche
routée (XSE interroge lui-même des indexers choisis, voir routing.py) : indexers cibles d'une demande, ou
à défaut ceux du groupe de la catégorie de la release.

Les ajouts en nombre (demande de l'onglet Chercher, règle, scan planifié, sélection) forment une « demande »
(batch) : la file les affiche regroupés, avec leur avancement, et on peut les annuler d'un coup."""
import asyncio
import itertools
import json
import time

import httpx

from . import clients, config, logs, routing, scan

_ids = itertools.count(1)


class SearchQueue:
    def __init__(self):
        self.items: list = []
        self.batches: dict = {}   # {id: {"label", "source", "targets", "total", "done", …}}
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
        self.batches = {str(k): v for k, v in (data.get("batches") or {}).items()}
        for i in data.get("items", []):
            if i.get("status") == "running":
                i["status"] = "pending"   # interrompue par un redémarrage : on la relance
            self.items.append(i)
        _ids = itertools.count(max([i["id"] for i in self.items] + [int(k) for k in self.batches if k.isdigit()],
                                   default=0) + 1)

    def save(self):
        finished = [i["id"] for i in self.items if i["status"] not in ("pending", "running")]
        drop = set(finished[:-300])
        if drop:
            self.items = [i for i in self.items if i["id"] not in drop]
        # demandes : on garde celles qui ont encore des recherches, et les 30 dernières terminées
        live = {i.get("batch") for i in self.items if i["status"] in ("pending", "running")}
        # demande vide (tout était déjà en file, ou annulée avant de commencer) : rien à afficher
        self.batches = {k: b for k, b in self.batches.items() if b["total"] > 0 or k in live}
        for k in self.batches:
            self._finish_check(k)
        old = sorted((b for k, b in self.batches.items() if k not in live), key=lambda b: b["created"])[:-30]
        for b in old:
            self.batches.pop(str(b["id"]), None)
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self._path().with_suffix(".tmp")
            tmp.write_text(json.dumps({"paused": self.paused, "batches": self.batches, "items": self.items},
                                      ensure_ascii=False))
            tmp.replace(self._path())
        except OSError:
            pass

    # --- gestion de la file ---
    @staticmethod
    def _sig(key: str, targets) -> tuple:
        """Une même release peut être en file une fois par jeu de cibles (vide = cibles par défaut)."""
        return key, tuple(sorted(targets or ()))

    def _active(self) -> set:
        return {self._sig(i["key"], i.get("targets")) for i in self.items if i["status"] in ("pending", "running")}

    @staticmethod
    def _is_front(i: dict) -> bool:
        return bool(i.get("front")) or i.get("source") == "manuel"

    def new_batch(self, label: str, source: str, targets=None, detail: str = "") -> str:
        bid = str(next(_ids))
        self.batches[bid] = {"id": bid, "label": label, "source": source, "detail": detail,
                             "targets": list(targets or []), "created": time.time(), "total": 0, "done": 0,
                             "injected": 0, "found": 0, "errors": 0, "cancelled": None, "finished": None}
        return bid

    def add(self, release: dict, source: str, front: bool = False, targets=None, batch: str | None = None,
            save: bool = True) -> bool:
        """Ajoute une recherche. `targets` : clés d'indexers à interroger (recherche routée), sinon
        indexers du groupe de la catégorie, sinon cross-seed sur tous ses indexers.
        `front` : passe avant les recherches automatiques (règles, scan) déjà en attente, après les autres
        recherches « devant » (l'ordre d'un ajout en nombre est conservé)."""
        targets = list(dict.fromkeys(targets or []))
        if self._sig(release["key"], targets) in self._active() or not any(release["payload"].values()):
            return False
        item = {
            "id": next(_ids), "key": release["key"], "name": release["name"],
            "payload": release["payload"], "mode": release["mode"], "source": source,
            "category": release.get("category", ""), "size": release.get("size", 0),
            "skip": release.get("seeded_keys", []), "targets": targets, "batch": batch, "front": front,
            "status": "pending", "added": time.time(), "sent": None, "done": None,
            "result": None, "error": None,
        }
        if front:
            # ordre de passage : recherches unitaires (bouton « Chercher »), puis demandes, puis automatiques
            # (règles, scan) ; premier arrivé, premier servi à l'intérieur de chaque niveau
            single = not batch

            def after(i):
                return self._is_front(i) and (not single or not i.get("batch"))
            pos = next((n for n, i in enumerate(self.items) if i["status"] == "pending" and not after(i)),
                       len(self.items))
            self.items.insert(pos, item)
        else:
            self.items.append(item)
        if batch and batch in self.batches:
            self.batches[batch]["total"] += 1
        self._wake.set()
        if save:
            self.save()
        return True

    def cancel_batch(self, bid: str) -> int:
        """Retire les recherches encore en attente d'une demande (celle en cours se termine)."""
        n = sum(1 for i in self.items if i.get("batch") == bid and i["status"] == "pending")
        self.items = [i for i in self.items if not (i.get("batch") == bid and i["status"] == "pending")]
        b = self.batches.get(bid)
        if b and n:
            b["cancelled"] = time.time()
            b["total"] -= n
            b["dropped"] = b.get("dropped", 0) + n
        self._finish_check(bid)
        self.save()
        return n

    def _finish_check(self, bid):
        b = self.batches.get(bid or "")
        if b and not b["finished"] and not any(i.get("batch") == bid and i["status"] in ("pending", "running")
                                              for i in self.items):
            b["finished"] = time.time()

    def _count(self, item: dict):
        """Met à jour les compteurs de la demande d'une recherche terminée."""
        b = self.batches.get(item.get("batch") or "")
        if not b:
            return
        b["done"] += 1
        r = item.get("result") or {}
        if item["status"] in ("error", "timeout"):
            b["errors"] += 1
        b["injected"] += len(r.get("injected") or [])
        b["found"] += r.get("found") or 0
        self._finish_check(item["batch"])

    def remove(self, item_id: int) -> bool:
        gone = [i for i in self.items if i["id"] == item_id and i["status"] == "pending"]
        self.items = [i for i in self.items if i not in gone]
        for i in gone:
            b = self.batches.get(i.get("batch") or "")
            if b:
                b["total"] -= 1
                self._finish_check(i["batch"])
        self.save()
        return bool(gone)

    def clear_pending(self) -> int:
        n = 0
        for bid in {i.get("batch") for i in self.items if i["status"] == "pending" and i.get("batch")}:
            n += self.cancel_batch(bid)
        rest = sum(1 for i in self.items if i["status"] == "pending")
        self.items = [i for i in self.items if i["status"] != "pending"]
        self.save()
        return n + rest

    def clear_done(self) -> int:
        n = sum(1 for i in self.items if i["status"] in ("done", "error", "timeout"))
        self.items = [i for i in self.items if i["status"] not in ("done", "error", "timeout")]
        live = {i.get("batch") for i in self.items}
        self.batches = {k: b for k, b in self.batches.items() if k in live}
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
            "batches": [self._batch_view(b, delay) for b in sorted(self.batches.values(), key=lambda b: -b["created"])],
            # recherches hors demande, plus les 400 dernières ; celles des demandes sont chargées à la demande
            "items": [self._item_view(i) for i in self.items if not i.get("batch")][-400:],
        }

    @staticmethod
    def _item_view(i: dict) -> dict:
        return {k: v for k, v in i.items() if k != "payload"} | {"target": next(iter(i["payload"].values()))}

    def _batch_view(self, b: dict, delay: int) -> dict:
        mine = [i for i in self.items if i.get("batch") == b["id"]]
        pending = sum(1 for i in mine if i["status"] == "pending")
        running = sum(1 for i in mine if i["status"] == "running")
        # position du premier élément en attente de la demande : temps avant qu'elle démarre
        order = [i for i in self.items if i["status"] == "pending"]
        first = next((n for n, i in enumerate(order) if i.get("batch") == b["id"]), None)
        last = max((n for n, i in enumerate(order) if i.get("batch") == b["id"]), default=None)
        return b | {"pending": pending, "running": running,
                    "starts_in": None if first is None else first * delay,
                    "eta_seconds": None if last is None else (last + 1) * delay}

    def batch_items(self, bid: str) -> list:
        return [self._item_view(i) for i in self.items if i.get("batch") == bid]

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
        route = item.get("targets") or routing.route_for(item.get("category", ""))
        if route:
            await self._send_routed(item, route)
            return
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
            self._count(item)
            self.last_sent = time.time()
            self.save()
            return
        self.last_sent = time.time()
        self.save()
        asyncio.create_task(self._follow(item, offset, ident))

    async def _send_routed(self, item: dict, keys: list):
        """Catégorie routée : XSE interroge lui-même les indexers choisis (voir routing.py)."""
        item.update(status="running", sent=time.time(), routed=True)
        self.save()
        try:
            res = await routing.search(item, keys)
        except httpx.TransportError:
            item.update(status="pending", sent=None)
            self.last_sent = time.time()
            self.save()
            return
        except Exception as e:  # noqa: BLE001
            item.update(status="error", error=str(e)[:300], done=time.time())
        else:
            item.update(status="done", result=res, done=time.time())
            scan.record_routed(item["key"], res.get("searched") or [])
        self._count(item)
        self.last_sent = time.time()
        self.save()

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
                self._count(item)
                self.save()
                return
        item.update(status="timeout", done=time.time(),
                    error="Aucun résultat trouvé dans les logs après 10 minutes")
        self._count(item)
        self.save()


queue = SearchQueue()
