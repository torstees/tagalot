"""Looking items' details up online (DESIGN.md §9 *Online details*, #339).

The only code in Tagalot that goes online. A theme declares its services
(``Theme.online_sources``), says which addresses to fetch for an item
(``Theme.online_requests``), and reads the answers (``Theme.online_details``); this module
checks every address against the declared hosts, fetches with the standard library
(``https`` only, rate-limited per host, backing off when a service is busy), keeps the
answers in the keep (``online_response``), and has the theme apply them in the DB writer
with provenance ``fetched``.

A theme can also name pictures of items in what it found (``ctx.online_picture``, #483):
they are fetched after the details, from the services the keep allows, and kept in the keep
(``online_picture``).

Nothing here asks the user: :meth:`KeepSession.look_up` refuses unless the keep allows
lookups (``[online] lookups = "allow"``), and only fetches from the services it allows
(``[online] allowed``); addresses on others wait until the user allows them.

A service that needs a token (``OnlineSource.token_env``, #485) is used only while its
variable is set, in the keep's environment (#544: the user's, in ``settings.toml``) or else
the real one; the token is sent as ``Authorization: Bearer`` and never logged or kept in
the keep. Addresses that find an item by title (``OnlineRequest``) are dropped unless the
keep allows them (``[online] find_by``).
"""

import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from email.message import Message
from functools import partial
from http.client import HTTPMessage
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from sqlalchemy import Connection, Engine, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from tagalot import __version__
from tagalot.core.ingest import IngestError, IngestSession
from tagalot.core.models import CachedResponse, Entity, FieldSource, OnlinePicture, utcnow
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import (
    EntityRef,
    OnlineRequest,
    OnlineResponse,
    OnlineSource,
    Theme,
)

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
MAX_REDIRECTS = 5
"""How many redirects one address may take (Open Library's covers take two, #484)."""
REFUSED = frozenset({401, 403})
"""Statuses that refuse the request, not the identifier: a token that isn't valid, or no
token. Such answers are never kept, and the service is given up on for the run (#487)."""
ROUNDS = 2
"""How many times a run looks up items: those asked for, then those whose details the
answers set (#484), or that a search by title found the ID of (#485)."""
ALLOWED_BY: dict[str, frozenset[str]] = {
    "ids": frozenset({"id"}),
    "title-year": frozenset({"id", "title-year"}),
    "title": frozenset({"id", "title-year", "title"}),
}
"""What a keep's ``[online] find_by`` lets an :class:`OnlineRequest` be ``by`` (#485)."""


Env = Mapping[str, str]
"""A keep's environment variables (#544), used ahead of the real environment."""


def token(source: OnlineSource, env: Env | None = None) -> str | None:
    """The token ``source`` needs, from the keep's environment ``env`` (#544), else the
    real one; ``None`` when it needs none or the variable isn't set (#485). Read each time,
    never kept."""
    if source.token_env is None:
        return None
    found = (env or {}).get(source.token_env, "").strip()
    return found or os.environ.get(source.token_env, "").strip() or None


def usable(sources: Iterable[OnlineSource], env: Env | None = None) -> list[OnlineSource]:
    """The services that can be used: those needing no token, and those whose token is set.
    The others are left out of lookups and of the consent question (#485)."""
    return [s for s in sources if s.token_env is None or token(s, env) is not None]


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
    location: str | None = None
    """Where a redirect (3xx) points: the :class:`Fetcher` follows it only to a service the
    keep allows."""


