from datetime import datetime, timedelta
from unittest.mock import patch

import sentry_sdk
from sentry_sdk.transport import Transport
from werkzeug.routing import Rule

import odoo.http
from odoo.sql_db import Cursor
from odoo.tests import HttpCase, tagged
from odoo.tools import config

from odoo.addons.base.models.ir_cron import ir_cron

from .. import hooks
from ..hooks import TIMEFMT

DSN = "http://public:secret@example.com/1"

# The route /web/image/res.partner/1/avatar_128 is dispatched to, see
# web/controllers/binary.py. It cannot be looked up from the routing map here,
# because building that map needs a bound request.
IMAGE_ROUTE = "/web/image/<string:model>/<int:id>/<string:field>"


class TransactionTransport(Transport):
    """A `sentry_sdk` transport that keeps transaction events in a list.

    `InMemoryTransport` in `test_client` cannot be reused: it reads envelopes
    with `Envelope.get_event()`, which only ever returns error events and
    returns None for a transaction.
    """

    def __init__(self, *args, **kwargs):
        self.transactions = []

    def capture_envelope(self, envelope, *args, **kwargs):
        event = envelope.get_transaction_event()
        if event is not None:
            self.transactions.append(event)

    def names(self):
        """Return the transaction name of every event captured so far."""
        return [event["transaction"] for event in self.transactions]

    def flush(self, *args, **kwargs):
        pass

    def kill(self, *args, **kwargs):
        pass


