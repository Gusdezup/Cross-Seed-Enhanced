"""Adaptateurs rTorrent, Transmission et Deluge, sur des réponses simulées (pas d'instance réelle)."""
import asyncio
import json
import xmlrpc.client

import httpx
import pytest

from app import config, torrentclients as tc

H = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"


def spec(entry: str) -> dict:
    return config.parse_client(entry)


# --- rTorrent ----------------------------------------------------------------------

def rtorrent_answer(method: str, params: tuple):
    if method == "d.multicall2":
        assert params[:2] == ("", "main")
        return [
            # hash, nom, taille, octets reçus, étiquette, dossier, multi, démarré, actif, complet, vérif, ajout
            [H, "Album", 200, 200, "musique%20flac", "/data/Album", 1, 1, 1, 1, 0, "1700000000"],
            ["11" * 20, "Film.mkv", 100, 25, "", "/data", 0, 0, 0, 0, 0, ""],
        ]
    if method == "system.multicall":
        return [[[["dht://"], ["https://tracker.example/announce"]]], [[]]]
    if method == "system.client_version":
        return "0.9.8"
    raise AssertionError(method)


def rtorrent_http(auth: str | None = None):
    def handler(req):
        if auth == "digest" and "digest" not in req.headers.get("authorization", "").lower():
            return httpx.Response(401, headers={"WWW-Authenticate": 'Digest realm="rt", nonce="n", qop="auth"'})
        params, method = xmlrpc.client.loads(req.content)
        return httpx.Response(200, content=xmlrpc.client.dumps((rtorrent_answer(method, params),),
                                                               methodresponse=True).encode())
    return handler


def check_rtorrent(out):
    a, f = out
    assert (a["hash"], a["category"], a["progress"], a["state"]) == (H.lower(), "musique flac", 1.0, "uploading")
    assert (a["content_path"], a["tracker"], a["added_on"]) == ("/data/Album", "https://tracker.example/announce", 1700000000)
    assert (f["progress"], f["state"], f["content_path"], f["tracker"]) == (0.25, "stoppedDL", "/data/Film.mkv", "")


def test_rtorrent_xmlrpc_http():
    rt = tc.RTorrent(spec("rtorrent:http://u:p@h:8080/RPC2"), transport=httpx.MockTransport(rtorrent_http()))
    check_rtorrent(asyncio.run(rt.torrents()))
    assert asyncio.run(rt.version()) == "0.9.8"


def test_rtorrent_switches_to_digest():
    rt = tc.RTorrent(spec("rtorrent:http://u:p@h/rutorrent/RPC2"), transport=httpx.MockTransport(rtorrent_http("digest")))
    assert asyncio.run(rt.version()) == "0.9.8"
    assert isinstance(rt.auth, httpx.DigestAuth)


def test_rtorrent_scgi(tmp_path):
    async def run():
        async def serve(reader, writer):
            n = int((await reader.readuntil(b":"))[:-1])
            head = await reader.readexactly(n + 1)
            length = int(head.split(b"\x00")[1])
            params, method = xmlrpc.client.loads(await reader.readexactly(length))
            body = xmlrpc.client.dumps((rtorrent_answer(method, params),), methodresponse=True).encode()
            writer.write(b"Status: 200 OK\r\nContent-Type: text/xml\r\n\r\n" + body)
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            rt = tc.RTorrent(spec(f"rtorrent:scgi://127.0.0.1:{port}"))
            return await rt.torrents(), await rt.version()
    out, version = asyncio.run(run())
    check_rtorrent(out)
    assert version == "0.9.8"


def test_rtorrent_fault_is_readable():
    def handler(req):
        return httpx.Response(200, content=xmlrpc.client.dumps(xmlrpc.client.Fault(-506, "Method 'x' not defined")).encode())
    rt = tc.RTorrent(spec("rtorrent:http://h/RPC2"), transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="not defined"):
        asyncio.run(rt.version())


# --- Transmission ------------------------------------------------------------------

