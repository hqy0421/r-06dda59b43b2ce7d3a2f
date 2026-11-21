import unittest
from typing import Any, Dict, List, Mapping, Optional

import gspread
from gspread import filter_view
from gspread.exceptions import (
    FilterViewError,
    FilterViewNotFound,
    UnsupportedFilterViewRule,
)
from gspread.http_client import HTTPClient
from gspread.worksheet import Worksheet

SHEET_ID = 0

FILTER_VIEW = {
    "filterViewId": 123,
    "title": "Inbound",
    "range": {
        "sheetId": SHEET_ID,
        "startRowIndex": 0,
        "endRowIndex": 6,
        "startColumnIndex": 0,
        "endColumnIndex": 3,
    },
    "filterSpecs": [
        {
            "columnIndex": 0,
            "filterCriteria": {
                "hiddenValues": ["SKU-003"],
                "condition": {
                    "type": "TEXT_CONTAINS",
                    "values": [{"userEnteredValue": "SKU-"}],
                },
            },
        },
        {
            "columnIndex": 2,
            "filterCriteria": {
                "condition": {
                    "type": "NUMBER_GREATER_THAN_EQ",
                    "values": [{"userEnteredValue": "10"}],
                }
            },
        },
    ],
}

DISPLAY_VALUES = [
    ["SKU", "Name", "Qty"],
    ["SKU-001", "alpha", "12"],
    ["SKU-002", "beta", "5"],
    ["SKU-003", "gamma", "100"],
    ["SKU-004", "delta", "10.00"],
    ["", "", ""],
]

NUMERIC_VALUES = [
    ["SKU", "Name", "Qty"],
    ["SKU-001", "alpha", 12],
    ["SKU-002", "beta", 5],
    ["SKU-003", "gamma", 100],
    ["SKU-004", "delta", 10],
    ["", "", ""],
]

GRID_RANGE = {
    "sheetId": SHEET_ID,
    "startRowIndex": 0,
    "endRowIndex": 6,
    "startColumnIndex": 0,
    "endColumnIndex": 3,
}


class FakeHTTPClient(HTTPClient):
    """An HTTP client that answers the filter view requests offline."""

    def __init__(
        self,
        metadata: Mapping[str, Any],
        data_metadata: Optional[Mapping[str, Any]] = None,
        display_values: Optional[List[List[Any]]] = None,
        numeric_values: Optional[List[List[Any]]] = None,
    ) -> None:
        self.timeout = None
        self.metadata = metadata
        self.data_metadata = data_metadata or metadata
        self.display_values = display_values or DISPLAY_VALUES
        self.numeric_values = numeric_values or NUMERIC_VALUES
        self.calls: List[Dict[str, Any]] = []

    def fetch_sheet_metadata(self, id, params=None):
        self.calls.append({"method": "fetch_sheet_metadata", "params": params})
        if params and params.get("includeGridData") == "true":
            return self.data_metadata
        return self.metadata

    def values_get(self, id, range, params=None):
        self.calls.append({"method": "values_get", "range": range, "params": params})
        render_option = (params or {}).get("valueRenderOption")
        if render_option == "UNFORMATTED_VALUE":
            values = self.numeric_values
        else:
            values = self.display_values
        return {"range": range, "majorDimension": "ROWS", "values": values}

    def batch_update(self, id, body):
        raise AssertionError("export must never write to the worksheet")


