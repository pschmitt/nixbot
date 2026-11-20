"""Eval and build host of an attribute: parsing, capture, persistence
and rendering."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nixbot import build_run, db
from nixbot.build_scheduler import AttributeResult, AttributeStatus
from nixbot.executor import (
    StructuredCapture,
    local_host,
    machine_host,
    render_log_event,
)
from nixbot.models import NixEvalJobError, NixEvalJobSuccess
from nixbot.web.templating import attr_hosts

from .support import insert_project, mk_job

if TYPE_CHECKING:
    import asyncpg


def test_machine_host() -> None:
    assert machine_host("ssh-ng://nix-remote-builder@rofl-16.brkn.lol") == (
        "rofl-16.brkn.lol"
    )
    assert machine_host("ssh://root@builder:2222?compress=true") == "builder"
    assert machine_host("ssh-ng://[fd00::1]:22") == "fd00::1"
    assert machine_host("builder.example") == "builder.example"
    assert machine_host("") == ""


def test_capture_build_host_from_build_activity() -> None:
    cap = StructuredCapture(clock=lambda: 1.0)
    activities: dict[int, str] = {}
    render_log_event(
        b'@nix {"action":"start","id":1,"type":105,"fields":'
        b'["/nix/store/aaa-top.drv","ssh-ng://nix-remote-builder@rofl-15.brkn.lol",1,1]}',
        activities,
        cap,
    )
    render_log_event(
        b'@nix {"action":"start","id":2,"type":105,'
        b'"fields":["/nix/store/bbb-dep.drv","",1,1]}',
        activities,
        cap,
    )
    # Short host names, like the wrapper's evalHost.
    assert cap.build_host("/nix/store/aaa-top.drv") == "rofl-15"
    # A local build reports no machine: this host built it.
    assert cap.build_host("/nix/store/bbb-dep.drv") == local_host()
    # Never built here (substituted, already valid).
    assert cap.build_host("/nix/store/ccc-cached.drv") is None


def test_eval_host_parsed_from_job_json() -> None:
    line = {
        "attr": "a",
        "attrPath": ["a"],
        "drvPath": "/nix/store/a.drv",
        "name": "a",
        "outputs": {"out": "/nix/store/a-out"},
        "system": "x86_64-linux",
        "neededBuilds": [],
        "neededSubstitutes": [],
        "evalHost": "rofl-15.brkn.lol",
    }
    assert NixEvalJobSuccess.model_validate(line).eval_host == "rofl-15.brkn.lol"
    del line["evalHost"]
    assert NixEvalJobSuccess.model_validate(line).eval_host is None
    err = NixEvalJobError.model_validate(
        {"attr": "b", "attrPath": ["b"], "error": "boom", "evalHost": "rofl-16"}
    )
    assert err.eval_host == "rofl-16"


def test_attr_hosts_filter() -> None:
    row = {"eval_host": "rofl-15.brkn.lol", "build_host": "rofl-16.brkn.lol"}
    assert attr_hosts(row) == "eval rofl-15 · build rofl-16"
    assert (
        attr_hosts(row, full=True) == "eval rofl-15.brkn.lol · build rofl-16.brkn.lol"
    )
    assert attr_hosts({"eval_host": None, "build_host": "10.0.0.7"}) == "build 10.0.0.7"
    assert attr_hosts({}) == ""


async def test_hosts_persisted(pool: asyncpg.Pool) -> None:
    project_id = await insert_project(pool, "attr-hosts")
    build, _ = await db.get_or_create_build(pool, project_id, "tree-h", "sha", "main")
    job = mk_job("a").model_copy(update={"eval_host": "rofl-15.brkn.lol"})
    await build_run.record_attributes(pool, build.id_, [job, mk_job("b")])

    async def hosts(attr: str) -> tuple[str | None, str | None]:
        row = await pool.fetchrow(
            "SELECT eval_host, build_host FROM build_attributes"
            " WHERE build_id = $1 AND attr = $2",
            build.id_,
            attr,
        )
        assert row is not None
        return row["eval_host"], row["build_host"]

    assert await hosts("a") == ("rofl-15.brkn.lol", None)
    assert await hosts("b") == (None, None)

    # Completion records the build host and keeps the eval host.
    await db.complete_attribute(
        pool,
        build.id_,
        AttributeResult(
            attr="a",
            status=AttributeStatus.succeeded,
            job=mk_job("a"),
            out_path="/nix/store/a-out",
            build_host="rofl-16.brkn.lol",
        ),
    )
    assert await hosts("a") == ("rofl-15.brkn.lol", "rofl-16.brkn.lol")

    # A failed eval reports its host through the completion itself.
    await db.complete_attribute(
        pool,
        build.id_,
        AttributeResult(
            attr="c",
            status=AttributeStatus.failed_eval,
            job=NixEvalJobError(
                attr="c", attr_path=["c"], error="boom", eval_host="rofl-16"
            ),
            error="boom",
        ),
    )
    assert await hosts("c") == ("rofl-16", None)