def test_transmission_session_id_and_mapping():
    calls = []

    def handler(req):
        calls.append(req.headers.get("x-transmission-session-id"))
        assert req.headers["authorization"].startswith("Basic ")
        if req.headers.get("x-transmission-session-id") != "jeton":
            return httpx.Response(409, headers={"X-Transmission-Session-Id": "jeton"})
        body = json.loads(req.content)
        if body["method"] == "session-get":
            return httpx.Response(200, json={"result": "success", "arguments": {"version": "4.0.6"}})
        return httpx.Response(200, json={"result": "success", "arguments": {"torrents": [
            {"hashString": H, "name": "Film", "totalSize": 300, "sizeWhenDone": 200, "percentDone": 1,
             "labels": ["radarr", "cross-seed"], "trackers": [{"announce": "https://t.example/a"}],
             "downloadDir": "/data/", "status": 0, "addedDate": 1700000000},
            {"hashString": "22" * 20, "name": "Série", "totalSize": 10, "sizeWhenDone": 10, "percentDone": 0.5,
             "labels": [], "trackers": [], "downloadDir": "/data", "status": 4, "addedDate": 1},
        ]}})
    tr = tc.Transmission(spec("transmission:http://u:p@h:9091/transmission/rpc"), transport=httpx.MockTransport(handler))
    a, b = asyncio.run(tr.torrents())
    assert calls == ["", "jeton"]
    assert (a["hash"], a["size"], a["total_size"], a["category"], a["tags"]) == (H.lower(), 200, 300, "radarr", "radarr,cross-seed")
    assert (a["state"], a["content_path"], a["tracker"]) == ("stoppedUP", "/data/Film", "https://t.example/a")
    assert (b["category"], b["state"], b["progress"]) == ("", "downloading", 0.5)
    assert asyncio.run(tr.version()) == "4.0.6"


def test_transmission_unauthorized():
    tr = tc.Transmission(spec("transmission:http://u:faux@h:9091/transmission/rpc"),
                         transport=httpx.MockTransport(lambda req: httpx.Response(401)))
    with pytest.raises(RuntimeError, match="authentification"):
        asyncio.run(tr.torrents())


# --- Deluge ------------------------------------------------------------------------

def deluge_handler(state):
    def handler(req):
        body = json.loads(req.content)
        m, p = body["method"], body["params"]
        state["log"].append(m)
        ok = lambda r: httpx.Response(200, json={"result": r, "error": None, "id": body["id"]})  # noqa: E731
        if m == "auth.login":
            return ok(p == ["pw"])
        if m == "web.connected":
            return ok(state["connected"])
        if m == "web.get_hosts":
            return ok([["hostid", "127.0.0.1", 58846, "localclient"]])
        if m == "web.connect":
            assert p == ["hostid"]
            state["connected"] = True
            return ok(None)
        if not state["connected"]:
            return httpx.Response(200, json={"result": None, "id": body["id"],
                                             "error": {"message": "Not authenticated", "code": 1}})
        if m == "daemon.info":
            return ok("2.1.1")
        if m == "core.get_torrents_status":
            return ok({H: {"name": "Album", "total_size": 200, "total_wanted": 200, "progress": 100.0,
                           "label": "musique", "trackers": [{"url": "https://t.example/a"}],
                           "save_path": "/data", "state": "Paused", "time_added": 1700000000.5}})
        raise AssertionError(m)
    return handler


def test_deluge_login_connect_and_reconnect():
    state = {"connected": False, "log": []}
    d = tc.Deluge(spec("deluge:http://:pw@h:8112/json"), transport=httpx.MockTransport(deluge_handler(state)))
    [t] = asyncio.run(d.torrents())
    assert state["log"][:4] == ["auth.login", "web.connected", "web.get_hosts", "web.connect"]
    assert (t["hash"], t["category"], t["progress"], t["state"]) == (H.lower(), "musique", 1.0, "stoppedUP")
    assert (t["content_path"], t["tracker"], t["added_on"]) == ("/data/Album", "https://t.example/a", 1700000000)
    state["connected"] = False   # démon déconnecté entre deux lectures
    assert asyncio.run(d.version()) == "2.1.1"


def test_deluge_wrong_password():
    state = {"connected": False, "log": []}
    d = tc.Deluge(spec("deluge:http://:faux@h:8112/json"), transport=httpx.MockTransport(deluge_handler(state)))
    with pytest.raises(RuntimeError, match="mot de passe"):
        asyncio.run(d.torrents())


def test_every_cross_seed_type_has_an_adapter():
    assert set(tc.ADAPTERS) == set(config.CLIENT_TYPES)
