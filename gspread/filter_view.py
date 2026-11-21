"""
gspread.filter_view
~~~~~~~~~~~~~~~~~~~

Read-only export of the rows selected by a saved Google Sheets *filter view*.

The Sheets API exposes the filter views of a sheet as ``FilterView`` objects.
A filter view is a saved set of per column rules plus a bounded range. This
module implements the client side evaluation of those rules so that the
matching rows can be exported without modifying the worksheet.

The evaluation follows the semantics of the Sheets API:

* ``filterSpecs`` takes precedence over the deprecated ``criteria`` map.
* A value listed in ``hiddenValues`` is always hidden, even if it also
  satisfies the column ``condition``.
* Text conditions are evaluated on the displayed (formatted) value and are
  case sensitive.
* Numeric conditions are evaluated on the computed (unformatted) number only.
* All column rules must match for a row to be exported (logical AND).

Rules that cannot be evaluated client side (colors, custom formulas, dates,
data source columns, ...) raise
:class:`~gspread.exceptions.UnsupportedFilterViewRule` so that an export never
silently returns a partial result.
"""

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .exceptions import (
    FilterViewError,
    FilterViewNotFound,
    GSpreadException,
    UnsupportedFilterViewRule,
)
from .utils import a1_range_to_grid_range, fill_gaps, rowcol_to_a1

__all__ = [
    "ColumnFilterRule",
    "FilterViewExclusion",
    "FilterViewExcludedRow",
    "FilterViewExport",
    "FilterViewRow",
    "align_values_to_grid",
    "build_export",
    "collapsed_row_indexes",
    "column_index_to_letter",
    "extract_column_rules",
    "find_filter_view",
    "grid_range_to_a1",
    "resolve_grid_range",
]

#: Condition types this module knows how to evaluate.
TEXT_CONDITION_TYPES = frozenset(
    [
        "TEXT_CONTAINS",
        "TEXT_NOT_CONTAINS",
        "TEXT_EQ",
        "TEXT_NOT_EQ",
        "TEXT_STARTS_WITH",
        "TEXT_ENDS_WITH",
    ]
)

NUMBER_CONDITION_TYPES = frozenset(
    [
        "NUMBER_GREATER",
        "NUMBER_GREATER_THAN_EQ",
        "NUMBER_LESS",
        "NUMBER_LESS_THAN_EQ",
        "NUMBER_EQ",
        "NUMBER_NOT_EQ",
        "NUMBER_BETWEEN",
        "NUMBER_NOT_BETWEEN",
    ]
)

EMPTY_CONDITION_TYPES = frozenset(["BLANK", "NOT_BLANK"])

SUPPORTED_CONDITION_TYPES = (
    TEXT_CONDITION_TYPES | NUMBER_CONDITION_TYPES | EMPTY_CONDITION_TYPES
)

_BETWEEN_CONDITION_TYPES = frozenset(["NUMBER_BETWEEN", "NUMBER_NOT_BETWEEN"])

#: Fields requested from the Sheets API to resolve and evaluate a filter view.
FILTER_VIEW_METADATA_FIELDS = (
    "namedRanges(namedRangeId,range),"
    "sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)),"
    "filterViews(filterViewId,title,range,namedRangeId,tableId,"
    "filterSpecs,criteria),"
    "rowGroups(range,collapsed),"
    "tables(tableId,range))"
)

#: Fields requested when the manually hidden rows must be discovered.
HIDDEN_ROWS_METADATA_FIELDS = (
    "sheets(properties(sheetId),"
    "data(startRow,rowMetadata(hiddenByUser)),"
    "rowGroups(range,collapsed))"
)

#: Keys of a ``FilterCriteria`` that hold color based rules.
_COLOR_CRITERIA_KEYS = (
    "visibleBackgroundColor",
    "visibleBackgroundColorStyle",
    "visibleForegroundColor",
    "visibleForegroundColorStyle",
)


