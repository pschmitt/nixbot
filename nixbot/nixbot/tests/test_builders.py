"""Builders page and live builders: machines parsing, live build
tracking and the rendered views."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import json
import socket
from typing import TYPE_CHECKING

import asyncpg
import pytest

from nixbot.builders import (
    BUILDERS_FILE_ENV,
    EVALUATORS_ENV,
    POOL_STATE_FILE_ENV,
    STATIC_BUILDERS_FILE_ENV,
    BuildersSource,
    classify,
    evaluators,
    parse_builders,
    parse_pool_state,
    pool_workers,
)
from nixbot.executor import (
    LogWriter,
    StructuredCapture,
    local_host,
    render_log_event,
    short_host,
)
from nixbot.web.logs import LogRegistry
from nixbot.web.templating import (
    build_hosts,
    display_host,
    host_counts,
    live_build_text,
    live_build_title,
)

from .e2e.support import seed
from .support import WebHarness, web_harness

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

REMOTE = "ssh-ng://nix-remote-builder@rofl-15.brkn.lol"

POOL_STATE = {
    "nixbot-worker-eb791d5d": {
        "id": "c09e984e",
        "created": 1000.0,
        "ready": True,
        "lastBusy": 2000.0,
        "ip": "100.65.47.26",
        "ips": {"netbird": "100.122.3.4", "tailscale": "100.65.47.26"},
        "state": "idle",
        "somethingNew": {"nested": [1, 2]},
    },
}


def _start(cap: StructuredCapture, act: int, drv: str, machine: str) -> None:
    event = {"action": "start", "id": act, "type": 105, "fields": [drv, machine, 1, 1]}
    render_log_event(f"@nix {json.dumps(event)}".encode(), {}, cap)


def _stop(cap: StructuredCapture, act: int) -> None:
    render_log_event(b'@nix {"action":"stop","id":%d}' % act, {}, cap)


def test_short_host() -> None:
    assert short_host("rofl-15.brkn.lol") == "rofl-15"
    assert short_host("rofl-14") == "rofl-14"
    assert short_host("100.122.3.4") == "100.122.3.4"
    assert short_host("fd00::1") == "fd00::1"
    assert local_host() == short_host(socket.gethostname())


def test_active_builds_follow_start_and_stop() -> None:
    now = [100.0]
    cap = StructuredCapture(clock=lambda: now[0])
    _start(cap, 1, "/nix/store/aaa-firefox-160.0.1.drv", REMOTE)
    now[0] = 130.0
    _start(cap, 2, "/nix/store/bbb-hello-2.12.drv", "")
    active = cap.active_builds()
    assert [(a.name, a.host, a.started) for a in active] == [
        ("firefox-160.0.1", "rofl-15", 100.0),
        ("hello-2.12", local_host(), 130.0),
    ]
    _stop(cap, 1)
    assert [a.name for a in cap.active_builds()] == ["hello-2.12"]
    # The finished build keeps its (short) host for persistence.
    assert cap.build_host("/nix/store/aaa-firefox-160.0.1.drv") == "rofl-15"
    _stop(cap, 2)
    assert cap.active_builds() == []


def test_registry_live_views(tmp_path: Path) -> None:
    registry = LogRegistry()
    cap = StructuredCapture(clock=lambda: 1.0)
    writer = LogWriter(path=tmp_path / "a.log")
    writer.capture = cap
    registry.register(7, "hosts.x", writer)
    # A registered writer without a capture yet (nix not started).
    registry.register(7, "hosts.y", LogWriter(path=tmp_path / "b.log"))
    _start(cap, 1, "/nix/store/aaa-linux-6.18.drv", REMOTE)
    _start(cap, 2, "/nix/store/bbb-dep.drv", "ssh-ng://agent@100.122.3.4")
    assert [a.name for a in registry.active_builds(7, "hosts.x")] == [
        "linux-6.18",
        "dep",
    ]
    assert registry.active_builds(7, "hosts.y") == []
    assert registry.active_builds(8, "hosts.x") == []
    assert registry.live_hosts(7) == ["100.122.3.4", "rofl-15"]
    assert registry.live_hosts(8) == []
    assert [(b, a, x.host) for b, a, x in registry.all_active()] == [
        (7, "hosts.x", "rofl-15"),
        (7, "hosts.x", "100.122.3.4"),
    ]
    live = registry.active_builds(7, "hosts.x")
    assert live_build_text(live) == "building linux-6.18 on rofl-15 +1 more"
    assert live_build_title(live) == "linux-6.18 on rofl-15\ndep on 100.122.3.4"
    assert live_build_text(live[:1]) == "building linux-6.18 on rofl-15"
    assert live_build_text([]) == ""


MACHINES = """\
# static builders
ssh-ng://nix-remote-builder@rofl-15.brkn.lol aarch64-linux,x86_64-linux /key 16 2 nixos-test,big-parallel - AAAA

