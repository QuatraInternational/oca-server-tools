# Copyright 2016-2017 Versada <https://versada.eu/>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import functools
import json
import logging
import os
import statistics
import time
import urllib.parse
import warnings
from array import array
from collections import abc
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

import odoo.http
from odoo.sql_db import Cursor
from odoo.tools import config as odoo_config
from odoo.tools.sql import SQL

from odoo.addons.base.models.ir_cron import ir_cron

from . import const
from .logutils import (
    InvalidGitRepository,
    SanitizeOdooCookiesProcessor,
    fetch_git_sha,
    get_extra_context,
)

_logger = logging.getLogger(__name__)
HAS_SENTRY_SDK = True
try:
    import sentry_sdk
    from sentry_sdk import start_span, start_transaction
    from sentry_sdk.integrations.logging import ignore_logger
    from sentry_sdk.integrations.threading import ThreadingIntegration
    from sentry_sdk.integrations.wsgi import SentryWsgiMiddleware
    from sentry_sdk.scrubber import EventScrubber
    from sentry_sdk.tracing import (
        TRANSACTION_SOURCE_ROUTE,
        TRANSACTION_SOURCE_URL,
    )
except ImportError:  # pragma: no cover
    HAS_SENTRY_SDK = False  # pragma: no cover
    _logger.debug(
        "Cannot import 'sentry-sdk'.\
                        Please make sure it is installed."
    )  # pragma: no cover

TIMEFMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def before_send(event, hint):
    """Prevent the capture of any exceptions in
    the DEFAULT_IGNORED_EXCEPTIONS list
        -- or --
    Add context to event if include_context is True
    and sanitize sensitive data"""

    exc_info = hint.get("exc_info")
    if exc_info is None and "log_record" in hint:
        # Odoo handles UserErrors by logging the raw exception rather
        # than a message string in odoo/http.py
        try:
            module_name = hint["log_record"].msg.__module__
            class_name = hint["log_record"].msg.__class__.__name__
            qualified_name = module_name + "." + class_name
        except AttributeError:
            qualified_name = "not found"

        if qualified_name in const.DEFAULT_IGNORED_EXCEPTIONS:
            return None

        # Check if the logger is muted
        try:
            logger_name = hint["log_record"].name
        except AttributeError:
            logger_name = None

        if logger_name and not logging.getLogger(logger_name).propagate:
            return None

    if event.setdefault("tags", {}).get("include_context"):
        cxtest = get_extra_context(odoo.http.request)
        info_request = ["tags", "user", "extra", "request"]

        for item in info_request:
            info_item = event.setdefault(item, {})
            info_item.update(cxtest.setdefault(item, {}))

    raven_processor = SanitizeOdooCookiesProcessor()
    raven_processor.process(event)

    return event


def get_odoo_commit(odoo_dir):
    """Attempts to get Odoo git commit from :param:`odoo_dir`."""
    if not odoo_dir:
        return
    try:
        return fetch_git_sha(odoo_dir)
    except InvalidGitRepository:
        _logger.debug("Odoo directory: '%s' not a valid git repository", odoo_dir)


def get_config(key, default=None):
    """Get the configuration parameter composed of `sentry_` + key

    Allow to distinguish by environment as indicated by the environment
    variable ODOO_STAGE (available on Odoo.sh).
    """
    stage = os.environ.get("ODOO_STAGE", "nostage")  # either production or staging
    return odoo_config.get(
        f"sentry_{stage}_{key}",
        odoo_config.get(f"sentry_{key}", default),
    )


STATIC_TRANSACTION_NAME = "<static>"
# What SentryWsgiMiddleware opens a transaction with, before anything names it.
DEFAULT_TRANSACTION_NAME = "generic WSGI request"


def name_transaction(name, source):
    """Rename the currently active transaction, if there is one.

    There is none for the request methods SentryWsgiMiddleware deliberately
    skips (OPTIONS, HEAD); those stay untraced rather than getting a
    transaction of their own.
    """
    scope = sentry_sdk.get_current_scope()
    if not scope.transaction:
        return
    scope.set_transaction_name(name, source=source)


def set_event_url(url, event, hint):
    """Report `event` under `url`, the address the client actually asked for.

    SentryWsgiMiddleware reads the environ the moment it wraps the request,
    which is before Odoo hands it to ProxyFix, so the url it reports on its own
    is the one the proxy used internally.
    """
    event.setdefault("request", {})["url"] = url
    return event


