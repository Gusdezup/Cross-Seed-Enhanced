"""Clients torrent : configuration, fusion des listes, client injoignable, qBittorrent (clé API ou login)."""
import asyncio
import json

import httpx
import pytest

from app import config, demandes, releases, torrentclients as tc, xsdb


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Réglages, config de cross-seed et caches propres à chaque test."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "XS_CONFIG_JS", tmp_path / "config.js")
    monkeypatch.setattr(config, "XS_VERSION", "6")
    monkeypatch.setattr(config, "ENV_CLIENTS", [])
    monkeypatch.setattr(config, "CLIENT_HOST", "")
    monkeypatch.setattr(config, "QBT_URL", "http://qbittorrent:8080")
    monkeypatch.setattr(config, "QBT_APIKEY", "")
    monkeypatch.setattr(config, "QBT_URL_SET", False)
    for d in (tc._state, tc._instances, tc._versions, tc._locks):
        d.clear()
    tc.reset()
    yield
    tc.reset()


def write_config(tmp_path, body: str):
    (tmp_path / "config.js").write_text("module.exports = {\n" + body + "\n};\n")


def tor(h, name="Film.2024.1080p", size=100, progress=1.0, category="radarr", **kw):
    return {"hash": h, "name": name, "size": size, "total_size": size, "progress": progress,
            "category": category, "tags": "", "tracker": "https://t.example/announce",
            "content_path": f"/data/{name}", "state": "uploading", "added_on": 1, **kw}


