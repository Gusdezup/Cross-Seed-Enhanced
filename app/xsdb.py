"""Lecture SEULE de cross-seed.db ; config.js est lu, et modifié seulement sur action explicite
(indexers, réglages), avec sauvegarde préalable.

Le schéma de la base peut changer entre versions de cross-seed : chaque requête
vérifie d'abord les tables et colonnes présentes, et l'interface affiche
simplement « indisponible » si quelque chose manque.
"""
import re
import sqlite3
from datetime import datetime

from . import config, logs


def _connect():
    if not config.XS_DB.exists():
        return None
    uri = f"file:{config.XS_DB}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        # Dossier monté en lecture seule + base en WAL : on lit sans verrou.
        con = sqlite3.connect(uri + "&immutable=1", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _columns(con, table: str) -> set:
    return {r["name"] for r in con.execute(f"PRAGMA table_info('{table}')")}


def _ts(v):
    if v in (None, "", 0):
        return None
    try:
        v = float(v)
        if v > 1e12:
            v /= 1000
        return datetime.fromtimestamp(v).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return str(v)


def fallback_name(url: str) -> str:
    m = re.search(r":9696/(\d+)", url or "")
    if m:
        return f"Prowlarr n°{m.group(1)}"
    m = _JK_RE.match(url or "")
    return f"Jackett {m['id']}" if m else logs.normalize_url(url)


def mask(url: str) -> str:
    return re.sub(r"(apikey=)[^&\s'\"]+", r"\1•••", url or "")


def indexers() -> dict:
    names = logs.indexer_names_from_logs()
    snoozes = logs.snoozes_from_logs()
    out = {"available": False, "items": [], "error": None}
    try:
        con = _connect()
    except sqlite3.Error as e:
        out["error"] = str(e)
        con = None
    if con:
        try:
            cols = _columns(con, "indexer")
            if cols:
                for r in con.execute("SELECT * FROM indexer"):
                    r = dict(r)
                    url = r.get("url", "")
                    name = r.get("name") or names.get(logs.normalize_url(url)) or fallback_name(url)
                    out["items"].append({
                        "id": r.get("id"),
                        "key": logs.normalize_url(url),
                        "name": name,
                        "url": mask(url),
                        "active": bool(r.get("active", 1)),
                        "status": r.get("status") or "OK",
                        "retry_after": _ts(r.get("retry_after")),
                        "snooze_log": snoozes.get(name),
                    })
                out["available"] = True
        except sqlite3.Error as e:
            out["error"] = str(e)
        finally:
            con.close()
    # État dans config.js : "active", "suspended" (ligne commentée) ou None (retiré)
    entries = {e["key"]: e for e in torznab_entries()}
    known = {i["key"] for i in out["items"]}
    for e in entries.values():
        if e["key"] not in known:
            name = names.get(e["key"]) or fallback_name(e["key"])
            out["items"].append({"id": None, "key": e["key"], "name": name, "url": mask(e["url"]),
                                 "active": e["state"] == "active", "status": None,
                                 "retry_after": None, "snooze_log": snoozes.get(name)})
    for i in out["items"]:
        i["config"] = entries[i["key"]]["state"] if i["key"] in entries else None
    return out


def history(names: list) -> dict:
    """Dernières recherches par indexer pour les torrents donnés (par nom)."""
    out = {"available": False, "rows": [], "error": None}
    try:
        con = _connect()
    except sqlite3.Error as e:
        out["error"] = str(e)
        return out
    if not con:
        out["error"] = "cross-seed.db introuvable"
        return out
    try:
        tcols, scols = _columns(con, "timestamp"), _columns(con, "searchee")
        if not ({"searchee_id", "indexer_id", "last_searched"} <= tcols and {"id", "name"} <= scols):
            out["error"] = "Schéma de base non reconnu"
            return out
        idx = {i["id"]: i["name"] for i in indexers()["items"] if i["id"] is not None}
        q = ("SELECT s.name AS searchee, t.indexer_id, t.first_searched, t.last_searched "
             "FROM timestamp t JOIN searchee s ON s.id = t.searchee_id "
             f"WHERE s.name IN ({','.join('?' * len(names))}) ORDER BY t.last_searched DESC")
        for r in con.execute(q, names):
            out["rows"].append({
                "searchee": r["searchee"],
                "indexer": idx.get(r["indexer_id"], f"#{r['indexer_id']}"),
                "first": _ts(r["first_searched"]),
                "last": _ts(r["last_searched"]),
            })
        out["available"] = True
    except sqlite3.Error as e:
        out["error"] = str(e)
    finally:
        con.close()
    return out


# --- config.js (analyse textuelle, jamais exécutée) ---------------------------

def _config_text() -> str:
    try:
        return config.XS_CONFIG_JS.read_text(encoding="utf-8")
    except OSError:
        return ""


_URL_RE = re.compile(r"https?://[^\s'\"`]+/api\?apikey=[^\s'\"`]+")


_STR_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`[^`\n]*`')


def _code_part(line: str) -> str:
    """La ligne sans ses chaînes ni ses commentaires (pour repérer un vrai « ] »)."""
    if line.lstrip().startswith("//"):
        return ""
    return _STR_RE.sub('""', line).split("//")[0]


def _torznab_lines(lines: list):
    """Indices des lignes d'URL du tableau torznab, « ] » final sur sa propre ligne ou non."""
    start = next((n for n, l in enumerate(lines) if re.match(r"\s*torznab\s*:\s*\[", l)), None)
    if start is None or "]" in _code_part(lines[start]) or _URL_RE.search(lines[start]):
        return None   # tableau sur une seule ligne : format non géré
    for n in range(start + 1, len(lines)):
        if "]" in _code_part(lines[n]):
            return range(start + 1, n + 1)
    return None


def torznab_entries() -> list:
    lines = _config_text().split("\n")
    rng = _torznab_lines(lines)
    out = []
    if rng is None:
        return out
    for n in rng:
        urls = _URL_RE.findall(lines[n])
        if len(urls) != 1:
            continue
        state = "suspended" if lines[n].lstrip().startswith("//") else "active"
        out.append({"line": n, "url": urls[0], "key": logs.normalize_url(urls[0]), "state": state})
    return out


def set_indexer(key: str, enable: bool) -> dict:
    """Commente (suspend) ou décommente (réactive) la ligne d'un indexer dans le tableau torznab."""
    text = _config_text()
    lines = text.split("\n")
    entries = torznab_entries()
    if not entries:
        raise ValueError("Tableau torznab introuvable ou au format non reconnu (une URL par ligne attendue)")
    target = next((e for e in entries if e["key"] == key), None)
    if not target:
        raise ValueError("Indexer absent de config.js")
    if not enable and sum(e["state"] == "active" for e in entries) <= 1:
        raise ValueError("Impossible de suspendre le dernier indexer actif")
    line = lines[target["line"]]
    indent = line[:len(line) - len(line.lstrip())]
    body = line.lstrip()
    if enable:
        if target["state"] == "active":
            return {"changed": False, "backup": None}
        body = re.sub(r"^//\s?", "", body)
        body = re.sub(r"\s*//\s*suspendu par cross-seed-enhanced.*$", "", body)
    else:
        if target["state"] == "suspended":
            return {"changed": False, "backup": None}
        # Si la ligne ferme aussi le tableau (« "url"], »), on sort le « ] » sur sa propre ligne
        # avant de commenter, sinon le commentaire avalerait la fin du tableau.
        m = re.match(r'^(.*?["\'`])\s*(\].*)$', body)
        tail = None
        if m and "]" in _code_part(body):
            body, tail = m.group(1) + ",", m.group(2)
        body = f"// {body}  // suspendu par cross-seed-enhanced le {datetime.now():%d/%m/%Y}"
        if tail is not None:
            start_indent = next(l for l in lines if re.match(r"\s*torznab\s*:\s*\[", l))
            base = start_indent[:len(start_indent) - len(start_indent.lstrip())]
            lines[target["line"]] = indent + body + "\n" + base + tail
            return _write_config(text, "\n".join(lines))
    lines[target["line"]] = indent + body
    return _write_config(text, "\n".join(lines))


