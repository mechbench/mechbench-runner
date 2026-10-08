from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dedicated-cuda.sh"


def load():
    text = SCRIPT.read_text()
    start = text.index("cat > \"$HOME/mechbench-find-peer.py\" <<'PY'\n")
    body = text[text.index("\n", start) + 1 :]
    body = body[: body.index("\nPY\n")]
    mod = types.ModuleType("find_peer")
    exec(compile(body, "mechbench-find-peer.py", "exec"), mod.__dict__)
    return mod


IP = {
    ("ip", "-4", "-o", "addr", "show"): "1: lo    inet 127.0.0.1/8 scope host lo\n"
    "2: enP7s7    inet 10.1.2.3/24 brd 10.1.2.255 scope global enP7s7\n"
    "5: enp1s0f0np0    inet 192.168.100.1/30 brd 192.168.100.3 scope global\n"
    "6: enp1s0f1np1    inet 169.254.7.10/16 scope link enp1s0f1np1\n",
    (
        "ip",
        "-4",
        "route",
        "show",
        "default",
    ): "default via 10.1.2.1 dev enP7s7 proto dhcp\n",
    (
        "ip",
        "-o",
        "link",
        "show",
    ): "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 state UNKNOWN\n"
    "2: enP7s7: <BROADCAST,UP,LOWER_UP> mtu 1500 state UP\n"
    "5: enp1s0f0np0: <BROADCAST,UP,LOWER_UP> mtu 9000 state UP\n"
    "6: enp1s0f1np1: <BROADCAST,UP,LOWER_UP> mtu 9000 state UP\n",
    ("ip", "-4", "neigh"): "10.1.2.1 dev enP7s7 lladdr aa:bb STALE\n"
    "169.254.7.11 dev enp1s0f1np1 lladdr cc:dd REACHABLE\n"
    "169.254.7.99 dev enp1s0f1np1 FAILED\n",
}
FILES = {
    "/etc/hosts": "127.0.0.1 localhost\n::1 ip6-localhost\n10.1.2.3 spark-a\n",
    "/etc/machine-id": "me-id\n",
    "~/.ssh/config": "Host *\n  User user\nHost github.com\n",
}


@pytest.fixture
def fp(monkeypatch):
    mod = load()
    monkeypatch.setattr(mod, "sh", lambda cmd, timeout=10.0: IP.get(tuple(cmd), ""))
    monkeypatch.setattr(mod, "read", lambda path: FILES.get(path, ""))
    monkeypatch.setattr(mod.socket, "gethostname", lambda: "spark-a")
    monkeypatch.setattr(mod.socket, "getfqdn", lambda: "spark-a.local")
    return mod


def answers(table):
    calls = []

    def run(cmd, capture_output, text, timeout):
        target = cmd[cmd.index("StrictHostKeyChecking=accept-new") + 1]
        calls.append((target, timeout))
        assert "BatchMode=yes" in cmd and "ConnectTimeout=5" in cmd
        rc, host, mid, gpu = table.get(target, (255, "", "", ""))
        m = "::mechbench-peer::"
        out = f"{m}host {host}\n{m}id {mid}\n" + (f"{m}gpu {gpu}\n" if gpu else "")
        return subprocess.CompletedProcess(cmd, rc, out if rc == 0 else "", "")

    return run, calls


def test_candidates_in_order_without_this_machine(fp):
    names, addrs, _ = fp.me()
    got = fp.candidates(names, addrs)
    assert [c for c, _ in got] == ["github.com", "192.168.100.2", "169.254.7.11"]
    assert "spark-a" not in dict(got) and "10.1.2.1" not in dict(
        got
    )


def test_first_gpu_answer_wins(fp, monkeypatch):
    run, calls = answers(
        {
            "github.com": (255, "", "", ""),
            "192.168.100.2": (0, "spark-b", "peer-id", "NVIDIA GB10"),
        }
    )
    monkeypatch.setattr(fp.subprocess, "run", run)
    out = fp.find()
    assert out["peer"] == "192.168.100.2" and out["gpu"] == "NVIDIA GB10"
    assert out["source"] == "the /30 peer on enp1s0f0np0"
    assert [t["result"] for t in out["tried"]] == ["ssh exit 255", "ok"]


def test_this_machine_and_gpu_less_hosts_are_refused(fp, monkeypatch):
    run, _ = answers(
        {
            "github.com": (0, "github", "", ""),
            "192.168.100.2": (0, "spark-a", "me-id", "NVIDIA GB10"),
            "169.254.7.11": (0, "spark-b", "peer-id", "NVIDIA GB10"),
        }
    )
    monkeypatch.setattr(fp.subprocess, "run", run)
    out = fp.find()
    assert out["peer"] == "169.254.7.11"
    assert [t["result"] for t in out["tried"]][:2] == [
        "no GPU (github)",
        "this machine",
    ]


def test_nothing_answers(fp, monkeypatch):
    run, _ = answers({})
    monkeypatch.setattr(fp.subprocess, "run", run)
    out = fp.find()
    assert (
        out["peer"] is None and "no candidate" in out["why"] and len(out["tried"]) == 3
    )


def test_bounded_in_time(fp, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(fp.time, "monotonic", lambda: clock["t"])

    def slow(cmd, capture_output, text, timeout):
        clock["t"] += timeout
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(fp.subprocess, "run", slow)
    out = fp.find(deadline=30)
    assert out["peer"] is None and clock["t"] <= 30
    assert out["why"].startswith("stopped after") or len(out["tried"]) <= 2


def test_no_candidates(fp, monkeypatch):
    monkeypatch.setattr(fp, "sh", lambda cmd, timeout=10.0: "")
    monkeypatch.setattr(fp, "read", lambda path: "")
    out = fp.find()
    assert out == {
        "peer": None,
        "why": "no candidates (no other names, no link peers, no neighbours)",
        "tried": [],
    }
