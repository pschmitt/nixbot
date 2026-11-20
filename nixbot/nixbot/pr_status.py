"""Pull-request build status comment, kept by the service itself.

One comment per pull request (marker `nixbot-build-status`) says whether
the build is pending, failed or passed, with the failure summary and a
link to the build. It replaces a per-repository onEvent effect, so every
project on a forge nixbot can comment on gets it without configuration.

Producers (the reporter wrapper in service.py) only queue intent: build,
PR number and the commit the event was about. The handler derives
everything else when it runs:

* The state comes from the build row, so duplicate or out-of-order items
  converge on the latest result instead of replaying old ones.
* A cancelled build says so, unless a later build of the same commit
  exists: that one reports for itself.
* Text from the pull request (commit subject, check names, logs) is
  untrusted: it is neutralized so it cannot mention anyone, link, or
  forge the hidden marker.
* The comment is skipped unless the forge still reports the item's commit
  as the PR head. A newer push queues its own item, which then wins.
* Items share their dedup key with an effect holding the lock
  `nixbot-status-{pr}` (deliver.event_dedup_key), so two writers never
  race on the list-then-post of one comment, whether it is two native
  items or a leftover onEvent effect during a rollout.
"""

from __future__ import annotations

import html
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import httpx

from .ansi import strip_ansi
from .db import BuildStatus
from .db_gen import builds as builds_q
from .deliver import build_payload, event_dedup_key
from .forge import ForgeError
from .gitrepo import GitError, run_git
from .repos import repo_info
from .work_queue import TransientError

if TYPE_CHECKING:
    from .events import RepoInfo
    from .forge_pr import PullRequestInfo
    from .service import CIService

logger = logging.getLogger(__name__)

WORK_KIND = "pr-status"
MARKER = "nixbot-build-status"
# Lock of the onEvent effect this replaces. Kept as the dedup key so a
# leftover effect and a native item exclude each other.
LEGACY_LOCK = "nixbot-status-{pr}"
# Reruns write their pending row right after announcing the start.
START_DELAY_SECONDS = 3.0

PENDING = "pending"
PASSED = "passed"
FAILED = "failed"
CANCELLED = "cancelled"

_PRESENTATION = {
    PENDING: (
        "\N{HOURGLASS WITH FLOWING SAND}",
        "Build in progress",
        "Nixbot is building this pull request.",
    ),
    PASSED: (
        "\N{WHITE HEAVY CHECK MARK}",
        "Build passed",
        "Nixbot built this pull request successfully.",
    ),
    FAILED: (
        "\N{CROSS MARK}",
        "Build failed",
        (
            "Nixbot could not build this pull request. "
            "Review the build output for the failing step."
        ),
    ),
    CANCELLED: (
        "\N{PROHIBITED SIGN}",
        "Build cancelled",
        "Nixbot cancelled the build of this pull request.",
    ),
}

_ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
_WARNING = "\N{WARNING SIGN}\N{VARIATION SELECTOR-16}"

_ACTIVE = frozenset({BuildStatus.PENDING, BuildStatus.EVALUATING, BuildStatus.BUILDING})

MAX_DETAILS_CHARS = 58_000
BUILD_ERROR_CHARS = 3_000
WARNING_CHARS = 3_000
WARNING_MESSAGE_CHARS = 500
ATTR_CHARS = 300
FAILURE_LOG_LINES = 1000

_GITHUB_PR_URL = re.compile(
    r"(https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/[0-9]+"
)
SUBJECT_CHARS = 200
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_SUPPORTED_FORGES = ("github", "gitea", "gitlab")


@dataclass(frozen=True)
class PrStatusItem:
    """Queue payload. Only identifies what to report on."""

    build_id: int
    pr_number: int
    rev: str

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


def dedup_key(project_id: int, pr_number: int) -> str:
    return event_dedup_key(
        project_id,
        0,
        "",
        "",
        LEGACY_LOCK.format(pr=pr_number),
    )


