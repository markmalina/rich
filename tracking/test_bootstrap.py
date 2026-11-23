# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import unittest

from bot.ledger import INBOX_HEADERS

from .bootstrap import setup_requests, workbook


class BootstrapTest(unittest.TestCase):
    def test_base_matches_bot_and_face_contract(self):
        base = workbook("БАЗА")
        sheets = {sheet["properties"]["title"]: sheet for sheet in base["sheets"]}
        self.assertEqual(list(sheets), ["Входящие", "События", "Действия", "Правило", "Faces", "Экспорт Faces"])
        rows = sheets["Входящие"]["data"][0]["rowData"]
        self.assertEqual([cell["userEnteredValue"]["stringValue"] for cell in rows[0]["values"]], INBOX_HEADERS)
        self.assertEqual(sheets["События"]["data"][0]["rowData"][0]["values"][8]["userEnteredValue"],
                         {"stringValue": "ID входящего"})
        self.assertGreaterEqual(sheets["Правило"]["properties"]["gridProperties"]["columnCount"], 4)
        self.assertTrue(sheets["Экспорт Faces"]["properties"]["hidden"])
        self.assertEqual(sheets["Экспорт Faces"]["data"][0]["rowData"][0],
                         workbook("Витрина", face=True)["sheets"][0]["data"][0]["rowData"][0])

    def test_setup_reuses_blank_tab_and_preserves_formula_types(self):
        base = workbook("БАЗА", default_sheet_id=2)
        self.assertEqual(base["sheets"][0]["properties"]["sheetId"], 2)
        self.assertEqual(len({sheet["properties"]["sheetId"] for sheet in base["sheets"]}), 6)
        requests = setup_requests(base, existing_default=True)["requests"]
        self.assertEqual(requests[1]["updateSheetProperties"]["properties"]["title"], "Входящие")
        self.assertEqual(sum("addSheet" in request for request in requests), 5)
        self.assertTrue(all("addSheet" in request for request in requests[2:7]))
        formula = requests[-1]["updateCells"]["rows"][0]["values"][0]["userEnteredValue"]
        self.assertTrue(formula["formulaValue"].startswith("=ARRAYFORMULA("))
