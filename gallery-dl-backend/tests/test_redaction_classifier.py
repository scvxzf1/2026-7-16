from __future__ import annotations

import unittest

from gdl_backend.classifier import classify_result
from gdl_backend.redaction import redact_data, redact_text


class RedactionTests(unittest.TestCase):
    def test_redacts_proxy_userinfo_query_and_headers(self):
        text = (
            "proxy=http://user:pass@example.test:8080/a?token=abc&x=1 "
            "Authorization: Bearer xyz Cookie=session123"
        )
        safe = redact_text(text)
        self.assertNotIn("user:pass", safe)
        self.assertNotIn("abc", safe)
        self.assertNotIn("session123", safe)
        self.assertIn("***", safe)

    def test_redacts_nested_secret_keys(self):
        value = redact_data({"token": "abc", "nested": {"password": "p", "ok": "yes"}})
        self.assertEqual(value["token"], "***")
        self.assertEqual(value["nested"]["password"], "***")
        self.assertEqual(value["nested"]["ok"], "yes")

    def test_redacts_eh_temporary_image_tokens(self):
        safe = redact_text("https://host/h/file/keystamp=temporary;fileindex=123;xres=1280/a.webp")
        self.assertNotIn("temporary", safe)
        self.assertNotIn("fileindex=123", safe)
        self.assertIn("keystamp=***", safe)


class ClassifierTests(unittest.TestCase):
    def test_proxy_failure_is_retryable_and_penalizes_node(self):
        result = classify_result(4, "ProxyError: tunnel connection failed")
        self.assertEqual(result.error_class, "proxy_failure")
        self.assertTrue(result.retryable)
        self.assertTrue(result.proxy_fault)

    def test_connection_reset_extraction_exit_is_retried_on_another_node(self):
        result = classify_result(
            4,
            "gallery_dl.exception.HttpError: ConnectionError: "
            "ConnectionResetError(10054, 'connection aborted')",
        )
        self.assertEqual(result.error_class, "proxy_failure")
        self.assertTrue(result.retryable)
        self.assertTrue(result.proxy_fault)

    def test_cloudflare_challenge_rotates_proxy_node(self):
        result = classify_result(
            4,
            "Cloudflare challenge (403 Forbidden) for 'https://x.com/account/access'",
        )
        self.assertEqual(result.error_class, "proxy_failure")
        self.assertTrue(result.retryable)
        self.assertTrue(result.proxy_fault)

    def test_public_gallery_access_denial_rotates_proxy_node(self):
        result = classify_result(
            16,
            "AuthorizationError: Insufficient privileges to access this resource",
        )
        self.assertEqual(result.error_class, "proxy_access_failure")
        self.assertTrue(result.retryable)
        self.assertTrue(result.proxy_fault)

    def test_auth_and_unsupported_are_permanent(self):
        self.assertFalse(classify_result(16, "AuthRequired").retryable)
        self.assertEqual(classify_result(64, "Unsupported URL").error_class, "unsupported_url")

    def test_transient_site_and_success(self):
        self.assertTrue(classify_result(4, "503 Service Unavailable").retryable)
        self.assertEqual(classify_result(0, "").error_class, "success")


if __name__ == "__main__":
    unittest.main()
