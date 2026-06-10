# Telegram Thumbnail Proxy Design

## Goal

Local Telegram video browsing should display Telegram-provided media thumbnails when available.

## Behavior

- Media list items from local Telegram resources should set `VodItem.vod_pic` to a local HTTP thumbnail URL.
- The thumbnail URL should be generated from `chat_id` and `msg_id`.
- Thumbnail downloads should be lazy: indexing and listing do not download image bytes.
- The local proxy should handle `GET /tg/thumb/{chat_id}/{msg_id}`.
- The proxy should ask `TelegramMediaService` for the largest available thumbnail bytes using Telethon.
- If no thumbnail is available, the proxy should return an error response and the existing poster loader will ignore it.
- Existing Telegram video stream URLs and media playback behavior remain unchanged.

## Architecture

- `TelegramSearchController` accepts an optional thumbnail URL factory.
- `_map_local_resource()` uses that factory only for media resources.
- `LocalHlsProxyServer` exposes `create_telegram_thumbnail_url()` and streams thumbnail responses.
- `TelegramMediaService` adds `get_media_thumbnail_bytes(chat_id, msg_id)`.

## Testing

- Controller test verifies `vod_pic` is set for local Telegram media.
- Proxy test verifies thumbnail endpoint writes returned image bytes.
- Service test verifies the Telethon `download_media(..., file=bytes, thumb=-1)` call path.