def sheet_metadata(
    filter_views: Optional[List[Mapping[str, Any]]] = None,
    named_ranges: Optional[List[Mapping[str, Any]]] = None,
    tables: Optional[List[Mapping[str, Any]]] = None,
    row_groups: Optional[List[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    sheet: Dict[str, Any] = {
        "properties": {
            "sheetId": SHEET_ID,
            "title": "Sheet1",
            "index": 0,
            "gridProperties": {"rowCount": 1000, "columnCount": 26},
        },
        "filterViews": filter_views if filter_views is not None else [FILTER_VIEW],
    }
    if tables is not None:
        sheet["tables"] = tables
    if row_groups is not None:
        sheet["rowGroups"] = row_groups

    metadata: Dict[str, Any] = {"sheets": [sheet]}
    if named_ranges is not None:
        metadata["namedRanges"] = named_ranges
    return metadata


def make_worksheet(
    metadata: Optional[Mapping[str, Any]] = None,
    data_metadata: Optional[Mapping[str, Any]] = None,
    **kwargs: Any,
):
    client = FakeHTTPClient(
        metadata or sheet_metadata(),
        data_metadata=data_metadata,
        **kwargs,
    )
    worksheet = Worksheet(
        None,  # type: ignore[arg-type]
        properties={"sheetId": SHEET_ID, "title": "Sheet1", "index": 0},
        spreadsheet_id="spreadsheet-id",
        client=client,
    )
    return worksheet, client


class FilterViewHelpersTest(unittest.TestCase):
    def test_column_index_to_letter(self):
        self.assertEqual(filter_view.column_index_to_letter(0), "A")
        self.assertEqual(filter_view.column_index_to_letter(25), "Z")
        self.assertEqual(filter_view.column_index_to_letter(26), "AA")
        self.assertEqual(filter_view.column_index_to_letter(701), "ZZ")
        self.assertRaises(FilterViewError, filter_view.column_index_to_letter, -1)

    def test_grid_range_to_a1(self):
        self.assertEqual(
            filter_view.grid_range_to_a1(
                {
                    "startRowIndex": 0,
                    "endRowIndex": 3,
                    "startColumnIndex": 0,
                    "endColumnIndex": 2,
                }
            ),
            "A1:B3",
        )
        self.assertEqual(
            filter_view.grid_range_to_a1(
                {
                    "startRowIndex": 4,
                    "endRowIndex": 5,
                    "startColumnIndex": 2,
                    "endColumnIndex": 3,
                }
            ),
            "C5",
        )

    def test_grid_range_to_a1_requires_bounds(self):
        self.assertRaises(
            FilterViewError,
            filter_view.grid_range_to_a1,
            {"startRowIndex": 0, "startColumnIndex": 0},
        )

    def test_find_filter_view_accepts_integer_like_id(self):
        self.assertIs(
            filter_view.find_filter_view([FILTER_VIEW], "123"),
            FILTER_VIEW,
        )

    def test_find_filter_view_unknown_id(self):
        self.assertRaises(
            FilterViewNotFound, filter_view.find_filter_view, [FILTER_VIEW], 999
        )

    def test_find_filter_view_invalid_id(self):
        self.assertRaises(
            FilterViewError, filter_view.find_filter_view, [FILTER_VIEW], None
        )


class ResolveGridRangeTest(unittest.TestCase):
    def test_uses_view_range(self):
        grid_range = filter_view.resolve_grid_range(
            FILTER_VIEW, sheet_id=SHEET_ID, row_count=10, column_count=10
        )
        self.assertEqual(grid_range, GRID_RANGE)

    def test_fills_missing_bounds_from_the_sheet_size(self):
        view = {"range": {"sheetId": SHEET_ID, "startRowIndex": 2}}
        grid_range = filter_view.resolve_grid_range(
            view, sheet_id=SHEET_ID, row_count=7, column_count=4
        )
        self.assertEqual(
            grid_range,
            {
                "sheetId": SHEET_ID,
                "startRowIndex": 2,
                "endRowIndex": 7,
                "startColumnIndex": 0,
                "endColumnIndex": 4,
            },
        )

    def test_uses_named_range(self):
        view = {"namedRangeId": "named-range-id"}
        named_ranges = [
            {
                "namedRangeId": "named-range-id",
                "name": "InboundData",
                "range": {
                    "sheetId": SHEET_ID,
                    "startRowIndex": 1,
                    "endRowIndex": 4,
                    "startColumnIndex": 0,
                    "endColumnIndex": 2,
                },
            }
        ]
        grid_range = filter_view.resolve_grid_range(
            view, named_ranges=named_ranges, sheet_id=SHEET_ID
        )
        self.assertEqual(grid_range["startRowIndex"], 1)
        self.assertEqual(grid_range["endRowIndex"], 4)

    def test_uses_table(self):
        view = {"tableId": "table-id"}
        metadata = {"tables": [{"tableId": "table-id", "range": GRID_RANGE}]}
        grid_range = filter_view.resolve_grid_range(
            view, sheet_metadata=metadata, sheet_id=SHEET_ID
        )
        self.assertEqual(grid_range, GRID_RANGE)

    def test_unknown_named_range(self):
        self.assertRaises(
            FilterViewError,
            filter_view.resolve_grid_range,
            {"namedRangeId": "missing"},
            named_ranges=[],
        )

    def test_empty_view(self):
        self.assertRaises(FilterViewError, filter_view.resolve_grid_range, {})

    def test_range_from_another_sheet(self):
        view = {"range": dict(GRID_RANGE, sheetId=99)}
        self.assertRaises(
            FilterViewError,
            filter_view.resolve_grid_range,
            view,
            sheet_id=SHEET_ID,
        )


class ColumnRulesTest(unittest.TestCase):
    def test_filter_specs_take_precedence_over_criteria(self):
        view = {
            "filterSpecs": [
                {
                    "columnIndex": 1,
                    "filterCriteria": {
                        "condition": {
                            "type": "TEXT_EQ",
                            "values": [{"userEnteredValue": "new"}],
                        }
                    },
                }
            ],
            "criteria": {"1": {"hiddenValues": ["old"]}},
        }
        rules = filter_view.extract_column_rules(view)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].column_index, 1)
        self.assertEqual(rules[0].condition_type, "TEXT_EQ")
        self.assertEqual(rules[0].hidden_values, [])

    def test_legacy_criteria_are_supported(self):
        view = {
            "criteria": {
                "2": {"hiddenValues": ["x"]},
                0: {
                    "condition": {
                        "type": "TEXT_CONTAINS",
                        "values": [{"userEnteredValue": "foo"}],
                    }
                },
            }
        }
        rules = filter_view.extract_column_rules(view)
        self.assertEqual([rule.column_index for rule in rules], [0, 2])

    def test_empty_criteria_is_ignored(self):
        view = {"filterSpecs": [{"columnIndex": 0, "filterCriteria": {}}]}
        self.assertEqual(filter_view.extract_column_rules(view), [])

    def test_unsupported_color_rule(self):
        view = {
            "filterSpecs": [
                {
                    "columnIndex": 0,
                    "filterCriteria": {
                        "visibleBackgroundColorStyle": {
                            "rgbColor": {"red": 1, "green": 0, "blue": 0}
                        }
                    },
                }
            ]
        }
        self.assertRaises(
            UnsupportedFilterViewRule, filter_view.extract_column_rules, view
        )

    def test_unsupported_custom_formula(self):
        view = {
            "filterSpecs": [
                {
                    "columnIndex": 0,
                    "filterCriteria": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": "=A1>1"}],
                        }
                    },
                }
            ]
        }
        self.assertRaises(
            UnsupportedFilterViewRule, filter_view.extract_column_rules, view
        )

    def test_unsupported_date_rule(self):
        view = {
            "criteria": {
                "0": {
                    "condition": {
                        "type": "DATE_AFTER",
                        "values": [{"userEnteredValue": "2024-01-01"}],
                    }
                }
            }
        }
        self.assertRaises(
            UnsupportedFilterViewRule, filter_view.extract_column_rules, view
        )

    def test_unsupported_relative_date(self):
        view = {
            "filterSpecs": [
                {
                    "columnIndex": 0,
                    "filterCriteria": {
                        "condition": {
                            "type": "DATE_EQ",
                            "values": [{"relativeDate": "TODAY"}],
                        }
                    },
                }
            ]
        }
        self.assertRaises(
            UnsupportedFilterViewRule, filter_view.extract_column_rules, view
        )

    def test_unsupported_data_source_column(self):
        view = {
            "filterSpecs": [
                {
                    "dataSourceColumnReference": {"name": "col"},
                    "filterCriteria": {"hiddenValues": ["x"]},
                }
            ]
        }
        self.assertRaises(
            UnsupportedFilterViewRule, filter_view.extract_column_rules, view
        )

    def test_missing_column_index(self):
        self.assertRaises(
            FilterViewError,
            filter_view.extract_column_rules,
            {"filterSpecs": [{"filterCriteria": {"hiddenValues": ["x"]}}]},
        )


