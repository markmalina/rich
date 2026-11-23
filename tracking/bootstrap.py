# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Build Google Sheets API requests for a new Rich workbook."""

import argparse
import json
from pathlib import Path


SPEC_PATH = Path(__file__).resolve().parents[1] / "schemas/google-base-v1.json"


def workbook(title, face=False, default_sheet_id=None):
    """Return a Sheets API Spreadsheet resource without accessing a Google account."""
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    tabs = [spec["face"]] if face else spec["sheets"]
    sheet_ids = iter(number for number in range(len(tabs) + 1) if number != default_sheet_id)
    sheets = []
    for index, tab in enumerate(tabs):
        sheet_id = default_sheet_id if index == 0 and default_sheet_id is not None else next(sheet_ids)
        rows = [tab["header"], *tab.get("rows", [])]
        column_count = max(tab.get("columnCount", 2), *(len(row) for row in rows))
        sheets.append({
            "properties": {
                "sheetId": sheet_id, "title": tab["title"], "hidden": tab.get("hidden", False),
                "gridProperties": {"rowCount": 1000, "columnCount": column_count, "frozenRowCount": 1},
            },
            "data": [{"rowData": [{"values": [cell(value) for value in row]} for row in rows]}],
        })
    return {
        "properties": {"title": title, "locale": spec["locale"], "timeZone": spec["timeZone"]},
        "sheets": sheets,
    }


def cell(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return {"userEnteredValue": {"formulaValue": value["formula"]}}
    if isinstance(value, str):
        return {"userEnteredValue": {"stringValue": value}}
    if isinstance(value, bool):
        return {"userEnteredValue": {"boolValue": value}}
    return {"userEnteredValue": {"numberValue": value}}


def setup_requests(resource, existing_default=False, face=False):
    """Build a batch for a new blank file, optionally reusing its default worksheet."""
    requests = [{"updateSpreadsheetProperties": {
        "properties": resource["properties"], "fields": "title,locale,timeZone",
    }}]
    formula_requests = []
    for index, sheet in enumerate(resource["sheets"]):
        properties = sheet["properties"]
        if index == 0 and existing_default:
            requests.append({"updateSheetProperties": {
                "properties": properties, "fields": "title,hidden,gridProperties",
            }})
        else:
            requests.append({"addSheet": {"properties": properties}})
    for sheet in resource["sheets"]:
        sheet_id = sheet["properties"]["sheetId"]
        rows = []
        for row_index, row in enumerate(sheet["data"][0]["rowData"]):
            values = []
            for column_index, value in enumerate(row["values"]):
                if "formulaValue" in value.get("userEnteredValue", {}):
                    formula_requests.append(write_cells(sheet_id, row_index, column_index, [{"values": [value]}]))
                    values.append({})
                else:
                    values.append(value)
            rows.append({"values": values})
        requests.append(write_cells(sheet_id, 0, 0, rows))
        if face:
            for column, kind, pattern in ((0, "DATE", "dd.mm.yyyy"), (1, "TIME", "hh:mm")):
                requests.append({"repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 1,
                              "startColumnIndex": column, "endColumnIndex": column + 1},
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": kind, "pattern": pattern}}},
                    "fields": "userEnteredFormat.numberFormat",
                }})
    return {"requests": [*requests, *formula_requests]}


def write_cells(sheet_id, row, column, rows):
    return {"updateCells": {
        "start": {"sheetId": sheet_id, "rowIndex": row, "columnIndex": column},
        "rows": rows, "fields": "userEnteredValue",
    }}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="БАЗА · Rich")
    parser.add_argument("--face", action="store_true", help="Generate a separate Face workbook")
    parser.add_argument("--batch", action="store_true", help="Generate spreadsheets.batchUpdate instead of create")
    parser.add_argument("--default-sheet-id", type=int, help="Reuse the initial blank tab with this ID, in batch mode")
    args = parser.parse_args()
    if args.default_sheet_id is not None and (not args.batch or args.default_sheet_id < 0):
        parser.error("--default-sheet-id requires --batch and a nonnegative sheet ID")
    result = workbook(args.title, args.face, args.default_sheet_id)
    if args.batch:
        result = setup_requests(result, args.default_sheet_id is not None, args.face)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