def on_service(url: str, sources: Sequence[OnlineSource]) -> OnlineSource | None:
    """The service a redirect's target is on: a plain ``https`` address on one of
    ``sources``' hosts or a subdomain of one (the Internet Archive answers from
    ``ia800401.us.archive.org``); ``None`` for anything else (#484)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return None
    return next((s for s in sources if host == s.host or host.endswith("." + s.host)), None)


class Opener(Protocol):
    """Fetches one address: ``(url, headers, timeout) -> Reply``. Raises ``OSError`` (or
    ``urllib.error.URLError``) when the service can't be reached. Tests pass their own."""

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> Reply: ...


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect here: it comes back as a reply with its ``Location``, and the
    :class:`Fetcher` decides (only to a service the keep allows, #484)."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None  # urllib then raises the 3xx as an HTTPError


def urlopen_reply(url: str, headers: Mapping[str, str], timeout: float) -> Reply:
    """The real :data:`Opener`: ``urllib``, with HTTP errors and redirects as replies, not
    exceptions or requests of its own."""
    request = urllib.request.Request(url, headers=dict(headers))
    opener = urllib.request.build_opener(_NoRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:  # https only: source_for
            return Reply(response.status, response.read(MAX_BODY + 1))
    except urllib.error.HTTPError as e:
        with e:
            body = e.read(MAX_BODY + 1) if e.fp is not None else b""
        location = e.headers.get("Location") if e.headers is not None else None
        return Reply(e.code, body, _retry_after(e.headers), location)


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
        env: Env | None = None,
    ) -> None:
        self.sources = tuple(sources)
        self._env = dict(env or {})
        """The keep's environment (#544), for services' tokens."""
        self._opener = opener
        self._clock = clock
        self._sleep = sleep
        self._stopped = stopped
        self._locks = {s.host: threading.Lock() for s in self.sources}
        self._last: dict[str, float] = {}
        self.down: dict[str, str] = {}
        """Hosts given up on in this run, with why."""
        self.fetched = 0

    def can_fetch(self, url: str) -> bool:
        """``url`` is on one of this fetcher's services (those the keep allows)."""
        try:
            source_for(url, self.sources)
        except OnlineError:
            return False
        return True

    def fetch(self, url: str) -> OnlineResponse:
        """Fetch ``url``: its answer, whatever its status (404 included), or
        :class:`UnavailableError` when the service can't be reached or stays busy."""
        reply = self._get(url)
        return OnlineResponse(url, reply.status, reply.body.decode("utf-8", errors="replace"))

    def fetch_picture(self, url: str) -> Reply:
        """Fetch the picture at ``url``, as bytes, by the same rules as :meth:`fetch`."""
        return self._get(url)

    def _get(self, url: str) -> Reply:
        """``url``'s answer, following redirects (each spaced like any request) only to the
        services this fetcher may use; a redirect elsewhere is the answer itself."""
        source = source_for(url, self.sources)  # the theme's address: on a declared host
        for _ in range(MAX_REDIRECTS + 1):
            reply = self._get_one(url, source)
            if not 300 <= reply.status < 400 or not reply.location:
                return reply
            target = urljoin(url, reply.location)
            onward = on_service(target, self.sources)
            if onward is None:
                logger.info("Not following %s's redirect to %s", url, target)
                return reply
            url, source = target, onward
        return reply

    def _get_one(self, url: str, source: OnlineSource) -> Reply:
        host = source.host
        with self._locks[host]:
            for attempt in range(RETRIES + 1):
                if host in self.down:
                    raise UnavailableError(f"{source.name}: {self.down[host]}")
                if self._stopped():
                    raise UnavailableError("stopped")
                self._wait_turn(host, source.interval)
                logger.info("Looking up %s", url)  # never the token
                headers = {"User-Agent": USER_AGENT}
                secret = token(source, self._env)
                if secret is not None:
                    headers["Authorization"] = f"Bearer {secret}"
                try:
                    reply = self._opener(url, headers, TIMEOUT)
                except (OSError, ValueError) as e:  # URLError and timeouts are OSErrors
                    reason = getattr(e, "reason", e)
                    self.down[host] = f"can't be reached ({reason})"
                    raise UnavailableError(f"{source.name}: {self.down[host]}") from e
                finally:
                    self._last[host] = self._clock()
                if reply.status in REFUSED:
                    self.down[host] = refused_text(source, reply.status)
                    raise UnavailableError(f"{source.name}: {self.down[host]}")
                busy = reply.status == 429 or reply.status >= 500
                if not busy:
                    if len(reply.body) > MAX_BODY:
                        raise UnavailableError(f"{source.name}: the answer for {url} is too big")
                    self.fetched += 1
                    return reply
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
    conn: Connection,
    schema: ThemeSchema,
    theme: Theme,
    entity_ids: Iterable[int],
    find_by: str = "ids",
    env: Env | None = None,
) -> Plan:
    """What to look up for these items (read-only: a worker runs it on the reader). Items
    the theme has nothing to look up for are left out, and so are addresses that find an
    item in a way ``find_by`` doesn't allow, or on a service whose token isn't set (#485)."""
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
            urls = _allowed_urls(theme.online_requests(entity_type, record), theme, find_by, env)
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