class ColumnRuleCheckTest(unittest.TestCase):
    def _rule(self, **criteria: Any):
        return filter_view.ColumnFilterRule.from_criteria(0, criteria)

    def test_text_contains_is_case_sensitive_and_uses_display_value(self):
        rule = self._rule(
            condition={
                "type": "TEXT_CONTAINS",
                "values": [{"userEnteredValue": "Abc"}],
            }
        )
        self.assertIsNone(rule.check("xxAbcxx", "unrelated"))
        self.assertEqual(rule.check("xxabcxx", "xxAbcxx"), "TEXT_CONTAINS")
        self.assertEqual(rule.check("Abc", "Abc"), None)

    def test_text_eq(self):
        rule = self._rule(
            condition={"type": "TEXT_EQ", "values": [{"userEnteredValue": "abc"}]}
        )
        self.assertIsNone(rule.check("abc", "abc"))
        self.assertEqual(rule.check("ABC", "ABC"), "TEXT_EQ")

    def test_numeric_conditions_use_unformatted_value_only(self):
        rule = self._rule(
            condition={
                "type": "NUMBER_GREATER",
                "values": [{"userEnteredValue": "1000"}],
            }
        )
        # displayed value "1,200" cannot be parsed, the number 1200 decides
        self.assertIsNone(rule.check("1,200", 1200))
        self.assertEqual(rule.check("2,000", 900), "NUMBER_GREATER")

    def test_numeric_condition_on_non_numeric_cell(self):
        rule = self._rule(
            condition={"type": "NUMBER_LESS", "values": [{"userEnteredValue": "10"}]}
        )
        self.assertEqual(rule.check("abc", "abc"), "NUMBER_LESS")

    def test_numeric_between(self):
        rule = self._rule(
            condition={
                "type": "NUMBER_BETWEEN",
                "values": [
                    {"userEnteredValue": "1"},
                    {"userEnteredValue": "3"},
                ],
            }
        )
        self.assertIsNone(rule.check("2", 2))
        self.assertIsNone(rule.check("1", 1))
        self.assertEqual(rule.check("4", 4), "NUMBER_BETWEEN")

    def test_hidden_values_win_over_the_condition(self):
        rule = self._rule(
            hiddenValues=["keep-out"],
            condition={
                "type": "TEXT_CONTAINS",
                "values": [{"userEnteredValue": "keep"}],
            },
        )
        self.assertEqual(rule.check("keep-out", "keep-out"), "HIDDEN_VALUE")
        self.assertIsNone(rule.check("keep-in", "keep-in"))

    def test_hidden_values_are_case_sensitive(self):
        rule = self._rule(hiddenValues=["ABC"])
        self.assertEqual(rule.check("ABC", "ABC"), "HIDDEN_VALUE")
        self.assertIsNone(rule.check("abc", "abc"))

    def test_blank_conditions(self):
        blank = self._rule(condition={"type": "BLANK"})
        not_blank = self._rule(condition={"type": "NOT_BLANK"})
        self.assertIsNone(blank.check("", ""))
        self.assertEqual(blank.check("x", "x"), "BLANK")
        self.assertIsNone(not_blank.check("x", "x"))
        self.assertEqual(not_blank.check("", ""), "NOT_BLANK")

    def test_condition_expects_numbers(self):
        self.assertRaises(
            FilterViewError,
            filter_view.ColumnFilterRule.from_criteria,
            0,
            {
                "condition": {
                    "type": "NUMBER_GREATER",
                    "values": [{"userEnteredValue": "ten"}],
                }
            },
        )


