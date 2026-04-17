# Player Playback Failure Log Design

## Summary

Show mpv playback failure reasons in the player window's right-side "播放日志" panel so users can see why a stream failed to open or stopped due to an error.

The scope is limited to log output inside the existing player UI. It does not add dialogs, notifications, or retry behavior changes.

## Goals

- Capture mpv playback failures for all media types handled by the embedded player
- Append a readable failure reason to the existing player log panel
- Keep natural playback completion separate from playback failures
- Preserve the existing synchronous exception logging in `PlayerWindow`

## Non-Goals

- Adding popup error dialogs or toast notifications
- Changing auto-play, next-item, or retry behavior
- Adding persistent storage for playback error logs
- Building a full mpv debug console inside the UI

## Design

### Failure Reporting Boundary

`MpvWidget` should become the source of truth for asynchronous mpv playback failures.

It already owns mpv event registration, so it should also:

- listen for mpv failure/end events that include error context
- distinguish natural end-of-playback from error termination
- emit a new Qt signal carrying a user-visible failure message

`PlayerWindow` should not inspect raw mpv events directly. Its responsibility is only to append the received message to the existing log view.

### Failure Message Rules

When mpv reports a playback failure, the message appended to the log should use the existing player log style:

- `播放失败: <reason>`

Message formatting should prefer the most specific mpv-provided reason available from the event data.

If mpv does not provide a useful reason string, fall back to a stable generic message:

- `播放失败: 未知错误`

The message should be a single log line so it matches the rest of the player log behavior.

### Event Semantics

Natural playback completion must keep the current behavior:

- continue emitting `playback_finished`
- do not emit the new failure signal
- do not append a failure log entry

Playback errors must:

- emit the new failure signal
- avoid emitting `playback_finished` for the same failure event

This keeps end-of-file auto-advance behavior unchanged while making error cases visible.

### UI Integration

`PlayerWindow` should connect the new `MpvWidget` failure signal during setup, similar to other existing player signals.

On signal receipt:

- append the provided message to `log_view`

No metadata panel changes are needed. The metadata area must remain dedicated to title and detail text.

## Testing

Add focused regression coverage in:

- `tests/test_mpv_widget.py`
  - emits the new failure signal when mpv reports a non-EOF `end-file` style failure with an explicit reason
  - falls back to `播放失败: 未知错误` when no usable reason is available
  - keeps emitting `playback_finished` only for natural EOF
- `tests/test_player_window_ui.py`
  - appends mpv failure messages to `log_view`
  - does not overwrite `metadata_view`

## Risks And Mitigations

- Risk: mpv event payloads vary by backend/runtime and some failures may not include a friendly string.
  Mitigation: centralize extraction in `MpvWidget` and keep a generic fallback.
- Risk: natural end-of-playback could be mistaken for an error.
  Mitigation: preserve the current EOF-only `playback_finished` rule and add explicit tests for both branches.
- Risk: duplicate log entries if both synchronous exceptions and asynchronous mpv failures fire.
  Mitigation: keep this change focused on asynchronous mpv event failures and do not alter existing synchronous exception logging.
