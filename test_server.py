"""Тесты server.py. Запуск: venv/bin/python -m unittest test_server -v

Покрывают то, что ломалось в бою:
  #2512 — валюта (уже починена, держим регрессию),
  #2529 — жёсткий домен и ПОДМЕНА выдачи вместо честного «не найдено».
"""

import os
import unittest
from unittest import mock

import server


class TestCleanPrice(unittest.TestCase):
    """Регрессия #2512: валюта не выдумывается."""

    def test_euro_code_with_space(self):
        self.assertEqual(server.clean_price("EUR 61.04"), "EUR 61.04")

    def test_euro_code_glued_to_amount(self):
        # \b не срабатывает между буквой и цифрой
        self.assertEqual(server.clean_price("EUR60.93"), "EUR 60.93")

    def test_euro_symbol(self):
        self.assertEqual(server.clean_price("€131.97"), "€131.97")

    def test_dollar_stays_dollar(self):
        self.assertEqual(server.clean_price("$19.99"), "$19.99")

    def test_list_prefix_ignored(self):
        self.assertEqual(server.clean_price("List: EUR 78.48"), "EUR 78.48")

    def test_empty(self):
        self.assertEqual(server.clean_price(""), "Price not available")


class TestResolveDomain(unittest.TestCase):
    """#2529: витрина выбирается, а не прибита к .com."""

    def test_bare_suffix(self):
        self.assertEqual(server.resolve_domain("de"), "https://www.amazon.de")

    def test_multipart_suffix(self):
        self.assertEqual(server.resolve_domain("co.uk"), "https://www.amazon.co.uk")

    def test_with_amazon_prefix(self):
        self.assertEqual(server.resolve_domain("amazon.de"), "https://www.amazon.de")

    def test_with_www(self):
        self.assertEqual(server.resolve_domain("www.amazon.de"), "https://www.amazon.de")

    def test_full_url(self):
        self.assertEqual(server.resolve_domain("https://www.amazon.de/"), "https://www.amazon.de")

    def test_uppercase(self):
        self.assertEqual(server.resolve_domain("DE"), "https://www.amazon.de")

    def test_default_is_com(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server.resolve_domain(None), "https://www.amazon.com")

    def test_env_default(self):
        with mock.patch.dict(os.environ, {"AMAZON_DOMAIN": "de"}, clear=True):
            self.assertEqual(server.resolve_domain(None), "https://www.amazon.de")

    def test_explicit_beats_env(self):
        with mock.patch.dict(os.environ, {"AMAZON_DOMAIN": "de"}, clear=True):
            self.assertEqual(server.resolve_domain("com"), "https://www.amazon.com")

    def test_rejects_foreign_host(self):
        # SSRF-защита: тул не должен уметь ходить на произвольный хост
        with self.assertRaises(ValueError):
            server.resolve_domain("evil.com")

    def test_rejects_url_with_amazon_in_path(self):
        with self.assertRaises(ValueError):
            server.resolve_domain("https://evil.com/amazon.de")

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            server.resolve_domain("de/../../etc/passwd")


class TestQueryCoverage(unittest.TestCase):
    """#2529: подмена должна быть измерима."""

    def test_exact_match_is_full(self):
        products = [{"name": "Nokia 2660 Flip 4G Dual SIM 128MB Black"}]
        self.assertEqual(server.best_query_coverage("Nokia 2660 Flip 4G", products), 1.0)

    def test_substitution_is_zero(self):
        # то, что реально пришло с .com на запрос про Xplora
        products = [
            {"name": "4G Kids Smart Watch with GPS Tracker, Video & Voice Call, SOS"},
            {"name": "5 Kids Smart Watch by Cosmo | Best Kid-Safe Phone Watch"},
        ]
        self.assertEqual(server.best_query_coverage("Xplora XGO3", products), 0.0)

    def test_partial_match(self):
        products = [{"name": "Xplora X6 Play Smartwatch for Children"}]
        self.assertAlmostEqual(server.best_query_coverage("Xplora XGO3", products), 0.5)

    def test_weak_match_of_common_word_only(self):
        # "2780 Flip" на запрос "Nokia 2660 Flip 4G" — 1 токен из 4
        products = [{"name": "2780 Flip | Unlocked | KaiOS | Verizon"}]
        self.assertAlmostEqual(server.best_query_coverage("Nokia 2660 Flip 4G", products), 0.25)

    def test_empty_products(self):
        self.assertEqual(server.best_query_coverage("anything", []), 0.0)

    def test_case_insensitive(self):
        products = [{"name": "XPLORA XGO3 kids"}]
        self.assertEqual(server.best_query_coverage("xplora xgo3", products), 1.0)