def _allowed_urls(
    requests: Iterable[str | OnlineRequest], theme: Theme, find_by: str, env: Env | None = None
) -> list[str]:
    """The addresses of ``requests`` to fetch: each checked against the theme's services,
    then those ``find_by`` doesn't allow and those on a service whose token isn't set
    dropped, without a problem (#485)."""
    allowed = ALLOWED_BY.get(find_by, ALLOWED_BY["ids"])
    urls: list[str] = []
    for request in requests:
        url, by = (
            (request.url, request.by) if isinstance(request, OnlineRequest) else (request, "id")
        )
        source = source_for(url, theme.online_sources)
        if by != "id" and not source.titles:
            raise OnlineError(f"{url!r}: {source.name} isn't declared with titles=True")
        if by in allowed and (source.token_env is None or token(source, env) is not None):
            urls.append(url)
    return list(dict.fromkeys(urls))


def _fill_cached(conn: Connection, lookups: Sequence[Lookup]) -> None:
    wanted = sorted({u for lookup in lookups for u in lookup.urls})
    found: dict[str, OnlineResponse] = {}
    for start in range(0, len(wanted), 500):
        rows = conn.execute(
            select(CachedResponse.url, CachedResponse.status, CachedResponse.body).where(
                CachedResponse.url.in_(wanted[start : start + 500])
            )
        )
        found.update(
            {
                url: OnlineResponse(url, status, body)
                for url, status, body in rows
                if status not in REFUSED  # kept by builds before #487: asked again
            }
        )
    for lookup in lookups:
        lookup.cached = {u: found[u] for u in lookup.urls if u in found}


def to_fetch(
    conn: Connection,
    schema: ThemeSchema,
    theme: Theme,
    entity_ids: Iterable[int],
    *,
    refresh: bool = False,
    find_by: str = "ids",
    env: Env | None = None,
) -> int:
    """How many of these items would go online (read-only): those with an address that
    has no kept answer (any address, to ``refresh``). Asking for consent waits on this, so
    the question only comes when something would be sent."""
    plan = plan_lookups(conn, schema, theme, entity_ids, find_by, env)
    return sum(1 for lookup in plan.lookups if lookup.missing(refresh))


def changed_since(conn: Connection, since: datetime | None) -> list[int]:
    """Items made or changed at or after ``since`` (a scan's start), newest first; every
    item for ``None``."""
    query = select(Entity.id).order_by(Entity.id.desc())
    if since is not None:
        query = query.where(or_(Entity.created_at >= since, Entity.updated_at >= since))
    return list(conn.scalars(query))


# --- applying the answers ---


class OnlineSession(IngestSession):
    """``ctx`` for ``Theme.online_details``: values it writes are marked ``fetched``, and
    the pictures it names are kept for fetching (:meth:`online_picture`)."""

    source = FieldSource.FETCHED

    def __init__(
        self, conn: Connection, schema: ThemeSchema, sources: Sequence[OnlineSource] = ()
    ) -> None:
        super().__init__(conn, schema)
        self.sources = tuple(sources)
        """The theme's services: a picture must be on one of them."""
        self.pictures: dict[int, str] = {}
        """The pictures named, by item."""
        self.updated: set[int] = set()
        """Items whose details the theme set: another item it found an identifier for
        (a book's author) is then looked up in the same run (#484)."""

    def update(self, entity: EntityRef, *, title: str | None = None, **fields: Any) -> None:
        super().update(entity, title=title, **fields)
        self.updated.add(entity.id)

    def online_picture(self, entity: EntityRef, url: str) -> None:
        try:
            source_for(url, self.sources)
        except OnlineError as e:
            raise IngestError(str(e)) from None
        self.pictures[entity.id] = url


@dataclass
class Applied:
    items: int = 0
    problems: list[str] = field(default_factory=list)
    updated: set[int] = field(default_factory=set)
    """Items the theme set details on, the looked-up ones included."""
    pictured: set[int] = field(default_factory=set)
    """Items the theme named a picture of: a movie's cast as well as the movie (#487)."""


