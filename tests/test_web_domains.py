"""Local domain mapping tests: pure functions plus an isolated hosts file."""
from __future__ import annotations

import pytest

from syvrio.app.web.hosts import (
    DEFAULT_DOMAIN,
    LOOPBACK,
    ensure_hosts_mapping,
    mapping_line,
    normalize_domain,
    remove_hosts_mapping,
)

pytestmark = pytest.mark.web


def test_normalize_domain_accepts_plain_hostnames():
    assert normalize_domain("Syvrio.Local") == "syvrio.local"
    assert normalize_domain(" syvrio.home.arpa. ") == "syvrio.home.arpa"
    assert DEFAULT_DOMAIN == "syvrio.local"


@pytest.mark.parametrize("value", [
    "", "   ", "http://syvrio.local", "syvrio.local:8765", "syvrio.local/path",
    "*.local", "127.0.0.1", "localhost", "-bad.local", "a" * 300,
])
def test_normalize_domain_rejects_ambiguous_or_unsafe_values(value):
    with pytest.raises(ValueError):
        normalize_domain(value)


def test_mapping_line_is_a_plain_hosts_entry():
    assert mapping_line("syvrio.local") == f"{LOOPBACK}\tsyvrio.local"


def test_ensure_mapping_is_idempotent_and_backed_up(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_bytes(b"127.0.0.1\tlocalhost\n# existing comment\n")

    first = ensure_hosts_mapping("syvrio.local", path=hosts)
    assert first.changed is True
    assert first.backup is not None
    assert first.backup.read_bytes() == b"127.0.0.1\tlocalhost\n# existing comment\n"
    assert "127.0.0.1\tsyvrio.local" in hosts.read_text(encoding="utf-8")
    assert "# existing comment" in hosts.read_text(encoding="utf-8")

    second = ensure_hosts_mapping("syvrio.local", path=hosts)
    assert second.changed is False
    assert hosts.read_text(encoding="utf-8").count("syvrio.local") == 1


def test_conflicting_mapping_is_never_overwritten(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text("192.168.1.9\tsyvrio.local\n", encoding="utf-8")
    result = ensure_hosts_mapping("syvrio.local", path=hosts)
    assert result.changed is False
    assert "resolve" in result.message
    assert hosts.read_text(encoding="utf-8") == "192.168.1.9\tsyvrio.local\n"


def test_mapping_can_be_removed_without_touching_other_entries(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_bytes(b"127.0.0.1\tlocalhost\r\n127.0.0.1\tsyvrio.local\r\n")
    result = remove_hosts_mapping("syvrio.local", path=hosts)
    assert result.changed is True
    assert hosts.read_bytes() == b"127.0.0.1\tlocalhost\r\n"
    assert remove_hosts_mapping("syvrio.local", path=hosts).changed is False


def test_web_map_only_prints_remote_url_without_starting_a_server():
    from typer.testing import CliRunner

    from syvrio.app.cli import app

    result = CliRunner().invoke(app, [
        "web", "--domain", "syvrio.local", "--domain-ip", "100.64.0.9",
        "--map-only", "--no-hosts", "--port", "8765",
    ])
    assert result.exit_code == 0, result.output
    assert "100.64.0.9\tsyvrio.local" in result.output
    assert "http://syvrio.local:8765/" in result.output


def test_web_rejects_an_invalid_mapping_ip():
    from typer.testing import CliRunner

    from syvrio.app.cli import app

    result = CliRunner().invoke(app, [
        "web", "--domain", "syvrio.local", "--domain-ip", "bogus",
        "--map-only", "--no-hosts",
    ])
    assert result.exit_code != 0


def test_unwritable_hosts_degrades_to_a_manual_instruction(tmp_path, monkeypatch):
    import syvrio.app.web.hosts as module

    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1\tlocalhost\n", encoding="utf-8")

    def denied(*args, **kwargs):
        raise PermissionError("elevated permission required")

    monkeypatch.setattr(module.Path, "read_bytes",
                        lambda self: b"127.0.0.1\tlocalhost\n")
    monkeypatch.setattr(module.Path, "write_bytes", lambda self, data: len(data))
    monkeypatch.setattr(module.Path, "open", denied)
    result = module.ensure_hosts_mapping("syvrio.local", path=hosts)
    assert result.changed is False
    assert "Add manually" in result.message
    assert "127.0.0.1\tsyvrio.local" in result.message