def column_index_to_letter(column_index: int) -> str:
    """Convert a zero-based column index to its A1 column letters.

    Examples::

        >>> column_index_to_letter(0)
        'A'
        >>> column_index_to_letter(27)
        'AB'
    """
    if column_index < 0:
        raise FilterViewError(
            "column index must be zero or positive, got {}".format(column_index)
        )

    index = int(column_index) + 1
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def grid_range_to_a1(grid_range: Mapping[str, int]) -> str:
    """Convert a bounded ``GridRange`` dict to an A1 notation range.

    All indexes of a GridRange are zero-based and half-open, A1 notation is
    one-based and inclusive.
    """
    try:
        start_row = int(grid_range.get("startRowIndex", 0))
        end_row = int(grid_range["endRowIndex"])
        start_column = int(grid_range.get("startColumnIndex", 0))
        end_column = int(grid_range["endColumnIndex"])
    except (KeyError, TypeError, ValueError):
        raise FilterViewError(
            "the filter view range must be bounded, got {!r}".format(dict(grid_range))
        )

    start = rowcol_to_a1(start_row + 1, start_column + 1)
    end = rowcol_to_a1(end_row, end_column)
    if start == end:
        return start
    return "{}:{}".format(start, end)


def align_values_to_grid(
    values: Sequence[Sequence[Any]],
    returned_range: Optional[str],
    grid_range: Mapping[str, int],
) -> List[List[Any]]:
    """Place the values returned by the API back on the requested grid.

    The ``values.get`` endpoint trims the empty leading and trailing rows and
    columns of the requested range, so the values it returns do not line up
    with the range anymore. The returned ``range`` (e.g. ``'Sheet1'!B2:D6``)
    tells where the values actually start. The values are padded to the exact
    size of ``grid_range`` so that each row keeps its sheet row number.

    :param values: the matrix returned by the API.
    :param returned_range: the ``range`` field of the API response.
    :param grid_range: the bounded range that was requested.
    """
    start_row = int(grid_range.get("startRowIndex", 0))
    start_column = int(grid_range.get("startColumnIndex", 0))
    end_row = int(grid_range["endRowIndex"])
    end_column = int(grid_range["endColumnIndex"])

    height = end_row - start_row
    width = end_column - start_column

    row_offset = 0
    column_offset = 0

    if returned_range:
        # the response range looks like ``'Sheet1'!B2:D6``; split on the last
        # ``!`` so that a sheet title containing one is still supported
        a1_range = returned_range.rsplit("!", 1)[-1]
        try:
            returned_grid_range = a1_range_to_grid_range(a1_range)
        except (GSpreadException, ValueError, TypeError):
            # an unexpected range is not fatal: the values are then aligned
            # on the requested grid as if the API had not trimmed anything
            returned_grid_range = {}

        row_offset = (
            int(returned_grid_range.get("startRowIndex", start_row)) - start_row
        )
        column_offset = (
            int(returned_grid_range.get("startColumnIndex", start_column))
            - start_column
        )

    matrix: List[List[Any]] = [["" for _ in range(width)] for _ in range(height)]

    for source_row, row in enumerate(values):
        target_row = row_offset + source_row
        if not 0 <= target_row < height:
            continue
        for source_column, value in enumerate(row):
            target_column = column_offset + source_column
            if 0 <= target_column < width:
                matrix[target_row][target_column] = value

    return matrix


def find_filter_view(
    filter_views: Iterable[Mapping[str, Any]], filter_view_id: int
) -> Mapping[str, Any]:
    """Return the filter view with the given ID.

    :raises gspread.exceptions.FilterViewError: if the ID is not an integer.
    :raises gspread.exceptions.FilterViewNotFound: if no such view exists.
    """
    try:
        wanted = int(filter_view_id)
    except (TypeError, ValueError):
        raise FilterViewError(
            "filter_view_id must be an integer, got {!r}".format(filter_view_id)
        )

    for filter_view in filter_views or []:
        try:
            view_id = int(filter_view.get("filterViewId", -1))
        except (TypeError, ValueError):
            continue
        if view_id == wanted:
            return filter_view

    raise FilterViewNotFound(
        "filter view with ID {} was not found in this worksheet".format(wanted)
    )