def name_pictures(conn: Connection, pictures: Mapping[int, str]) -> None:
    """Keep the pictures a theme named (item id -> address): a new address replaces the
    item's picture, to be fetched; the same one keeps what was fetched."""
    for entity_id, url in pictures.items():
        current = conn.scalar(select(OnlinePicture.url).where(OnlinePicture.entity_id == entity_id))
        if current == url:
            continue
        insert = sqlite_insert(OnlinePicture).values(entity_id=entity_id, url=url)
        conn.execute(
            insert.on_conflict_do_update(
                index_elements=["entity_id"],
                set_={"url": url, "status": None, "data": None, "fetched_at": None},
            )
        )


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
            ctx = OnlineSession(conn, schema, theme.online_sources)
            theme.online_details(ref, responses, ctx)
            report = ctx.flush()
            name_pictures(conn, ctx.pictures)
            applied.updated |= ctx.updated
            applied.pictured |= set(ctx.pictures)
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
    pictures: list[int] = field(default_factory=list)
    """Items that got a picture from a service in this run."""
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
    find_by: str = "ids",
    env: Env | None = None,
    fetcher: Fetcher | None = None,
    progress: Progress | None = None,
    stopped: Callable[[], bool] = lambda: False,
) -> LookupReport:
    """Look these items up (runs in a worker): fetch the addresses the theme asks for that
    have no kept answer (all of them, to ``refresh``), then apply each item's answers, a
    batch at a time. Items whose services can't be reached are left for next time."""
    report = LookupReport()
    with reader.connect() as conn:
        plan = plan_lookups(conn, schema, theme, entity_ids, find_by, env)
    report.problems += plan.problems
    if not plan.lookups:
        return report
    fetcher = fetcher or Fetcher(theme.online_sources, stopped=stopped, env=env)
    answers: list[tuple[EntityRef, Mapping[str, OnlineResponse]]] = []
    fresh: list[OnlineResponse] = []
    updated: set[int] = set()
    pictured: set[int] = set()

    def write() -> None:
        if not answers:
            return
        batch, new = list(answers), list(fresh)
        applied = writer.run(lambda conn: apply_details(conn, schema, theme, batch, new))
        report.items += applied.items
        report.problems += applied.problems
        updated.update(applied.updated)
        pictured.update(applied.pictured)
        answers.clear()
        fresh.clear()

    lookups = list(plan.lookups)
    planned = {lookup.entity.id for lookup in lookups}
    asked = {u for lookup in lookups for u in lookup.urls}
    rounds = 1
    total = len(lookups)
    looked_up: list[int] = []
    done = 0
    while True:
        if done == len(lookups):
            write()
            followers = sorted(updated)
            if rounds == ROUNDS or not followers or stopped():
                break
            # Items the details named (a book's author, with the identifier Open Library
            # gave), and items a search found the ID of (#485): looked up now too, once
            # (#484), each for the addresses not asked for yet in this run.
            rounds += 1
            with reader.connect() as conn:
                more = plan_lookups(conn, schema, theme, followers, find_by, env)
            report.problems += [p for p in more.problems if p not in report.problems]
            new = [
                lookup
                for lookup in more.lookups
                if lookup.entity.id not in planned or not asked.issuperset(lookup.urls)
            ]
            planned |= set(followers)
            asked |= {u for lookup in new for u in lookup.urls}
            lookups += new
            total = len(lookups)
            continue
        lookup = lookups[done]
        done += 1
        if stopped():
            break
        responses = dict(lookup.cached)
        missing = lookup.missing(refresh)
        if not all(fetcher.can_fetch(url) for url in missing):
            # On a service the keep hasn't allowed yet: left until it is.
            if progress is not None:
                progress(done, total)
            continue
        try:
            for url in missing:
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
            looked_up.append(lookup.entity.id)
        if len(answers) >= BATCH:
            write()
        if progress is not None:
            progress(done, total)
    write()
    if not stopped():
        # The items looked up, and those their details named pictures of (a movie's cast,
        # #487), which have nothing to look up themselves.
        wanted = list(dict.fromkeys([*looked_up, *sorted(pictured)]))
        fetch_pictures(writer, reader, wanted, report, fetcher, refresh=refresh, stopped=stopped)
    report.fetched = fetcher.fetched
    return report