_TPL_RE = re.compile(r"^(?P<prefix>.*)/\d+/api\?apikey=(?P<key>[^&\s'\"`]+)$")


def prowlarr_from_config():
    """(adresse, clé) de Prowlarr déduites de la première ligne Torznab de config.js, ou None.
    La clé Torznab de Prowlarr est sa clé API."""
    for e in torznab_entries():
        m = _TPL_RE.match(e["url"])
        if m:
            return m["prefix"], m["key"]
    return None


def torznab_url(prowlarr_id: int) -> str:
    """URL Torznab d'un indexer Prowlarr, sur le modèle des lignes déjà présentes dans config.js
    (c'est cross-seed qui la contacte : on garde l'adresse qu'il utilise déjà)."""
    for e in torznab_entries():
        m = _TPL_RE.match(e["url"])
        if m:
            return f"{m['prefix']}/{prowlarr_id}/api?apikey={m['key']}"
    url, key, _ = config.source("prowlarr")
    if not (url and key):
        raise ValueError("Aucune ligne Torznab existante à imiter et Prowlarr non configuré")
    return f"{url}/{prowlarr_id}/api?apikey={key}"


_JK_RE = re.compile(r"^(?P<prefix>.*)/api/v2\.0/indexers/(?P<id>[^/]+)/results/torznab/api\?apikey=(?P<key>[^&\s'\"`]+)$")


