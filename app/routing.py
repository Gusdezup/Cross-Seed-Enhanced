"""Recherche « routée » par catégorie.

cross-seed interroge toujours tous ses indexers et ne sait pas en choisir certains selon la catégorie.
Pour une release dont la catégorie a une route, XSE interroge donc lui-même les seuls indexers choisis
(mêmes URL Torznab que dans config.js), puis soumet chaque résultat plausible à cross-seed par
/api/announce (le point d'entrée d'autobrr). cross-seed fait sa vérification habituelle (nom, taille,
fichiers) et injecte comme pour n'importe quelle correspondance.

Limite : une recherche routée n'est pas enregistrée dans l'historique de recherche de cross-seed
(excludeRecentSearch) ; XSE la note dans son propre historique (scan.record_routed).
"""
import asyncio
import json
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from . import clients, config, logs, xsdb

# --- requête Torznab, reprise simplifiée de cross-seed 6.13 (createTorznabSearchQueries) ------------

_EXTS = (".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".wmv", ".mov", ".m4v", ".mpg", ".mpeg", ".webm",
         ".iso", ".flac", ".mp3", ".m4a", ".m4b", ".epub", ".pdf", ".mobi", ".azw3", ".cbz", ".cbr")
_EP_RE = re.compile(
    r"^(?P<title>.+?)[_.\s-]+(?:(?P<season>S\d+)?[_.\s-]{0,3}(?P<episode>E\d+)(?:[\s-]?E?\d+)?(?![pix])"
    r"|(?P<date>(?P<year>(?:19|20)\d{2})[_.\s-](?P<month>\d{2})[_.\s-](?P<day>\d{2})))", re.I)
_SEASON_RE = re.compile(r"^(?P<title>.+?)[\[(_.\s-]+(?P<season>S(?:eason)?\s*\d+)(?=\b(?![_.\s~-]*E\d+))", re.I)
_MOVIE_RE = re.compile(r"^(?P<title>.+?)-?[_.\s][\[(]?(?P<year>(?:18|19|20)\d{2})[)\]]?(?![pix])", re.I)
_YEAR_RE = re.compile(r"(?:19|20)\d{2}(?![pix])", re.I)
_SCENE_RE = re.compile(r"^(?:[a-z0-9]{3,5}-)?(?P<title>.*)")
_BRACKETS_RE = re.compile(r"\[.*?\]|「.*?」|｢.*?｣|【.*?】")


