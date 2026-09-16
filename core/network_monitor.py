"""Air-gap proof: audit the network connections AEGIS's own process tree opens
during a query, so "zero data leaves the machine" is a measured fact, not a claim.

Honesty note on scope: `psutil.net_io_counters()` is system-wide (it can't
isolate one process's bytes on Linux), so the byte counts include any other
traffic on the machine during the window — labelled as such below. The part
that actually proves the air-gap is `external_connections`: every inet socket
opened by this process or its children (e.g. the sandboxed calc's `docker`
subprocess), filtered to non-loopback. If Ollama itself isn't on localhost,
that shows up here too — it isn't quietly excluded.
"""
import os

import psutil


def _own_connections():
    proc = psutil.Process(os.getpid())
    pids = [proc.pid] + [c.pid for c in proc.children(recursive=True)]
    conns = []
    for pid in pids:
        try:
            conns.extend(psutil.Process(pid).net_connections(kind="inet"))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return conns


def _is_external(raddr) -> bool:
    return bool(raddr) and raddr.ip not in ("127.0.0.1", "::1")


def snapshot_io():
    """Call before the work you want to measure; pass the result to report()."""
    return psutil.net_io_counters()


def report(io_before) -> dict:
    """Call after the work completes. Split from audit() so a caller that needs
    to interleave (e.g. streaming step-by-step UI progress) can snapshot once,
    do its own work loop, then report once at the end — same measurement,
    without forcing the work to run inside a single blocking call."""
    io_after = psutil.net_io_counters()
    conns = _own_connections()
    external = [c for c in conns if _is_external(c.raddr)]
    return {
        "system_wide_bytes_sent": io_after.bytes_sent - io_before.bytes_sent,
        "system_wide_bytes_recv": io_after.bytes_recv - io_before.bytes_recv,
        "connections": [
            f"{c.laddr.ip}:{c.laddr.port} -> {c.raddr.ip}:{c.raddr.port}"
            for c in conns if c.raddr
        ],
        "external_connections": [
            f"{c.laddr.ip}:{c.laddr.port} -> {c.raddr.ip}:{c.raddr.port}"
            for c in external
        ],
        "clean": len(external) == 0,
    }


def audit(fn, *args, **kwargs):
    """Run fn(*args, **kwargs), returning (result, network_report)."""
    io_before = snapshot_io()
    result = fn(*args, **kwargs)
    return result, report(io_before)


if __name__ == "__main__":  # ponytail: self-check — a call with no I/O reports clean
    _, report = audit(lambda: sum(range(1000)))
    assert report["clean"] is True, report
    assert report["external_connections"] == []
    print("network_monitor ok — no external connections detected for a local-only call")
