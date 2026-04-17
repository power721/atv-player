# Live Source Auto Name Design

## Summary

Stop asking users to manually enter a display name when adding remote or local live sources. Instead, derive the name automatically from the selected file path or the URL filename.

Manual live sources keep the existing explicit name prompt because they do not have a file path or URL filename to derive from.

## Goals

- Remove the extra display-name prompt for `添加远程源`
- Remove the extra display-name prompt for `添加本地源`
- Derive local-source names from the selected file name
- Derive remote-source names from the URL path's last file name segment
- Keep manual live source naming unchanged

## Non-Goals

- Renaming existing live sources automatically
- Changing the stored source URL or file path
- Moving naming logic into `CustomLiveService`
- Adding a confirmation dialog for auto-generated names

## UI Design

`LiveSourceManagerDialog` changes only the add-source prompts:

- `添加远程源` prompts only for the M3U URL
- `添加本地源` prompts only for the file selection
- neither flow asks for `显示名称`
- `添加手动源` still prompts for `显示名称`

No other dialog layout or button behavior changes are needed.

## Name Derivation Rules

### Local Source

Local source names are derived with the selected file path's stem:

- `/home/user/iptv.m3u` -> `iptv`
- `/home/user/my.live.m3u8` -> `my.live`

Implementation can use `Path(path).stem`.

### Remote Source

Remote source names are derived from the URL path's final segment, ignoring query strings and fragments:

- `https://example.com/live/itv.m3u` -> `itv`
- `https://example.com/live/itv.m3u8?token=1` -> `itv`

Implementation should parse the URL path, take the final segment, and derive the stem from that segment.

If the URL path does not provide a usable final segment, fall back to:

- `直播源`

### Manual Source

Manual sources keep the current prompt-based naming flow and are not affected by this change.

## Data Flow

1. User selects `添加远程源` or `添加本地源`
2. Dialog collects only the URL or file path
3. Dialog derives the display name locally
4. Dialog calls the existing `add_remote_source()` or `add_local_source()` manager method with the derived name
5. Dialog reloads the source table

## Testing

Add focused coverage in `tests/test_live_source_manager_dialog.py` for:

- remote source creation deriving the name from the URL filename
- remote source creation ignoring query strings during name derivation
- remote source creation falling back to `直播源` when the URL has no usable filename
- local source creation deriving the name from the selected file path stem

No service-layer test changes are needed because naming remains a dialog responsibility.

## Risks And Mitigations

- Risk: unusual URLs such as trailing-slash paths produce an empty name.
  Mitigation: add an explicit fallback to `直播源`.
- Risk: hidden query strings or fragments leak into stored names.
  Mitigation: derive the name from the parsed URL path segment only.
- Risk: changing service-layer APIs would create unnecessary churn.
  Mitigation: keep the behavior in `LiveSourceManagerDialog` and reuse existing add methods unchanged.
