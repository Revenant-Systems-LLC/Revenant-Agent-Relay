import pytest

from src.adapter_loader import load_adapter
from src.platforms.tiktok import TikTokAdapter


def _set_credentials(monkeypatch):
    monkeypatch.setenv("RR_TIKTOK_USERNAME", "user")
    monkeypatch.setenv("RR_TIKTOK_PASSWORD", "pw")


def test_disabled_automation_leaves_tiktok_unavailable(monkeypatch):
    # Credentials alone must not load an adapter whose every post fails; the
    # loader needs an init error so dev runs fall back to the simulator.
    _set_credentials(monkeypatch)
    monkeypatch.delenv("RR_TIKTOK_ENABLE_AUTOMATION", raising=False)

    with pytest.raises(RuntimeError, match="RR_TIKTOK_ENABLE_AUTOMATION"):
        TikTokAdapter({}, {})
    assert load_adapter("tiktok", {"mode": "dev"}) is None


def test_enabled_automation_loads_tiktok_adapter(monkeypatch):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("RR_TIKTOK_ENABLE_AUTOMATION", "true")

    assert isinstance(load_adapter("tiktok", {"mode": "dev"}), TikTokAdapter)