def state_for(status: str) -> str | None:
    """Comment state of a build status. None leaves the comment alone."""
    if status in _ACTIVE:
        return PENDING
    if status == BuildStatus.SUCCEEDED:
        return PASSED
    if status == BuildStatus.FAILED:
        return FAILED
    if status == BuildStatus.CANCELLED:
        return CANCELLED
    return None


def clean(text: str) -> str:
    """Untrusted text without ANSI sequences, control characters and
    invisible format characters (bidi overrides, zero-width marks).
    Newlines and tabs stay."""
    return "".join(
        c
        for c in strip_ansi(text)
        if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf")
    )


def escape(text: str) -> str:
    """Untrusted text for the inside of a <pre> block, which the forge
    renders as raw HTML: no tags, no mentions."""
    return html.escape(text, quote=False).replace("@", "&#64;")


def neutralize(text: str, limit: int | None = None) -> str:
    """Untrusted one-line text (commit subject, check name, warning) for
    inline use. The forge parses Markdown between inline HTML tags, so
    everything but letters, digits and spaces becomes a character
    reference: it renders as itself but can no longer form a link, an
    autolink, an emphasis, an `@mention`, an issue reference, an emoji
    code or the hidden marker comment."""
    text = " ".join(clean(text).split())
    if limit is not None and len(text) > limit:
        text = text[: limit - 1].rstrip() + _ELLIPSIS
    return "".join(c if c.isalnum() or c == " " else f"&#{ord(c)};" for c in text)


def escaped_tail(text: str, budget: int) -> str:
    """The end of `text`, HTML-escaped, within `budget` characters of the
    escaped result."""
    text = clean(text)
    marker = escape(f"{_ELLIPSIS} earlier output omitted {_ELLIPSIS}\n")
    if budget <= 0:
        return ""
    if len(text) <= budget:
        encoded = escape(text)
        if len(encoded) <= budget:
            return encoded

    pieces: list[str] = []
    used = 0
    include_marker = budget > len(marker)
    content_budget = budget - len(marker) if include_marker else budget
    consumed = 0
    for char in reversed(text):
        escaped = escape(char)
        if used + len(escaped) > content_budget:
            break
        pieces.append(escaped)
        used += len(escaped)
        consumed += 1

    suffix = "".join(reversed(pieces))
    if consumed < len(text) and include_marker:
        return marker + suffix
    return suffix


