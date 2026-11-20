"""Native PR status comment: states, rendering, stale-head protection,
retries and the lock shared with the legacy onEvent effect."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest

from nixbot import pr_status
from nixbot.db import BuildStatus
from nixbot.deliver import event_dedup_key
from nixbot.events import NullStatusReporter
from nixbot.forge import ForgeError
from nixbot.service import RetryingReporter
from nixbot.work_queue import TransientError, WorkQueue

from .support import insert_build
from .test_deliver import PR, FakeForgePr
from .test_service import make_service, seed_project, service  # noqa: F401

if TYPE_CHECKING:
    from nixbot.service import CIService

SHA = "a" * 40
PR_URL = "https://github.com/acme/widget/pull/7"
LIMITED_LEN = 20
LONG_LINE_LEN = 2000


def test_state_for() -> None:
    for status in (BuildStatus.PENDING, BuildStatus.EVALUATING, BuildStatus.BUILDING):
        assert pr_status.state_for(status) == "pending"
    assert pr_status.state_for(BuildStatus.SUCCEEDED) == "passed"
    assert pr_status.state_for(BuildStatus.FAILED) == "failed"
    assert pr_status.state_for(BuildStatus.CANCELLED) == "cancelled"
    assert pr_status.state_for("something-else") is None


def test_dedup_key_is_the_legacy_effect_lock() -> None:
    assert pr_status.dedup_key(4, 7) == event_dedup_key(4, 0, "", "", "nixbot-status-7")
    assert pr_status.dedup_key(4, 7) != pr_status.dedup_key(4, 8)
    assert pr_status.dedup_key(4, 7) != pr_status.dedup_key(5, 7)


def test_render_comment_states() -> None:
    common: dict[str, Any] = {
        "rev": SHA,
        "pr_url": PR_URL,
        "subject": "fix <it>",
        "details": "",
        "build_url": "http://ci.test/b/1",
        "service_url": "http://ci.test",
    }
    pending = pr_status.render_comment("pending", **common)
    assert pending.startswith("## \N{HOURGLASS WITH FLOWING SAND} Build in progress")
    assert f"[{SHA[:12]}](https://github.com/acme/widget/commit/{SHA})" in pending
    assert "(<code>fix &#60;it&#62;</code>)" in pending
    assert "[View build details \N{RIGHTWARDS ARROW}](http://ci.test/b/1)" in pending
    assert pending.endswith("latest build result.</sub>\n")

    passed = pr_status.render_comment("passed", **common)
    assert "Build passed" in passed
    failed = pr_status.render_comment("failed", **{**common, "details": "DETAILS"})
    assert "Build failed" in failed
    assert "\n\nDETAILS\n\n" in failed


HOSTILE = (
    "@everyone @org/team [click](https://evil.example/x) ![i](https://evil.example/p) "
    "<!-- nixbot:nixbot-build-status --> </code><img src=x> `tick` **b** _i_ "
    "Fixes #1 :+1: www.evil.example https://evil.example user@evil.example "
    "\u202eRTL\x1b[31m red \u200b"
)


def _assert_inert(out: str) -> None:
    assert "@" not in out
    assert "<!--" not in out
    assert "<img" not in out
    assert "](" not in out
    assert "https://" not in out
    assert "www." not in out
    assert "`" not in out
    assert "&#64;everyone" in out
    assert "\u202e" not in out
    assert "\x1b" not in out
    assert "\u200b" not in out


def test_neutralize() -> None:
    out = pr_status.neutralize(HOSTILE)
    assert all(c.isalnum() or c == " " or (c in "&#;") for c in out)
    _assert_inert(out)
    # Whitespace and newlines collapse, so nothing starts a new block.
    assert pr_status.neutralize("a\n\n# h\n- x") == "a &#35; h &#45; x"
    assert pr_status.neutralize("x" * 500, 10).endswith("&#8230;")
    assert len(pr_status.neutralize("x" * 500, 10)) < LIMITED_LEN


def test_commit_line_neutralizes_the_subject() -> None:
    out = pr_status.commit_line(SHA, PR_URL, HOSTILE)
    # Only the commit link of our own is a link.
    assert out.count("](") == 1
    assert out.startswith(
        f"Commit: [{SHA[:12]}](https://github.com/acme/widget/commit/"
    )
    _assert_inert(out.split(") (", 1)[1])
    assert out.count("<code>") == 1
    assert out.count("</code>") == 1
    long_subject = pr_status.commit_line(SHA, PR_URL, "word " * 1000)
    assert len(long_subject) < LONG_LINE_LEN


def test_commit_line_rejects_odd_pull_request_urls() -> None:
    for url in (
        "https://github.com/ac)me/widget/pull/7",
        "https://github.com/acme/wid get/pull/7",
        "https://github.com/acme/widget/pull/7?x=](https://evil.example)",
    ):
        assert "](" not in pr_status.commit_line(SHA, url, "")
    # A rev that is not a SHA is never inserted raw.
    assert "<b>" not in pr_status.commit_line("<b>x" * 5, PR_URL, "")


def test_render_details_neutralizes_inline_fields() -> None:
    out = pr_status.render_details(
        {
            "failures": [{"attr": f"a.{HOSTILE}", "log_tail": HOSTILE}],
            "eval_warnings": [
                {"level": HOSTILE, "message": HOSTILE, "count": "x"},
            ],
        }
    )
    summary = out.split("<summary>", 1)[1].split("</summary>", 1)[0]
    _assert_inert(summary)
    warning = out.split("Evaluation warnings", 1)[1]
    _assert_inert(warning)
    assert "(\N{MULTIPLICATION SIGN}1)" in warning
    # The log tail is raw HTML inside <pre>: tags and mentions are inert.
    log = out.split("<pre>", 1)[1].split("</pre>", 1)[0]
    assert "@" not in log
    assert "<img" not in log
    assert "<!--" not in log
    assert "&lt;!-- nixbot:nixbot-build-status --&gt;" in log
    assert "\x1b" not in log
    assert "\u202e" not in log
    assert "&lt;/code&gt;" in log


def test_render_comment_has_one_marker_slot_for_us() -> None:
    body = pr_status.render_comment(
        "failed",
        rev=SHA,
        pr_url=PR_URL,
        subject=HOSTILE,
        details=pr_status.render_details({"error": HOSTILE}),
        build_url="http://ci.test/b/1",
        service_url="http://ci.test",
    )
    assert "<!--" not in body
    assert "@" not in body


def test_commit_line_without_github_url() -> None:
    assert pr_status.commit_line(SHA, "https://git.example/pr/7", "") == (
        f"Commit: <code>{SHA[:12]}</code>"
    )
    # A short or odd rev never becomes a link.
    assert pr_status.commit_line("abc", PR_URL, "") == "Commit: <code>abc</code>"


def test_render_details_escapes_and_limits() -> None:
    summary = {
        "error": "boom @user <b>",
        "failures": [{"attr": "x86_64-linux.a", "log_tail": "line\n" * 100000}],
    }
    out = pr_status.render_details(summary)
    assert len(out) <= pr_status.MAX_DETAILS_CHARS
    assert "&#64;user &lt;b&gt;" in out
    assert "Failed checks (1)" in out
    assert f"<code>{pr_status.neutralize('x86_64-linux.a')}</code>" in out
    assert "earlier output omitted" in out
    assert pr_status.render_details({}) == ""


def test_render_details_warnings() -> None:
    out = pr_status.render_details(
        {
            "failures": [{"attr": "a", "log_tail": "short"}],
            "eval_warnings": [{"level": "warning", "message": "w <x>", "count": 2}],
        }
    )
    assert "Evaluation warnings" in out
    assert "<code>w &#60;x&#62;</code> (\N{MULTIPLICATION SIGN}2)" in out
    assert "short" in out


async def test_reporter_queues_status_for_prs() -> None:
    svc = SimpleNamespace(enqueue_work=AsyncMock())
    wrapper = RetryingReporter(NullStatusReporter(), svc)  # type: ignore[arg-type]
    build = SimpleNamespace(id_=15, project_id=3)
    event = SimpleNamespace(pr_number=7, actor="github:alice", commit_sha=SHA)
    result = SimpleNamespace()

    expected_key = pr_status.dedup_key(3, 7)
    payload = {"build_id": 15, "pr_number": 7, "rev": SHA}

    await wrapper.build_started(event, build)  # type: ignore[arg-type]
    started = [c for c in svc.enqueue_work.await_args_list if c.args[0] == "pr-status"]
    assert len(started) == 1
    assert started[0].args[1:] == (expected_key, payload)
    assert started[0].kwargs == {"delay": pr_status.START_DELAY_SECONDS}

    svc.enqueue_work.reset_mock()
    await wrapper.build_finished(event, build, result)  # type: ignore[arg-type]
    svc.enqueue_work.assert_awaited_once_with(
        "pr-status", expected_key, payload, delay=0
    )

    # Pushes and branch builds have no pull request to comment on.
    svc.enqueue_work.reset_mock()
    event.pr_number = None
    await wrapper.build_started(event, build)  # type: ignore[arg-type]
    await wrapper.build_finished(event, build, result)  # type: ignore[arg-type]
    svc.enqueue_work.assert_not_awaited()


async def test_reporter_queues_status_even_when_the_post_fails() -> None:
    class Failing(NullStatusReporter):
        async def build_finished(self, *_a: Any, **_k: Any) -> None:
            msg = "forge down"
            raise RuntimeError(msg)

    svc = SimpleNamespace(
        enqueue_work=AsyncMock(),
        config=SimpleNamespace(work_retry_backoff=0),
    )
    wrapper = RetryingReporter(Failing(), svc)  # type: ignore[arg-type]
    build = SimpleNamespace(id_=15, project_id=3)
    event = SimpleNamespace(pr_number=7, actor=None, commit_sha=SHA)
    await wrapper.build_finished(event, build, SimpleNamespace())  # type: ignore[arg-type]
    kinds = [c.args[0] for c in svc.enqueue_work.await_args_list]
    assert kinds == ["report", "pr-status"]


async def test_reporter_survives_enqueue_failure() -> None:
    svc = SimpleNamespace(enqueue_work=AsyncMock(side_effect=OSError("db")))
    wrapper = RetryingReporter(NullStatusReporter(), svc)  # type: ignore[arg-type]
    build = SimpleNamespace(id_=15, project_id=3)
    event = SimpleNamespace(pr_number=7, actor=None, commit_sha=SHA)
    await wrapper.build_finished(event, build, SimpleNamespace())  # type: ignore[arg-type]


@pytest.fixture
async def env(
    service: CIService,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    pool = service.pool
    project_id = await seed_project(pool, "file:///nonexistent")
    build_id = await insert_build(
        pool,
        project_id,
        number=1,
        branch="feature",
        commit_sha=SHA,
        tree_hash="t1",
        status="building",
        pr_number=7,
        pr_author="github:dave",
    )
    forge = FakeForgePr(replace(PR, head_rev=SHA, url=PR_URL), {})
    monkeypatch.setattr(type(service), "forge_pr", property(lambda _s: forge))
    monkeypatch.setattr(service, "github", object(), raising=False)
    return {
        "service": service,
        "project_id": project_id,
        "build_id": build_id,
        "forge": forge,
    }


async def _run(env: dict[str, Any], rev: str = SHA) -> None:
    await pr_status.update_comment(
        env["service"],
        pr_status.PrStatusItem(env["build_id"], 7, rev).as_payload(),
    )


async def _set_status(
    env: dict[str, Any], status: str, error: str | None = None
) -> None:
    await env["service"].pool.execute(
        "UPDATE builds SET status = $2, error = $3 WHERE id = $1",
        env["build_id"],
        status,
        error,
    )


async def test_pending_comment(env: dict[str, Any]) -> None:
    await _run(env)
    [body] = env["forge"].comments
    assert "Build in progress" in body
    assert f"/commit/{SHA}" in body
    assert "http://ci.test" in body


async def test_passed_comment(env: dict[str, Any]) -> None:
    await _set_status(env, "succeeded")
    await _run(env)
    [body] = env["forge"].comments
    assert "Build passed" in body


async def test_failed_comment_has_the_build_error(env: dict[str, Any]) -> None:
    await _set_status(env, "failed", "evaluation exploded")
    await _run(env)
    [body] = env["forge"].comments
    assert "Build failed" in body
    assert "evaluation exploded" in body


async def test_stale_head_is_not_commented(env: dict[str, Any]) -> None:
    env["forge"].pr = replace(env["forge"].pr, head_rev="b" * 40)
    await _set_status(env, "succeeded")
    await _run(env)
    assert env["forge"].comments == []


async def test_cancelled_build_is_reported(env: dict[str, Any]) -> None:
    """A cancelled build without successor must not leave the comment on
    "in progress"."""
    await _run(env)
    await _set_status(env, "cancelled")
    await _run(env)
    assert "Build in progress" in env["forge"].comments[0]
    assert "Build cancelled" in env["forge"].comments[1]


async def test_cancelled_build_with_a_newer_build_is_left_alone(
    env: dict[str, Any],
) -> None:
    await insert_build(
        env["service"].pool,
        env["project_id"],
        number=2,
        branch="feature",
        commit_sha=SHA,
        tree_hash="t2",
        status="building",
        pr_number=7,
        pr_author="github:dave",
    )
    await _set_status(env, "cancelled")
    await _run(env)
    assert env["forge"].comments == []


async def test_state_is_read_when_the_item_runs(env: dict[str, Any]) -> None:
    """An item queued while the build was pending still reports the final
    result when it runs late."""
    await _set_status(env, "succeeded")
    await _run(env)
    await _set_status(env, "failed", "late failure")
    await _run(env)
    assert ["passed" in b for b in env["forge"].comments] == [True, False]
    assert "late failure" in env["forge"].comments[1]


async def test_transient_forge_error_is_retried(env: dict[str, Any]) -> None:

    env["forge"].fail = [503]
    with pytest.raises(TransientError):
        await _run(env)
    assert env["forge"].comments == []
    await _run(env)
    comments = env["forge"].comments
    assert len(comments) == 1, [c.splitlines()[0] for c in comments]


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout(""),
        httpx.RemoteProtocolError("closed"),
    ],
)
async def test_transport_error_on_lookup_is_retried(
    env: dict[str, Any], error: httpx.TransportError
) -> None:
    forge = env["forge"]
    original = forge.pull_request
    calls = 0

    async def flaky(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        return await original(*args)

    forge.pull_request = flaky
    with pytest.raises(TransientError) as caught:
        await _run(env)
    assert str(caught.value)
    assert forge.comments == []
    await _run(env)
    assert len(forge.comments) == 1


async def test_transport_error_on_comment_is_retried(env: dict[str, Any]) -> None:
    forge = env["forge"]
    posted = forge.comment

    async def down(*_a: Any, **_k: Any) -> None:
        reason = ""
        raise httpx.ConnectTimeout(reason)

    forge.comment = down
    with pytest.raises(TransientError):
        await _run(env)
    forge.comment = posted
    await _run(env)
    assert len(forge.comments) == 1


async def test_transport_error_is_requeued_not_failed(env: dict[str, Any]) -> None:
    """Through the queue: the item goes back to pending, not to failed."""
    svc: CIService = env["service"]
    forge = env["forge"]
    posted = forge.comment

    async def down(*_a: Any, **_k: Any) -> None:
        reason = "reset"
        raise httpx.ReadError(reason)

    forge.comment = down
    key = pr_status.dedup_key(env["project_id"], 7)
    payload = pr_status.PrStatusItem(env["build_id"], 7, SHA).as_payload()
    await svc.enqueue_work("pr-status", key, payload)
    item = await WorkQueue(svc.pool).claim_next()
    assert item is not None
    await svc._execute_work(WorkQueue(svc.pool), item)  # noqa: SLF001
    rows = await svc.pool.fetch(
        "SELECT status FROM work_queue WHERE kind = 'pr-status'"
    )
    assert [r["status"] for r in rows] == ["pending"]
    forge.comment = posted
    # The retry stays queued; do not leak it into later tests' drain_work.
    await svc.pool.execute("DELETE FROM work_queue WHERE kind = 'pr-status'")


async def test_missing_pull_request_is_dropped(env: dict[str, Any]) -> None:
    env["forge"].fail = [404]
    await _run(env)
    assert env["forge"].comments == []


async def test_rejected_comment_does_not_retry(env: dict[str, Any]) -> None:
    async def reject(*_a: Any, **_k: Any) -> None:
        msg = "forbidden"
        raise ForgeError(msg, status_code=403)

    env["forge"].comment = reject
    await _run(env)


async def test_queue_dispatch_and_dedup(env: dict[str, Any]) -> None:
    svc: CIService = env["service"]
    key = pr_status.dedup_key(env["project_id"], 7)
    payload = pr_status.PrStatusItem(env["build_id"], 7, SHA).as_payload()
    await svc.enqueue_work("pr-status", key, payload)
    await svc.enqueue_work("pr-status", key, payload)
    await svc.drain_work()
    comments = env["forge"].comments
    assert len(comments) == 1, [c.splitlines()[0] for c in comments]