class TestFormatWarnsOnSubstitution(unittest.TestCase):
    """Предупреждение должно быть В ТЕКСТЕ ответа — модель читает только его."""

    def _fmt(self, query, names):
        products = [
            {"name": n, "price": "EUR 1.00", "rating": "4 out of 5", "url": "u"} for n in names
        ]
        return server.format_search_results(products, query)

    def test_warns_when_nothing_matches(self):
        out = self._fmt("Xplora XGO3", ["4G Kids Smart Watch with GPS Tracker"])
        self.assertIn("НЕ содержит", out)

    def test_notes_partial_match(self):
        out = self._fmt("Xplora XGO3", ["Xplora X6 Play Smartwatch"])
        self.assertIn("частичное", out.lower())

    def test_silent_on_good_match(self):
        out = self._fmt("Nokia 2660 Flip 4G", ["Nokia 2660 Flip 4G Dual SIM Black"])
        self.assertNotIn("НЕ содержит", out)
        self.assertNotIn("частичное", out.lower())

    def test_empty_result_unchanged(self):
        out = server.format_search_results([], "whatever")
        self.assertIn("No products found", out)


class TestAccessoryDetection(unittest.TestCase):
    """Аксессуар покрывает все слова запроса, но товаром не является.

    Живой случай: на «Elari KidPhone 4G» лучшим совпадением (100%) оказалась
    плёнка «for Elari KidPhone 4G Kids Watch», а самих часов в выдаче не было.
    """

    def test_screen_protector(self):
        self.assertTrue(
            server.looks_like_accessory(
                "[Pack of 6] Flexible Glass Screen Protector for Elari KidPhone 4G Kids Watch"
            )
        )

    def test_case(self):
        self.assertTrue(server.looks_like_accessory("Mobile Phone Belt Bag for Samsung Galaxy Xcover Pro Case"))

    def test_german_accessory(self):
        self.assertTrue(server.looks_like_accessory("Panzerglas Schutzfolie für Xplora X6 Play"))

    def test_real_device_is_not_accessory(self):
        self.assertFalse(server.looks_like_accessory("Nokia 2660 Flip 4G Dual SIM 128MB 48MB RAM Black"))
        self.assertFalse(
            server.looks_like_accessory("Xplora X6 Play Smartwatch for Children, with GPS Tracker & SOS Button")
        )

    def test_warns_when_best_match_is_accessory(self):
        products = [
            {
                "name": "[Pack of 6] Glass Screen Protector for Elari KidPhone 4G Kids Watch",
                "price": "EUR 9.99",
                "rating": "4 out of 5",
                "url": "u",
            }
        ]
        out = server.format_search_results(products, "Elari KidPhone 4G")
        self.assertIn("аксессуар", out.lower())

    def test_silent_when_accessory_was_asked_for(self):
        products = [
            {
                "name": "Glass Screen Protector for Elari KidPhone 4G",
                "price": "EUR 9.99",
                "rating": "4 out of 5",
                "url": "u",
            }
        ]
        out = server.format_search_results(products, "Elari KidPhone screen protector")
        self.assertNotIn("аксессуар", out.lower())


class TestSearchUrlUsesDomain(unittest.TestCase):
    def test_builds_de_url(self):
        self.assertEqual(
            server.build_search_url("Xplora XGO3", "https://www.amazon.de"),
            "https://www.amazon.de/s?k=Xplora+XGO3",
        )

    def test_escapes_special_chars(self):
        # KW-44 и & не должны ломать запрос
        url = server.build_search_url("Canyon & KW-44", "https://www.amazon.de")
        self.assertNotIn(" ", url)
        self.assertIn("KW-44", url)


class TestRelativeLinksFollowDomain(unittest.TestCase):
    """Ссылка на товар с .de не должна вести на .com."""

    HTML = """
    <div data-component-type="s-search-result">
      <a href="/dp/B123"><h2><span>Nokia 2660 Flip 4G</span></h2></a>
      <span class="a-price"><span class="a-offscreen">EUR 62.35</span></span>
    </div>
    """

    def test_absolute_url_uses_given_base(self):
        products = server.extract_search_results(self.HTML, 5, base_url="https://www.amazon.de")
        self.assertEqual(products[0]["url"], "https://www.amazon.de/dp/B123")

    def test_defaults_to_com(self):
        products = server.extract_search_results(self.HTML, 5)
        self.assertTrue(products[0]["url"].startswith("https://www.amazon.com"))


if __name__ == "__main__":
    unittest.main()