def jackett_from_config():
    """(adresse, clé) de Jackett déduites de la première ligne Torznab Jackett de config.js, ou None."""
    for e in torznab_entries():
        m = _JK_RE.match(e["url"])
        if m:
            return m["prefix"], m["key"]
    return None


def jackett_torznab_url(jackett_id: str) -> str:
    """URL Torznab d'un indexer Jackett, sur le modèle des lignes Jackett déjà présentes dans config.js."""
    for e in torznab_entries():
        m = _JK_RE.match(e["url"])
        if m:
            return f"{m['prefix']}/api/v2.0/indexers/{jackett_id}/results/torznab/api?apikey={m['key']}"
    url, key, _ = config.source("jackett")
    if not (url and key):
        raise ValueError("Aucune ligne Torznab Jackett à imiter et Jackett non configuré")
    return f"{url}/api/v2.0/indexers/{jackett_id}/results/torznab/api?apikey={key}"


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def add_indexer(url: str) -> dict:
    """Ajoute une URL au tableau torznab (ou réactive sa ligne si elle est suspendue)."""
    key = logs.normalize_url(url)
    entries = torznab_entries()
    existing = next((e for e in entries if e["key"] == key), None)
    if existing:
        return set_indexer(key, True) if existing["state"] == "suspended" else {"changed": False, "backup": None}
    text = _config_text()
    lines = text.split("\n")
    rng = _torznab_lines(lines)
    if rng is None:
        raise ValueError("Tableau torznab introuvable ou au format non reconnu (une URL par ligne attendue)")
    base = _indent(lines[rng.start - 1])
    indent = _indent(lines[entries[0]["line"]]) if entries else base + "    "
    last = rng[-1]                      # ligne qui contient le « ] » final
    body = lines[last]
    if _URL_RE.search(body) and not body.lstrip().startswith("//"):
        # « "url"], » : on coupe avant le « ] », puis nouvelle ligne, puis « ], »
        m = re.match(r'^(.*?["\'`])\s*(\].*)$', body)
        if not m:
            raise ValueError("Fin du tableau torznab au format non reconnu")
        lines[last:last + 1] = [m.group(1) + ",", f'{indent}"{url}"', base + m.group(2).strip()]
    else:
        # « ] » sur sa propre ligne : la dernière entrée active doit finir par une virgule
        active = [e for e in entries if e["state"] == "active"]
        if active:
            n = active[-1]["line"]
            if not _code_part(lines[n]).rstrip().endswith(","):
                lines[n] = re.sub(r'(["\'`])(\s*(?://.*)?)$', r"\1,\2", lines[n], count=1)
        lines.insert(last, f'{indent}"{url}",')
    return _write_config(text, "\n".join(lines))


