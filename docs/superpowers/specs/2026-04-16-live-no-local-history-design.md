# Live Playback Without Local History Design

## Context

The player already supports sources that should not participate in local playback history through the `use_local_history` flag on `OpenPlayerRequest` and `PlayerSession`.

`PlayerController` already uses that flag for both sides of history behavior:

- when `use_local_history=False`, it skips loading resume history during session creation
- when `use_local_history=False`, it skips saving playback progress back to the local history API

Emby and Jellyfin requests already use this mechanism. Network live playback currently does not, so live streams still read and write local history.

## Goal

Make network live playback skip both local history resume lookup and local history persistence.

## Non-Goals

- Changing history behavior for regular VOD playback
- Hiding live items from the playback history UI after they were already recorded
- Refactoring player history handling across all sources
- Changing remote/server-side live APIs

## Proposed Approach

### Request-Level Ownership

Keep source-specific history policy at the controller layer that constructs `OpenPlayerRequest`.

`LiveController.build_request()` should explicitly set `use_local_history=False` on the returned request, matching the existing pattern used by Emby and Jellyfin controllers.

This is the smallest coherent change because:

- `LiveController` already owns the semantics of a live playback request
- `MainWindow` already passes `request.use_local_history` through into `PlayerController.create_session()`
- `PlayerController` already implements the correct skip-read and skip-write behavior

No changes should be required in `PlayerController` or `MainWindow` logic beyond existing pass-through behavior.

### Expected Behavior

When a user opens a network live item:

- the live request sent to the player has `use_local_history=False`
- the player session does not request any prior local history record for that live item
- playback progress updates do not call the local history save API

This should apply to both direct live room opens and live items reached by clicking into a live folder, because both flows end in `LiveController.build_request()`.

## Testing

Add focused tests at the edges of the request flow:

- in `tests/test_live_controller.py`, assert that `LiveController.build_request()` returns `use_local_history=False`
- in `tests/test_app.py`, assert that opening a live item from the main window produces an `OpenPlayerRequest` with `use_local_history=False`

The existing `PlayerController` tests already cover what `use_local_history=False` means in session creation and progress reporting, so they should not be duplicated here.

## Risks and Mitigations

- Risk: live playback still saves history because the flag is not passed through from request to session.
  Mitigation: keep the app-level test around the live open flow so the request contract remains visible.
- Risk: future sources forget to declare their desired history behavior.
  Mitigation: keep policy explicit in each source controller instead of hiding it behind UI heuristics.