class BuildExportTest(unittest.TestCase):
    def _export(self, **kwargs: Any):
        options: Dict[str, Any] = {
            "filter_view": FILTER_VIEW,
            "grid_range": GRID_RANGE,
            "display_values": DISPLAY_VALUES,
            "numeric_values": NUMERIC_VALUES,
        }
        options.update(kwargs)
        return filter_view.build_export(**options)

    def test_exports_matching_rows_only(self):
        export = self._export()
        self.assertEqual([row.row_number for row in export.rows], [2, 5])
        self.assertEqual(
            [row.values for row in export.rows],
            [
                ["SKU-001", "alpha", "12"],
                ["SKU-004", "delta", "10.00"],
            ],
        )

    def test_headers_come_from_the_first_row_of_the_range(self):
        self.assertEqual(self._export().headers, ["SKU", "Name", "Qty"])

    def test_excluded_rows_report_the_rule(self):
        export = self._export()
        self.assertEqual([row.row_number for row in export.excluded_rows], [3, 4])

        quantity, hidden = export.excluded_rows
        self.assertEqual(quantity.rules, ["NUMBER_GREATER_THAN_EQ"])
        self.assertEqual(quantity.reasons[0].column_index, 2)
        self.assertIn("Qty", quantity.reasons[0].message)

        self.assertEqual(hidden.rules, ["HIDDEN_VALUE"])
        self.assertIn("SKU-003", hidden.reasons[0].message)

    def test_all_failing_rules_are_reported(self):
        view = {
            "filterViewId": 1,
            "filterSpecs": [
                {
                    "columnIndex": 0,
                    "filterCriteria": {
                        "condition": {
                            "type": "TEXT_CONTAINS",
                            "values": [{"userEnteredValue": "nope"}],
                        }
                    },
                },
                {
                    "columnIndex": 2,
                    "filterCriteria": {
                        "condition": {
                            "type": "NUMBER_GREATER",
                            "values": [{"userEnteredValue": "1000"}],
                        }
                    },
                },
            ],
        }
        export = self._export(filter_view=view)
        self.assertEqual(export.rows, [])
        first = export.excluded_rows[0]
        self.assertEqual(first.rules, ["TEXT_CONTAINS", "NUMBER_GREATER"])

    def test_conditions_are_combined_with_and(self):
        view = {
            "filterViewId": 1,
            "filterSpecs": [
                {
                    "columnIndex": 0,
                    "filterCriteria": {
                        "condition": {
                            "type": "TEXT_CONTAINS",
                            "values": [{"userEnteredValue": "SKU-00"}],
                        }
                    },
                },
                {
                    "columnIndex": 2,
                    "filterCriteria": {
                        "condition": {
                            "type": "NUMBER_LESS",
                            "values": [{"userEnteredValue": "10"}],
                        }
                    },
                },
            ],
        }
        export = self._export(filter_view=view)
        self.assertEqual([row.row_number for row in export.rows], [3])

    def test_original_row_numbers_and_order_are_preserved(self):
        export = self._export()
        numbers = [row.row_number for row in export.rows]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(numbers, [2, 5])

    def test_row_numbers_are_absolute(self):
        grid_range = dict(GRID_RANGE, startRowIndex=9, endRowIndex=15)
        display = [["SKU", "Name", "Qty"]] + DISPLAY_VALUES[1:]
        numeric = [["SKU", "Name", "Qty"]] + NUMERIC_VALUES[1:]
        export = self._export(
            grid_range=grid_range, display_values=display, numeric_values=numeric
        )
        self.assertEqual([row.row_number for row in export.rows], [11, 14])

    def test_blank_rows_are_skipped(self):
        export = self._export()
        self.assertEqual(export.match_count + export.excluded_count, 4)

        export = self._export(skip_blank_rows=False)
        self.assertEqual([row.row_number for row in export.excluded_rows], [3, 4, 6])

    def test_blank_row_in_the_middle_is_kept(self):
        display = [
            ["SKU", "Name", "Qty"],
            ["SKU-001", "alpha", "12"],
            ["", "", ""],
            ["SKU-004", "delta", "10.00"],
        ]
        numeric = [
            ["SKU", "Name", "Qty"],
            ["SKU-001", "alpha", 12],
            ["", "", ""],
            ["SKU-004", "delta", 10],
        ]
        view = {
            "filterViewId": 1,
            "filterSpecs": [
                {
                    "columnIndex": 0,
                    "filterCriteria": {"condition": {"type": "NOT_BLANK"}},
                }
            ],
        }
        export = self._export(
            filter_view=view,
            grid_range=dict(GRID_RANGE, endRowIndex=4),
            display_values=display,
            numeric_values=numeric,
        )
        self.assertEqual([row.row_number for row in export.rows], [2, 4])
        self.assertEqual([row.row_number for row in export.excluded_rows], [3])

    def test_trailing_blank_row_selected_by_a_blank_condition_is_kept(self):
        view = {
            "filterViewId": 1,
            "filterSpecs": [
                {
                    "columnIndex": 1,
                    "filterCriteria": {"condition": {"type": "BLANK"}},
                }
            ],
        }
        display = [
            ["SKU", "Name", "Qty"],
            ["SKU-001", "", "12"],
            ["SKU-002", "", "5"],
            ["", "", ""],
        ]
        numeric = [
            ["SKU", "Name", "Qty"],
            ["SKU-001", "", 12],
            ["SKU-002", "", 5],
            ["", "", ""],
        ]
        export = self._export(
            filter_view=view,
            grid_range=dict(GRID_RANGE, endRowIndex=4),
            display_values=display,
            numeric_values=numeric,
        )
        self.assertEqual([row.row_number for row in export.rows], [2, 3, 4])

    def test_manually_hidden_rows_are_marked_not_excluded(self):
        export = self._export(hidden_row_indexes=[0])
        self.assertEqual(export.manually_hidden_rows, [])
        self.assertEqual([row.row_number for row in export.rows], [2, 5])

        export = self._export(hidden_row_indexes=[1, 2])
        self.assertEqual([row.row_number for row in export.manually_hidden_rows], [2])
        self.assertEqual([row.row_number for row in export.visible_rows], [5])
        self.assertEqual([row.row_number for row in export.rows], [2, 5])

    def test_excluded_rows_are_marked_when_manually_hidden_too(self):
        export = self._export(hidden_row_indexes=[2])
        excluded = export.excluded_rows[0]
        self.assertEqual(excluded.row_number, 3)
        self.assertTrue(excluded.hidden_by_user)
        self.assertEqual(len(export.rows), 2)

    def test_to_records(self):
        export = self._export()
        self.assertEqual(
            export.to_records(),
            [
                {"SKU": "SKU-001", "Name": "alpha", "Qty": "12"},
                {"SKU": "SKU-004", "Name": "delta", "Qty": "10.00"},
            ],
        )

    def test_to_records_with_blank_and_duplicate_headers(self):
        display = [
            ["SKU", "", "SKU"],
            ["a", "b", "c"],
        ]
        numeric = display
        grid_range = dict(GRID_RANGE, endRowIndex=2)
        export = self._export(
            grid_range=grid_range,
            display_values=display,
            numeric_values=numeric,
            filter_view={"filterViewId": 1},
        )
        self.assertEqual(
            export.to_records(), [{"SKU": "a", "Column B": "b", "SKU (2)": "c"}]
        )

    def test_rule_outside_the_view_range(self):
        view = {
            "filterViewId": 1,
            "filterSpecs": [
                {"columnIndex": 9, "filterCriteria": {"hiddenValues": ["x"]}}
            ],
        }
        self.assertRaises(FilterViewError, self._export, filter_view=view)

    def test_export_is_iterable(self):
        export = self._export()
        self.assertEqual(len(export), 2)
        self.assertEqual([row.row_number for row in export], [2, 5])


