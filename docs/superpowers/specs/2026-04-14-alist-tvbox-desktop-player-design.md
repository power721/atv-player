# alist-tvbox Desktop Player Design

## Summary

Build a Linux-first desktop player for `alist-tvbox` using `PySide6` and `Qt Widgets`. The application provides login, file browsing, Telegram search, play history management, and a dedicated player window backed by `mpv`.

The desktop app mirrors the core behaviors of [`VodView.vue`](/home/harold/workspace/alist-tvbox/web-ui/src/views/VodView.vue) while adapting the interaction model for desktop:

- The main window handles login, browsing, search, and play history.
- Playback opens in a separate player window.
- The player window hides the file list and focuses on video, controls, playlist, and details.
- Tokens are stored in `sqlite`; passwords are not stored.

## Goals

- Support login against an `alist-tvbox` backend, defaulting to `http://127.0.0.1:4567`.
- Persist the backend URL, username, token, and lightweight UI state in local `sqlite`.
- Browse backend file lists and open folders.
- Open playlists from playlist entries or from video files inside a folder.
- Restore playback state from backend history before playback starts.
- Report playback progress to the backend every 5 seconds during playback.
- Search Telegram resources through the backend and filter results by drive type.
- View, reopen, delete, and clear play history.

## Non-Goals

- Cross-platform parity in the first iteration. Linux is the only target platform for the initial build.
- Password persistence or silent re-login after token expiry.
- Image viewing, admin operations, renaming, deleting files, or external player handoff.
- Pixel-perfect cloning of the Vue web UI.

## Platform And Technology Choices

### UI Toolkit

Use `PySide6` with `Qt Widgets`.

Reasoning:

- The app is form-heavy and list-heavy.
- Desktop-style navigation, dialogs, and split panes are simpler with widgets than with QML.
- Embedding an `mpv` render surface inside a widget is straightforward on Linux.

### Playback Engine

Use `mpv` as the default playback backend.

Reasoning:

- Better fit for an embedded Linux desktop player than VLC in Python.
- Straightforward time-position access for resume and periodic history upload.
- Simpler event handling for playback state and media lifecycle.

### Storage

Use local `sqlite` for persisted application state.

Store:

- `base_url`
- `username`
- `token`
- `last_path`
- window geometry for the main window and player window

Do not store:

- `password`

## User Experience

### Application Startup

On startup, the app reads local configuration from `sqlite`.

- If a stored token exists, the app attempts to use it for authenticated requests.
- If authenticated requests succeed, the app opens the main window directly.
- If the backend returns `401`, the app clears the stored token and shows the login page.

### Login Flow

The login page contains:

- Backend URL input, default `http://127.0.0.1:4567`
- Username input
- Password input
- Login button

On successful login:

- Save `base_url`, `username`, and `token` to `sqlite`.
- Open the main window.

On failed login:

- Keep the user on the login page.
- Show the backend error message.
- Do not overwrite the previous valid token unless the request explicitly failed with `401`.

### Main Window Layout

The main window follows the approved layout direction:

- Left navigation
- Central content area
- No embedded player region

Primary navigation entries:

- `Browse`
- `Search`
- `History`

The main window owns all content discovery workflows. Playback never happens inline in this window.

### Browse Page

The browse page contains:

- Breadcrumb path bar
- Refresh action
- File list table

Behavior:

- Clicking a folder loads the next folder level.
- Clicking a playlist-like item loads full detail for that item, resolves its playlist, restores backend history, and opens the separate player window.
- Clicking a video file scans the current folder for all video files, builds a playlist ordered as shown in the folder, starts from the clicked file, restores backend history if present, and opens the player window.

### Search Page

The search page contains:

- Keyword input
- Search action
- Drive-type filter
- Clear-results action
- Results list

Behavior:

- Search calls the backend Telegram search endpoint.
- Results are shown in a list with source type information.
- Clicking a result first resolves the share link through the backend, then opens the resulting file list in the browse context.
- The drive-type filter is applied client-side to the returned search results.
- Clear removes the current results and resets the filter state.

### History Page

The history page contains:

- History list
- Reopen action
- Delete selected action
- Clear all action

Behavior:

- Clicking a history item restores playback through the same player-opening path as browse results.
- Delete removes one or more selected history records through the backend API.
- Clear all removes all history records through the backend API.

### Player Window

The player opens in a separate top-level window and contains:

- Video area
- Playback controls
- Playlist panel
- Video detail panel

The player window does not display the file browser.

Behavior:

- Opening a player window does not close the main window.
- The player receives a resolved playlist plus playback metadata.
- The player restores the last known episode and position before starting playback.
- The player supports previous, next, play, pause, seek, volume, and speed controls.
- Closing the player window does not close the main window.

## Architecture

The application is split into four major layers.

### 1. API Client

Responsibilities:

- Build authenticated HTTP requests to the backend.
- Attach the stored token as `Authorization`.
- Parse JSON responses into Python models.
- Raise structured application errors for UI handling.

This layer is the only place that knows raw endpoint URLs and request payloads.

### 2. Local Storage

Responsibilities:

- Create and migrate the local `sqlite` schema.
- Load and save application configuration.
- Store the last visited browse path.
- Store window geometry and similar local-only UI state.

This layer never stores passwords.

### 3. Main Window Layer

Responsibilities:

- Show login, browse, search, and history screens.
- Coordinate user navigation.
- Resolve clicks into playback requests.
- Open or focus the player window with the correct playback context.

### 4. Player Window Layer

