import unittest

from symbols import _map_shortcut, normalize_symbol, suggest_symbols, valid_isin


class SymbolRulesTests(unittest.TestCase):
    def test_isin_requires_twelve_valid_characters(self):
        self.assertTrue(valid_isin("IRO1IKCO0001"))
        self.assertFalse(valid_isin("IRO1IKCO000"))
        self.assertFalse(valid_isin("not-an-isin"))

    def test_persian_and_arabic_keyboard_variants_normalize(self):
        self.assertEqual(normalize_symbol("كالا"), normalize_symbol("کالا"))
        self.assertEqual(normalize_symbol("ميهن"), normalize_symbol("میهن"))
        self.assertEqual(normalize_symbol("صندوق\u200cدرآمد"), normalize_symbol("صندوقدرآمد"))

    def test_maps_daya_instrument_shortcut_field_names(self):
        item = _map_shortcut({
            "instrumentid": "IRO1IKCO0001",
            "instrument_Name": "خودرو",
            "company_Name_Persian": "ایران خودرو",
            "marketcategoryname": "بازار اول",
        })
        self.assertEqual(item["symbol"], "خودرو")
        self.assertEqual(item["isin"], "IRO1IKCO0001")
        self.assertEqual(item["company_name"], "ایران خودرو")
        self.assertEqual(item["market_type"], "بازار اول")

    def test_invalid_instrument_is_rejected(self):
        self.assertIsNone(_map_shortcut({
            "instrumentid": "BAD",
            "instrument_Name": "نماد نامعتبر",
        }))


if __name__ == "__main__":
    unittest.main()