def remove_indexer(key: str) -> dict:
    """Supprime la ligne d'un indexer (active ou suspendue) du tableau torznab."""
    text = _config_text()
    lines = text.split("\n")
    entries = torznab_entries()
    target = next((e for e in entries if e["key"] == key), None)
    if not target:
        raise ValueError("Indexer absent de config.js")
    if target["state"] == "active" and sum(e["state"] == "active" for e in entries) <= 1:
        raise ValueError("Impossible de retirer le dernier indexer actif")
    n = target["line"]
    if target["state"] == "active" and "]" in _code_part(lines[n]):
        # la ligne ferme aussi le tableau : on ne garde que « ], »
        m = re.match(r'^.*?["\'`]\s*(\].*)$', lines[n])
        base = _indent(lines[_torznab_lines(lines).start - 1])
        lines[n] = base + m.group(1).strip()
    else:
        del lines[n]
    return _write_config(text, "\n".join(lines))


def _write_config(old: str, new: str) -> dict:
    config.guard("modification de config.js", config_write=True)
    backups = config.DATA_DIR / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / f"config.js.{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.bak"
    backup.write_text(old, encoding="utf-8")
    # Écriture sur place (même inode) : compatible avec un montage de fichier unique.
    with open(config.XS_CONFIG_JS, "r+", encoding="utf-8") as f:
        f.seek(0)
        f.write(new)
        f.truncate()
    return {"changed": True, "backup": backup.name}


USEFUL_KEYS = ("action", "matchMode", "linkType", "delay", "searchCadence", "rssCadence",
               "excludeRecentSearch", "excludeOlder", "searchLimit", "includeSingleEpisodes",
               "seasonFromEpisodes", "duplicateCategories", "linkCategory")

_VAL = r"""(?P<val>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`[^`\n]*`|[^,\n/]+?)"""


def _line_re(key: str):
    return re.compile(rf"^(?P<pre>[ \t]*{key}[ \t]*:[ \t]*){_VAL}(?P<post>[ \t]*,?[ \t]*(?://[^\n]*)?)$", re.M)


def _kind(raw: str) -> str:
    raw = raw.strip()
    if raw[:1] in "\"'`":
        return "string"
    if raw in ("true", "false"):
        return "boolean"
    if raw in ("undefined", "null"):
        return "empty"
    if re.fullmatch(r"-?\d+(\.\d+)?", raw):
        return "number"
    return "other"


def useful_settings() -> dict:
    """{clé: {"value": str, "kind": string|number|boolean|empty|other}}"""
    text = _config_text()
    out = {}
    for key in USEFUL_KEYS:
        m = _line_re(key).search(text)
        if m:
            raw = m.group("val").strip()
            kind = _kind(raw)
            value = raw[1:-1] if kind == "string" else ("" if kind == "empty" else raw)
            out[key] = {"value": value, "kind": kind}
    return out


def _literal(new: str, kind: str, key: str) -> str:
    new = str(new).strip()
    if kind == "boolean":
        if new not in ("true", "false"):
            raise ValueError(f"{key} : true ou false attendu")
        return new
    if kind == "number":
        if not re.fullmatch(r"-?\d+(\.\d+)?", new):
            raise ValueError(f"{key} : nombre attendu")
        return new
    if kind == "other":
        raise ValueError(f"{key} : valeur trop complexe pour être modifiée ici")
    if kind == "empty" and new == "":
        return "undefined"
    if kind == "empty" and (new in ("true", "false") or re.fullmatch(r"-?\d+(\.\d+)?", new)):
        return new
    return '"' + new.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --- Validation : mêmes règles que cross-seed v6.13 (src/configSchema.ts) -----------

