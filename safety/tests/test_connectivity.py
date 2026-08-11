import urllib.error

from safety import config
from safety import connectivity as cn


def test_reachable_true_on_response(monkeypatch):
    class _R:
        def close(self):
            pass
    monkeypatch.setattr(cn.urllib.request, "urlopen", lambda *a, **k: _R())
    assert cn._reachable("http://x", 5) is True


def test_reachable_http_error_still_reachable(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.HTTPError("http://x", 404, "nf", {}, None)
    monkeypatch.setattr(cn.urllib.request, "urlopen", boom)
    assert cn._reachable("http://x", 5) is True     # got an HTTP response => reachable


def test_reachable_false_on_conn_error(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("no route")
    monkeypatch.setattr(cn.urllib.request, "urlopen", boom)
    assert cn._reachable("http://x", 5) is False


def test_online_probe_resets_clock(monkeypatch):
    w = cn.ConnectivityWatch(config, start_ts=0.0)
    monkeypatch.setattr(cn, "_reachable", lambda url, t: True)
    assert w.probe_once(now=5000.0) is True
    c = w.component(now=5001.0)
    assert c["online"] is True and c["safe"] is True and c["offline_sec"] <= 1


def test_offline_within_grace_is_safe(monkeypatch):
    w = cn.ConnectivityWatch(config, start_ts=1000.0)
    monkeypatch.setattr(cn, "_reachable", lambda url, t: False)
    w.probe_once(now=1000.0)
    # 30 min offline < 1 h threshold -> still safe
    c = w.component(now=1000.0 + 1800)
    assert c["online"] is False and c["safe"] is True


def test_offline_too_long_is_unsafe(monkeypatch):
    w = cn.ConnectivityWatch(config, start_ts=1000.0)
    monkeypatch.setattr(cn, "_reachable", lambda url, t: False)
    w.probe_once(now=1000.0)
    c = w.component(now=1000.0 + config.CONN_OFFLINE_UNSAFE_SEC + 60)
    assert c["safe"] is False and c["offline_min"] >= 61


def test_recovery_auto_resolves(monkeypatch):
    w = cn.ConnectivityWatch(config, start_ts=1000.0)
    monkeypatch.setattr(cn, "_reachable", lambda url, t: False)
    w.probe_once(now=1000.0)
    assert w.component(now=1000.0 + 7200)["safe"] is False   # 2 h offline -> unsafe
    monkeypatch.setattr(cn, "_reachable", lambda url, t: True)
    w.probe_once(now=1000.0 + 7300)                          # internet back
    assert w.component(now=1000.0 + 7301)["safe"] is True    # resolved