def set_request_context(request):
    """Copy the Odoo request context onto the isolation scope (=~trace root)."""
    context = get_extra_context(request)
    if not context:
        return
    scope = sentry_sdk.get_isolation_scope()
    for key, value in context.get("tags", {}).items():
        scope.set_tag(key, value)
    for key, value in context.get("extra", {}).items():
        scope.set_extra(key, value)
    if context.get("user"):
        scope.set_user(context["user"])
    url = context.get("request", {}).get("url")
    if url:
        # SentryWsgiMiddleware forked this scope for this one request, so the
        # processor is gone once the request is.
        scope.add_event_processor(functools.partial(set_event_url, url))


def get_route_name(rule, path):
    """Return the (name, source) a request matching `rule` is reported under.

    Make sure paths like `/web/dataset/call_kw/<path:path>` use the complete url as the
    transaction name, since those contain all info on model etc.
    """
    if "<path:" in rule.rule:
        return path, TRANSACTION_SOURCE_URL
    return rule.rule, TRANSACTION_SOURCE_ROUTE


# HTTP transactions
orig_serve_db = odoo.http.Request._serve_db


def wrapped_serve_db(self, *args, **kwargs):
    # The context has to wait until the session is loaded, add it here.
    set_request_context(self)
    return orig_serve_db(self, *args, **kwargs)


orig_serve_nodb = odoo.http.Request._serve_nodb


def wrapped_serve_nodb(self, *args, **kwargs):
    # The context has to wait until the session is loaded, add it here.
    set_request_context(self)
    return orig_serve_nodb(self, *args, **kwargs)


orig_serve_static = odoo.http.Request._serve_static


def wrapped_serve_static(self, *args, **kwargs):
    # All static files share one name: asset urls are pure cardinality.
    name_transaction(STATIC_TRANSACTION_NAME, TRANSACTION_SOURCE_ROUTE)
    return orig_serve_static(self, *args, **kwargs)


orig_serve_ir_http = odoo.http.Request._serve_ir_http


def wrapped_serve_ir_http(self, rule, args):
    # Refine the url into the route it matched, now that routing succeeded.
    # _transactioning calls this twice on the readonly -> read-write retry;
    # renaming is idempotent.
    name_transaction(*get_route_name(rule, self.httprequest.path))
    return orig_serve_ir_http(self, rule, args)


# Cron transactions
# Patch ir_cron._callback so a Sentry transaction is started for crons
orig_callback = ir_cron._callback


def wrapped_callback(self, cron_name, server_action_id, *args, **kwargs):
    # Crons can be called from xmlrpcs, when that happens, edit the existing transaction
    scope = sentry_sdk.get_current_scope()
    if scope.transaction:
        scope.set_transaction_name(cron_name)
        return orig_callback(
            self,
            cron_name,
            server_action_id,
            *args,
            **kwargs,
        )

    try:
        with start_transaction(op="cron", name=cron_name), collecting_queries():
            return orig_callback(
                self,
                cron_name,
                server_action_id,
                *args,
                **kwargs,
            )
    finally:
        # Send before returning. Covers the case where a cron has timed out (SIGKILL)
        sentry_sdk.flush()


# SQL spans
# Odoo issues lots of short queries per request or cron, so a span per query cannot
# be sent: sentry_sdk keeps the first 1000 spans of a transaction,
# and allows only 1MB data. (413 error if it happens and transaction is lost)
# Collect the queries instead, and turn them into spans once, when the transaction ends.

# Above this many queries the individual ones are given up on and only the
# grouped spans are reported.
MAX_INDIVIDUAL_QUERIES = 500
# Length a query is cropped to when the event does not fit otherwise.
CROPPED_QUERY_LENGTH = 200
# Sentry rejects a single event over 1MB with a 413; stay clear of the edge.
MAX_EVENT_BYTES = 800 * 1024

query_collector = ContextVar("sentry_query_collector", default=None)


def get_query_string(query):
    """Return the sql `query` will run, as it was written."""
    return query.code if isinstance(query, SQL) else str(query)


# Sentry renders a span's own duration in milliseconds, so report ours in them
# too: data reading 0.00409 beside a bar reading 4.09ms is misread every time.
MS_PER_SECOND = 1000