_MS_RE = re.compile(r"^(-?(?:\d+)?\.?\d+) *(milliseconds?|msecs?|ms|seconds?|secs?|s|minutes?|mins?|m|"
                    r"hours?|hrs?|h|days?|d|weeks?|w|years?|yrs?|y)$", re.I)
_UNIT_MS = {"ms": 1, "msec": 1, "millisecond": 1, "s": 1e3, "sec": 1e3, "second": 1e3,
            "m": 6e4, "min": 6e4, "minute": 6e4, "h": 3.6e6, "hr": 3.6e6, "hour": 3.6e6,
            "d": 8.64e7, "day": 8.64e7, "w": 6.048e8, "week": 6.048e8, "y": 3.15576e10, "yr": 3.15576e10,
            "year": 3.15576e10}
MIN, HOUR, DAY = 6e4, 3.6e6, 8.64e7


def parse_duration(v: str):
    """Durée au format de la librairie `ms` utilisée par cross-seed ; None si invalide."""
    m = _MS_RE.match(v.strip())
    if not m:
        return None
    unit = m.group(2).lower()
    unit = {"msecs": "msec", "milliseconds": "millisecond", "seconds": "second", "secs": "sec",
            "minutes": "minute", "mins": "min", "hours": "hour", "hrs": "hr", "days": "day",
            "weeks": "week", "years": "year", "yrs": "yr"}.get(unit, unit)
    return float(m.group(1)) * _UNIT_MS[unit]


def _human(msv: float) -> str:
    for unit, size in (("jours", DAY), ("heures", HOUR), ("minutes", MIN)):
        if msv >= size:
            return f"{msv / size:g} {unit}"
    return f"{msv / 1000:g} secondes"


def validate(values: dict) -> dict:
    """Erreurs bloquantes par clé (cross-seed refuserait de démarrer) et avertissements."""
    err, warn = {}, {}
    get = lambda k: (values.get(k) or "").strip()  # noqa: E731
    durations = {}
    for k in ("searchCadence", "rssCadence", "excludeRecentSearch", "excludeOlder"):
        if get(k):
            d = parse_duration(get(k))
            if d is None or d <= 0:
                err[k] = "Format invalide. Exemples : 30 minutes, 2 hours, 1 day, 2 weeks (unités : m, h, d, w, y ; pas de mois)"
            else:
                durations[k] = d
    if get("delay"):
        if not re.fullmatch(r"\d+", get("delay")) or not 30 <= int(get("delay")) <= 3600:
            err["delay"] = "Nombre entier de secondes entre 30 et 3600"
    if "rssCadence" in durations and not 10 * MIN <= durations["rssCadence"] <= 2 * HOUR:
        err["rssCadence"] = "Entre 10 minutes et 2 hours"
    sc = durations.get("searchCadence")
    if sc is not None and sc < DAY:
        err["searchCadence"] = "Au moins 1 day"
    ers, eo = durations.get("excludeRecentSearch"), durations.get("excludeOlder")
    if sc and "searchCadence" not in err:
        if ers is None:
            err.setdefault("excludeRecentSearch", "Obligatoire quand searchCadence est défini")
        elif ers < 3 * sc:
            err.setdefault("excludeRecentSearch", f"Au moins 3 × searchCadence, soit {_human(3 * sc)} minimum")
        if eo is None:
            err.setdefault("excludeOlder", "Obligatoire quand searchCadence est défini")
        elif ers and not 2 * ers <= eo <= 5 * ers:
            err.setdefault("excludeOlder", f"Entre 2 et 5 × excludeRecentSearch, soit de {_human(2 * ers)} à {_human(5 * ers)}")
    if get("searchLimit") and not re.fullmatch(r"\d+", get("searchLimit")):
        err["searchLimit"] = "Nombre entier positif ou nul"
    if get("action") and get("action") not in ("inject", "save"):
        err["action"] = "inject ou save"
    mm = get("matchMode")
    if mm and mm not in ("strict", "flexible", "partial", "safe", "risky"):
        err["matchMode"] = "strict, flexible ou partial (safe et risky sont les anciens noms de strict et flexible)"
    if get("linkType") and get("linkType") not in ("hardlink", "symlink", "reflink"):
        err["linkType"] = "hardlink, symlink ou reflink"
    for k in ("includeSingleEpisodes", "duplicateCategories"):
        if get(k) and get(k) not in ("true", "false"):
            err[k] = "true ou false"
    sfe = get("seasonFromEpisodes")
    if sfe:
        try:
            x = float(sfe)
            if not 0 < x <= 1:
                err["seasonFromEpisodes"] = "Entre 0 (exclu) et 1"
            elif (sc or durations.get("rssCadence")) and x < 0.5:
                err["seasonFromEpisodes"] = "Au moins 0.5 quand le scan complet ou le RSS est actif"
            elif x < 1 and mm not in ("partial",):
                err["seasonFromEpisodes"] = "Une valeur inférieure à 1 exige matchMode partial"
        except ValueError:
            err["seasonFromEpisodes"] = "Nombre entre 0 et 1, ou vide pour désactiver"
    if get("action") == "inject" and mm in ("flexible", "partial", "risky") and not _has_link_dirs():
        err["matchMode"] = "flexible et partial exigent linkDirs dans config.js avec action inject"
    if get("searchLimit") == "0":
        warn["searchLimit"] = "0 : aucune limite, le scan complet cherchera tous les torrents éligibles"
    return {"errors": err, "warnings": warn}


