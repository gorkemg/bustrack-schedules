import unittest

from src.pvta_parser import parse_maps_page, parse_schedules_page


class PVTARevizeDocumentCenterTests(unittest.TestCase):
    def test_schedules_page_extracts_route_ids_and_resolves_relative_urls(self):
        html = """
        <base href="https://www.pvta.com/">
        <article id="document-center"><ul class="file-group">
          <li><a href="schedules/Rt39.pdf?t=1">39 Smith College</a></li>
          <li><a href="schedules/B43no_school.pdf?t=2">B43 No School</a></li>
          <li><a href="Documents/Bus%20Service/Schedules/PalmerShuttle.pdf?t=3">Palmer Shuttle</a></li>
        </ul></article>
        """

        links = parse_schedules_page(html)

        self.assertEqual(
            links["39"],
            "https://www.pvta.com/schedules/Rt39.pdf?t=1",
        )
        self.assertEqual(
            links["B43no_school"],
            "https://www.pvta.com/schedules/B43no_school.pdf?t=2",
        )
        self.assertEqual(
            links["PS"],
            "https://www.pvta.com/Documents/Bus%20Service/Schedules/PalmerShuttle.pdf?t=3",
        )

    def test_document_center_rejects_credentials_in_links_and_base(self):
        html = """<base href="https://u:p@evil.example/">
        <article id="document-center"><ul class="file-group">
          <li><a href="https://u:p@evil.example/Rt39.pdf?t=1">39</a></li>
          <li><a href="/schedules/Rt30.pdf?t=1">30</a></li>
        </ul></article>"""
        self.assertEqual(parse_schedules_page(html), {"30": "https://www.pvta.com/schedules/Rt30.pdf?t=1"})

    def test_maps_page_extracts_shared_map_from_document_center(self):
        html = """
        <base href="https://www.pvta.com/">
        <article id="document-center"><ul class="file-group">
          <li><a href="RouteMaps/Rt34_35.jpg?t=1">34/35</a></li>
          <li><a href="RouteMaps/B43no_school.jpg?t=2">B43 No-School</a></li>
        </ul></article>
        """

        links = parse_maps_page(html)

        self.assertEqual(
            links["34"],
            "https://www.pvta.com/RouteMaps/Rt34_35.jpg?t=1",
        )
        self.assertEqual(links["35"], links["34"])
        self.assertEqual(
            links["B43no_school"],
            "https://www.pvta.com/RouteMaps/B43no_school.jpg?t=2",
        )


if __name__ == "__main__":
    unittest.main()