def _cleanse(s: str) -> str:
    s = _BRACKETS_RE.sub("", s)
    s = re.sub(r"[._()\[\]]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return re.sub(r"^\s*-+|-+\s*$", "", s).strip()


def _clean_title(s: str) -> str:
    return _SCENE_RE.match(_cleanse(s)).group("title")


def _series_title(s: str) -> str:
    t = _clean_title(s)
    if len(t) <= 4:
        return t
    years = list(_YEAR_RE.finditer(t))
    if years:
        last = years[-1]
        t = t[:last.start()] + t[last.end():]
    return re.sub(r"\s+", " ", t).strip()


def _int(s: str) -> int:
    return int(re.sub(r"\D", "", s))


def queries(name: str) -> list:
    """Paramètres Torznab à essayer dans l'ordre : la requête de cross-seed, puis une recherche
    texte simple si l'indexer ne gère pas tvsearch ou movie."""
    stem = name
    for ext in _EXTS:
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    m = _EP_RE.match(stem)
    if m:
        title = _series_title(m.group("title"))
        if m.group("date"):
            return [{"t": "tvsearch", "q": title, "season": m.group("year"), "ep": f"{m.group('month')}/{m.group('day')}"},
                    {"t": "search", "q": f"{title} {m.group('year')} {m.group('month')} {m.group('day')}"}]
        ep = _int(m.group("episode"))
        if m.group("season"):
            season = _int(m.group("season"))
            return [{"t": "tvsearch", "q": title, "season": season, "ep": ep},
                    {"t": "search", "q": f"{title} S{season:02d}E{ep:02d}"}]
        return [{"t": "tvsearch", "q": title, "ep": ep}, {"t": "search", "q": f"{title} E{ep:02d}"}]
    m = _SEASON_RE.match(stem)
    if m:
        title, season = _series_title(m.group("title")), _int(m.group("season"))
        return [{"t": "tvsearch", "q": title, "season": season}, {"t": "search", "q": f"{title} S{season:02d}"}]
    m = _MOVIE_RE.match(stem)
    if m:
        q = _clean_title(m.group(0))
        return [{"t": "movie", "q": q}, {"t": "search", "q": q}]
    return [{"t": "search", "q": _clean_title(stem) or stem}]


# --- interrogation d'un indexer -------------------------------------------------------------------

class TorznabError(RuntimeError):
    pass


class RateLimited(TorznabError):
    """HTTP 429 : l'indexer demande de ralentir pendant `seconds` secondes."""
    def __init__(self, seconds: int):
        super().__init__(f"HTTP 429 (trop de requêtes), en pause {seconds // 60} min")
        self.seconds = seconds


# --- pauses d'indexers ---------------------------------------------------------------------------
# Un indexer en pause n'est pas interrogé : pause posée par cross-seed (base, ou « snoozing » dans ses
# logs) ou par XSE lui-même après un HTTP 429 (data/indexer_pauses.json).

DEFAULT_PAUSE = 3600


def _pauses_path():
    return config.DATA_DIR / "indexer_pauses.json"


def _load_pauses() -> dict:
    try:
        return {k: float(v) for k, v in json.loads(_pauses_path().read_text()).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def pause_indexer(key: str, seconds: int) -> None:
    data = {k: v for k, v in _load_pauses().items() if v > time.time()}
    data[key] = time.time() + seconds
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _pauses_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(_pauses_path())
    except OSError:
        pass


def _epoch(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip()).timestamp()
    except ValueError:
        return None


def paused_until(item: dict, local: dict):
    """Fin de pause (epoch) d'un indexer de xsdb.indexers(), ou None s'il est disponible."""
    ends = [_epoch(item.get("retry_after")), _epoch(item.get("snooze_log")), local.get(item["key"])]
    end = max((e for e in ends if e), default=None)
    return end if end and end > time.time() else None


def _retry_after(r) -> int:
    v = (r.headers.get("Retry-After") or "").strip()
    if v.isdigit():
        return max(60, min(int(v), 24 * 3600))
    try:
        return max(60, min(int(parsedate_to_datetime(v).timestamp() - time.time()), 24 * 3600))
    except (TypeError, ValueError):
        return DEFAULT_PAUSE


def _with_params(url: str, params: dict) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.update({k: str(v) for k, v in params.items() if v not in (None, "")})
    return urlunsplit(parts._replace(query=urlencode(query)))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_results(xml_text: str) -> list:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise TorznabError(f"réponse illisible ({e})") from e
    if _local(root.tag) == "error":
        raise TorznabError(f"erreur {root.get('code')} : {root.get('description') or ''}".strip())
    out = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        d = {"title": "", "link": "", "size": 0, "tracker": ""}
        for el in item:
            tag = _local(el.tag)
            text = (el.text or "").strip()
            if tag == "title":
                d["title"] = text
            elif tag == "link":
                d["link"] = text
            elif tag == "enclosure" and not d["link"]:
                d["link"] = el.get("url", "")
            elif tag == "size" and text.isdigit():
                d["size"] = int(text)
            elif tag == "attr" and el.get("name") == "size" and not d["size"]:
                d["size"] = int(el.get("value") or 0)
            elif tag in ("prowlarrindexer", "jackettindexer", "indexer") and text and not d["tracker"]:
                d["tracker"] = text
        if d["title"] and d["link"].startswith(("http://", "https://")):
            out.append(d)
    return out


async def search_indexer(url: str, params_list: list) -> list:
    """Résultats d'un indexer pour la première requête qu'il accepte."""
    last = None
    for params in params_list:
        r = await clients.torznab_get(_with_params(url, params))
        if r.status_code == 429:
            raise RateLimited(_retry_after(r))
        if r.status_code >= 400:
            last = TorznabError(f"HTTP {r.status_code}")
            continue
        try:
            return parse_results(r.text)
        except TorznabError as e:
            last = e   # ex. « Function Not Available » pour tvsearch : on tente la recherche texte
    raise last or TorznabError("aucune requête possible")


# Écart de taille au-delà duquel un résultat n'est même pas soumis à cross-seed (qui garde le dernier
# mot, avec son propre seuil). Évite d'envoyer en announce des dizaines de releases sans rapport.
SIZE_TOLERANCE = 0.10
MAX_PER_INDEXER = 10


def plausible(cands: list, size: int) -> list:
    if size:
        cands = [c for c in cands if not c["size"] or abs(c["size"] - size) <= size * SIZE_TOLERANCE]
    cands.sort(key=lambda c: abs((c["size"] or size) - size))
    return cands[:MAX_PER_INDEXER]


# --- routes -----------------------------------------------------------------------------------------

def route_for(category: str) -> list:
    """Clés des indexers (URL Torznab normalisée) associés à cette catégorie, ou [] : recherche normale."""
    cat = (category or "").strip().lower()
    if not cat:
        return []
    for r in config.load_settings().get("routes", []):
        if r["category"].lower() == cat and r["indexers"]:
            return r["indexers"]
    return []


async def search(item: dict, keys: list) -> dict:
    """Recherche routée d'une release de la file. Renvoie un résultat au format de logs.analyse,
    complété de « routed » (indexers interrogés), « candidates » et « errors »."""
    config.guard("recherche routée")
    entries = {e["key"]: e for e in await asyncio.to_thread(xsdb.torznab_entries) if e["state"] == "active"}
    infos = {i["key"]: i for i in (await asyncio.to_thread(xsdb.indexers))["items"]}
    names = {k: i["name"] for k, i in infos.items()}
    local = _load_pauses()
    paused = {k: paused_until(infos[k], local) for k in keys if k in infos}
    paused = {k: v for k, v in paused.items() if v}
    res = {"found": 0, "injected": [], "failed": [], "exists": [], "skipped": None, "refused": None,
           "routed": [names.get(k, xsdb.fallback_name(k)) for k in keys
                      if k not in paused and k not in set(item.get("skip") or [])],
           "seeded": [names.get(k, k) for k in keys if k in set(item.get("skip") or []) and k not in paused],
           "paused": [f"{names.get(k, k)} (jusqu'à {datetime.fromtimestamp(v).strftime('%H:%M')})" for k, v in paused.items()],
           "candidates": 0, "errors": []}
    params = queries(item["name"])
    offset, ident = logs.position("info")
    sent = []
    skip = set(item.get("skip") or [])
    for key in keys:
        if key in paused or key in skip:
            continue
        name = names.get(key, xsdb.fallback_name(key))
        entry = entries.get(key)
        if not entry:
            res["errors"].append(f"{name} : suspendu ou absent de config.js")
            continue
        try:
            cands = plausible(await search_indexer(entry["url"], params), item.get("size") or 0)
        except RateLimited as e:
            pause_indexer(key, e.seconds)
            res["paused"].append(f"{name} (429, jusqu'à {datetime.fromtimestamp(time.time() + e.seconds).strftime('%H:%M')})")
            continue
        except (TorznabError, httpx.HTTPError) as e:
            res["errors"].append(f"{name} : {xsdb.mask(str(e) or type(e).__name__)[:150]}")
            continue
        res["candidates"] += len(cands)
        for c in cands:
            tracker = c["tracker"] or name
            try:
                status = await clients.xs_announce(c["title"], c["link"], tracker)
            except httpx.TransportError:
                raise   # cross-seed injoignable : la file remettra la recherche en attente
            except Exception as e:  # noqa: BLE001
                res["errors"].append(f"{tracker} : {str(e)[:150]}")
                continue
            if status in (200, 202):
                res["found"] += 1
                sent.append((c["title"], tracker, status))
    if sent:
        await asyncio.sleep(2)   # laisse à cross-seed le temps d'écrire ses logs
        try:
            text, _, _ = await asyncio.to_thread(logs.read_from, "info", offset, ident)
        except OSError:
            text = ""
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)   # couleurs éventuelles
        for title, tracker, status in sent:
            if re.search(r"Already exists " + re.escape(title), text):
                res["exists"].append(tracker)
            elif status == 202 or re.search(r"Saved " + re.escape(title), text):
                res["failed"].append(tracker)
            else:
                res["injected"].append(tracker)
    return res
