# Browse Local Sorting Design

## Summary

Add local sorting to the browse page file list so the user can sort the currently loaded desktop rows by clicking table headers.

This change is intentionally scoped to the current loaded page only. It does not change backend ordering, API parameters, or pagination behavior.

## Scope

The browse page file table will support click-to-sort for these columns:

- `名称`
- `大小`
- `豆瓣ID`
- `评分`
- `时间`

The `类型` column will remain unsorted.

Sorting applies only to the rows already loaded into the table for the current page. Changing folders, refreshing, or moving between pages continues to use the backend response for that page and then allows local sorting on that page's rows.

## UI Approach

Keep the existing `QTableWidget` and enable header sorting for the browse file table.

Interaction:

- clicking a sortable column header sorts ascending
- clicking the same header again toggles to descending
- non-sortable columns do not participate in sorting

This matches desktop expectations and keeps the change small.

## Data Handling

The visible text should stay the same as today, but sortable columns need stable comparison values:

- `名称` sorts by the displayed text
- `大小` sorts by a parsed numeric value instead of the raw display string
- `豆瓣ID` sorts as an integer
- `评分` sorts as a float
- `时间` sorts by a normalized comparable time value

The implementation should assign sort keys to each table item while populating rows, so Qt can sort using real values instead of string order.

## Edge Cases

- folder rows that display `-` in `大小` must sort predictably without exceptions
- empty or invalid `豆瓣ID` values must not crash sorting
- empty or invalid `评分` values must not crash sorting
- empty or invalid `时间` values must not crash sorting
- sorting does not trigger any new backend request
- sorting state does not need to persist across page loads or path changes

## Testing

Add browse page UI coverage for:

- clicking `名称` sorts the current rows and can reverse order
- clicking `大小` uses numeric ordering rather than string ordering
- clicking sortable headers does not request data again
- empty values in sortable columns do not crash the table interaction