Responsibilities:

- Host the embedded `mpv` player.
- Manage the current playlist and current item.
- Apply resume state before playback begins.
- Report play progress every 5 seconds.
- Save a final progress update on close and on item switch.

## Data Model

### AppConfig

Local model for persisted application state.

Fields:

- `base_url: str`
- `username: str`
- `token: str`
- `last_path: str`
- `main_window_geometry: bytes | None`
- `player_window_geometry: bytes | None`

### VodItem

Backend-aligned model for browse items, details, and search results.

Fields used by the desktop app:

- `vod_id`
- `path`
- `vod_name`
- `vod_pic`
- `vod_tag`
- `vod_time`
- `vod_remarks`
- `vod_play_from`
- `vod_play_url`
- `type_name`
- `vod_content`
- `dbid`
- `type`
- `items`

### PlayItem

Normalized player entry used by the player window.

Fields:

- `title`
- `url`
- `path`
- `index`
- `size`

### HistoryRecord

Backend-aligned play history entry.

Fields:

- `id`
- `key`
- `vod_name`
- `vod_pic`
- `vod_remarks`
- `episode`
- `episode_url`
- `position`
- `opening`
- `ending`
- `speed`
- `create_time`

### SearchFilterState

Transient UI model.

Fields:

- `keyword`
- `selected_drive_type`
- `results`
- `filtered_results`

## Playback Context Resolution

### Opening A Playlist Entry

When the user clicks an item that represents a backend playlist:

1. Request item detail from the backend.
2. Extract the playlist from the detail payload.
3. Request history by the playlist key.
4. Resolve the starting episode and start position.
5. Open the player window.

### Opening A Video File

When the user clicks a plain video file:

1. Inspect the current folder contents already loaded in the browse page.
2. Filter them to video files only.
3. Build a playlist in the current UI order.
4. Set the clicked file as the initial item.
5. Request history by the backend key that best represents the folder item.
6. Restore the last known episode or matching episode file if history exists.
7. Open the player window.

## Resume Rules

Resume behavior follows the web client logic from `VodView.vue`.

Order of precedence:

1. If history contains a valid `episode >= 0`, resume by episode index.
2. Otherwise, if history contains `episode_url`, strip query parameters, take the last path segment, and match it against playlist URLs.
3. If no match is found, start from the clicked item for folder-based playback, or index `0` for detail-based playlists.

Additionally:

- Restore `position` as the start seek position in seconds.
- Restore `speed`.
- Restore `opening` and `ending` markers if the UI exposes them in the first version.

## Backend Endpoint Mapping

The desktop client is expected to rely on the same backend surface used by the web UI.

Primary endpoints inferred from the reference implementation:

- `POST /api/accounts/login`
- `POST /api/accounts/logout`
- `GET /vod/{token}?ac=web&pg={page}&size={size}&t={path_id}`
- `GET /vod/{token}?ac=web&ids={vod_id}`
- `GET /api/telegram/search?wd={keyword}`
- `POST /api/share-link`
- `GET /history/{token}?key={id}`
- `DELETE /history/{token}`
- `POST /api/history?log=false`
- `GET /api/history?sort=createTime,desc&page={page}&size={size}`
- `POST /api/history/-/delete`
- `DELETE /api/history/{id}`

If the local backend differs slightly, the desktop client should keep the API logic isolated so endpoint adjustments stay inside the API client layer.

## Error Handling

### Authentication Errors

- Any `401` response clears the stored token from `sqlite`.
- The app returns to the login page.
- Username and backend URL remain populated.
- Password remains empty and must be re-entered manually.

### Browse, Search, And History Errors

- Keep the current visible state intact.
- Show a non-blocking error message.
- Allow the user to retry.

### Player Errors

- If `mpv` fails to open a media URL, keep the player window open.
- Show the failure in the player UI.
- Allow switching to another playlist item.
- If periodic history upload fails, log the error locally and retry on the next 5-second tick.

## History Reporting

Playback progress reporting is owned by the player window.

Rules:

- Start a `QTimer` when playback begins.
- Every 5 seconds, send the current playback state to `POST /api/history?log=false`.
- Also send a final history update when:
  - the user closes the player window
  - the user switches to another item
  - the current media ends

Payload fields should mirror the web client:

- `key`
- `vodName`
- `vodPic`
- `vodRemarks`
- `episode`
- `episodeUrl`
- `position`
- `opening`
- `ending`
- `speed`
- `createTime`

## Testing Strategy

Testing follows TDD during implementation.

### Unit Tests

Required unit coverage:

- API response parsing
- Token persistence and clearing in `sqlite`
- Search result filtering by drive type
- Resume rule resolution by episode index
- Resume rule resolution by `episode_url`
- Playlist construction from current folder video files

### Integration Tests

Required integration coverage:

- Login success persists configuration and opens the main window
- `401` during an authenticated request clears the token and returns to login
- Clicking a search result resolves a share link and opens the browse view
- Clicking a history item opens playback with restored state

### Manual Verification

Required manual verification on Linux:

- Successful embedded `mpv` playback in a dedicated window
- Periodic 5-second history upload while media is playing
- Final progress upload on close
- Reopening a title resumes at the expected position
- Deleting and clearing history reflect correctly in the main window

## Implementation Boundaries

The first implementation should stay focused on the approved scope:

- Login
- Browse and open playback
- Search with filtering and clearing
- History view and deletion
- Dedicated `mpv` player window
- Token persistence in `sqlite`

Anything beyond that is out of scope for the first delivery.
