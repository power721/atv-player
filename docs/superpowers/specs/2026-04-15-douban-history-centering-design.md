# Douban And History Centering Design

## Summary

Make the `豆瓣电影` and `播放记录` tabs use the same page-level horizontal centering pattern as `文件浏览`.

This only changes the outer page layout. It does not change table cell alignment, card content alignment, paging behavior, or data loading.

## Scope

### Douban Page

- Keep the current left category list and right card grid structure.
- Wrap the existing page content in an inner container with a bounded maximum width.
- Center that container with left and right stretch in the outer page layout.

### History Page

- Keep the current toolbar and table structure.
- Wrap the existing page content in an inner container with a bounded maximum width.
- Center that container with left and right stretch in the outer page layout.

## Architecture

Follow the existing `BrowsePage` centering pattern:

- expose a `content_container` widget on each page
- move the current content layout into that container
- set a maximum width on the container
- place the container inside an outer horizontal layout with stretch on both sides

This keeps the change local to page boundaries and preserves internal widget behavior.

## Testing

Add UI assertions that verify:

- `DoubanPage` exposes a centered `content_container`
- `HistoryPage` exposes a centered `content_container`
- on a wide window, each container center is horizontally aligned with the page center within a small tolerance
