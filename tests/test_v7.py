"""Régressions du mode cross-seed v7, sans accès à une instance réelle."""
import asyncio
import sqlite3

import pytest
from fastapi import HTTPException

from app import config, logs, main, xsdb


def test_v7_database_indexers_and_settings(tmp_path, monkeypatch):
    db = tmp_path / "cross-seed.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE indexer (id INTEGER, url TEXT, apikey TEXT, enabled INTEGER,
                              name TEXT, status TEXT, retry_after INTEGER);
        CREATE TABLE settings (settings_json TEXT);
        INSERT INTO indexer VALUES (1, 'http://prowlarr:9696/12/api', 'secret', 1, 'Tracker', 'OK', NULL);
        INSERT INTO settings VALUES ('{"linkType":"hardlink","searchLimit":8,"apiKey":"hidden"}');
    """)
    con.close()
    monkeypatch.setattr(config, "XS_VERSION", "7")
    monkeypatch.setattr(config, "XS_DB", db)
    monkeypatch.setattr(config, "XS_CONFIG_JS", tmp_path / "absent-config.js")
    result = xsdb.indexers()
    assert result["items"][0]["config"] == "active"
    assert result["items"][0]["key"] == logs.normalize_url("http://prowlarr:9696/12/api")
    assert "secret" not in str(result)
    assert xsdb.prowlarr_from_config() == ("http://prowlarr:9696", "secret")
    assert xsdb.useful_settings()["searchLimit"]["value"] == "8"
    assert "apiKey" not in xsdb.useful_settings()
    with pytest.raises(ValueError, match="interface native"):
        xsdb.update_settings({"searchLimit": "10"})


def test_v7_indexer_api_mapping_and_last_enabled(monkeypatch):
    monkeypatch.setattr(config, "XS_VERSION", "7")
    calls = []
    items = [dict(id=1, url="http://prowlarr:9696/12/api", enabled=True)]

    async def list_items():
        return items

    async def mutate(method, indexer_id=None, body=None):
        calls.append((method, indexer_id, body))
        return {"ok": True}

    monkeypatch.setattr(main.clients, "xs_indexers_v7", list_items)
    monkeypatch.setattr(main.clients, "xs_indexer_v7", mutate)
    url = "http://prowlarr:9696/12/api?apikey=secret"
    assert asyncio.run(main._v7_add(url)) == {"changed": False}
    assert not calls
    with pytest.raises(HTTPException, match="dernier indexer"):
        asyncio.run(main.indexer_toggle({"key": logs.normalize_url(url), "enable": False}))
    items.append(dict(id=2, url="http://prowlarr:9696/13/api", enabled=False))
    asyncio.run(main.indexer_toggle({"key": logs.normalize_url("http://prowlarr:9696/13/api"), "enable": True}))
    assert calls == [("PUT", 2, {"id": 2, "enabled": True})]
    assert main._v7_indexer(url) == {"url": "http://prowlarr:9696/12/api", "apikey": "secret", "enabled": True}