class AlignValuesToGridTest(unittest.TestCase):
    def test_values_are_placed_back_on_the_requested_grid(self):
        grid_range = {
            "startRowIndex": 0,
            "endRowIndex": 6,
            "startColumnIndex": 1,
            "endColumnIndex": 4,
        }
        # the API trimmed the leading empty row and the trailing empty column
        aligned = filter_view.align_values_to_grid(
            [["a", "b"], ["c", "d"]], "'Sheet1'!B2:C3", grid_range
        )
        self.assertEqual(
            aligned,
            [
                ["", "", ""],
                ["a", "b", ""],
                ["c", "d", ""],
                ["", "", ""],
                ["", "", ""],
                ["", "", ""],
            ],
        )

    def test_values_without_returned_range_are_aligned_on_the_start(self):
        grid_range = {
            "startRowIndex": 0,
            "endRowIndex": 2,
            "startColumnIndex": 0,
            "endColumnIndex": 2,
        }
        self.assertEqual(
            filter_view.align_values_to_grid([["a", "b"]], None, grid_range),
            [["a", "b"], ["", ""]],
        )

    def test_values_outside_the_grid_are_ignored(self):
        grid_range = {
            "startRowIndex": 0,
            "endRowIndex": 1,
            "startColumnIndex": 0,
            "endColumnIndex": 2,
        }
        aligned = filter_view.align_values_to_grid(
            [["a", "b", "c"], ["d"]], "'Sheet1'!A1:C2", grid_range
        )
        self.assertEqual(aligned, [["a", "b"]])

    def test_trimmed_values_keep_their_row_numbers_in_the_export(self):
        grid_range = {
            "sheetId": SHEET_ID,
            "startRowIndex": 1,
            "endRowIndex": 5,
            "startColumnIndex": 0,
            "endColumnIndex": 2,
        }
        view = {
            "filterViewId": 1,
            "filterSpecs": [
                {
                    "columnIndex": 1,
                    "filterCriteria": {
                        "condition": {
                            "type": "NUMBER_GREATER",
                            "values": [{"userEnteredValue": "5"}],
                        }
                    },
                }
            ],
        }
        export = filter_view.build_export(
            filter_view=view,
            grid_range=grid_range,
            # the header is sheet row 2, the value rows are rows 3 and 4,
            # the last row of the range (sheet row 5) is empty and was trimmed
            display_values=[["SKU", "Qty"], ["SKU-2", "1"], ["SKU-3", "6"]],
            numeric_values=[["SKU", "Qty"], ["SKU-2", 1], ["SKU-3", 6]],
            display_value_range="'Sheet1'!A2:B4",
            numeric_value_range="'Sheet1'!A2:B4",
        )
        self.assertEqual(export.headers, ["SKU", "Qty"])
        self.assertEqual([row.row_number for row in export.rows], [4])
        self.assertEqual([row.row_number for row in export.excluded_rows], [3])