class Fake(tc.Client):
    """Adaptateur de test : liste fixe ou erreur."""
    def __init__(self, spec, data=None, error=None):
        super().__init__(spec)
        self.data, self.error, self.calls = data or [], error, 0

    async def torrents(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.data

    async def version(self):
        if self.error:
            raise self.error
        return "5.3.0"


def install(clients: dict):
    """{nom de client: Fake} ; configuration .env équivalente."""
    specs = []
    for name, fake in clients.items():
        fake.spec["name"] = name
        specs.append(fake.spec)
    config.ENV_CLIENTS[:] = specs
    tc.reset()
    for c in tc.configured()["clients"]:
        fake = clients[c["name"]]
        tc._instances[c["id"]] = (tc._signature(c), fake)


# --- configuration -------------------------------------------------------------

def test_parse_client_variants():
    c = config.parse_client("qbittorrent:readonly:http://admin:p%40ss@192.168.1.20:8082/")
    assert (c["type"], c["host"], c["port"], c["user"], c["password"]) == \
        ("qbittorrent", "192.168.1.20", 8082, "admin", "p@ss")
    assert config.client_url(c) == "http://192.168.1.20:8082"
    assert "pass" not in config.client_id(c)
    assert config.client_url(config.parse_client("rtorrent:scgi:///run/rt.sock")) == "scgi:///run/rt.sock"
    with pytest.raises(ValueError, match="•••"):
        config.parse_client("utorrent:http://admin:secret@h:1")
    with pytest.raises(ValueError):
        config.parse_client("transmission:scgi://h:5000")


def test_config_js_torrent_clients(tmp_path):
    write_config(tmp_path, """
  // torrentClients: ["qbittorrent:http://old:1"],
  torrentClients: [
    "qbittorrent:http://admin:pw@192.168.1.20:8081", // principal
    // "deluge:http://:pw@h:8112/json",
    'qbittorrent:readonly:http://admin:pw@192.168.1.20:8082',
  ],
  torznab: [],""")
    assert xsdb.torrent_client_entries() == ["qbittorrent:http://admin:pw@192.168.1.20:8081",
                                            "qbittorrent:readonly:http://admin:pw@192.168.1.20:8082"]
    write_config(tmp_path, 'torrentClients: ["rtorrent:http://h/RPC2"], torznab: []')
    assert xsdb.torrent_client_entries() == ["rtorrent:http://h/RPC2"]


def test_config_js_template_examples_are_ignored(tmp_path):
    """Modèle de config.js de cross-seed : exemples dans un commentaire /* */, vraie ligne après."""
    write_config(tmp_path, """    /**
     * torrentClients: ["qbittorrent:http://username:password@localhost:8080"]
     *
     * torrentClients: ["deluge:http://:password@localhost:8112/json"]
     * e.g. "qbittorrent:readonly:http://username:password@localhost:8080"
     */
    torrentClients: ["qbittorrent:http://gus:pw@192.168.1.20:8081"],
    /** url: "http://exemple/*pas un commentaire*/" */
    torznab: [],""")
    assert xsdb.torrent_client_entries() == ["qbittorrent:http://gus:pw@192.168.1.20:8081"]


def test_legacy_qbt_url_alone(tmp_path):
    """Rétro-compatibilité : sans TORRENT_CLIENT_N ni torrentClients, QBT_URL et QBT_APIKEY."""
    config.QBT_URL, config.QBT_APIKEY, config.QBT_URL_SET = "http://192.168.1.10:8080", "k", True
    conf = tc.configured()
    assert conf["origin"] == ".env (QBT_URL)"
    [c] = conf["clients"]
    assert (config.client_url(c), c["apikey"], c["name"]) == ("http://192.168.1.10:8080", "k", "qBittorrent")


def test_cross_seed_clients_reuse_qbt_url_and_rewrite_localhost(tmp_path):
    write_config(tmp_path, """torrentClients: [
    "qbittorrent:http://admin:pw@localhost:8081",
    "qbittorrent:http://admin:pw@localhost:8082",
  ],""")
    config.QBT_URL, config.QBT_APIKEY, config.QBT_URL_SET = "http://192.168.1.20:8081", "cle", True
    conf = tc.configured()
    assert conf["origin"].startswith("config de cross-seed")
    a, b = conf["clients"]
    assert (config.client_url(a), a["apikey"]) == ("http://192.168.1.20:8081", "cle")
    assert (config.client_url(b), b["apikey"], b["user"]) == ("http://192.168.1.20:8082", "", "admin")
    assert (a["name"], b["name"]) == ("qBittorrent :8081", "qBittorrent :8082")


def test_env_clients_take_priority_and_names(tmp_path):
    write_config(tmp_path, 'torrentClients: ["qbittorrent:http://ignored:1"],')
    c = config.parse_client("qbittorrent:http://192.168.1.20:8082")
    c["name"] = "musique"
    config.ENV_CLIENTS[:] = [config.parse_client("qbittorrent:http://192.168.1.20:8081"), c]
    conf = tc.configured()
    assert conf["origin"] == ".env (TORRENT_CLIENT_N)"
    assert [x["name"] for x in conf["clients"]] == ["qBittorrent :8081", "musique"]
    config.save_settings({"client_names": {conf["clients"][0]["id"]: "principal", conf["clients"][1]["id"]: "x"}})
    tc.reset()
    assert [x["name"] for x in tc.configured()["clients"]] == ["principal", "musique"]   # .env prioritaire


# --- fusion et erreurs ---------------------------------------------------------

def test_merge_deduplicates_and_keeps_clients():
    merged = tc.merge([("principal", [tor("AA", progress=0.5), tor("bb")]),
                       ("musique", [tor("aa", progress=1.0), tor("cc")])])
    by = {t["hash"]: t for t in merged}
    assert [t["hash"] for t in merged] == ["aa", "bb", "cc"]
    assert by["aa"]["clients"] == ["principal", "musique"]
    assert (by["aa"]["client"], by["aa"]["progress"]) == ("musique", 1.0)   # le plus avancé
    assert by["bb"]["clients"] == ["principal"]


def test_snapshot_partial_when_one_client_fails():
    ok = Fake(config.parse_client("qbittorrent:http://h:8081"), [tor("aa")])
    ko = Fake(config.parse_client("qbittorrent:http://h:8082"), error=httpx.ConnectError("refused"))
    install({"principal": ok, "musique": ko})
    snap = asyncio.run(tc.snapshot())
    assert [t["hash"] for t in snap["torrents"]] == ["aa"]
    st = {c["name"]: c for c in snap["clients"]}
    assert st["principal"]["ok"] and st["principal"]["torrents"] == 1
    assert (st["musique"]["ok"], st["musique"]["error"]) == (False, "connexion impossible")
    status = asyncio.run(tc.status())["clients"]
    assert [(c["name"], c["ok"]) for c in status] == [("principal", True), ("musique", False)]
    # cache de 20 s par client ; force=True relit
    asyncio.run(tc.snapshot())
    assert ok.calls == 1
    asyncio.run(tc.snapshot(force=True))
    assert ok.calls == 2


def test_snapshot_unavailable_when_all_fail():
    ko = Fake(config.parse_client("qbittorrent:http://h:8081"), error=httpx.ConnectError("refused"))
    install({"principal": ko})
    with pytest.raises(tc.Unavailable, match="principal"):
        asyncio.run(tc.snapshot())


def test_releases_and_groups_across_clients():
    """Une même catégorie dans deux clients donne le même groupe ; une release porte ses clients."""
    torrents = tc.merge([("principal", [tor("aa", category="cross-seed", tags="cross-seed")]),
                         ("musique", [tor("bb", category="cross-seed", tags="cross-seed"),
                                      tor("cc", name="Album", category="musique")])])
    groups = config.clean_groups([{"name": "Musique", "categories": ["musique", "cross-seed"], "indexers": []}])
    items = releases.build(torrents, {"rules": [], "tracker_aliases": {}})
    film = next(r for r in items if r["name"].startswith("Film"))
    assert film["count"] == 2 and film["clients"] == ["principal", "musique"]
    assert config.group_for(film["category"], groups)["name"] == "Musique"
    crit = demandes.normalize({"client": "MUSIQUE"})
    assert all(demandes._passes(r, crit, []) for r in items)
    crit = demandes.normalize({"client": "principal"})
    assert [r["name"] for r in items if demandes._passes(r, crit, [])] == [film["name"]]


# --- qBittorrent ----------------------------------------------------------------

def test_qbittorrent_api_key():
    seen = []

    def handler(req):
        seen.append(req.headers.get("authorization"))
        return httpx.Response(200, json=[tor("aa")])
    spec = config.parse_client("qbittorrent:http://h:8081")
    spec["apikey"] = "secret"
    q = tc.QBittorrent(spec, transport=httpx.MockTransport(handler))
    assert asyncio.run(q.torrents())[0]["hash"] == "aa"
    assert seen == ["Bearer secret"]


def test_qbittorrent_login_and_relogin():
    """Version sans clé API : identifiants de l'URL, cookie de session, reconnexion sur 403."""
    log, state = [], {"sid": None}

    def handler(req):
        log.append(req.url.path)
        if req.url.path == "/api/v2/auth/login":
            body = dict(x.split("=") for x in req.content.decode().split("&"))
            if body != {"username": "admin", "password": "pw"}:
                return httpx.Response(200, text="Fails.")
            state["sid"] = "abc"
            return httpx.Response(200, text="Ok.", headers={"set-cookie": "SID=abc; path=/"})
        if req.headers.get("cookie") != f"SID={state['sid']}" or not state["sid"]:
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, text="v4.6.7") if req.url.path.endswith("version") \
            else httpx.Response(200, json=[tor("aa")])
    q = tc.QBittorrent(config.parse_client("qbittorrent:http://admin:pw@h:8082"),
                       transport=httpx.MockTransport(handler))
    assert asyncio.run(q.version()) == "v4.6.7"
    assert log == ["/api/v2/app/version", "/api/v2/auth/login", "/api/v2/app/version"]
    state["sid"] = "expired"   # session expirée côté qBittorrent
    assert len(asyncio.run(q.torrents())) == 1
    bad = tc.QBittorrent(config.parse_client("qbittorrent:http://admin:faux@h:8082"),
                         transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="identifiants refusés"):
        asyncio.run(bad.torrents())


def test_status_never_exposes_credentials(tmp_path):
    write_config(tmp_path, 'torrentClients: ["qbittorrent:http://admin:topsecret@h:8081"],')
    out = asyncio.run(tc.status())
    assert "topsecret" not in json.dumps(out) and "admin" not in json.dumps(out)