def duration_stats(durations):
    """Return what a group of `durations`, in seconds, is worth reporting as.

    A count and a total say how much a query cost, but not whether the cost is
    the query or one bad run of it: a median far below the maximum is a single
    outlier, and one next to it is every run being that slow. Which of the two
    it is decides whether the query or what it is called on is worth looking at.
    """
    total = sum(durations)
    count = len(durations)
    return {
        "db.query.count": count,
        "db.query.total_duration_ms": total * MS_PER_SECOND,
        "db.query.min_duration_ms": min(durations) * MS_PER_SECOND,
        "db.query.max_duration_ms": max(durations) * MS_PER_SECOND,
        "db.query.avg_duration_ms": total / count * MS_PER_SECOND,
        "db.query.median_duration_ms": statistics.median(durations) * MS_PER_SECOND,
    }


class QueryCollector:
    """Collects the queries of a single transaction.

    Odoo builds its queries with `%s` placeholders rather than inlined values,
    so repeating the same work produces byte-identical sql and grouping it is a
    plain dict lookup.

    Of every run only how long it took is kept, as eight bytes in an array:
    enough to report what the runs of a query cost between them, without the
    text of any but the first of them being held on to.
    """

    __slots__ = ("epoch", "epoch_counter", "groups", "individual", "count")

    def __init__(self):
        self.epoch = datetime.now(timezone.utc)
        # perf_counter, not monotonic: the latter only has a 15ms resolution on
        # Windows, which reads every Odoo query as having taken no time at all.
        self.epoch_counter = time.perf_counter()
        # sql -> [durations, first start, last end]
        self.groups = {}
        # (sql, start, duration), until MAX_INDIVIDUAL_QUERIES is passed
        self.individual = []
        self.count = 0

    def add(self, query, start, duration):
        """Record that `query` ran at `start` (perf_counter) for `duration`.

        The sql is grouped on as it was written, without collapsing whitespace
        first: that runs over every query only to build a dict key, and the same
        Odoo code path always writes its sql the same way anyway.
        """
        sql = get_query_string(query)
        self.count += 1

        group = self.groups.get(sql)
        if group is None:
            self.groups[sql] = [array("d", (duration,)), start, start + duration]
        else:
            group[0].append(duration)
            # Queries run one after the other on a cursor, so this one ends last.
            group[2] = start + duration

        if self.individual is None:
            return
        if self.count > MAX_INDIVIDUAL_QUERIES:
            # Too many to report one by one, so stop paying for the list.
            self.individual = None
        else:
            self.individual.append((sql, start, duration))

    def emit(self):
        """Add the collected queries to the transaction that is still open."""
        if self.individual is not None:
            for sql, start, duration in self.individual:
                self._add_span(sql, start, duration)
            return

        for sql, (durations, first, last) in self.groups.items():
            if len(durations) == 1:
                # It only ran once, so report it as it happened, uncounted.
                self._add_span(sql, first, durations[0])
                continue
            # The group is timed on what its queries took together: the waterfall
            # draws whatever a span does not cover as a gap labelled "no
            # instrumentation", and reading the time a thousand collapsed
            # queries took as time spent outside the database is worse than
            # reading their bar as one stretch. They cannot outlast the window
            # they ran in, but clamp rather than trust the clock for it.
            self._add_span(
                sql,
                first,
                min(sum(durations), last - first),
                stats=duration_stats(durations),
            )

    def _add_span(self, sql, start, duration, stats=None):
        """Add one finished `db.query` span, standing in for the `stats` runs."""
        started_at = self.epoch + timedelta(seconds=start - self.epoch_counter)
        sql = " ".join(sql.split())
        span = start_span(
            op="db.query",
            name=sql if stats is None else f"{stats['db.query.count']}x {sql}",
            start_timestamp=started_at,
        )
        # What the one bar stands in for, since it is no single query.
        for key, value in (stats or {}).items():
            span.set_data(key, value)
        span.finish(end_timestamp=started_at + timedelta(seconds=duration))


@contextmanager
def collecting_queries():
    """Collect queries for the duration of the transaction being opened."""
    collector = QueryCollector()
    token = query_collector.set(collector)
    try:
        yield collector
    finally:
        query_collector.reset(token)
        # Still inside the transaction, so the spans land on it.
        collector.emit()


