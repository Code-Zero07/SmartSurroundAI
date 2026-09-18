"""
tests/test_driver_contract.py
-----------------------------
Resend/smtplib driver contract tests (the old threshold auto-send workflow
was removed with the corroboration redesign; driver safety rules remain).

Run:  python -m unittest discover -s tests -p "test_driver_contract.py" -v
"""

import os
import sys
import tempfile
import types
import unittest

import email_driver


class _StubResendEmail:
    def __init__(self):
        self.sent = []

    def send(self, params):
        self.sent.append(params)
        return {"id": "stub-id"}


class _StubResendError(Exception):
    pass


def _make_resend_stub(fail_with=None):
    resend = types.ModuleType("resend")
    resend.api_key = ""
    emails = _StubResendEmail()
    if fail_with is None:
        resend.Emails = emails
    else:
        def _send(params):
            raise fail_with
        resend.Emails = types.SimpleNamespace(send=_send)
    exceptions = types.ModuleType("resend.exceptions")
    exceptions.ResendError = _StubResendError
    sys.modules["resend.exceptions"] = exceptions
    return resend, emails


class DriverContractTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ss_driver_")
        self._orig_get_driver = email_driver.get_driver
        self._orig_test_mode = email_driver.TEST_MODE
        self._orig_driver = email_driver.DRIVER
        self._orig_resend_key = email_driver.RESEND_API_KEY
        self._orig_resend_from = email_driver.RESEND_FROM_EMAIL
        self._orig_resend = sys.modules.get("resend")
        self._orig_resend_exc = sys.modules.get("resend.exceptions")

    def tearDown(self):
        email_driver.get_driver = self._orig_get_driver
        email_driver.TEST_MODE = self._orig_test_mode
        email_driver.DRIVER = self._orig_driver
        email_driver.RESEND_API_KEY = self._orig_resend_key
        email_driver.RESEND_FROM_EMAIL = self._orig_resend_from
        if self._orig_resend is None:
            sys.modules.pop("resend", None)
        else:
            sys.modules["resend"] = self._orig_resend
        if self._orig_resend_exc is None:
            sys.modules.pop("resend.exceptions", None)
        else:
            sys.modules["resend.exceptions"] = self._orig_resend_exc

    def test_test_recipient_is_gmail(self):
        self.assertTrue(
            email_driver.TEST_RECIPIENT.endswith("@gmail.com"),
            f"got {email_driver.TEST_RECIPIENT!r}",
        )

    def test_resend_driver_selects_and_posts_params(self):
        email_driver.TEST_MODE = False
        email_driver.DRIVER = "resend"
        email_driver.RESEND_API_KEY = "re_test_key"
        email_driver.RESEND_FROM_EMAIL = "SmartSurround <notices@example.com>"
        resend, emails = _make_resend_stub()
        sys.modules["resend"] = resend

        driver = email_driver.get_driver()
        self.assertIsInstance(driver, email_driver._ResendDriver)
        result = driver.send("subject", "case@authority.gov", "body", None)
        self.assertTrue(result["ok"], msg=result)
        params = emails.sent[0]
        self.assertEqual(params["from"], "SmartSurround <notices@example.com>")
        self.assertEqual(params["to"], ["case@authority.gov"])
        self.assertEqual(params["subject"], "subject")
        self.assertEqual(params["text"], "body")

    def test_resend_driver_attaches_pdf(self):
        email_driver.TEST_MODE = False
        email_driver.DRIVER = "resend"
        email_driver.RESEND_API_KEY = "re_test_key"
        email_driver.RESEND_FROM_EMAIL = "notices@example.com"
        resend, emails = _make_resend_stub()
        sys.modules["resend"] = resend

        letter = os.path.join(self._tmp, "letter.pdf")
        with open(letter, "wb") as fh:
            fh.write(b"%PDF-1.4 test-kb")

        result = email_driver.get_driver().send("s", "case@authority.gov", "b", letter)
        self.assertTrue(result["ok"], msg=result)
        params = emails.sent[0]
        self.assertEqual(params["attachments"][0]["filename"], "letter.pdf")
        self.assertEqual(params["attachments"][0]["content"], list(b"%PDF-1.4 test-kb"))

    def test_resend_missing_config_fails_soft(self):
        email_driver.TEST_MODE = False
        email_driver.DRIVER = "resend"
        email_driver.RESEND_API_KEY = ""
        email_driver.RESEND_FROM_EMAIL = ""
        result = email_driver.get_driver().send("s", "case@authority.gov", "b", None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "missing_resend_config")

    def test_resend_api_error_fails_soft(self):
        email_driver.TEST_MODE = False
        email_driver.DRIVER = "resend"
        email_driver.RESEND_API_KEY = "re_test_key"
        email_driver.RESEND_FROM_EMAIL = "notices@example.com"
        resend, _emails = _make_resend_stub(fail_with=_StubResendError("boom"))
        sys.modules["resend"] = resend
        result = email_driver.get_driver().send("s", "case@authority.gov", "b", None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "resend_error:_StubResendError")


if __name__ == "__main__":
    unittest.main()