def _count(value: Any) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def render_details(summary: dict[str, Any]) -> str:  # noqa: C901, PLR0912, PLR0915
    """Build error, failed checks with their log tails and evaluation
    warnings, within the forge's comment size limit."""
    parts: list[str] = []

    def current_length() -> int:
        return sum(map(len, parts)) + max(0, len(parts) - 1) * 2

    def remaining() -> int:
        separator = 2 if parts else 0
        return MAX_DETAILS_CHARS - current_length() - separator

    build_error = summary.get("error")
    if build_error:
        header = "### \N{CROSS MARK} Build error\n<pre>\n"
        footer = "\n</pre>"
        content_budget = min(BUILD_ERROR_CHARS, remaining() - len(header) - len(footer))
        if content_budget > 0:
            content = escaped_tail(str(build_error), content_budget)
            parts.append(header + content + footer)

    failures = summary.get("failures") or []
    if failures:
        failure_heading = f"### Failed checks ({len(failures)})"
        if len(failure_heading) <= remaining():
            parts.append(failure_heading)

        failure_blocks = []
        for failure in failures:
            attr = neutralize(str(failure.get("attr", "unknown check")), ATTR_CHARS)
            header = f"<details>\n<summary><code>{attr}</code></summary>\n\n<pre>\n"
            footer = "\n</pre>\n</details>"
            log_tail = failure.get("log_tail")
            if not isinstance(log_tail, str) or not log_tail:
                log_tail = "No build logs were returned."
            failure_blocks.append((log_tail, header, footer))

        for index, (log_tail, header, footer) in enumerate(failure_blocks):
            future_overhead = sum(
                len(next_header) + len(next_footer) + 2
                for _, next_header, next_footer in failure_blocks[index + 1 :]
            )
            available = remaining() - len(header) - len(footer) - future_overhead
            if available <= 0:
                break
            sections_left = len(failure_blocks) - index
            share = max(1, available // sections_left)
            content = escaped_tail(log_tail, share)
            block = header + content + footer
            if len(block) > remaining():
                content = escaped_tail(log_tail, available)
                block = header + content + footer
            if len(block) > remaining():
                break
            parts.append(block)

    warnings = summary.get("eval_warnings") or []
    warning_lines = [f"### {_WARNING} Evaluation warnings"] if warnings else []
    warning_used = len(warning_lines[0]) if warning_lines else 0
    for warning in warnings:
        level = neutralize(str(warning.get("level", "warning")), 40)
        message = neutralize(str(warning.get("message", "")), WARNING_MESSAGE_CHARS)
        count = _count(warning.get("count", 1))
        line = (
            f"- <code>{level}</code>: <code>{message}</code> "
            f"(\N{MULTIPLICATION SIGN}{count})"
        )
        if warning_used + len(line) + 1 > WARNING_CHARS:
            break
        warning_lines.append(line)
        warning_used += len(line) + 1
    if warning_lines:
        warning_block = "\n".join(warning_lines)
        if len(warning_block) <= remaining():
            parts.append(warning_block)

    return "\n\n".join(parts)


def commit_line(rev: str, pr_url: str, subject: str) -> str:
    match = _GITHUB_PR_URL.fullmatch(pr_url)
    url = f"{match.group(1)}/commit/{rev}" if match and _FULL_SHA.fullmatch(rev) else ""
    short = neutralize(rev[:12])
    commit = f"[{short}]({url})" if url else f"<code>{short}</code>"
    suffix = f" (<code>{neutralize(subject, SUBJECT_CHARS)}</code>)" if subject else ""
    return f"Commit: {commit}{suffix}"


def render_comment(  # noqa: PLR0913
    state: str,
    *,
    rev: str,
    pr_url: str,
    subject: str,
    details: str,
    build_url: str,
    service_url: str,
) -> str:
    emoji, title, summary = _PRESENTATION[state]
    lines = [
        f"## {emoji} {title}",
        "",
        summary,
        "",
        commit_line(rev, pr_url, subject),
    ]
    if details:
        lines += ["", details]
    if build_url:
        lines += ["", f"[View build details \N{RIGHTWARDS ARROW}]({build_url})"]
    lines += [
        "",
        "---",
        "",
        (
            f"<sub>Automated by [Nixbot]({service_url}) \N{MIDDLE DOT} "
            "This comment updates with the latest build result.</sub>"
        ),
    ]
    return "\n".join(lines) + "\n"


async def commit_subject(s: CIService, info: RepoInfo, rev: str) -> str:
    """First line of the commit message from the mirror; empty when the
    mirror does not have the commit (a comment without it is still right)."""
    try:
        out = await run_git(
            ["show", "-s", "--format=%s", rev],
            cwd=s.orchestrator.repos.clone_path(info.key),
        )
    except (GitError, OSError):
        return ""
    return out.strip()


async def failure_details(s: CIService, build_id: int, state: str) -> str:
    """The failure summary of the build as markdown. A broken summary
    must not stop the status itself: failed builds point at the build
    page instead, other states just go without."""
    try:
        from .web.app import WebContext  # noqa: PLC0415
        from .web.logs import _failure_summary  # noqa: PLC0415

        row = await s.pool.fetchrow(
            "SELECT id, status, error, eval_warnings FROM builds WHERE id = $1",
            build_id,
        )
        if row is None:
            return ""
        summary = await _failure_summary(
            WebContext(s.pool, s.config.state_dir),
            s.orchestrator.log_registry,
            dict(row),
            FAILURE_LOG_LINES,
        )
        return render_details(summary)
    except Exception:
        logger.warning(
            "pr status details failed", extra={"build_id": build_id}, exc_info=True
        )
        if state == FAILED:
            return (
                f"{_WARNING} Could not retrieve the failure summary; "
                "see the build details."
            )
        return ""


def _has_client(s: CIService, forge: str) -> bool:
    return forge in _SUPPORTED_FORGES and getattr(s, forge) is not None


async def _pr_at_item_rev(
    s: CIService, info: RepoInfo, item: PrStatusItem
) -> PullRequestInfo | None:
    """The pull request, or None when there is nothing to report on: it is
    gone, or its head is no longer the item's commit (the newer push queued
    its own item)."""
    try:
        pr = await s.forge_pr.pull_request(info, item.pr_number)
    except httpx.TransportError as e:
        # The transport retried and gave up (RetryTransport's budget).
        raise _transient(e) from e
    except ForgeError as e:
        if e.transient:
            raise _transient(e) from e
        # Gone or not ours to read.
        logger.info(
            "pr status skipped, pull request unavailable",
            extra={"build_id": item.build_id, "pr": item.pr_number},
        )
        return None
    if pr.head_rev != item.rev:
        logger.info(
            "pr status skipped, head moved",
            extra={"build_id": item.build_id, "pr": item.pr_number, "rev": item.rev},
        )
        return None
    return pr


async def _has_newer_build(s: CIService, build: Any) -> bool:
    """Whether a later build exists for the same pull request and commit."""
    return bool(
        await s.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM builds WHERE project_id = $1 "
            "AND pr_number = $2 AND commit_sha = $3 AND id > $4)",
            build.project_id,
            build.pr_number,
            build.commit_sha,
            build.id_,
        )
    )