class WorksheetExportFilterViewTest(unittest.TestCase):
    def test_exports_the_selected_filter_view(self):
        worksheet, _ = make_worksheet()

        export = worksheet.export_filter_view(123)

        self.assertEqual(export.filter_view_id, 123)
        self.assertEqual(export.title, "Inbound")
        self.assertEqual(export.sheet_title, "Sheet1")
        self.assertEqual(export.range, "A1:C6")
        self.assertEqual(export.headers, ["SKU", "Name", "Qty"])
        self.assertEqual([row.row_number for row in export.rows], [2, 5])
        self.assertEqual([row.row_number for row in export.excluded_rows], [3, 4])

    def test_view_is_read_with_formatted_and_unformatted_values(self):
        worksheet, client = make_worksheet()

        worksheet.export_filter_view(123)

        reads = [call for call in client.calls if call["method"] == "values_get"]
        self.assertEqual(len(reads), 2)
        for read in reads:
            self.assertTrue(read["range"].endswith("A1:C6"))
            self.assertTrue(read["range"].startswith("'Sheet1'!"))
        render_options = sorted(read["params"]["valueRenderOption"] for read in reads)
        self.assertEqual(render_options, ["FORMATTED_VALUE", "UNFORMATTED_VALUE"])

    def test_export_never_writes_to_the_worksheet(self):
        # FakeHTTPClient.batch_update raises on any write attempt
        worksheet, _ = make_worksheet()
        worksheet.export_filter_view(123)

    def test_unknown_filter_view(self):
        worksheet, _ = make_worksheet()
        self.assertRaises(FilterViewNotFound, worksheet.export_filter_view, 999)

    def test_unsupported_rule_fails_the_whole_export(self):
        view = {
            "filterViewId": 123,
            "range": GRID_RANGE,
            "filterSpecs": [
                {
                    "columnIndex": 0,
                    "filterCriteria": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": "=A2>1"}],
                        }
                    },
                }
            ],
        }
        worksheet, _ = make_worksheet(sheet_metadata(filter_views=[view]))
        self.assertRaises(UnsupportedFilterViewRule, worksheet.export_filter_view, 123)

    def test_uses_the_named_range_of_the_view(self):
        view = {"filterViewId": 7, "namedRangeId": "nr-1"}
        named_ranges = [
            {
                "namedRangeId": "nr-1",
                "name": "Inbound",
                "range": {
                    "sheetId": SHEET_ID,
                    "startRowIndex": 0,
                    "endRowIndex": 2,
                    "startColumnIndex": 0,
                    "endColumnIndex": 3,
                },
            }
        ]
        metadata = sheet_metadata(filter_views=[view], named_ranges=named_ranges)
        worksheet, client = make_worksheet(metadata)

        export = worksheet.export_filter_view(7)

        self.assertEqual(export.range, "A1:C2")
        reads = [call for call in client.calls if call["method"] == "values_get"]
        self.assertTrue(reads[0]["range"].endswith("A1:C2"))

    def test_does_not_use_the_basic_filter_of_the_worksheet(self):
        metadata = sheet_metadata()
        metadata["sheets"][0]["basicFilter"] = {
            "range": {
                "sheetId": SHEET_ID,
                "startRowIndex": 0,
                "endRowIndex": 100,
                "startColumnIndex": 0,
                "endColumnIndex": 10,
            }
        }
        worksheet, client = make_worksheet(metadata)

        worksheet.export_filter_view(123)

        reads = [call for call in client.calls if call["method"] == "values_get"]
        for read in reads:
            self.assertIn("A1:C6", read["range"])

    def test_manually_hidden_rows_are_marked(self):
        data_metadata = {
            "sheets": [
                {
                    "properties": {"sheetId": SHEET_ID},
                    "data": [
                        {
                            "startRow": 0,
                            "rowMetadata": [
                                {"hiddenByUser": False},
                                {"hiddenByUser": True},
                                {"hiddenByUser": False},
                            ],
                        }
                    ],
                }
            ]
        }
        worksheet, _ = make_worksheet(data_metadata=data_metadata)

        export = worksheet.export_filter_view(123)

        self.assertEqual([row.row_number for row in export.rows], [2, 5])
        self.assertEqual([row.row_number for row in export.manually_hidden_rows], [2])
        # rows excluded by the view are still reported as excluded
        self.assertEqual([row.row_number for row in export.excluded_rows], [3, 4])

    def test_marking_hidden_rows_can_be_disabled(self):
        worksheet, client = make_worksheet()

        export = worksheet.export_filter_view(123, mark_hidden_rows=False)

        self.assertEqual(export.manually_hidden_rows, [])
        self.assertEqual(
            len([c for c in client.calls if c["method"] == "fetch_sheet_metadata"]),
            1,
        )

    def test_collapsed_row_groups_are_reported_as_manually_hidden(self):
        metadata = sheet_metadata(
            row_groups=[
                {
                    "range": {"startIndex": 1, "endIndex": 2},
                    "depth": 0,
                    "collapsed": True,
                }
            ]
        )
        data_metadata = {"sheets": [{"properties": {"sheetId": SHEET_ID}}]}
        worksheet, _ = make_worksheet(metadata, data_metadata=data_metadata)

        export = worksheet.export_filter_view(123)

        self.assertEqual([row.row_number for row in export.manually_hidden_rows], [2])

    def test_hidden_row_metadata_and_collapsed_groups_are_merged(self):
        metadata = sheet_metadata(
            row_groups=[
                {
                    "range": {"startIndex": 4, "endIndex": 5},
                    "depth": 0,
                    "collapsed": True,
                }
            ]
        )
        data_metadata = {
            "sheets": [
                {
                    "properties": {"sheetId": SHEET_ID},
                    "data": [
                        {
                            "startRow": 0,
                            "rowMetadata": [
                                {"hiddenByUser": False},
                                {"hiddenByUser": True},
                                {"hiddenByUser": False},
                                {"hiddenByUser": False},
                                {"hiddenByUser": False},
                                {"hiddenByUser": False},
                            ],
                        }
                    ],
                }
            ]
        }
        worksheet, _ = make_worksheet(metadata, data_metadata=data_metadata)

        export = worksheet.export_filter_view(123)

        self.assertEqual(
            [row.row_number for row in export.manually_hidden_rows], [2, 5]
        )

    def test_get_filter_views(self):
        worksheet, _ = make_worksheet()
        views = worksheet.get_filter_views()
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0]["filterViewId"], 123)

    def test_get_filter_view(self):
        worksheet, _ = make_worksheet()
        self.assertEqual(worksheet.get_filter_view(123)["title"], "Inbound")
        self.assertRaises(FilterViewNotFound, worksheet.get_filter_view, 1)

    def test_metadata_request_is_bounded_to_the_filter_view_fields(self):
        worksheet, client = make_worksheet()

        worksheet.export_filter_view(123)

        metadata_calls = [
            call for call in client.calls if call["method"] == "fetch_sheet_metadata"
        ]
        fields = metadata_calls[0]["params"]["fields"]
        self.assertIn("filterViews", fields)
        self.assertIn("namedRanges", fields)
        self.assertIn("filterSpecs", fields)


class ExceptionsAreExportedTest(unittest.TestCase):
    def test_exceptions_are_available_from_the_package(self):
        self.assertTrue(issubclass(gspread.FilterViewError, gspread.GSpreadException))
        self.assertTrue(
            issubclass(gspread.UnsupportedFilterViewRule, gspread.FilterViewError)
        )
        self.assertIs(gspread.filter_view, filter_view)
