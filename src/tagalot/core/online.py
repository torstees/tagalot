"""Looking items' details up online (DESIGN.md §9 *Online details*, #339).

The only code in Tagalot that goes online. A theme declares its services
(``Theme.online_sources``), says which addresses to fetch for an item
(``Theme.online_requests``), and reads the answers (``Theme.online_details``); this module
checks every address against the declared hosts, fetches with the standard library
(``https`` only, rate-limited per host, backing off when a service is busy), keeps the
answers in the keep (``online_response``), and has the theme apply them in the DB writer
with provenance ``fetched``.

Nothing here asks the user: :meth:`KeepSession.look_up` refuses unless the keep allows
lookups (``[online] lookups = "allow"``).
"""

import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import Message
from typing import Protocol
from urllib.parse import urlsplit

from sqlalchemy import Connection, Engine, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot import __version__
from tagalot.core.ingest import IngestError, IngestSession
from tagalot.core.models import CachedResponse, Entity, FieldSource, utcnow
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import EntityRef, OnlineResponse, OnlineSource, Theme

logger = logging.getLogger(__name__)

USER_AGENT = f"Tagalot/{__version__} (+https://github.com/torstees/tagalot)"
TIMEOUT = 20.0
"""Seconds to wait for a service before giving up on it for this run."""
MAX_BODY = 8 * 1024 * 1024
"""The most of a response that is read; anything longer is a mistake, not details."""
RETRIES = 2
"""How many more times a busy service (429, or a server error) is tried."""
BACKOFF = 5.0
"""Seconds to wait before trying a busy service again, doubling each time, unless it says
how long (``Retry-After``)."""
MAX_WAIT = 60.0
"""The longest Tagalot waits for a busy service before giving up on it for this run."""
BATCH = 20
"""Items applied in one write."""


class OnlineError(Exception):
    """A lookup can't be made; the message is meant for the user."""


class NotAllowedError(OnlineError):
    """The keep doesn't allow online lookups (not asked yet, or the user said never)."""


class UnavailableError(OnlineError):
    """A service couldn't be reached or kept failing: nothing changes, and the items are
    looked up again next time."""


def source_for(url: str, sources: Sequence[OnlineSource]) -> OnlineSource:
    """The declared service ``url`` is on, or :class:`OnlineError`: only ``https``
    addresses on a declared host (no user name, password, or other port) are fetched."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise OnlineError(f"{url!r} isn't a web address") from None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        raise OnlineError(f"{url!r}: only plain https addresses are looked up")
    found = next((s for s in sources if s.host == host), None)
    if found is None:
        raise OnlineError(f"{url!r}: {host or 'no host'} isn't one of the theme's services")
    return found


@dataclass(frozen=True)
class Reply:
    """What an :data:`Opener` got back."""

    status: int
    body: bytes
    retry_after: float | None = None


class Opener(Protocol):
    """Fetches one address: ``(url, headers, timeout) -> Reply``. Raises ``OSError`` (or
    ``urllib.error.URLError``) when the service can't be reached. Tests pass their own."""

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> Reply: ...


def urlopen_reply(url: str, headers: Mapping[str, str], timeout: float) -> Reply:
    """The real :data:`Opener`: ``urllib``, with HTTP errors as replies, not exceptions."""
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # https only: source_for
            return Reply(response.status, response.read(MAX_BODY + 1))
    except urllib.error.HTTPError as e:
        with e:
            body = e.read(MAX_BODY + 1)
        return Reply(e.code, body, _retry_after(e.headers))


def _retry_after(headers: Message | None) -> float | None:
    value = headers.get("Retry-After") if headers is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None  # an HTTP date: back off our own way