original_execute = Cursor.execute


def wrapped_execute(self, query, *args, **kwargs):
    # Hand the query to the collector
    collector = query_collector.get()
    if collector is None:
        return original_execute(self, query, *args, **kwargs)

    start = time.perf_counter()
    try:
        return original_execute(self, query, *args, **kwargs)
    finally:
        collector.add(query, start, time.perf_counter() - start)


def install_tracing():
    """Patch the Odoo entry points a Sentry transaction is assembled from.

    For http requests the transaction is opened by SentryWsgiMiddleware, so
    the `_serve_*` patches only name it and add context. Crons run without a
    wsgi request, so those do open one of their own.
    """
    odoo.http.Request._serve_db = wrapped_serve_db
    odoo.http.Request._serve_nodb = wrapped_serve_nodb
    odoo.http.Request._serve_static = wrapped_serve_static
    odoo.http.Request._serve_ir_http = wrapped_serve_ir_http
    ir_cron._callback = wrapped_callback
    if not getattr(Cursor, "_sentry_patched", False):
        Cursor.execute = wrapped_execute
        Cursor._sentry_patched = True


if get_config("traces_enabled", False):
    install_tracing()


def event_size(event):
    """Return how many bytes `event` takes as sent."""
    return len(json.dumps(event, default=str))


def span_duration(span):
    """Return how long `span` took, in seconds."""
    try:
        start = datetime.strptime(span["start_timestamp"], TIMEFMT)
        end = datetime.strptime(span["timestamp"], TIMEFMT)
    except (KeyError, TypeError, ValueError):
        return 0
    return (end - start).total_seconds()


def span_interest(span):
    """Rank spans by how much is lost by dropping them."""
    return (
        span.get("data", {}).get("db.query.count", 1),
        span_duration(span),
    )


def shrink_event(event):
    """Bring `event` under the size Sentry accepts, least destructive first.

    An event over MAX_EVENT_BYTES is rejected with a 413 and lost whole, so
    losing detail here always beats leaving it alone. Cropping the sql keeps
    every span, so that comes first; only if that is not enough are spans
    dropped, the ones standing in for the fewest and fastest queries first.
    """
    if event_size(event) <= MAX_EVENT_BYTES:
        return event

    spans = event.get("spans") or []
    for span in spans:
        description = span.get("description") or ""
        if len(description) > CROPPED_QUERY_LENGTH:
            span["description"] = description[:CROPPED_QUERY_LENGTH]
    if event_size(event) <= MAX_EVENT_BYTES:
        return event

    budget = MAX_EVENT_BYTES - event_size({**event, "spans": []})
    # Ordered by interest, so popping from the end drops the cheapest first.
    kept = sorted(spans, key=span_interest, reverse=True)
    for index, span in enumerate(kept):
        budget -= event_size(span) + 2  # +2 for the ", " that joins it
        if budget < 0:
            del kept[index:]
            break
    while kept and event_size({**event, "spans": kept}) > MAX_EVENT_BYTES:
        kept.pop()

    _logger.warning(
        "Sentry transaction %r is too large, dropping %d of its %d spans",
        event.get("transaction"),
        len(spans) - len(kept),
        len(spans),
    )
    # Back into the order they ran in, so the trace still reads as a waterfall.
    event["spans"] = sorted(kept, key=lambda span: span["start_timestamp"])
    return event


def name_unnamed_transaction(event):
    """Rename `event` after its url if nothing got round to naming it.

    Requests are named on the first line they reach, so a transaction that
    still carries the default arrived through a path that misses it: worth a
    warning, and worth reporting under something that can be told apart from
    every other endpoint in the meantime.
    """
    if event.get("transaction") != DEFAULT_TRANSACTION_NAME:
        return
    url = event.get("request", {}).get("url")
    _logger.warning(
        "Sentry transaction for %s was never named", url or "an unknown url"
    )
    path = urllib.parse.urlsplit(url).path if url else None
    if path:
        event["transaction"] = path
        event["transaction_info"] = {"source": TRANSACTION_SOURCE_URL}