ssh-ng://nix-remote-builder@rofl-16.brkn.lol x86_64-linux - - - - - -
ssh-ng://agent@100.122.3.4 x86_64-linux /key 8 1 kvm - BBBB  # pool worker
"""


def test_parse_builders_file_and_inline_entries() -> None:
    files = {"/m": MACHINES, "/nested": "@/m\n"}
    machines = parse_builders(
        "@/nested; ssh://builder.example i686-linux", read_file=files.__getitem__
    )
    assert [(m.host, m.systems, m.max_jobs) for m in machines] == [
        ("rofl-15", ["aarch64-linux", "x86_64-linux"], 16),
        ("rofl-16", ["x86_64-linux"], 1),  # "-" fields fall back
        ("100.122.3.4", ["x86_64-linux"], 8),
        ("builder", ["i686-linux"], 1),
    ]
    assert machines[0].supported_features == ["nixos-test", "big-parallel"]
    assert machines[1].supported_features == []
    assert machines[1].speed_factor == 1


def test_parse_builders_tolerates_missing_files() -> None:
    def missing(path: str) -> str:
        raise FileNotFoundError(path)

    assert parse_builders("@/nope", read_file=missing) == []
    assert parse_builders("") == []


def test_classify_pool_workers() -> None:
    machines = parse_builders(MACHINES)
    # Without the static list: IP-addressed entries are pool workers.
    assert [m.kind for m in classify(machines, None)] == ["static", "static", "pool"]
    static = {machines[0].uri}
    assert [m.kind for m in classify(machines, static)] == ["static", "pool", "pool"]


def test_evaluators(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        EVALUATORS_ENV, "nix-remote-builder@rofl-15.brkn.lol rofl-16.brkn.lol"
    )
    assert evaluators() == ["rofl-15", "rofl-16"]
    monkeypatch.delenv(EVALUATORS_ENV)
    assert evaluators() == []


def test_parse_pool_state_is_tolerant() -> None:
    workers = parse_pool_state(POOL_STATE)
    assert set(workers) == {"100.65.47.26", "100.122.3.4"}
    worker = workers["100.122.3.4"]
    assert worker.name == "nixbot-worker-eb791d5d"
    assert worker.state == "idle"
    assert (worker.created, worker.last_busy) == (1000.0, 2000.0)
    # Malformed or partial entries never break the page.
    assert parse_pool_state(None) == {}
    assert parse_pool_state([1, 2]) == {}
    assert parse_pool_state({"w": "oops", 3: {}}) == {}
    partial = parse_pool_state({"w": {"ip": "10.0.0.1", "created": "soon"}})
    assert partial["10.0.0.1"].name == "w"
    assert partial["10.0.0.1"].created is None
    assert partial["10.0.0.1"].state is None


def test_pool_worker_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(POOL_STATE_FILE_ENV, raising=False)
    assert pool_workers() == {}
    assert display_host("100.122.3.4") == "100.122.3.4"
    state = tmp_path / "state.json"
    state.write_text(json.dumps(POOL_STATE))
    monkeypatch.setenv(POOL_STATE_FILE_ENV, str(state))
    assert display_host("100.122.3.4") == "nixbot-worker-eb791d5d"
    assert display_host("rofl-15.brkn.lol") == "rofl-15"
    # A missing or unreadable file means no names, not an error.
    monkeypatch.setenv(POOL_STATE_FILE_ENV, str(tmp_path / "missing.json"))
    assert pool_workers() == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv(POOL_STATE_FILE_ENV, str(bad))
    assert pool_workers() == {}


async def test_builders_source_names_pool_workers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "machines").write_text(MACHINES)
    (tmp_path / "state.json").write_text(json.dumps(POOL_STATE))
    monkeypatch.setenv(BUILDERS_FILE_ENV, str(tmp_path / "machines"))
    monkeypatch.delenv(STATIC_BUILDERS_FILE_ENV, raising=False)
    monkeypatch.setenv(POOL_STATE_FILE_ENV, str(tmp_path / "state.json"))
    machines = {m.host: m for m in await BuildersSource().machines()}
    worker = machines["100.122.3.4"]
    assert worker.kind == "pool"
    assert worker.worker is not None
    assert worker.worker.name == "nixbot-worker-eb791d5d"
    assert machines["rofl-15"].worker is None


def test_host_counts_merges_short_names() -> None:
    assert host_counts([]) == ""
    assert (
        host_counts([("rofl-15.brkn.lol", 2), ("rofl-15", 3), ("rofl-16", 65)])
        == "rofl-15 (\N{MULTIPLICATION SIGN}5), rofl-16 (\N{MULTIPLICATION SIGN}65)"
    )


def test_build_hosts_filter() -> None:
    assert build_hosts({}) == ""
    assert build_hosts({"hosts": {"eval": [], "build": []}}) == ""
    assert (
        build_hosts(
            {"hosts": {"eval": ["rofl-15", "rofl-16.brkn.lol"], "build": ["rofl-14"]}}
        )
        == "eval rofl-15, rofl-16 · build rofl-14"
    )
    assert build_hosts({"hosts": {"eval": [], "build": ["rofl-16"]}}) == "build rofl-16"
    # Older rows stored FQDNs: one host, listed once.
    assert (
        build_hosts({"hosts": {"eval": ["rofl-15.brkn.lol", "rofl-15"], "build": []}})
        == "eval rofl-15"
    )


@pytest.fixture(scope="module")
async def postgres_dsn(postgres_dsn: str) -> str:
    await seed(postgres_dsn)
    pool = await asyncpg.create_pool(postgres_dsn)
    try:
        # Build 1: finished, evaluated on two hosts. Build 3 (building):
        # one attribute still building.
        await pool.execute(
            """
            UPDATE build_attributes a SET eval_host = CASE
                WHEN a.attr LIKE 'aarch64%' THEN 'rofl-16' ELSE 'rofl-15' END,
              build_host = CASE WHEN a.attr = 'x86_64-linux.ok' THEN 'rofl-14' END
            FROM builds b WHERE b.id = a.build_id AND b.number = 1
            """
        )
        await pool.execute(
            """
            INSERT INTO build_attributes (build_id, attr, system, status, eval_host)
            SELECT id, 'x86_64-linux.slow', 'x86_64-linux', 'building', 'rofl-15'
            FROM builds WHERE number = 3
            """
        )
    finally:
        await pool.close()
    return postgres_dsn


@pytest.fixture(scope="module")
def client(
    postgres_dsn: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[WebHarness]:
    tmp = tmp_path_factory.mktemp("builders")
    (tmp / "machines").write_text(MACHINES)
    (tmp / "static").write_text(MACHINES.splitlines()[1] + "\n")
    (tmp / "state.json").write_text(json.dumps(POOL_STATE))
    mp = pytest.MonkeyPatch()
    mp.setenv(POOL_STATE_FILE_ENV, str(tmp / "state.json"))
    mp.setenv(BUILDERS_FILE_ENV, str(tmp / "machines"))
    mp.setenv(STATIC_BUILDERS_FILE_ENV, str(tmp / "static"))
    mp.setenv(EVALUATORS_ENV, "rofl-15.brkn.lol rofl-16.brkn.lol")
    try:
        with web_harness(postgres_dsn) as harness:
            harness.tmp = tmp  # type: ignore[attr-defined]
            yield harness
    finally:
        mp.undo()


def _build_id(client: WebHarness, number: int) -> int:
    async def get() -> int:
        pool = client.app.state.web_context.pool
        return await pool.fetchval("SELECT id FROM builds WHERE number = $1", number)

    return client.run(get())


def _go_live(client: WebHarness) -> StructuredCapture:
    """Register the building attribute of build 3 as running on rofl-15."""
    registry: LogRegistry = client.app.state.log_registry
    cap = StructuredCapture(clock=lambda: 0.0)
    writer = LogWriter(path=client.tmp / "slow.log")  # type: ignore[attr-defined]
    writer.capture = cap
    registry.register(_build_id(client, 3), "x86_64-linux.slow", writer)
    _start(cap, 1, "/nix/store/aaa-firefox-160.0.1.drv", REMOTE)
    return cap


def test_nav_links_builders(client: WebHarness) -> None:
    page = client.get("/builds").text
    assert '<a href="/builders">builders</a>' in page


def test_builders_page(client: WebHarness) -> None:
    _go_live(client)
    page = client.get("/builders")
    assert page.status_code == 200
    text = page.text
    assert 'id="machine-rofl-15"' in text
    assert 'id="machine-100.122.3.4"' in text
    assert "pool worker" in text
    # The pool worker reads by its name, its address in the tooltip.
    assert ">nixbot-worker-eb791d5d</code>" in text
    assert "· idle" in text
    assert "firefox-160.0.1" in text
    assert "1 / 16" in text  # rofl-15: one of 16 slots busy
    assert "/builds/3/logs/x86_64-linux.slow" in text
    # The local machine is always listed.
    assert f'id="machine-{local_host()}"' in text
    # Evaluators and the latest evaluations' host split.
    assert "<code>rofl-16</code>" in text
    # Neither the SSH key path nor the host key leaks.
    assert "/key" not in text
    assert "AAAA" not in text


def test_building_row_shows_live_builder(client: WebHarness) -> None:
    _go_live(client)
    page = client.get("/repos/github/acme/widget/builds/3").text
    assert "building firefox-160.0.1 on rofl-15" in page
    # Live refresh while attributes build.
    assert "every 10s" in page
    finished = client.get("/repos/github/acme/widget/builds/1").text
    assert "every 10s" not in finished


def test_repo_builds_list_shows_builders(client: WebHarness) -> None:
    _go_live(client)
    page = client.get("/repos/github/acme/widget").text
    assert ">builders</th>" in page
    assert "eval rofl-15, rofl-16 · build rofl-14" in page
    # The running build includes its live builder.
    assert "eval rofl-15 · build rofl-15" in page
    rows = client.get("/repos/github/acme/widget/rows?limit=3").text
    assert "eval rofl-15, rofl-16 · build rofl-14" in rows
    feed = client.get("/builds/rows").text
    assert "eval rofl-15, rofl-16 · build rofl-14" in feed