class Fetcher:
    """Fetches addresses on declared services, at most one request per ``interval`` to
    each host, backing off when a service is busy. A host that can't be reached is given
    up on for the rest of this fetcher's life (one run). Safe to use from several threads."""

    def __init__(
        self,
        sources: Sequence[OnlineSource],
        *,
        opener: Opener = urlopen_reply,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        stopped: Callable[[], bool] = lambda: False,
    ) -> None:
        self.sources = tuple(sources)
        self._opener = opener
        self._clock = clock
        self._sleep = sleep
        self._stopped = stopped
        self._locks = {s.host: threading.Lock() for s in self.sources}
        self._last: dict[str, float] = {}
        self.down: dict[str, str] = {}
        """Hosts given up on in this run, with why."""
        self.fetched = 0

    def fetch(self, url: str) -> OnlineResponse:
        """Fetch ``url``: its answer, whatever its status (404 included), or
        :class:`UnavailableError` when the service can't be reached or stays busy."""
        source = source_for(url, self.sources)
        host = source.host
        with self._locks[host]:
            for attempt in range(RETRIES + 1):
                if host in self.down:
                    raise UnavailableError(f"{source.name}: {self.down[host]}")
                if self._stopped():
                    raise UnavailableError("stopped")
                self._wait_turn(host, source.interval)
                logger.info("Looking up %s", url)
                try:
                    reply = self._opener(url, {"User-Agent": USER_AGENT}, TIMEOUT)
                except (OSError, ValueError) as e:  # URLError and timeouts are OSErrors
                    reason = getattr(e, "reason", e)
                    self.down[host] = f"can't be reached ({reason})"
                    raise UnavailableError(f"{source.name}: {self.down[host]}") from e
                finally:
                    self._last[host] = self._clock()
                busy = reply.status == 429 or reply.status >= 500
                if not busy:
                    if len(reply.body) > MAX_BODY:
                        raise UnavailableError(f"{source.name}: the answer for {url} is too big")
                    self.fetched += 1
                    text = reply.body.decode("utf-8", errors="replace")
                    return OnlineResponse(url, reply.status, text)
                wait = reply.retry_after if reply.retry_after is not None else BACKOFF * 2**attempt
                if attempt == RETRIES or wait > MAX_WAIT:
                    break
                logger.info("%s is busy (%d); trying again in %.0f s", host, reply.status, wait)
                self._sleep(wait)
            self.down[host] = f"is busy or failing (HTTP {reply.status})"
            raise UnavailableError(f"{source.name}: {self.down[host]}")

    def _wait_turn(self, host: str, interval: float) -> None:
        last = self._last.get(host)
        if last is not None:
            wait = last + interval - self._clock()
            if wait > 0:
                self._sleep(wait)


# --- what to look up ---


@dataclass
class Lookup:
    """One item to look up: the addresses its theme asked for, and those already answered."""

    entity: EntityRef
    urls: list[str]
    cached: dict[str, OnlineResponse]

    def missing(self, refresh: bool) -> list[str]:
        """The addresses still to fetch (all of them, to ``refresh``)."""
        return list(self.urls) if refresh else [u for u in self.urls if u not in self.cached]


@dataclass
class Plan:
    lookups: list[Lookup] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    """The theme asked for something it can't have, naming the item."""


def plan_lookups(
    conn: Connection, schema: ThemeSchema, theme: Theme, entity_ids: Iterable[int]
) -> Plan:
    """What to look up for these items (read-only: a worker runs it on the reader). Items
    the theme has nothing to look up for are left out."""
    plan = Plan()
    if not theme.online_sources:
        return plan
    ids = list(dict.fromkeys(entity_ids))
    types = dict(conn.execute(select(Entity.id, Entity.type).where(Entity.id.in_(ids))).all())
    reader = IngestSession(conn, schema)
    for entity_id in ids:
        type_id = types.get(entity_id)
        if type_id is None:
            continue
        try:
            entity_type = schema.by_type_id(type_id).entity
        except KeyError:
            continue
        ref = EntityRef(entity_id, type_id)
        record = reader.get(ref)
        try:
            urls = list(dict.fromkeys(theme.online_requests(entity_type, record)))
            for url in urls:
                source_for(url, theme.online_sources)
        except OnlineError as e:
            plan.problems.append(f"{record.title}: {e}")
            continue
        except Exception as e:  # a theme bug: report it, look the rest up
            logger.exception("online_requests failed for entity %d", entity_id)
            plan.problems.append(f"{record.title}: the theme failed to say what to look up: {e}")
            continue
        if urls:
            plan.lookups.append(Lookup(ref, urls, {}))
    _fill_cached(conn, plan.lookups)
    return plan


def _fill_cached(conn: Connection, lookups: Sequence[Lookup]) -> None:
    wanted = sorted({u for lookup in lookups for u in lookup.urls})
    found: dict[str, OnlineResponse] = {}
    for start in range(0, len(wanted), 500):
        rows = conn.execute(
            select(CachedResponse.url, CachedResponse.status, CachedResponse.body).where(
                CachedResponse.url.in_(wanted[start : start + 500])
            )
        )
        found.update({url: OnlineResponse(url, status, body) for url, status, body in rows})
    for lookup in lookups:
        lookup.cached = {u: found[u] for u in lookup.urls if u in found}


# --- applying the answers ---


class OnlineSession(IngestSession):
    """``ctx`` for ``Theme.online_details``: values it writes are marked ``fetched``."""

    source = FieldSource.FETCHED


@dataclass
class Applied:
    items: int = 0
    problems: list[str] = field(default_factory=list)


