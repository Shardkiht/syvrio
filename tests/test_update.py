from __future__ import annotations

import hashlib

import httpx
import pytest

from syvrio.infra import update

pytestmark = pytest.mark.cli


def wheel(version: str) -> dict:
    return {"filename": f"syvrio-{version}-py3-none-any.whl"}


def test_update_channels_ignore_yanked_and_source_releases():
    releases = {v: [wheel(v)] for v in ("0.5.2rc1", "0.5.2rc10", "0.5.2", "0.5.3")}
    releases["0.5.3"][0]["yanked"] = True
    releases["0.6.0"] = [{"filename": "syvrio-0.6.0.tar.gz"}]
    assert update.select_release(releases, "0.5.2rc1", "stable")[0] == "0.5.2"
    assert update.select_release(releases, "0.5.2rc1", "evo")[0] == "0.5.2rc10"
    assert update.select_release(releases, "0.5.2", "evo") is None


def test_download_rejects_corrupt_wheel(tmp_path):
    asset = {
        "url": "https://files.pythonhosted.org/packages/test.whl",
        "digests": {"sha256": hashlib.sha256(b"expected").hexdigest()},
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"corrupt"))
    with httpx.Client(transport=transport) as client, pytest.raises(ValueError, match="checksum"):
        update._download(client, asset, tmp_path / "test.whl")


@pytest.mark.parametrize("healthy", [True, False])
def test_update_switches_or_restores_service(tmp_path, monkeypatch, healthy):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.touch()
    new.touch()
    link = tmp_path / "current"
    link.symlink_to(old)
    events = []
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"releases": {"0.5.2rc1": [wheel("0.5.2rc1")]}})
    )
    factory = httpx.Client
    monkeypatch.setattr(update.httpx, "Client", lambda **kwargs: factory(transport=transport))
    monkeypatch.setattr(update, "_install", lambda *args: new)
    monkeypatch.setattr(update, "_healthy", lambda c, u, version: version == "0.3.4" or healthy)

    def run(*args, **kwargs):
        if args == (str(old), "--version"):
            return "Syvrio 0.3.4"
        events.append((args[2], link.resolve()))
        return ""

    monkeypatch.setattr(update, "_run", run)
    if healthy:
        assert "0.5.2rc1" in update.update(tmp_path, "evo", "syvrio-web.service", 8765)
        assert link.resolve() == new
        assert events == [("stop", old), ("start", new)]
    else:
        with pytest.raises(ValueError, match="restored"):
            update.update(tmp_path, "evo", "syvrio-web.service", 8765)
        assert link.resolve() == old
        assert events == [("stop", old), ("start", new), ("stop", new), ("start", old)]
