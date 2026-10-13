import unittest

from app import api_items, ingest
from app.crawler import runner
from app.url_secrets import is_secret_key, is_secret_param

LINK = ("https://user:pass@cdn.example.com/report.pdf?X-Amz-Signature=sig123&refresh_token=rt456"
        "&id_token=it789&key=k0&page=2&utm_source=feed")
SECRETS = ("user:pass", "sig123", "rt456", "it789", "k0")


class UrlSecretTests(unittest.TestCase):
    def test_stored_item_urls_drop_credentials_and_tracking(self):
        url = runner._normalize_url(LINK)
        self.assertEqual(url, "https://cdn.example.com/report.pdf?page=2")

    def test_evidence_keeps_parameter_names_but_not_values(self):
        url = ingest._safe_url(LINK)
        for secret in SECRETS:
            self.assertNotIn(secret, url)
        self.assertIn("X-Amz-Signature=%5Bredacted%5D", url)
        self.assertIn("page=2", url)

    def test_the_api_serves_the_same_clean_url(self):
        url = api_items.public_document_url(LINK)
        for secret in SECRETS:
            self.assertNotIn(secret, url)
        self.assertIn("page=2", url)

    def test_query_parameters_and_record_fields_use_the_right_rule(self):
        for name in ("X-Amz-Signature", "refresh_token", "id_token", "api_key", "X-Api-Key", "client_secret"):
            self.assertTrue(is_secret_param(name) and is_secret_key(name), name)
        for name in ("key", "sign", "auth"):  # credentials in a URL, ordinary fields in a record
            self.assertTrue(is_secret_param(name), name)
            self.assertFalse(is_secret_key(name), name)
        for name in ("page", "id", "keyword", "signal"):
            self.assertFalse(is_secret_param(name), name)

    def test_record_fields_named_key_keep_their_values(self):
        # Stored evidence must hash as before: a source record's "key" field is data.
        cleaned = ingest._safe_value({"key": "AAPL", "sign": "+", "access_token": "t1"})
        self.assertEqual(cleaned, {"access_token": "[redacted]", "key": "AAPL", "sign": "+"})


if __name__ == "__main__":
    unittest.main()
