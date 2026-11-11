from ringrouter.notify import ConsoleNotifier
from ringrouter.watchdog import diagnose, run_once


def make_checker(results):
    """results: dict url-substring -> bool"""
    def checker(url):
        ok = results["local"] if "127.0.0.1" in url else results["public"]
        return ok, "HTTP 200" if ok else "connection refused"
    return checker


def test_diagnose():
    assert diagnose(True, True) == "up"
    assert diagnose(False, False) == "listener down"
    assert diagnose(True, False) == "tunnel down"


def test_alert_after_two_failures_then_recovery(cfg, tmp_path):
    cfg.raw["server"]["public_url"] = "https://example.ts.net"
    n, state = ConsoleNotifier(echo=False), tmp_path / "w.json"
    down = make_checker({"local": False, "public": False})
    run_once(cfg, n, down, state)
    assert n.sent == []                                   # one failure: no page
    run_once(cfg, n, down, state)
    assert n.sent[-1].title == "Ring router: listener down"
    run_once(cfg, n, down, state)
    assert len(n.sent) == 1                               # no repeat while still down
    run_once(cfg, n, make_checker({"local": True, "public": True}), state)
    assert n.sent[-1].title == "Ring router is back up"


def test_tunnel_down_reported_separately(cfg, tmp_path):
    cfg.raw["server"]["public_url"] = "https://example.ts.net"
    n, state = ConsoleNotifier(echo=False), tmp_path / "w.json"
    tunnel = make_checker({"local": True, "public": False})
    run_once(cfg, n, tunnel, state); run_once(cfg, n, tunnel, state)
    assert n.sent[-1].title == "Ring router: tunnel down" and "tailscale" in n.sent[-1].text.lower()
