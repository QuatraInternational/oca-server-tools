The following additional configuration options can be added to your Odoo configuration
file:

[TABLE]

Other [client arguments](https://docs.sentry.io/platforms/python/configuration/) can be
configured by prepending the argument name with `sentry_` in your Odoo config file.
Currently supported additional client arguments are:
`with_locals, max_breadcrumbs, release, environment, server_name, shutdown_timeout, in_app_include, in_app_exclude, default_integrations, dist, sample_rate, send_default_pii, http_proxy, https_proxy, request_bodies, debug, attach_stacktrace, ca_certs, propagate_traces, traces_sample_rate, auto_enabling_integrations, max_value_length`.

Performance tracing is off unless `sentry_traces_enabled` is set, and reports
transactions that take longer than a second. While it is on, `sentry_send_client_ip` (on
by default) reports the address a request came from. Turning it off leaves Sentry
showing every trace as coming from the server that sent it, because that is the only
address it has left to place on a map.

If you are on Odoo.sh, or you control the environment variables on your Odoo nodes, you
can distinguish between settings for different environments by inserting the environment
in the configuration key. For example:

    sentry_production_enabled = True
    sentry_staging_enabled = False

The value that is inserted in the key is provided by the _ODOO_STAGE_ environment
variable which is set on Odoo.sh to either `production` or `staging` (but you can use
any value). A key without environment part can be used as a fallback.

## Example Odoo configuration

Below is an example of Odoo configuration file with _Odoo Sentry_ options:

    [options]
    sentry_dsn = https://<public_key>:<secret_key>@sentry.example.com/<project id>
    sentry_enabled = true
    sentry_logging_level = warn
    sentry_exclude_loggers = werkzeug
    sentry_ignore_exceptions = odoo.exceptions.AccessDenied,
        odoo.exceptions.AccessError,odoo.exceptions.MissingError,
        odoo.exceptions.RedirectWarning,odoo.exceptions.UserError,
        odoo.exceptions.ValidationError,odoo.exceptions.Warning,
        odoo.exceptions.except_orm
    sentry_include_context = true
    sentry_environment = production
    sentry_release = 1.3.2
    sentry_odoo_dir = /home/odoo/odoo/
