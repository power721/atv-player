# Live Source Rename Delete Design

## Summary

Add rename and delete actions to the existing live source management dialog so users can manage outdated or incorrectly named live sources without editing the database directly.

The built-in example source `IPTV` is not protected for this feature. It can be renamed or deleted the same way as any custom live source.

## Goals

- Add a visible `重命名` action for the selected live source
- Add a visible `删除` action for the selected live source
- Allow both actions for every live source, including the example `IPTV` source
- Keep the current table-based live source management dialog
- Refresh the live source table after rename or delete

## Non-Goals

- Adding a right-click context menu
- Making table cells directly editable
- Adding undo or recycle-bin behavior
- Stopping any currently playing live stream when its source is deleted
- Protecting default/example sources from modification

## UI Design

`LiveSourceManagerDialog` will add two buttons to the existing action row:

- `重命名`
- `删除`

Button behavior:

- both buttons require a selected row
- when no source is selected, both buttons are disabled
- `重命名` opens a `QInputDialog` prefilled with the current display name
- an empty rename value cancels the operation
- `删除` opens a confirmation prompt before deleting
- after either successful action, the dialog reloads the source table

The dialog keeps its existing read-only table behavior. Rename is driven only by the button and prompt.

## Service Design

`CustomLiveService` is already the object passed into `LiveSourceManagerDialog`, so it should expose the two new management methods:

- `rename_source(source_id: int, display_name: str) -> None`
- `delete_source(source_id: int) -> None`

`rename_source()` should:

- fetch the existing source by id
- update only `display_name`
- preserve `enabled`, `source_value`, `cache_text`, `last_error`, and `last_refreshed_at`

`delete_source()` should delegate to `LiveSourceRepository.delete_source()`.

No database migration is needed because `LiveSourceRepository` already supports updating and deleting sources.

## Data Flow

1. User selects a source row in `LiveSourceManagerDialog`
2. User clicks `重命名` or `删除`
3. Dialog validates selection and prompt/confirmation result
4. Dialog calls the matching `CustomLiveService` management method
5. Dialog reloads its table
6. Existing `MainWindow._open_live_source_manager()` reloads live categories after the dialog closes

## Testing

Add focused coverage in:

- `tests/test_custom_live_service.py`
  - `rename_source()` changes only the display name and preserves existing source state
  - `delete_source()` removes the source from `list_sources()`
- `tests/test_live_source_manager_dialog.py`
  - rename action calls the manager with selected source id and new name
  - delete action confirms before calling the manager
  - rename and delete buttons are disabled when no source is selected

## Risks And Mitigations

- Risk: accidental deletion.
  Mitigation: require confirmation before deleting.
- Risk: rename accidentally clears cached source metadata.
  Mitigation: implement rename by preserving all existing source fields except `display_name`.
- Risk: default source receives special-case behavior inconsistent with the user request.
  Mitigation: apply rename and delete uniformly to every selected source.