def _bounded_grid_range(
    grid_range: Mapping[str, Any],
    *,
    sheet_id: Optional[int],
    row_count: Optional[int],
    column_count: Optional[int],
) -> Dict[str, int]:
    """Fill the missing bounds of a GridRange and validate it."""
    range_sheet_id = grid_range.get("sheetId")
    if (
        range_sheet_id is not None
        and sheet_id is not None
        and int(range_sheet_id) != int(sheet_id)
    ):
        raise FilterViewError(
            "the filter view range belongs to sheet {} but the worksheet is {}".format(
                range_sheet_id, sheet_id
            )
        )

    start_row = int(grid_range.get("startRowIndex", 0))
    start_column = int(grid_range.get("startColumnIndex", 0))

    end_row = grid_range.get("endRowIndex")
    end_column = grid_range.get("endColumnIndex")

    if end_row is None:
        if row_count is None:
            raise FilterViewError(
                "the filter view range has no end row and the sheet size is unknown"
            )
        end_row = int(row_count)
    if end_column is None:
        if column_count is None:
            raise FilterViewError(
                "the filter view range has no end column and the sheet size is unknown"
            )
        end_column = int(column_count)

    end_row = int(end_row)
    end_column = int(end_column)

    if start_row < 0 or start_column < 0:
        raise FilterViewError(
            "the filter view range cannot start before the first row or column"
        )

    if end_row <= start_row or end_column <= start_column:
        raise FilterViewError(
            "the filter view range must contain at least one row and one column"
        )

    bounded = {
        "startRowIndex": start_row,
        "endRowIndex": end_row,
        "startColumnIndex": start_column,
        "endColumnIndex": end_column,
    }

    if sheet_id is not None:
        bounded["sheetId"] = int(sheet_id)

    return bounded


def resolve_grid_range(
    filter_view: Mapping[str, Any],
    *,
    sheet_metadata: Optional[Mapping[str, Any]] = None,
    named_ranges: Iterable[Mapping[str, Any]] = (),
    sheet_id: Optional[int] = None,
    row_count: Optional[int] = None,
    column_count: Optional[int] = None,
) -> Dict[str, int]:
    """Resolve the actual bounded range covered by a filter view.

    A filter view is backed by one of: a GridRange (``range``), a named range
    (``namedRangeId``) or a table (``tableId``). The returned ``GridRange`` is
    zero-based and half-open, with all the bounds filled.

    :raises gspread.exceptions.FilterViewError: if the range cannot be resolved.
    """
    view_range = filter_view.get("range") or {}

    if view_range:
        return _bounded_grid_range(
            view_range,
            sheet_id=sheet_id,
            row_count=row_count,
            column_count=column_count,
        )

    named_range_id = filter_view.get("namedRangeId")
    if named_range_id:
        for named_range in named_ranges or []:
            if named_range.get("namedRangeId") == named_range_id:
                return _bounded_grid_range(
                    named_range.get("range") or {},
                    sheet_id=sheet_id,
                    row_count=row_count,
                    column_count=column_count,
                )

        raise FilterViewError(
            "the filter view is backed by the named range {!r} "
            "but it was not found in the spreadsheet".format(named_range_id)
        )

    table_id = filter_view.get("tableId")
    if table_id:
        for table in (sheet_metadata or {}).get("tables") or []:
            if table.get("tableId") == table_id:
                return _bounded_grid_range(
                    table.get("range") or {},
                    sheet_id=sheet_id,
                    row_count=row_count,
                    column_count=column_count,
                )

        raise FilterViewError(
            "the filter view is backed by the table {!r} "
            "but it was not found in the worksheet".format(table_id)
        )

    raise FilterViewError(
        "the filter view has no range, named range or table to resolve its bounds"
    )


