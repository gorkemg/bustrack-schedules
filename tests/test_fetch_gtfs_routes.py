import io
import os
import requests
import stat
import sys
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import fetch_gtfs_routes as fetch


def archive(entries):
    data = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(data, "w") as z:
            for name, content in entries:
                z.writestr(name, content)
    return data.getvalue()


class FetchTests(unittest.TestCase):
    def test_extracts_only_root_routes(self):
        data = archive([("../routes.txt", "bad"), ("routes.txt", "good")])
        self.assertEqual(fetch.extract_routes(data), b"good")

    def test_rejects_missing_or_duplicate_routes(self):
        for entries in [[("../routes.txt", "bad")], [("routes.txt", "a"), ("routes.txt", "b")]]:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                fetch.extract_routes(archive(entries))

    def test_rejects_unsupported_compression_and_special_files(self):
        for compression in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w", compression=compression) as compressed_archive:
                compressed_archive.writestr("routes.txt", "route_id\n1\n")
            with self.subTest(compression=compression), self.assertRaises(ValueError):
                fetch.extract_routes(data.getvalue())
        special = zipfile.ZipInfo("routes.txt")
        special.create_system = 3
        special.external_attr = (stat.S_IFIFO | 0o600) << 16
        with self.assertRaises(ValueError):
            fetch.extract_routes(archive([(special, "data")]))

    def test_rejects_symlink_and_oversize(self):
        link = zipfile.ZipInfo("routes.txt")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(ValueError):
            fetch.extract_routes(archive([(link, "target")]))
        with patch.object(fetch, "MAX_ROUTES", 2), self.assertRaises(ValueError):
            fetch.extract_routes(archive([("routes.txt", "abc")]))

    def test_rejects_insecure_redirect(self):
        response = Mock(is_redirect=True, headers={"Location": "http://example.com/feed"})
        session = Mock()
        session.get.return_value.__enter__ = Mock(return_value=response)
        session.get.return_value.__exit__ = Mock(return_value=False)
        with self.assertRaises(ValueError):
            fetch.download("https://example.com/feed", session)
        self.assertEqual(session.get.call_count, 1)

    def test_rejects_invalid_and_corrupt_zip(self):
        with self.assertRaises(zipfile.BadZipFile):
            fetch.extract_routes(b"not a zip")
        data = bytearray(archive([("routes.txt", "content")]))
        offset = data.find(b"content")
        self.assertNotEqual(offset, -1)
        data[offset] ^= 1
        with self.assertRaises(zipfile.BadZipFile):
            fetch.extract_routes(data)

    def test_rejects_oversize_content_length_before_reading(self):
        response = Mock(is_redirect=False, status_code=200, headers={"Content-Length": "3"})
        session = Mock()
        session.get.return_value.__enter__ = Mock(return_value=response)
        session.get.return_value.__exit__ = Mock(return_value=False)
        with patch.object(fetch, "MAX_ARCHIVE", 2), self.assertRaises(ValueError):
            fetch.download("https://example.com/feed", session)
        response.raw.read1.assert_not_called()

    def test_accepts_https_redirect(self):
        redirect = Mock(is_redirect=True, headers={"Location": "/new"})
        result = Mock(is_redirect=False, status_code=200, headers={})
        result.raw.read1.side_effect = [b"zip", b""]
        first, second = Mock(), Mock()
        first.__enter__ = Mock(return_value=redirect)
        first.__exit__ = Mock(return_value=False)
        second.__enter__ = Mock(return_value=result)
        second.__exit__ = Mock(return_value=False)
        session = Mock()
        session.get.side_effect = [first, second]
        self.assertEqual(fetch.download("https://example.com/feed", session), b"zip")
        self.assertEqual(session.get.call_args_list[1].args[0], "https://example.com/new")

    def test_session_ignores_environment_credentials(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://user:pass@proxy.example:8080"}), \
             patch("requests.sessions.get_netrc_auth", side_effect=AssertionError("netrc accessed")):
            session = fetch.make_session({})
            request = session.prepare_request(requests.Request("GET", "https://example.com/feed"))
            self.assertNotIn("Authorization", request.headers)
            settings = session.merge_environment_settings(request.url, {}, None, None, None)
            self.assertFalse(settings["proxies"])
            session.close()

    def test_rejects_deadline_during_stream(self):
        response = Mock(is_redirect=False, status_code=200, headers={})
        response.raw.read1.return_value = b"x"
        session = Mock()
        session.get.return_value.__enter__ = Mock(return_value=response)
        session.get.return_value.__exit__ = Mock(return_value=False)
        with patch.object(fetch.time, "monotonic", side_effect=[0, 0, 0, 121]), self.assertRaises(ValueError):
            fetch.download("https://example.com/feed", session)

    def test_rejects_credentials_and_redirect_loop(self):
        session = Mock()
        with self.assertRaises(ValueError):
            fetch.download("https://user:password@example.com/feed", session)
        session.get.assert_not_called()
        response = Mock(is_redirect=True, headers={"Location": "/again"})
        session.get.return_value.__enter__ = Mock(return_value=response)
        session.get.return_value.__exit__ = Mock(return_value=False)
        with self.assertRaises(ValueError):
            fetch.download("https://example.com/feed", session)
        self.assertEqual(session.get.call_count, fetch.MAX_REDIRECTS + 1)

    def test_rejects_stream_larger_than_limit(self):
        response = Mock(is_redirect=False, status_code=200, headers={})
        response.raw.read1.side_effect = [b"abc", b""]
        session = Mock()
        session.get.return_value.__enter__ = Mock(return_value=response)
        session.get.return_value.__exit__ = Mock(return_value=False)
        with patch.object(fetch, "MAX_ARCHIVE", 2), self.assertRaises(ValueError):
            fetch.download("https://example.com/feed", session)


if __name__ == "__main__":
    unittest.main()
