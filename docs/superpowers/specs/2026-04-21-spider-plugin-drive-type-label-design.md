# Spider Plugin Drive Type Route Label Design

## Summary

When a spider-plugin detail route points to a supported drive-share link, the route label shown in playback UI should include the detected drive type.

For example, a plugin route named `网盘线` with a share link like `https://pan.quark.cn/s/...` should appear as `网盘线(夸克)` in the route selector. The same labeled route name should continue to be used after the share link expands into a replacement playlist.

## Goals

- Show the drive type in spider-plugin route labels for supported drive-share routes.
- Keep the detected label stable before and after lazy drive-playlist replacement.
- Leave ordinary direct-media routes unchanged.
- Keep the change scoped to spider-plugin playback parsing and player route display.

## Non-Goals

- Changing episode titles such as `第1集`.
- Changing the player window title.
- Changing browse, Telegram, live, Emby, or Jellyfin playback labels.
- Adding new persisted metadata for drive types.

## Current Behavior

Spider-plugin grouped playback uses `PlayItem.play_source` as the route label shown by the player route selector.

For drive routes, the current label is whatever the plugin returns in `vod_play_from`, such as `网盘线`, `quark`, or `百度线`. If the route later expands into a replacement playlist, the replacement items inherit the same raw `play_source`.

This means the UI can show that a route is a separate line, but it does not explicitly tell the user which drive provider the route belongs to when the original route name is generic.

## Design

### Drive Type Detection

Keep drive-type detection local to `SpiderPluginController`.

Detection should inspect the raw route value before playback resolution. The primary input is the share-link hostname. When the hostname matches a supported provider, map it to a user-facing provider name:

- `pan.quark.cn`, `drive-h.quark.cn`, and other `quark.cn` hosts -> `夸克`
- `alipan.com`, `aliyundrive.com` -> `阿里`
- `pan.baidu.com` and other `baidu.com` hosts -> `百度`
- `115.com`, `115cdn.com`, `anxia.com` -> `115`
- `uc.cn` -> `UC`
- `cloud.189.cn` and other `189.cn` hosts -> `天翼`
- `123pan.com`, `123pan.cn`, `123684.com`, `123865.com`, `123912.com`, `123592.com` -> `123云盘`
- `yun.139.com` and other `139.com` hosts -> `移动云盘`
- `xunlei.com` -> `迅雷`
- `mypikpak.com` -> `PikPak`

If hostname detection fails but the backend drive-detail payload later exposes a usable `type_name` or `share_type`-derived name, that value may be used as a fallback label source. If neither source yields a recognized type, keep the original route name unchanged.

### Route Label Formatting

When a spider-plugin route contains at least one supported drive-share link, format the displayed route label as:

- `<original route>(<drive type>)`

Examples:

- `网盘线` + Quark share link -> `网盘线(夸克)`
- `百度线` + Baidu share link -> `百度线(百度)`

Formatting rules:

- preserve the original route text exactly
- do not append the suffix twice if the route already includes the same provider name
- if the original route name is empty and the existing fallback route name such as `线路 1` is used, append the provider to that fallback label

### Playlist Construction And Replacement

`SpiderPluginController._build_playlist()` should compute the effective route label once per group and store it into every group's `PlayItem.play_source`.

When a drive route later resolves into a replacement playlist, `_build_drive_replacement_playlist()` should continue to use the already formatted `play_source` from the clicked item. This keeps the route selector label stable for the full player session.

Direct media routes and non-drive plugin ids should keep their existing route labels unchanged.

### Player Boundary

No player-specific label logic should be added.

`PlayerWindow` already uses `PlayItem.play_source` as the route-group display label. By formatting the route name inside the spider-plugin controller, the player can show the new label automatically without needing to know about drive-provider detection.

## Error Handling

- Unrecognized or unsupported share-link hosts should keep the original route label.
- If the route contains mixed values and the first playable placeholder is a supported drive link, use that detected drive type for the route label.
- If a drive route later fails to resolve for playback, keep the labeled route name visible; the failure should only affect playback resolution, not route-label formatting.

## Testing Strategy

Add or update tests to cover:

- a generic route such as `网盘线` becomes `网盘线(夸克)` for a Quark share link
- a provider-specific route such as `百度线` does not become `百度线(百度)(百度)`
- a non-drive route such as `直链线` keeps its original label
- replacement playlists created from drive detail keep the formatted route label
- player route labels continue to come from `PlayItem.play_source`, so the formatted spider label is visible in the selector

## Risks And Mitigations

- Risk: provider-name formatting could become inconsistent with browse-page drive names.
  Mitigation: reuse existing provider display names where they are already exposed by the codebase, and keep hostname-to-name mapping narrow and explicit.

- Risk: some plugins may already include provider names inside the route label.
  Mitigation: guard against duplicate suffixing before appending `(<drive type>)`.

- Risk: future drive providers may appear with unsupported domains.
  Mitigation: fall back to the original route name instead of guessing.