def _to_text(value: Any) -> str:
    """Render a value the same way the Sheets UI displays it as text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value)


def _to_number(value: Any) -> Optional[float]:
    """Convert a value to a finite float, or return None."""
    if isinstance(value, bool) or value is None:
        return None

    number: Optional[float]
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None

    if number is None or not math.isfinite(number):
        return None
    return number


class ColumnFilterRule:
    """A single column rule of a filter view.

    :ivar int column_index: zero-based index of the column in the sheet.
    :ivar list hidden_values: displayed values that are always hidden.
    :ivar str condition_type: the ``BooleanCondition.type`` or ``None``.
    :ivar list condition_values: the raw ``userEnteredValue`` entries.
    """

    def __init__(
        self,
        column_index: int,
        hidden_values: Sequence[str] = (),
        condition_type: Optional[str] = None,
        condition_values: Sequence[Any] = (),
    ):
        self.column_index = int(column_index)
        self.hidden_values: List[str] = [str(value) for value in hidden_values]
        self.condition_type = condition_type
        self.condition_values: List[Any] = list(condition_values)

    def __repr__(self) -> str:
        return "<{} column:{} rule:{}>".format(
            self.__class__.__name__,
            column_index_to_letter(self.column_index),
            self.condition_type or "hiddenValues",
        )

    @property
    def column_letter(self) -> str:
        """A1 letters of the column this rule applies to."""
        return column_index_to_letter(self.column_index)

    @property
    def is_empty(self) -> bool:
        """True when the rule does not restrict anything."""
        return not self.hidden_values and self.condition_type is None

    def describe(self) -> str:
        """Human readable description of the rule."""
        if self.condition_type is None:
            return "hidden values {}".format(list(self.hidden_values))

        if self.condition_type in _BETWEEN_CONDITION_TYPES:
            return "{} between {} and {}".format(
                self.condition_type,
                _to_text(self.condition_values[0]),
                _to_text(self.condition_values[1]),
            )

        if self.condition_type in EMPTY_CONDITION_TYPES:
            return self.condition_type

        return "{} {!r}".format(self.condition_type, _to_text(self.condition_values[0]))

    @classmethod
    def from_criteria(
        cls, column_index: int, criteria: Mapping[str, Any]
    ) -> "ColumnFilterRule":
        """Build a rule from a ``FilterCriteria`` object.

        :raises gspread.exceptions.UnsupportedFilterViewRule: when the criteria
            contains a rule that cannot be evaluated client side.
        """
        _reject_color_rules(column_index, criteria)

        hidden_values = [str(value) for value in criteria.get("hiddenValues") or []]

        condition = criteria.get("condition") or {}
        condition_type = condition.get("type") or None

        if condition_type is None:
            # an empty FilterCriteria does not filter anything
            return cls(column_index, hidden_values=hidden_values)

        condition_values = _parse_condition_values(
            column_index, condition_type, condition.get("values") or []
        )

        return cls(
            column_index,
            hidden_values=hidden_values,
            condition_type=condition_type,
            condition_values=condition_values,
        )

    def check(self, display_value: Any, numeric_value: Any) -> Optional[str]:
        """Evaluate the rule for one cell.

        :param display_value: the cell value as displayed (formatted).
        :param numeric_value: the computed (unformatted) cell value.

        :returns: ``None`` when the cell passes every part of the rule,
            otherwise the name of the part that rejected it.
        """
        display_text = _to_text(display_value)

        # hidden values always win over the condition, and are compared
        # against the displayed value, case sensitively
        if self.hidden_values and display_text in self.hidden_values:
            return "HIDDEN_VALUE"

        if self.condition_type is None:
            return None

        if not self._matches_condition(display_text, numeric_value):
            return self.condition_type

        return None

    def _matches_condition(self, display_text: str, numeric_value: Any) -> bool:
        condition_type = self.condition_type
        assert condition_type is not None

        if condition_type in EMPTY_CONDITION_TYPES:
            return _matches_blank(condition_type, display_text)

        if condition_type in TEXT_CONDITION_TYPES:
            return _compare_text(
                condition_type, display_text, _to_text(self.condition_values[0])
            )

        number = _to_number(numeric_value)
        if number is None:
            return False

        return _compare_number(condition_type, number, self.condition_values)


def _reject_color_rules(column_index: int, criteria: Mapping[str, Any]) -> None:
    for key in _COLOR_CRITERIA_KEYS:
        if criteria.get(key):
            raise UnsupportedFilterViewRule(
                "the filter view uses a color rule ({}) on column {}; "
                "color rules cannot be exported".format(
                    key, column_index_to_letter(column_index)
                )
            )


def _parse_condition_values(
    column_index: int,
    condition_type: str,
    raw_values: Sequence[Mapping[str, Any]],
) -> List[Any]:
    """Validate a ``BooleanCondition`` and return its raw values."""
    label = column_index_to_letter(column_index)

    if condition_type not in SUPPORTED_CONDITION_TYPES:
        raise UnsupportedFilterViewRule(
            "the filter view uses the unsupported condition {} on column {}; "
            "supported conditions are: {}".format(
                condition_type,
                label,
                ", ".join(sorted(SUPPORTED_CONDITION_TYPES)),
            )
        )

    for raw_value in raw_values:
        if "relativeDate" in raw_value:
            raise UnsupportedFilterViewRule(
                "the filter view uses a relative date on column {}; "
                "relative dates cannot be exported".format(label)
            )

    values = [raw_value.get("userEnteredValue") for raw_value in raw_values]

    if condition_type in EMPTY_CONDITION_TYPES:
        expected_count = 0
    elif condition_type in _BETWEEN_CONDITION_TYPES:
        expected_count = 2
    else:
        expected_count = 1

    if len(values) != expected_count:
        raise FilterViewError(
            "the condition {} on column {} expects {} value(s), got {}".format(
                condition_type, label, expected_count, len(values)
            )
        )

    if condition_type in NUMBER_CONDITION_TYPES:
        for value in values:
            if _to_number(value) is None:
                raise FilterViewError(
                    "the condition {} on column {} expects numbers, got {!r}".format(
                        condition_type, label, value
                    )
                )

    return values


def _matches_blank(condition_type: str, display_text: str) -> bool:
    if condition_type == "BLANK":
        return display_text == ""
    return display_text != ""


def _compare_text(condition_type: str, value: str, expected: str) -> bool:
    if condition_type == "TEXT_CONTAINS":
        return expected in value
    if condition_type == "TEXT_NOT_CONTAINS":
        return expected not in value
    if condition_type == "TEXT_EQ":
        return value == expected
    if condition_type == "TEXT_NOT_EQ":
        return value != expected
    if condition_type == "TEXT_STARTS_WITH":
        return value.startswith(expected)
    if condition_type == "TEXT_ENDS_WITH":
        return value.endswith(expected)
    raise UnsupportedFilterViewRule(
        "the condition {} cannot be evaluated".format(condition_type)
    )


def _compare_number(
    condition_type: str, number: float, condition_values: Sequence[Any]
) -> bool:
    if condition_type in _BETWEEN_CONDITION_TYPES:
        lower = _to_number(condition_values[0])
        upper = _to_number(condition_values[1])
        assert lower is not None and upper is not None
        inside = lower <= number <= upper
        return inside if condition_type == "NUMBER_BETWEEN" else not inside

    expected = _to_number(condition_values[0])
    assert expected is not None

    if condition_type == "NUMBER_GREATER":
        return number > expected
    if condition_type == "NUMBER_GREATER_THAN_EQ":
        return number >= expected
    if condition_type == "NUMBER_LESS":
        return number < expected
    if condition_type == "NUMBER_LESS_THAN_EQ":
        return number <= expected
    if condition_type == "NUMBER_EQ":
        return number == expected
    if condition_type == "NUMBER_NOT_EQ":
        return number != expected

    raise UnsupportedFilterViewRule(
        "the condition {} cannot be evaluated".format(condition_type)
    )


def extract_column_rules(filter_view: Mapping[str, Any]) -> List[ColumnFilterRule]:
    """Extract the column rules of a filter view.

    ``filterSpecs`` takes precedence over the deprecated ``criteria`` map,
    exactly as the Sheets API does.

    :raises gspread.exceptions.UnsupportedFilterViewRule: when the filter view
        contains a rule that cannot be evaluated client side.
    """
    filter_specs = filter_view.get("filterSpecs") or []

    if filter_specs:
        return _rules_from_filter_specs(filter_specs)

    return _rules_from_criteria(filter_view.get("criteria") or {})


def _rules_from_filter_specs(
    filter_specs: Sequence[Mapping[str, Any]],
) -> List[ColumnFilterRule]:
    rules: List[ColumnFilterRule] = []

    for filter_spec in filter_specs:
        if filter_spec.get("dataSourceColumnReference"):
            raise UnsupportedFilterViewRule(
                "the filter view filters a data source column; "
                "data source columns cannot be exported"
            )

        if "columnIndex" not in filter_spec:
            raise FilterViewError(
                "a filter view rule has no column index: {!r}".format(filter_spec)
            )

        rule = ColumnFilterRule.from_criteria(
            int(filter_spec["columnIndex"]), filter_spec.get("filterCriteria") or {}
        )
        if not rule.is_empty:
            rules.append(rule)

    return rules


def _rules_from_criteria(
    raw_criteria: Mapping[Any, Mapping[str, Any]]
) -> List[ColumnFilterRule]:
    normalized: List[Tuple[int, Mapping[str, Any]]] = []

    for key, criteria in raw_criteria.items():
        try:
            column_index = int(key)
        except (TypeError, ValueError):
            raise FilterViewError(
                "the filter view criteria has a non numeric column key: {!r}".format(
                    key
                )
            )
        normalized.append((column_index, criteria or {}))

    # the criteria map has no meaningful order, keep it deterministic
    normalized.sort(key=lambda item: item[0])

    rules: List[ColumnFilterRule] = []
    for column_index, criteria in normalized:
        rule = ColumnFilterRule.from_criteria(column_index, criteria)
        if not rule.is_empty:
            rules.append(rule)

    return rules


class FilterViewExclusion:
    """The reason why one column rule rejected a row."""

    def __init__(
        self,
        column_index: int,
        rule: str,
        expected: str,
        actual: str,
        column_header: Optional[str] = None,
    ):
        self.column_index = column_index
        self.rule = rule
        self.expected = expected
        self.actual = actual
        self.column_header = column_header

    def __repr__(self) -> str:
        return "<{} column:{} rule:{}>".format(
            self.__class__.__name__, self.column_letter, self.rule
        )

    @property
    def column_letter(self) -> str:
        """A1 letters of the column that rejected the row."""
        return column_index_to_letter(self.column_index)

    @property
    def message(self) -> str:
        """Human readable explanation of the exclusion."""
        label = "column {}".format(self.column_letter)
        if self.column_header:
            label += " ({!r})".format(self.column_header)

        if self.rule == "HIDDEN_VALUE":
            return "excluded by {}: value {!r} is a hidden value".format(
                label, self.actual
            )

        return "excluded by {}: value {!r} does not satisfy {}".format(
            label, self.actual, self.expected
        )

    def __str__(self) -> str:
        return self.message


class FilterViewRow:
    """A row exported by a filter view."""

    def __init__(
        self,
        row_number: int,
        values: Sequence[Any],
        hidden_by_user: bool = False,
    ):
        self.row_number = int(row_number)
        self.values: List[str] = [str(value) for value in values]
        self.hidden_by_user = bool(hidden_by_user)

    def __repr__(self) -> str:
        return "<{} row:{} hidden_by_user:{}>".format(
            self.__class__.__name__, self.row_number, self.hidden_by_user
        )


class FilterViewExcludedRow:
    """A row rejected by at least one column rule of the filter view."""

    def __init__(
        self,
        row_number: int,
        values: Sequence[Any],
        reasons: Sequence[FilterViewExclusion],
        hidden_by_user: bool = False,
    ):
        self.row_number = int(row_number)
        self.values: List[str] = [str(value) for value in values]
        self.reasons: List[FilterViewExclusion] = list(reasons)
        self.hidden_by_user = bool(hidden_by_user)

    def __repr__(self) -> str:
        return "<{} row:{} reasons:{}>".format(
            self.__class__.__name__, self.row_number, len(self.reasons)
        )

    @property
    def rules(self) -> List[str]:
        """The names of the rules that rejected this row."""
        return [reason.rule for reason in self.reasons]


class FilterViewExport:
    """The result of :meth:`gspread.worksheet.Worksheet.export_filter_view`.

    :ivar int filter_view_id: ID of the filter view that was exported.
    :ivar str title: title of the filter view.
    :ivar str range: A1 notation of the bounded range of the filter view.
    :ivar dict grid_range: the bounded ``GridRange`` of the filter view.
    :ivar list headers: displayed values of the first row of the range.
    :ivar list rows: the exported rows, in the original sheet order.
    :ivar list excluded_rows: the rows rejected by the filter view, in the
        original sheet order, each with the list of rules that rejected it.
    """

    def __init__(
        self,
        filter_view_id: int,
        grid_range: Mapping[str, int],
        headers: Sequence[Any],
        rows: Sequence[FilterViewRow],
        excluded_rows: Sequence[FilterViewExcludedRow],
        title: Optional[str] = None,
        sheet_title: Optional[str] = None,
    ):
        self.filter_view_id = int(filter_view_id)
        self.title = title
        self.sheet_title = sheet_title
        self.grid_range: Dict[str, int] = dict(grid_range)
        self.range = grid_range_to_a1(grid_range)
        self.headers: List[str] = [str(value) for value in headers]
        self.rows: List[FilterViewRow] = list(rows)
        self.excluded_rows: List[FilterViewExcludedRow] = list(excluded_rows)

    def __repr__(self) -> str:
        return "<{} id:{} rows:{} excluded:{}>".format(
            self.__class__.__name__,
            self.filter_view_id,
            len(self.rows),
            len(self.excluded_rows),
        )

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Any:
        return iter(self.rows)

    @property
    def match_count(self) -> int:
        """Number of exported rows."""
        return len(self.rows)

    @property
    def excluded_count(self) -> int:
        """Number of rows excluded by the filter view."""
        return len(self.excluded_rows)

    @property
    def manually_hidden_rows(self) -> List[FilterViewRow]:
        """Exported rows that are manually hidden in the worksheet."""
        return [row for row in self.rows if row.hidden_by_user]

    @property
    def visible_rows(self) -> List[FilterViewRow]:
        """Exported rows that are not manually hidden in the worksheet."""
        return [row for row in self.rows if not row.hidden_by_user]

    def to_records(self) -> List[Dict[str, str]]:
        """Return the exported rows as a list of dictionaries.

        The keys are the headers of the filter view. Empty headers are
        replaced by the A1 letters of their column.
        """
        keys = _record_keys(self.headers, self.grid_range)
        return [dict(zip(keys, row.values)) for row in self.rows]


def _record_keys(headers: Sequence[str], grid_range: Mapping[str, int]) -> List[str]:
    start_column = int(grid_range.get("startColumnIndex", 0))
    keys: List[str] = []
    used: Dict[str, int] = {}

    for offset, header in enumerate(headers):
        key = (
            header
            if header
            else "Column {}".format(column_index_to_letter(start_column + offset))
        )

        if key in used:
            used[key] += 1
            key = "{} ({})".format(key, used[key])
        else:
            used[key] = 1

        keys.append(key)

    return keys


def _column_header(headers: Sequence[str], offset: int) -> Optional[str]:
    if 0 <= offset < len(headers):
        return headers[offset] or None
    return None


def _data_end_offset(
    display_matrix: Sequence[Sequence[Any]],
    numeric_matrix: Sequence[Sequence[Any]],
    rules: Sequence[ColumnFilterRule],
    start_column: int,
    skip_blank_rows: bool,
) -> int:
    """Return the exclusive offset where the data of the view ends.

    The rows after the last non empty row of a sheet are empty padding of the
    grid, not data. When ``skip_blank_rows`` is set, those trailing rows are
    dropped, but only when the filter view itself would not select them: a row
    matching every column rule is always exported, even when it is blank, so
    that a ``BLANK`` condition keeps working.
    """
    end = len(display_matrix)

    if not skip_blank_rows:
        return end

    while end > 1 and _is_trailing_padding(
        display_matrix[end - 1], numeric_matrix[end - 1], rules, start_column
    ):
        end -= 1

    return end


def _is_trailing_padding(
    display_row: Sequence[Any],
    numeric_row: Sequence[Any],
    rules: Sequence[ColumnFilterRule],
    start_column: int,
) -> bool:
    if any(_to_text(value) != "" for value in display_row):
        return False

    if not rules:
        # an unfiltered view selects every row: a trailing blank row is padding
        return True

    for rule in rules:
        offset = rule.column_index - start_column
        if rule.check(display_row[offset], numeric_row[offset]) is not None:
            return True

    return False


def build_export(
    *,
    filter_view: Mapping[str, Any],
    grid_range: Mapping[str, int],
    display_values: Sequence[Sequence[Any]],
    numeric_values: Sequence[Sequence[Any]],
    hidden_row_indexes: Iterable[int] = (),
    skip_blank_rows: bool = True,
    sheet_title: Optional[str] = None,
    display_value_range: Optional[str] = None,
    numeric_value_range: Optional[str] = None,
) -> FilterViewExport:
    """Build a :class:`FilterViewExport` from the values of the view range.

    :param filter_view: the ``FilterView`` object from the Sheets API.
    :param grid_range: the bounded ``GridRange`` of the filter view.
    :param display_values: the values of the range as displayed (formatted).
    :param numeric_values: the computed (unformatted) values of the range.
    :param hidden_row_indexes: zero-based sheet row indexes hidden manually.
    :param skip_blank_rows: when True, the trailing rows whose every displayed
        value is empty are ignored (they are padding rows of the sheet grid).
    :param sheet_title: title of the worksheet, for convenience.
    :param display_value_range: the ``range`` of the API response that returned
        ``display_values``. When given, the values are placed back on the
        requested grid, since the API trims empty leading rows and columns.
    :param numeric_value_range: same as ``display_value_range`` for
        ``numeric_values``.
    """
    start_row = int(grid_range.get("startRowIndex", 0))
    start_column = int(grid_range.get("startColumnIndex", 0))
    end_row = int(grid_range["endRowIndex"])
    end_column = int(grid_range["endColumnIndex"])

    height = end_row - start_row

    if height < 1:
        raise FilterViewError(
            "the filter view range must contain at least one row, "
            "got {!r}".format(dict(grid_range))
        )

    if display_value_range or numeric_value_range:
        display_matrix = align_values_to_grid(
            display_values, display_value_range, grid_range
        )
        numeric_matrix = align_values_to_grid(
            numeric_values, numeric_value_range, grid_range
        )
    else:
        # the values are already aligned on the grid
        display_matrix = fill_gaps(
            [list(row) for row in display_values],
            rows=height,
            cols=end_column - start_column,
        )
        numeric_matrix = fill_gaps(
            [list(row) for row in numeric_values],
            rows=height,
            cols=end_column - start_column,
        )

    rules = extract_column_rules(filter_view)
    for rule in rules:
        if not start_column <= rule.column_index < end_column:
            raise FilterViewError(
                "the filter view rule on column {} is outside the view range "
                "{}".format(rule.column_letter, grid_range_to_a1(grid_range))
            )

    headers = [_to_text(value) for value in display_matrix[0]]

    hidden_rows = set(int(index) for index in hidden_row_indexes)

    data_end = _data_end_offset(
        display_matrix, numeric_matrix, rules, start_column, skip_blank_rows
    )

    rows: List[FilterViewRow] = []
    excluded_rows: List[FilterViewExcludedRow] = []

    for offset in range(1, data_end):
        sheet_row_index = start_row + offset
        display_row = display_matrix[offset]
        numeric_row = numeric_matrix[offset]

        values = [_to_text(value) for value in display_row]
        hidden_by_user = sheet_row_index in hidden_rows

        exclusions = _collect_exclusions(
            rules, display_row, numeric_row, headers, start_column
        )

        if exclusions:
            excluded_rows.append(
                FilterViewExcludedRow(
                    row_number=sheet_row_index + 1,
                    values=values,
                    reasons=exclusions,
                    hidden_by_user=hidden_by_user,
                )
            )
        else:
            rows.append(
                FilterViewRow(
                    row_number=sheet_row_index + 1,
                    values=values,
                    hidden_by_user=hidden_by_user,
                )
            )

    return FilterViewExport(
        filter_view_id=int(filter_view.get("filterViewId", 0)),
        grid_range=grid_range,
        headers=headers,
        rows=rows,
        excluded_rows=excluded_rows,
        title=filter_view.get("title"),
        sheet_title=sheet_title,
    )


def _collect_exclusions(
    rules: Sequence[ColumnFilterRule],
    display_row: Sequence[Any],
    numeric_row: Sequence[Any],
    headers: Sequence[str],
    start_column: int,
) -> List[FilterViewExclusion]:
    exclusions: List[FilterViewExclusion] = []

    for rule in rules:
        offset = rule.column_index - start_column
        display_value = display_row[offset]
        numeric_value = numeric_row[offset]

        rejected_by = rule.check(display_value, numeric_value)
        if rejected_by is None:
            continue

        if rejected_by == "HIDDEN_VALUE":
            expected = "hidden values {}".format(list(rule.hidden_values))
        else:
            expected = rule.describe()

        exclusions.append(
            FilterViewExclusion(
                column_index=rule.column_index,
                rule=rejected_by,
                expected=expected,
                actual=_to_text(display_value),
                column_header=_column_header(headers, offset),
            )
        )

    return exclusions


def collapsed_row_indexes(
    sheet_metadata: Mapping[str, Any],
    grid_range: Mapping[str, int],
) -> List[int]:
    """Return the row indexes hidden by a collapsed row group.

    This is a best effort fallback used when the row metadata of the grid
    cannot be fetched. A collapsed group hides all the rows it covers,
    whatever their own visibility.
    """
    start_row = int(grid_range.get("startRowIndex", 0))
    end_row = int(grid_range.get("endRowIndex", 0))

    indexes: List[int] = []

    for group in sheet_metadata.get("rowGroups") or []:
        if group.get("collapsed") is not True:
            continue

        row_range = group.get("range") or {}
        group_start = int(row_range.get("startIndex", start_row))
        group_end = int(row_range.get("endIndex", end_row))

        for index in range(max(group_start, start_row), min(group_end, end_row)):
            if index not in indexes:
                indexes.append(index)

    indexes.sort()
    return indexes
