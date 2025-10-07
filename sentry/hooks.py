# Copyright 2016-2017 Versada <https://versada.eu/>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
import os
import warnings
from datetime import datetime
from collections import abc

import odoo.http
from odoo.addons.base.models.ir_cron import ir_cron
from odoo.service.server import server
from odoo.tools import config as odoo_config
from odoo.tools.sql import SQL
from odoo.sql_db import Cursor


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
except ImportError:  # pragma: no cover
    HAS_SENTRY_SDK = False  # pragma: no cover
    _logger.debug(
        "Cannot import 'sentry-sdk'.\
                        Please make sure it is installed."
    )  # pragma: no cover

TIMEFMT = "%Y-%m-%dT%H:%M:%S.%fZ"

# HTTP transactions
# Patch _serve_db so a Sentry transaction is started for http requests
orig_serve_db = odoo.http.Request._serve_db

def wrapped_serve_db(self, *args, **kwargs):
    with start_transaction(op="http.server", name=self.httprequest.path):
        return orig_serve_db(self, *args, **kwargs)


odoo.http.Request._serve_db = wrapped_serve_db

# Cron transactions
# Patch ir_cron._callback so a Sentry transaction is started for crons
orig_callback = ir_cron._callback

def wrapped_callback(self, cron_name, server_action_id, *args, **kwargs):
    # Crons can be called from xmlrpcs, when that happens, edit the existing transaction
    scope = sentry_sdk.get_current_scope()
    if scope.transaction:
        scope.set_transaction_name(cron_name)
        return orig_callback(self, cron_name=cron_name, server_action_id=server_action_id, *args, **kwargs)

    with start_transaction(op="cron", name=cron_name):
        return orig_callback(self, cron_name=cron_name, server_action_id=server_action_id, *args, **kwargs)

ir_cron._callback = wrapped_callback

# SQL spans
# Patch cr.execute so all queries are added to the current transaction as a span
if not getattr(Cursor, "_sentry_patched", False):
    original_execute = Cursor.execute

    def wrapped_execute(self, query, *args, **kwargs):
        if isinstance(query, SQL):
            query_string = query.code
        else:
            query_string = str(query)
        with start_span(op="db.query", name=query_string.replace("\n","")):
            return original_execute(self, query, *args, **kwargs)

    Cursor.execute = wrapped_execute
    Cursor._sentry_patched = True

def before_send_transaction(event, hint):
    """Only send transactions that take longer than one second"""
    cxtest = get_extra_context(odoo.http.request)
    info_request = ["tags", "user", "extra", "request"]

    for item in info_request:
        info_item = event.setdefault(item, {})
        info_item.update(cxtest.setdefault(item, {}))

    start = datetime.strptime(event.get("start_timestamp"), TIMEFMT)
    end = datetime.strptime(event.get("timestamp"), TIMEFMT)

    if (end - start).total_seconds() >= 1:
        return event
    return None

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
    options["before_send_transaction"] = before_send_transaction

    client = sentry_sdk.init(**options)

    sentry_sdk.set_tag("include_context", get_config("include_context", True))

    if exclude_loggers:
        for item in exclude_loggers:
            ignore_logger(item)

    # The server app is already registered so patch it here
    if server:
        server.app = SentryWsgiMiddleware(server.app)

    # Patch the wsgi server in case of further registration
    odoo.http.Application = SentryWsgiMiddleware(odoo.http.Application)

    with sentry_sdk.new_scope() as scope:
        scope.set_extra("debug", False)
        # Quatra: disable welcome message as it is logged 600+ times a day on Odoo.sh
        # sentry_sdk.capture_message("Starting Odoo Server", "info")

    return client


def post_load():
    initialize_sentry()