def _has_link_dirs() -> bool:
    text = _config_text()
    m = re.search(r"^\s*linkDirs\s*:\s*\[(.*?)\]", text, re.M | re.S)
    if m and re.search(r"^\s*[\"'`]", m.group(1), re.M):
        return True
    return bool(re.search(r"^\s*linkDir\s*:\s*[\"'`]", text, re.M))


def check_settings(changes: dict) -> dict:
    base = {k: v["value"] for k, v in useful_settings().items()}
    values = {**base, **{k: str(v) for k, v in changes.items()}}
    res = validate(values)
    # Une « erreur » déjà présente dans la config actuelle, alors que cross-seed tourne avec,
    # vient sans doute d'une détection imparfaite ici : on la rétrograde en avertissement.
    before = validate(base)["errors"]
    for k in list(res["errors"]):
        if k in before and before[k] == res["errors"][k] and k not in changes:
            res["warnings"][k] = res["errors"].pop(k) + " (déjà le cas dans ta config actuelle)"
    return res


def update_settings(changes: dict) -> dict:
    """Modifie uniquement la valeur des clés demandées, en conservant commentaires et mise en forme.
    Une copie de sauvegarde est faite dans DATA_DIR/backups avant écriture."""
    text = _config_text()
    if not text:
        raise ValueError("config.js illisible")
    current = useful_settings()
    problems = check_settings(changes)["errors"]
    if problems:
        raise ValueError("Valeurs refusées (cross-seed ne démarrerait pas) : " +
                         " ; ".join(f"{k} : {v}" for k, v in problems.items()))
    new_text = text
    for key, value in changes.items():
        if key not in USEFUL_KEYS or key not in current:
            raise ValueError(f"Clé non modifiable : {key}")
        lit = _literal(value, current[key]["kind"], key)
        new_text, n = _line_re(key).subn(lambda m, lit=lit: m.group("pre") + lit + m.group("post"), new_text, count=1)
        if n != 1:
            raise ValueError(f"{key} introuvable dans config.js")
    if new_text == text:
        return {"changed": False, "backup": None}
    return _write_config(text, new_text)