def before_send_transaction(event, hint):
    """Only send transactions that take longer than one second

    The request context is not added here but in `set_request_context`, while
    the request is still bound.
    """
    start = datetime.strptime(event.get("start_timestamp"), TIMEFMT)
    end = datetime.strptime(event.get("timestamp"), TIMEFMT)

    if (end - start).total_seconds() < 1:
        return None
    name_unnamed_transaction(event)
    return shrink_event(event)


def install_wsgi_middleware():
    """Wrap Odoo's WSGI entry point in :class:`SentryWsgiMiddleware`.

    The middleware has to be installed on ``odoo.http.Application.__call__``
    rather than on ``odoo.service.server.server.app``, because server is still ``None``
    at time of patching.
    """
    orig_app_call = odoo.http.Application.__call__
    if getattr(orig_app_call, "_sentry_patched", False):
        return

    def named_app_call(self, environ, start_response):
        # Name the transaction on the very first line of the request, before
        # anything can return without reaching one of the _serve_* methods:
        # a failure in _post_init (session loading) or get_static_file is
        # answered straight from Application.__call__, and used to leave
        # SentryWsgiMiddleware's "generic WSGI request" in place.
        name_transaction(environ.get("PATH_INFO", "/"), TRANSACTION_SOURCE_URL)
        with collecting_queries():
            return orig_app_call(self, environ, start_response)

    def wrapped_app_call(self, environ, start_response):
        # Manually add forwarding header so the host the client entered is reflected
        middleware = SentryWsgiMiddleware(
            functools.partial(named_app_call, self),
            use_x_forwarded_for=odoo_config["proxy_mode"],
        )
        return middleware(environ, start_response)

    wrapped_app_call._sentry_patched = True
    odoo.http.Application.__call__ = wrapped_app_call


def initialize_sentry():
    """Setup an instance of :class:`sentry_sdk.Client`.
    :param config: Sentry configuration
    :param client: class used to instantiate the sentry_sdk client.
    """
    enabled = get_config("enabled", False)
    if not (HAS_SENTRY_SDK and enabled):
        return
    _logger.info("Initializing sentry...")
    if get_config("odoo_dir") and get_config("release"):
        _logger.debug(
            "Both sentry_odoo_dir and \
                       sentry_release defined, choosing sentry_release"
        )
    if get_config("transport"):
        warnings.warn(
            "`sentry_transport` has been deprecated.  "
            "Its not neccesary send it, will use `HttpTranport` by default.",
            DeprecationWarning,
            stacklevel=1,
        )
    options = {}
    for option in const.get_sentry_options():
        value = get_config(option.key, option.default)
        if isinstance(option.converter, abc.Callable):
            value = option.converter(value)
        options[option.key] = value

    exclude_loggers = const.split_multiple(
        get_config("exclude_loggers", const.DEFAULT_EXCLUDE_LOGGERS)
    )

    if not options.get("release"):
        options["release"] = get_config(
            "release", get_odoo_commit(get_config("odoo_dir"))
        )

    # Change name `ignore_exceptions` (with raven)
    # to `ignore_errors' (sentry_sdk)
    options["ignore_errors"] = options["ignore_exceptions"]
    del options["ignore_exceptions"]

    options["before_send"] = before_send

    options["integrations"] = [
        options["logging_level"],
        ThreadingIntegration(propagate_hub=True),
    ]
    # Remove logging_level, since in sentry_sdk is include in 'integrations'
    del options["logging_level"]

    # Quatra Tracing options
    if get_config("traces_enabled", False):
        options["before_send_transaction"] = before_send_transaction

    if get_config("send_client_ip", True):
        # The default scrubber drops `user.ip_address` by name whenever
        # send_default_pii is off, which leaves Sentry geolocating the server
        # that sent the event instead of the client that made the request. Scrub
        # as if pii were on, so the address set in `get_extra_context` survives
        # without cookies and request bodies being sent along with it.
        options["event_scrubber"] = EventScrubber(send_default_pii=True)

    client = sentry_sdk.init(**options)

    sentry_sdk.set_tag("include_context", get_config("include_context", True))

    if exclude_loggers:
        for item in exclude_loggers:
            ignore_logger(item)

    install_wsgi_middleware()

    with sentry_sdk.new_scope() as scope:
        scope.set_extra("debug", False)
        # Quatra: disable welcome message as it is logged 600+ times a day on Odoo.sh
        # sentry_sdk.capture_message("Starting Odoo Server", "info")

    return client


def post_load():
    initialize_sentry()