def fetch_pictures(
    writer: DbWriter,
    reader: Engine,
    entity_ids: Sequence[int],
    report: LookupReport,
    fetcher: Fetcher,
    *,
    refresh: bool = False,
    stopped: Callable[[], bool] = lambda: False,
) -> None:
    """Fetch the pictures named for these items that haven't been (all of them, to
    ``refresh``), from the services the fetcher may use, and keep them (#483)."""
    wanted: list[tuple[int, str]] = []
    with reader.connect() as conn:
        for start in range(0, len(entity_ids), 500):
            rows = conn.execute(
                select(OnlinePicture.entity_id, OnlinePicture.url, OnlinePicture.status).where(
                    OnlinePicture.entity_id.in_(entity_ids[start : start + 500])
                )
            )
            wanted += [(r.entity_id, r.url) for r in rows if refresh or _unfetched(r.status)]
    for entity_id, url in wanted:
        if stopped():
            return
        if not fetcher.can_fetch(url):
            continue  # a service the keep hasn't allowed yet
        try:
            reply = fetcher.fetch_picture(url)
        except UnavailableError as e:
            if str(e) not in report.problems:
                report.problems.append(str(e))
            continue
        ok = 200 <= reply.status < 300 and bool(reply.body)
        if not ok:
            report.not_found += 1
        writer.run(partial(_keep_picture, entity_id, url, reply.status, reply.body if ok else None))
        if ok:
            report.pictures.append(entity_id)


def titles_sent(source: OnlineSource, find_by: str) -> str:
    """What a service declared with ``titles`` may be sent besides its IDs, by ``[online]
    find_by``: ", or titles with their years,"; ", or titles with their authors and
    years," for one that sends authors too (``titles_with``, #627)."""
    if not source.titles or find_by not in ("title-year", "title"):
        return ""
    with_ = [source.titles_with] if source.titles_with else []
    if find_by == "title-year":
        with_.append("years")
    return f", or titles with their {' and '.join(with_)}," if with_ else ", or titles,"


def consent_text(sources: Sequence[OnlineSource], find_by: str = "ids") -> str:
    """What looking up sends, for the consent question and Keep configuration: "Tagalot
    would send DOIs to Crossref and arXiv IDs to arXiv. Nothing else about your files leaves
    this computer." A service that finds items by title, when the keep allows it, is sent
    "TMDb IDs, or titles with their years," too (#485)."""
    parts = [f"{s.sends}{titles_sent(s, find_by)} to {s.name}" for s in sources]
    if not parts:
        return "This keep's theme looks nothing up online."
    sent = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"Tagalot would send {sent}. Nothing else about your files leaves this computer."


def tokens_text(sources: Sequence[OnlineSource], env: Env | None = None) -> str:
    """How to turn on the services whose token isn't set, for the Keep tab (#485, #544):
    "TMDb needs a token: set TAGALOT_TMDB_TOKEN to yours under Environment, below. Until
    then it isn't used." Empty when none is missing."""
    missing = [s for s in sources if s.token_env is not None and token(s, env) is None]
    return "\n".join(
        f"{s.name} needs a token: set {s.token_env} to yours under Environment, below. "
        "Until then it isn't used."
        for s in missing
    )


def needed_env(sources: Sequence[OnlineSource]) -> list[tuple[str, str]]:
    """The variables the services need, with whose: ``[("TAGALOT_TMDB_TOKEN", "TMDb")]``,
    to suggest in the Keep tab's Environment (#544)."""
    found: dict[str, list[str]] = {}
    for source in sources:
        if source.token_env is not None:
            found.setdefault(source.token_env, []).append(source.name)
    return [(name, ", ".join(names)) for name, names in found.items()]


def refused_text(source: OnlineSource, status: int) -> str:
    """What to say when a service refuses Tagalot's requests (401 or 403)."""
    if source.token_env:
        return (
            f"refused the token in {source.token_env} (HTTP {status}): check it in Keep "
            "configuration, Keep tab, Environment"
        )
    return f"refused the request (HTTP {status})"


def _unfetched(status: int | None) -> bool:
    """A picture still to fetch: never asked for, or only redirected, to a service the keep
    didn't allow then (#484): tried again in later runs."""
    return status is None or 300 <= status < 400 or status in REFUSED


def _keep_picture(
    entity_id: int, url: str, status: int, data: bytes | None, conn: Connection
) -> None:
    """In the DB writer: what fetching an item's picture got (unless the theme named
    another meanwhile)."""
    conn.execute(
        update(OnlinePicture)
        .where(OnlinePicture.entity_id == entity_id, OnlinePicture.url == url)
        .values(status=status, data=data, fetched_at=utcnow())
    )
    if data is not None:
        # Forget the file it showed, so the chain is run again: a theme can place the
        # online picture ahead of its own files (OnlineImage, #487).
        conn.execute(update(Entity).where(Entity.id == entity_id).values(thumb_resource_id=None))
