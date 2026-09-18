import tempfile
import unittest
from pathlib import Path

from audit.audit_core import AuditResult, generate_moshell_scripts, _collect_set_rows


class ScriptSafetyTests(unittest.TestCase):
    def test_feature_activation_requires_enabled_license(self):
        row = AuditResult("feature", "BB1", "SystemFunctions=1,Lm=1,FeatureState=CXC1",
                          "featureState", "ACTIVATED (LTE)", "DEACTIVATED / DISABLED",
                          "Mismatch", "feature", "BB1")
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(generate_moshell_scripts([row], folder, "SITE", "audit.xlsx"), [])
            row.feature_activation_allowed = True
            files = generate_moshell_scripts([row], folder, "SITE", "audit.xlsx")
            self.assertIn("featureState 1", Path(files[0]).read_text(encoding="utf-8"))
            row.feature_activation_allowed = False
            row.expected = "DEACTIVATED (Non LTE)"
            files = generate_moshell_scripts([row], folder, "SITE", "audit.xlsx")
            self.assertIn("featureState 0", Path(files[0]).read_text(encoding="utf-8"))

    def test_license_state_never_set(self):
        row = AuditResult("cdd", "BB1", "Lm=1,FeatureState=CXC1", "licenseState",
                          "ENABLED", "DISABLED", "Mismatch", "CDD", "BB1")
        self.assertEqual(_collect_set_rows([row], ("Mismatch",), "SITE"), {})
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(generate_moshell_scripts([row], folder, "SITE", "audit.xlsx"), [])