async def update_comment(s: CIService, payload: dict[str, Any]) -> None:
    """Work handler: create or edit the PR's status comment."""
    item = PrStatusItem(**payload)
    build = await builds_q.get_build(s.pool, id_=item.build_id)
    if build is None:
        return
    project = await s.repo_store.by_id(build.project_id)
    if project is None or not project.enabled:
        return
    info = repo_info(project)
    if not _has_client(s, info.forge):
        return
    pr = await _pr_at_item_rev(s, info, item)
    if pr is None:
        return
    # The row after the forge round trip, not the one the item was queued for.
    current = await builds_q.get_build(s.pool, id_=item.build_id)
    state = state_for(current.status) if current is not None else None
    if current is None or state is None:
        return
    if state == CANCELLED and await _has_newer_build(s, current):
        # A restart or a rebuild of this commit reports for itself; a
        # cancelled row must not overwrite its pending comment.
        return
    body = render_comment(
        state,
        rev=item.rev,
        pr_url=pr.url,
        subject=await commit_subject(s, info, item.rev),
        details=await failure_details(s, current.id_, state),
        build_url=build_payload(s.config.url, info, current)["url"],
        service_url=s.config.url,
    )
    try:
        await s.forge_pr.comment(info, item.pr_number, body, MARKER)
    except httpx.TransportError as e:
        raise _transient(e) from e
    except ForgeError as e:
        if e.transient:
            raise _transient(e) from e
        logger.warning(
            "pr status comment rejected",
            extra={"build_id": build.id_, "pr": item.pr_number, "error": str(e)},
        )


def _transient(error: Exception) -> TransientError:
    """The work queue retries a TransientError with backoff. Any other
    exception fails the item for good, so network errors must not leak.
    httpx's own message can be empty (timeouts), hence the type name."""
    message = str(error) or type(error).__name__
    return TransientError(message, retry_after=getattr(error, "retry_after", None))
