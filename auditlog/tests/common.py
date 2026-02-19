# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo.tests.common import SETATTR_SOURCES

from odoo.addons.base.tests.common import BaseCommon


class AuditLogRuleCommon(BaseCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.models = set()
        # Register rule.py as a known path in odoo.tests.common for patching methods
        cls.setattr_sources = SETATTR_SOURCES.get("_patch_method")
        SETATTR_SOURCES["_patch_method"] = "/auditlog/models/rule.py"

    @classmethod
    def create_rule(cls, vals):
        # Deprecated, just call `create` in your test setup.
        return cls.env["auditlog.rule"].create(vals)

    def tearDown(self):
        # Unsubscribe all rules in tearDown to prevent Odoo's patch checker in
        # tests/common.py from ringing the alarm.
        for rule in self.env["auditlog.rule"].search([]):
            try:
                rule.unsubscribe()
            except KeyError:  # pragma: no cover
                continue  # Model not loaded yet
        return super().tearDown()

    @classmethod
    def tearDownClass(cls):
        # Deregister from Odoo's patch checker
        if cls.setattr_sources:
            SETATTR_SOURCES["_patch_method"] = cls.setattr_sources
        else:
            del SETATTR_SOURCES["_patch_method"]
        super().tearDownClass()
