import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.security_utils import fetch_html, make_session, resolve_document_url, valid_document_url
from src.validate_artifacts import load, validate
from src.uta_parser import _extract_pdf_links
from src.pvta_parser import parse_schedules_page


def sample(agency="UTA"):
    return {"agency_id": agency, "schema_version": "1.0", "generated_at": "2026-10-28T00:00:00+00:00", "routes": {"1": {"route_short_name": "1", "route_long_name": "", "route_color": None, "schedule_pdf_url": "https://example.org/1.pdf", "map_pdf_url": None}}, "groupings": {"vehicle_type" if agency == "UTA" else "region": [{"group_id": "bus", "name": "Bus", "route_ids": ["1"]}]}}


def response(status=200, headers=None, body=b"ok"):
    result = Mock(status_code=status, is_redirect=status in (301, 302, 303, 307, 308), headers=headers or {}, encoding="utf-8")
    result.raw.read1 = Mock(side_effect=[body, b""])
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class HTMLTests(unittest.TestCase):
    def test_session_has_no_environment_credentials(self):
        self.assertFalse(make_session({}).trust_env)

    def test_url_policy_and_links(self):
        for bad in ("javascript:foo.pdf", "data:foo.pdf", "https://user:pw@example.com/a.pdf", "https://a/x%0a.pdf", "https://a/\nfoo.pdf", "file:///a.pdf", "https://a:bad/a.pdf", "https://-bad.example/a.pdf", "https://a/%zz.pdf"):
            with self.subTest(bad=bad):
                self.assertFalse(valid_document_url(bad))
                self.assertIsNone(resolve_document_url("https://a/", bad))
        self.assertTrue(valid_document_url("http://external.example/a.pdf"))
        self.assertEqual(_extract_pdf_links('<a href="https://u:p@host/a.pdf">schedule</a><a href="http://external.example/b.pdf">schedule</a>', "https://a/"), ("http://external.example/b.pdf", None))
        html = '<article id="document-center"><ul class="file-group"><a href="https://u:p@host/Rt39.pdf">39</a><a href="https://external.example/Rt39.pdf">39</a></ul></article>'
        self.assertEqual(parse_schedules_page(html)["39"], "https://external.example/Rt39.pdf")

    def test_redirect_https_and_reject_downgrade(self):
        session = Mock(); session.get.side_effect = [response(302, {"Location": "/new"}), response(body=b"hello")]
        self.assertEqual(fetch_html(session, "https://example.com/start"), "hello")
        self.assertEqual(session.get.call_args_list[1].args[0], "https://example.com/new")
        session = Mock(); session.get.return_value = response(302, {"Location": "http://example.com/"})
        with self.assertRaises(ValueError): fetch_html(session, "https://example.com/")
        self.assertEqual(session.get.call_count, 1)

    def test_rejects_oversized_and_deadline_and_redirect_loop(self):
        for resp in (response(headers={"Content-Length": "5"}), response(body=b"abcde")):
            session = Mock(); session.get.return_value = resp
            with patch("src.security_utils.MAX_HTML_BYTES", 4), self.assertRaises(ValueError): fetch_html(session, "https://example.com/")
        session = Mock(); session.get.return_value = response(302, {"Location": "/again"})
        with self.assertRaises(ValueError): fetch_html(session, "https://example.com/")
        session = Mock(); session.get.return_value = response()
        with patch("src.security_utils.time.monotonic", side_effect=[0, 31]), self.assertRaises(ValueError): fetch_html(session, "https://example.com/")
        session = Mock(); session.get.return_value = response(body=b"slow")
        with patch("src.security_utils.time.monotonic", side_effect=[0, 0, 0, 31]), self.assertRaises(ValueError): fetch_html(session, "https://example.com/")


class ArtifactTests(unittest.TestCase):
    def test_valid_and_nullable(self):
        validate(sample(), "UTA"); validate(sample("PVTA"), "PVTA")

    def test_rejects_invalid_envelopes_and_routes(self):
        cases = []
        for field, value in (("agency_id", "PVTA"), ("schema_version", "2.0"), ("generated_at", "2026-01-01"), ("routes", {}), ("groupings", {})):
            data = sample(); data[field] = value; cases.append(data)
        for field, value in (("route_color", "red"), ("route_short_name", ""), ("schedule_pdf_url", "javascript:bad"), ("map_pdf_url", "https://u:p@evil/a")):
            data = sample(); data["routes"]["1"][field] = value; cases.append(data)
        data = sample(); data["groupings"]["vehicle_type"][0]["route_ids"] = ["missing"]; cases.append(data)
        data = sample(); data["routes"]["1"]["schedule_pdf_url"] = None; cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError): validate(data, "UTA")

    def test_rejects_duplicate_json_and_large_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "data.json"; path.write_text('{"x":1,"x":2}')
            with self.assertRaises(ValueError): load(path)
            path.write_bytes(b"12345")
            with patch("src.validate_artifacts.MAX_FILE_BYTES", 4), self.assertRaises(ValueError): load(path)


if __name__ == "__main__": unittest.main()