@tagged("post_install", "-at_install")
class TestTracing(HttpCase):
    """Every http request must yield exactly one Sentry transaction.

    The regression covered here is `_serve_db` opening a *second* transaction
    on top of the one SentryWsgiMiddleware already started: `start_transaction`
    never adopts the active span as a parent, so the two ended up as unrelated
    root transactions in separate traces, with every SQL span on the inner one.
    Whenever that inner one was dropped, all Sentry had left was a nameless
    "generic WSGI request".
    """

    def setUp(self):
        super().setUp()
        self._patch_config(
            {
                "sentry_enabled": True,
                "sentry_dsn": DSN,
                "sentry_traces_enabled": True,
                "sentry_traces_sample_rate": 1.0,
            }
        )

        # traces_enabled was False when hooks.py was imported, so the tracing
        # patches still have to be applied. The cron patch is left alone: it is
        # not what these tests cover, and patching a model class trips Odoo's
        # setattr audit.
        request_cls = odoo.http.Request
        self.patch(request_cls, "_serve_db", hooks.wrapped_serve_db)
        self.patch(request_cls, "_serve_nodb", hooks.wrapped_serve_nodb)
        self.patch(request_cls, "_serve_static", hooks.wrapped_serve_static)
        self.patch(request_cls, "_serve_ir_http", hooks.wrapped_serve_ir_http)
        self.patch(Cursor, "execute", hooks.wrapped_execute)
        self._install_wsgi_middleware()

        previous_client = sentry_sdk.get_client()
        self.addCleanup(sentry_sdk.get_global_scope().set_client, previous_client)
        self.client = hooks.initialize_sentry()._client
        self.transport = TransactionTransport()
        self.client.transport = self.transport

    def _patch_config(self, options):
        """Patch Odoo's config, undoing it when the test completes."""
        config_patcher = patch.dict(in_dict=config.options, values=options)
        config_patcher.start()
        self.addCleanup(config_patcher.stop)

    def _install_wsgi_middleware(self):
        """Wrap the running test server's wsgi app, unwrapping it afterwards."""
        application_cls = odoo.http.Application
        self.addCleanup(setattr, application_cls, "__call__", application_cls.__call__)
        hooks.install_wsgi_middleware()

    def _send_everything(self):
        """Stop dropping sub-second transactions for the rest of the test."""
        self.client.options["before_send_transaction"] = None

    def test_http_request_yields_one_named_transaction(self):
        # Requests faster than a second are dropped by the duration filter.
        self.url_open("/web/login")
        self.assertFalse(
            self.transport.names(),
            "A request faster than a second should not be reported",
        )

        # With the filter out of the way, a request is exactly one transaction,
        # named after the route it matched -- not "generic WSGI request".
        self._send_everything()
        self.url_open("/web/login")
        self.assertEqual(self.transport.names(), ["/web/login"])

        event = self.transport.transactions[0]
        self.assertEqual(event["transaction_info"]["source"], "route")
        self.assertEqual(event["contexts"]["trace"]["op"], "http.server")

        # The queries of the request it is named after belong to it.
        self.assertTrue(
            [span for span in event["spans"] if span["op"] == "db.query"],
            "The transaction carries no SQL spans",
        )

        # Context that used to be collected too late to ever be included.
        self.assertEqual(event["tags"]["database"], self.env.cr.dbname)

        # A url carrying a record id is reported under its route, so that
        # Sentry aggregates the endpoint instead of listing one transaction per
        # record.
        self.transport.transactions.clear()
        self.url_open("/web/image/res.partner/1/avatar_128")
        self.assertEqual(self.transport.names(), [IMAGE_ROUTE])

    def test_a_proxied_request_is_reported_under_the_public_host(self):
        """Behind a proxy the url and the ip have to be the client's.

        SentryWsgiMiddleware reads the environ before Odoo runs it through
        ProxyFix, so on its own it reports the address of the proxy: the host
        the request was forwarded to, and an ip that puts every user of the
        system in the datacenter the server happens to run in.
        """
        # Serve one unproxied request first: whatever the middleware reads out
        # of the configuration, it has to read per request, not once per process.
        self.url_open("/web/login")
        self._patch_config({"proxy_mode": True})
        self._send_everything()
        self.url_open(
            "/web/login",
            headers={
                "X-Forwarded-Host": "erp.example.com",
                "X-Forwarded-For": "91.235.85.45",
            },
        )

        event = self.transport.transactions[0]
        self.assertEqual(event["request"]["url"], "http://erp.example.com/web/login")
        # Kept out of the hands of the event scrubber, which drops an ip address
        # by name as long as send_default_pii is off.
        self.assertEqual(event["user"]["ip_address"], "91.235.85.45")

    def test_static_requests_share_one_transaction_name(self):
        self._send_everything()
        self.url_open("/web/static/img/favicon.ico")
        self.assertEqual(self.transport.names(), [hooks.STATIC_TRANSACTION_NAME])

    def test_repeated_queries_collapse_into_one_counted_span(self):
        """Past MAX_INDIVIDUAL_QUERIES only one span per distinct query is kept.

        Odoo parameterizes its sql, so a thousand searches for a stop are byte
        identical and collapse onto a single span carrying the count.
        """
        # What a group is reported as, on durations that are known rather than
        # measured. The median is what tells the two cases that matter apart:
        # one bad run of a fine query, or a query that is slow every time.
        # Timed in seconds, reported in the milliseconds Sentry draws the bar in.
        stats = hooks.duration_stats([0.001, 0.002, 0.003, 0.100])
        self.assertEqual(stats["db.query.count"], 4)
        self.assertAlmostEqual(stats["db.query.total_duration_ms"], 106)
        self.assertAlmostEqual(stats["db.query.min_duration_ms"], 1)
        self.assertAlmostEqual(stats["db.query.max_duration_ms"], 100)
        self.assertAlmostEqual(stats["db.query.avg_duration_ms"], 26.5)
        # Halfway between the two middle runs, where the outlier cannot drag it.
        self.assertAlmostEqual(stats["db.query.median_duration_ms"], 2.5)

        self._send_everything()
        self.patch(hooks, "MAX_INDIVIDUAL_QUERIES", 5)
        with (
            sentry_sdk.start_transaction(op="cron", name="many queries"),
            hooks.collecting_queries(),
        ):
            for _ in range(20):
                self.env.cr.execute("SELECT id FROM res_partner LIMIT 1")
            self.env.cr.execute("SELECT id FROM res_users LIMIT 1")

        event = self.transport.transactions[0]
        spans = [span for span in event["spans"] if span["op"] == "db.query"]
        self.assertEqual(len(spans), 2, "The two distinct queries collapsed wrongly")

        partner = next(s for s in spans if "res_partner" in s["description"])
        self.assertEqual(partner["data"]["db.query.count"], 20)
        self.assertTrue(partner["description"].startswith("20x SELECT"))
        # The one span is timed on what the twenty took together, starting where
        # the first of them did: time it does not cover is drawn as a gap, and a
        # gap says the transaction was busy somewhere other than the database.
        data = partner["data"]
        self.assertAlmostEqual(
            hooks.span_duration(partner) * hooks.MS_PER_SECOND,
            data["db.query.total_duration_ms"],
            places=2,
        )
        self.assertGreaterEqual(
            data["db.query.total_duration_ms"], data["db.query.max_duration_ms"]
        )
        # The same spread, now over runs that were really timed.
        self.assertLessEqual(
            data["db.query.min_duration_ms"], data["db.query.median_duration_ms"]
        )
        self.assertLessEqual(
            data["db.query.median_duration_ms"], data["db.query.max_duration_ms"]
        )
        self.assertAlmostEqual(
            data["db.query.avg_duration_ms"], data["db.query.total_duration_ms"] / 20
        )
        # A query that only ran once is reported as it happened, uncounted.
        users = next(s for s in spans if "res_users" in s["description"])
        self.assertNotIn("db.query.count", users["data"])
        self.assertTrue(users["description"].startswith("SELECT"))
        # The twenty ran before it, so that is where their span has to start.
        self.assertLess(partner["start_timestamp"], users["start_timestamp"])

    def test_an_oversized_event_crops_before_it_drops(self):
        base = datetime(2026, 8, 19, 9, 0, 0)
        query = "SELECT " + ", ".join(f'"stop"."column_{i}"' for i in range(200))

        def build(span_count, counted=0):
            spans = [
                {
                    "op": "db.query",
                    "description": query,
                    "start_timestamp": (base + timedelta(seconds=i)).strftime(TIMEFMT),
                    "timestamp": (
                        base + timedelta(seconds=i, microseconds=900)
                    ).strftime(TIMEFMT),
                    **({"data": {"db.query.count": 500}} if i < counted else {}),
                }
                for i in range(span_count)
            ]
            return {
                "transaction": "/web/dataset/call_kw/quatra_dispatching.stop/web_read",
                "start_timestamp": base.strftime(TIMEFMT),
                "timestamp": (base + timedelta(minutes=1)).strftime(TIMEFMT),
                "spans": spans,
            }

        # Cropping the sql is enough here, so every span survives.
        event = build(1000)
        self.assertGreater(hooks.event_size(event), hooks.MAX_EVENT_BYTES)
        shrunk = hooks.shrink_event(event)
        self.assertLessEqual(hooks.event_size(shrunk), hooks.MAX_EVENT_BYTES)
        self.assertEqual(len(shrunk["spans"]), 1000)
        self.assertLessEqual(
            max(len(span["description"]) for span in shrunk["spans"]),
            hooks.CROPPED_QUERY_LENGTH,
        )

        # Too many to fit even cropped, so the spans standing in for the most
        # queries are the ones kept, still in the order they ran in.
        event = build(4000, counted=10)
        with self.assertLogs(hooks.__name__, "WARNING") as logs:
            shrunk = hooks.shrink_event(event)
        self.assertIn("dropping", logs.output[0])
        self.assertLessEqual(hooks.event_size(shrunk), hooks.MAX_EVENT_BYTES)
        self.assertLess(len(shrunk["spans"]), 4000)
        self.assertEqual(
            len([s for s in shrunk["spans"] if "db.query.count" in s.get("data", {})]),
            10,
            "The counted spans should have been kept",
        )
        self.assertEqual(
            shrunk["spans"],
            sorted(shrunk["spans"], key=lambda span: span["start_timestamp"]),
        )

    def test_an_unnamed_transaction_is_reported_under_its_url(self):
        """Nothing reaches Sentry under the name the sdk opens a request with.

        Requests are named on the first line they reach, so a transaction that
        still carries the default arrived through a path that misses it: worth a
        warning, and worth reporting under something that can be told apart from
        every other endpoint in the meantime.
        """
        base = datetime(2026, 8, 19, 9, 0, 0)
        event = {
            "transaction": hooks.DEFAULT_TRANSACTION_NAME,
            "start_timestamp": base.strftime(TIMEFMT),
            "timestamp": (base + timedelta(seconds=2)).strftime(TIMEFMT),
            "request": {"url": "https://erp.example.com/blog"},
            "spans": [],
        }
        with self.assertLogs(hooks.__name__, "WARNING") as logs:
            sent = hooks.before_send_transaction(event, None)
        self.assertIn("never named", logs.output[0])
        self.assertEqual(sent["transaction"], "/blog")
        self.assertEqual(sent["transaction_info"]["source"], "url")

    def test_path_converter_routes_keep_their_url(self):
        """A <path:...> route covers unrelated operations, so it keeps the url.

        /web/dataset/call_kw/<path:path> is every model and every method in the
        database, so grouping on it says nothing at all.
        """
        self.assertEqual(
            hooks.get_route_name(
                Rule("/web/dataset/call_kw/<path:path>"),
                "/web/dataset/call_kw/purchase.order/web_read",
            ),
            ("/web/dataset/call_kw/purchase.order/web_read", "url"),
        )
        # Whereas a route whose parameters are record data does group.
        self.assertEqual(
            hooks.get_route_name(
                Rule(IMAGE_ROUTE), "/web/image/res.partner/1/avatar_128"
            ),
            (IMAGE_ROUTE, "route"),
        )

    def test_cron_yields_a_transaction_named_after_the_job(self):
        self._send_everything()
        self.patch(ir_cron, "_callback", hooks.wrapped_callback)
        cron = self.env["ir.cron"].create(
            {
                "name": "Sentry tracing test cron",
                "model_id": self.env.ref("base.model_res_partner").id,
                "state": "code",
                "code": "env['res.partner'].search_count([])",
                "interval_number": 1,
                "interval_type": "days",
                "active": False,
            }
        )
        cron._callback(cron.name, cron.ir_actions_server_id.id)

        self.assertEqual(self.transport.names(), [cron.name])
        event = self.transport.transactions[0]
        self.assertEqual(event["contexts"]["trace"]["op"], "cron")
        self.assertTrue(
            [span for span in event["spans"] if span["op"] == "db.query"],
            "The cron transaction carries no SQL spans",
        )
        # No client made this happen, so the server is the right place for it.
        self.assertIsNone(event.get("user"))
