import io
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

    def test_rejects_stream_larger_than_limit(self):
        response = Mock(is_redirect=False, headers={})
        response.iter_content.return_value = [b"abc"]
        session = Mock()
        session.get.return_value.__enter__ = Mock(return_value=response)
        session.get.return_value.__exit__ = Mock(return_value=False)
        with patch.object(fetch, "MAX_ARCHIVE", 2), self.assertRaises(ValueError):
            fetch.download("https://example.com/feed", session)


if __name__ == "__main__":
    unittest.main()
