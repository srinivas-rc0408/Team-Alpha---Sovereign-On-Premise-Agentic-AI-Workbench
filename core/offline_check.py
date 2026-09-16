"""Offline verification: prove, rather than assert, that nothing talks outward.

Four independent checks, because each catches a failure the others can't see:

1. endpoints    — every configured service URL resolves to loopback (catches a
                  .env pointed at a remote Ollama, which would be a real leak).
2. telemetry    — the phone-home switches core/__init__.py forces off are still
                  off at the moment of the check (catches a later re-enable).
3. live         — external sockets open right now in this process tree, via
                  core/network_monitor.py (catches anything actually dialling out).
4. history      — every network_audit entry ever written to the audit log was
                  clean (catches a leak that happened earlier and has since closed,
                  which a live snapshot would miss entirely).

Only 3 is a point-in-time observation; 4 is what makes "no external URLs are
ever called" a claim about the whole recorded history rather than this instant.
"""
import os
from ipaddress import ip_address
from urllib.parse import urlparse

from . import audit, network_monitor

ENDPOINT_VARS = ("OLLAMA_HOST",)


def _classify(host: str) -> tuple[bool, str]:
    """(stays_on_this_machine, warning). Distinguishes three cases that a plain
    loopback test would collapse: true loopback, the unspecified address
    (0.0.0.0/:: — as a *destination* it resolves to this host, so nothing leaves,
    but it's ambiguous and not portable off Linux), and everything else."""
    h = (host or "").strip().lower().strip("[]")
    if h == "localhost":
        return True, ""
    try:
        addr = ip_address(h)
    except ValueError:
        return False, f"'{host}' is a DNS name, not a local address — traffic may leave this machine"
    if addr.is_loopback:
        return True, ""
    if addr.is_unspecified:
        return True, f"'{host}' resolves to this host, but is non-portable — prefer 127.0.0.1"
    return False, f"'{host}' is a non-loopback address — traffic can leave this machine"


def _is_loopback(host: str) -> bool:
    return _classify(host)[0]


def _hostname(url: str) -> str:
    # Accepts "http://localhost:11434" and bare "localhost:11434" alike.
    return urlparse(url if "//" in url else f"http://{url}").hostname or ""


def check_endpoints() -> dict:
    """Every configured service endpoint must point at this machine."""
    endpoints, warnings = {}, []
    for var in ENDPOINT_VARS:
        value = os.getenv(var, "http://localhost:11434")
        local, warning = _classify(_hostname(value))
        endpoints[var] = {"value": value, "loopback": local, "warning": warning}
        if warning:
            warnings.append(f"{var}: {warning}")
    return {
        "ok": all(e["loopback"] for e in endpoints.values()),
        "endpoints": endpoints,
        "warnings": warnings,
    }


def check_telemetry() -> dict:
    """The switches core/__init__.py forces off must still be off."""
    from . import TELEMETRY_OFF

    actual = {var: os.getenv(var) for var in TELEMETRY_OFF}
    mismatched = [v for v, expected in TELEMETRY_OFF.items() if actual[v] != expected]
    return {"ok": not mismatched, "disabled": actual, "mismatched": mismatched}


def check_live_connections() -> dict:
    """External sockets open in this process tree right now."""
    report = network_monitor.report(network_monitor.snapshot_io())
    return {"ok": report["clean"], "external_connections": report["external_connections"]}


def check_audit_history(path: str = None) -> dict:
    """Every network_audit entry ever recorded was clean."""
    entries = [e for e in audit.read(path) if e["event"] == "network_audit"]
    dirty = [e for e in entries if not e["data"].get("clean", False)]
    return {
        "ok": not dirty,
        "queries_audited": len(entries),
        "leaking_queries": len(dirty),
        "first_leak_ts": dirty[0]["ts"] if dirty else None,
    }


def verify_offline(path: str = None) -> dict:
    """Aggregate verdict. `offline` is True only if all four checks pass."""
    checks = {
        "endpoints": check_endpoints(),
        "telemetry": check_telemetry(),
        "live": check_live_connections(),
        "history": check_audit_history(path),
    }
    return {
        "offline": all(c["ok"] for c in checks.values()),
        "external_connections": checks["live"]["external_connections"],
        "queries_audited": checks["history"]["queries_audited"],
        "warnings": checks["endpoints"]["warnings"],
        "checks": checks,
    }


if __name__ == "__main__":  # ponytail: self-check — loopback parsing + aggregate verdict
    assert _is_loopback("127.0.0.1") and _is_loopback("localhost") and _is_loopback("::1")
    assert not _is_loopback("10.0.0.5") and not _is_loopback("api.openai.com")
    assert _is_loopback("0.0.0.0") and _classify("0.0.0.0")[1], "0.0.0.0 is local but must warn"
    assert _hostname("http://localhost:11434") == "localhost"
    assert _hostname("192.168.1.9:11434") == "192.168.1.9"

    assert check_telemetry()["ok"], check_telemetry()["mismatched"]
    result = verify_offline()
    assert result["checks"]["endpoints"]["ok"], result["checks"]["endpoints"]
    print(f"offline_check ok — offline={result['offline']}, "
          f"external={result['external_connections']}, "
          f"network_audits_reviewed={result['queries_audited']}")
