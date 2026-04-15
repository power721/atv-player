# Search Layout Centering Design

## Summary

Center the overall search content for both desktop search entry points:

- the standalone search page
- the browse page area that contains the search bar, search results, and file list

This change only affects page-level layout positioning. It does not change table cell alignment, search behavior, or pagination behavior.

## Scope

### Search Page

- Center the full content block horizontally within the page.
- Keep the current vertical stacking order:
  - search controls
  - status field
  - results table
- Allow the content to shrink on narrow windows while staying bounded on wide windows.

### Browse Page

- Center the full browse content block horizontally within the page.
- Keep the current vertical stacking order:
  - top search controls
  - search/file splitter
- Preserve the current split behavior and search panel show/hide logic.

## Architecture

Use a dedicated content container widget for each page:

- move the existing content widgets into an inner container
- apply a maximum width to that container
- place the container inside an outer layout with left and right stretch

This keeps centering logic at the page boundary and avoids changing the internals of tables or splitter behavior.

## Testing

Add UI coverage that verifies:

- each page exposes a centered content container
- on a wide window, the content container is horizontally centered within a small tolerance
- existing search table and splitter behavior remain intact
