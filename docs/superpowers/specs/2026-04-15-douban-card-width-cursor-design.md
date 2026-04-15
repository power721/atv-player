# Douban Card Width and Cursor Design

## Summary

Adjust the `DoubanPage` movie cards so each card is visibly wider and uses a pointing-hand cursor on hover. To make the wider cards fit naturally, the poster grid should stop enforcing a fixed minimum of five columns and instead reduce the column count automatically when the available width is smaller.

## Goals

- make Douban movie cards easier to scan by giving each card more horizontal space
- keep poster and text proportions aligned with the wider card size
- show a pointing-hand cursor so cards read as clickable controls
- preserve the existing centered content layout while letting the grid adapt to narrower widths

## Non-Goals

- no changes to Douban data loading, pagination, or click behavior
- no redesign of the category list, status area, or page chrome
- no changes to browse-page cards or player-page controls

## UI Changes

### Card Sizing

Increase the fixed card width used by `DoubanPage` and scale the poster icon size up with it. Update the card height alongside those values so the larger poster and title block still fit cleanly inside a single fixed-size button. The text remains below the poster using the existing `ToolButtonTextUnderIcon` layout.

### Pointer Feedback

Every Douban movie card should use `Qt.CursorShape.PointingHandCursor`.

This applies only to the clickable poster cards and should not change the cursor for unrelated widgets on the page.

## Layout Behavior

The grid currently keeps card columns between five and six. After widening cards, that rule becomes too rigid for narrower windows.

Update the column calculation so it is based on the actual available width and card width, with a lower bound of one column. This allows the grid to reduce to fewer columns when space is tight and increase again when the window grows.

The existing centered outer container remains in place. The page should still look centered within large windows, but the cards inside the scroll area may now wrap into fewer columns than before.

## Implementation Notes

Touch only `src/atv_player/ui/douban_page.py` for production behavior:

- widen the card size constants
- widen the poster icon size used for the button and async poster loading
- set the card button cursor to `PointingHandCursor`
- relax `_column_count_for_width()` so it no longer forces a minimum of five columns

No controller or API changes are required.

## Testing

Update `tests/test_douban_page_ui.py` to cover the new behavior:

- verify a rendered card uses a pointing-hand cursor
- verify the card fixed width and icon size match the widened constants
- replace the fixed `5 -> 6` column expectation with a responsive assertion that a narrower width yields fewer columns than a wider width
- keep existing coverage for card click, poster loading, async stale-response protection, pagination, and centered container behavior

## Risks and Mitigations

- Wider cards reduce how many cards fit per row.
  Mitigation: make column count responsive instead of preserving the old fixed minimum.

- A wider button without a larger poster could leave awkward empty space.
  Mitigation: scale the poster icon size with the card width so the visual balance remains intentional.
