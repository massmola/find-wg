import unittest
import html
import json
from email.message import EmailMessage

from notification_bot import (
    build_self_test_email,
    contract_is_allowed,
    extract_contract_term,
    extract_postcode,
    extract_prices,
    format_alert,
    is_relevant,
    parse_alert,
    parse_oeh_page,
    parse_wg_gesucht_page,
    parse_willhaben_page,
)


class NotificationBotTests(unittest.TestCase):
    def make_email(self, subject: str, body: str) -> bytes:
        message = EmailMessage()
        message["From"] = "Alerts <alerts@wg-gesucht.de>"
        message["To"] = "student@example.com"
        message["Subject"] = subject
        message["Message-ID"] = "<test-listing@example.com>"
        message.set_content(body)
        return message.as_bytes()

    def test_extracts_price_and_postcode(self):
        text = "WG-Zimmer in 1040 Wien, Miete 620 EUR, Kaution EUR 1.240"
        self.assertEqual(extract_prices(text), (620, 1240))
        self.assertEqual(extract_postcode(text), "1040")

    def test_relevant_alert_is_prioritized(self):
        raw = self.make_email(
            "Neue WG in Wieden",
            "Zimmer in 1040 Wien für € 650, Mietdauer 12 Monate. https://www.wg-gesucht.de/123.html",
        )
        alert = parse_alert(1, raw)
        self.assertTrue(is_relevant(alert, ("wg-gesucht",), 700))
        self.assertTrue(alert.priority.startswith("A"))
        self.assertIn("Open listing", format_alert(alert))

    def test_over_budget_alert_is_rejected(self):
        raw = self.make_email("Neue Wohnung", "Miete: 850 EUR in 1040 Wien, Mietdauer 1 Jahr")
        alert = parse_alert(2, raw)
        self.assertFalse(is_relevant(alert, ("wg-gesucht",), 700))

    def test_unknown_price_is_forwarded_for_review(self):
        raw = self.make_email("WG-Zimmer verfügbar", "Neues Angebot nahe TU Wien, 1-year contract")
        alert = parse_alert(3, raw)
        self.assertTrue(is_relevant(alert, ("wg-zimmer",), 700))

    def test_email_without_contract_term_is_rejected(self):
        raw = self.make_email("WG-Zimmer verfügbar", "Neues Angebot nahe TU Wien für 600 EUR")
        alert = parse_alert(4, raw)
        self.assertFalse(is_relevant(alert, ("wg-zimmer",), 700))

    def test_contract_duration_filter(self):
        for text, expected in (
            ("Mietdauer: 6 Monate", 6),
            ("12-month contract", 12),
            ("Befristung auf 2 Jahre", 24),
            ("01.09.2026 bis 31.08.2027", 12),
        ):
            term = extract_contract_term(text)
            self.assertEqual(term.months, expected)
            self.assertTrue(contract_is_allowed(term))

        self.assertFalse(contract_is_allowed(extract_contract_term("Befristung 3 Jahre")))
        self.assertFalse(contract_is_allowed(extract_contract_term("unbefristet")))
        self.assertFalse(contract_is_allowed(extract_contract_term("Mietdauer nicht angegeben")))

    def test_self_test_email_exercises_pipeline(self):
        alert = parse_alert(0, build_self_test_email())
        self.assertEqual(alert.postcode, "1040")
        self.assertIn(650, alert.prices)
        self.assertTrue(alert.priority.startswith("A"))
        self.assertTrue(alert.urls)
        self.assertTrue(is_relevant(alert, ("wg-gesucht",), 700))

    def test_parses_willhaben_structured_search_result(self):
        item = {
            "id": "12345",
            "advertStatus": {"id": "active"},
            "description": "WG-Zimmer nahe TU Wien",
            "attributes": {
                "attribute": [
                    {"name": "POSTCODE", "values": ["1040"]},
                    {"name": "LOCATION", "values": ["Wien, Wieden"]},
                    {"name": "PRICE", "values": ["650"]},
                    {"name": "ESTATE_SIZE", "values": ["12"]},
                    {"name": "PROPERTY_TYPE", "values": ["Zimmer/WG"]},
                    {"name": "SEO_URL", "values": ["immobilien/d/example-12345/"]},
                ]
            },
        }
        payload = {
            "props": {"pageProps": {"searchResult": {"advertSummaryList": {"advertSummary": [item]}}}}
        }
        page = f'<script id="__NEXT_DATA__" type="application/json">{html.escape(json.dumps(payload))}</script>'
        # Script contents are raw text in real HTML, not entity-escaped.
        page = page.replace("&quot;", '"')
        listings = parse_willhaben_page(page, 700, {"1040"})
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].price, 650)
        self.assertEqual(listings[0].postcode, "1040")
        self.assertIn("example-12345", listings[0].url)
        self.assertEqual(listings[0].search_profile, "Single room")

    def test_classifies_whole_apartment_for_three_people(self):
        item = {
            "id": "group-three",
            "advertStatus": {"id": "active"},
            "description": "3-Zimmer-Wohnung, 3er WG geeignet, nahe U1",
            "attributes": {
                "attribute": [
                    {"name": "POSTCODE", "values": ["1100"]},
                    {"name": "LOCATION", "values": ["Wien, Favoriten"]},
                    {"name": "PRICE", "values": ["1450"]},
                    {"name": "NUMBER_OF_ROOMS", "values": ["3"]},
                    {"name": "PROPERTY_TYPE", "values": ["Wohnung"]},
                    {"name": "SEO_URL", "values": ["immobilien/d/group-three/"]},
                ]
            },
        }
        payload = {
            "props": {"pageProps": {"searchResult": {"advertSummaryList": {"advertSummary": [item]}}}}
        }
        page = f'<script id="__NEXT_DATA__">{json.dumps(payload)}</script>'
        listings = parse_willhaben_page(page, 700, {"1100"})
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].rooms, 3)
        self.assertEqual(listings[0].search_profile, "Whole apartment for 3")

    def test_parses_wg_gesucht_card_and_filters_district(self):
        page = """
        <div id="liste-details-ad-987" class="offer_list_item">
          <h2><a href="/wg-zimmer-in-Wien-04-Bezirk-Wieden.987.html">Small TU room</a></h2>
          <span>Wien 04. Bezirk Wieden | Favoritenstraße 1</span>
          <b>620 &euro;</b><span>01.09.2026</span><b>10 m&sup2;</b>
          <div>Verfügbar: 01.09.2026</div>
        </div>
        """
        listings = parse_wg_gesucht_page(page, 700, {"1040"})
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].listing_id, "987")
        self.assertEqual(listings[0].price, 620)
        self.assertEqual(listings[0].available, "01.09.2026")

    def test_parses_oeh_card_and_rejects_non_vienna_result(self):
        page = """
        <ul>
          <li><h3><a href="https://immobilien.derstandard.at/detail/42/room">TU room</a></h3>
          <strong>690,00 EUR</strong><p><strong>1040 Wien</strong> <strong>15 m²</strong></p></li>
          <li><h3><a href="https://example.test/detail/43/room">Room elsewhere</a></h3>
          <strong>400,00 EUR</strong><p><strong>4400 Steyr</strong></p></li>
        </ul>
        """
        listings = parse_oeh_page(page, 700, {"1040"})
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].listing_id, "42")
        self.assertEqual(listings[0].price, 690)

    def test_parses_wg_gesucht_whole_apartment_profile(self):
        page = """
        <div id="liste-details-ad-333" class="offer_list_item">
          <h2><a href="/wohnungen-in-Wien-10-Bezirk-Favoriten.333.html">Three-room flat, 3er WG geeignet</a></h2>
          <span>3-Zimmer-Wohnung | Wien 10. Bezirk Favoriten | Keplerplatz</span>
          <b>1450 &euro;</b><span>01.09.2026</span><b>75 m&sup2;</b>
          <div>Verfügbar: 01.09.2026</div>
        </div>
        """
        listings = parse_wg_gesucht_page(page, 1600, {"1100"}, whole_apartments=True)
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].search_profile, "Whole apartment for 3")


if __name__ == "__main__":
    unittest.main()