def apply_details(
    conn: Connection,
    schema: ThemeSchema,
    theme: Theme,
    answers: Sequence[tuple[EntityRef, Mapping[str, OnlineResponse]]],
    fresh: Sequence[OnlineResponse],
) -> Applied:
    """In the DB writer: keep the ``fresh`` answers, then have the theme apply each item's.
    An item the theme fails on is reported and left as it was; the rest go ahead."""
    if fresh:
        insert = sqlite_insert(CachedResponse)
        conn.execute(
            insert.on_conflict_do_update(
                index_elements=["url"],
                set_={
                    "status": insert.excluded.status,
                    "body": insert.excluded.body,
                    "fetched_at": insert.excluded.fetched_at,
                },
            ),
            [
                {"url": r.url, "status": r.status, "body": r.text, "fetched_at": utcnow()}
                for r in {r.url: r for r in fresh}.values()
            ],
        )
    applied = Applied()
    for ref, responses in answers:
        title = conn.scalar(select(Entity.title).where(Entity.id == ref.id))
        if title is None:
            continue  # deleted while it was looked up
        savepoint = conn.begin_nested()
        try:
            ctx = OnlineSession(conn, schema)
            theme.online_details(ref, responses, ctx)
            report = ctx.flush()
        except Exception as e:
            savepoint.rollback()
            if not isinstance(e, IngestError):
                logger.exception("online_details failed for entity %d", ref.id)
            applied.problems.append(f"{title}: the theme failed to read what was found: {e}")
            continue
        savepoint.commit()
        applied.items += 1
        applied.problems += [f"{title}: {w.message}" for w in report.warnings]
    return applied


# --- one run ---


@dataclass
class LookupReport:
    """What :func:`look_up` did."""

    items: int = 0
    """Items whose details were applied (from new answers or kept ones)."""
    fetched: int = 0
    """Addresses fetched."""
    not_found: int = 0
    """Addresses a service answered with an error status, such as 404 (unknown)."""
    skipped: int = 0
    """Items left for next time: a service couldn't be reached."""
    problems: list[str] = field(default_factory=list)
    """What went wrong, for the activity panel."""


Progress = Callable[[int, int], None]
"""``(items done, items in all)``."""


def look_up(
    writer: DbWriter,
    reader: Engine,
    schema: ThemeSchema,
    theme: Theme,
    entity_ids: Iterable[int],
    *,
    refresh: bool = False,
    fetcher: Fetcher | None = None,
    progress: Progress | None = None,
    stopped: Callable[[], bool] = lambda: False,
) -> LookupReport:
    """Look these items up (runs in a worker): fetch the addresses the theme asks for that
    have no kept answer (all of them, to ``refresh``), then apply each item's answers, a
    batch at a time. Items whose services can't be reached are left for next time."""
    report = LookupReport()
    with reader.connect() as conn:
        plan = plan_lookups(conn, schema, theme, entity_ids)
    report.problems += plan.problems
    if not plan.lookups:
        return report
    fetcher = fetcher or Fetcher(theme.online_sources, stopped=stopped)
    answers: list[tuple[EntityRef, Mapping[str, OnlineResponse]]] = []
    fresh: list[OnlineResponse] = []

    def write() -> None:
        if not answers:
            return
        batch, new = list(answers), list(fresh)
        applied = writer.run(lambda conn: apply_details(conn, schema, theme, batch, new))
        report.items += applied.items
        report.problems += applied.problems
        answers.clear()
        fresh.clear()

    total = len(plan.lookups)
    for done, lookup in enumerate(plan.lookups, start=1):
        if stopped():
            break
        responses = dict(lookup.cached)
        try:
            for url in lookup.missing(refresh):
                responses[url] = response = fetcher.fetch(url)
                fresh.append(response)
                if not response.ok:
                    report.not_found += 1
        except UnavailableError as e:
            if stopped():
                break
            report.skipped += 1
            if str(e) not in report.problems:
                report.problems.append(str(e))
        else:
            answers.append((lookup.entity, responses))
        if len(answers) >= BATCH:
            write()
        if progress is not None:
            progress(done, total)
    write()
    report.fetched = fetcher.fetched
    return report


def consent_text(sources: Sequence[OnlineSource]) -> str:
    """What looking up sends, for the consent question and Keep configuration: "Tagalot
    would send DOIs to Crossref and arXiv IDs to arXiv. Nothing else about your files leaves
    this computer." """
    parts = [f"{s.sends} to {s.name}" for s in sources]
    if not parts:
        return "This keep's theme looks nothing up online."
    sent = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"Tagalot would send {sent}. Nothing else about your files leaves this computer."
