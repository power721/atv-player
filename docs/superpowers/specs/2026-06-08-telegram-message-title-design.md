# Telegram Message Title Design

## Goal

Local Telegram video browsing should show the human-written message title instead of the raw video file name when a message contains a usable first paragraph.

## Behavior

- For Telegram video media resources, derive `TelegramResource.title` from the first non-empty paragraph of the message text.
- A paragraph is separated by one or more blank lines.
- Preserve the full first paragraph text after normalizing whitespace inside that paragraph.
- Strip embedded Telegram resource links from the derived title if they appear in that paragraph.
- Limit the derived title to 120 characters, matching existing link title behavior.
- If the message has no usable first paragraph, keep the current fallback: use the file name.
- Keep `TelegramResource.file_name` unchanged so playback, media lookup, and file-name search continue to work.

## Example

Given a Telegram video message whose first paragraph is:

```text
《恐怖地窖》地窖台阶永无止境？全家人陷入“数数”诅咒
```

the video list title should be exactly that paragraph, not the attached file name.

## Implementation Scope

- Add a small helper in `src/atv_player/telegram_media.py` for deriving a message title.
- Use that helper only for video media resources created by `_resources_from_message()`.
- Add focused regression coverage in `tests/test_telegram_search_controller.py`.

## Testing

Run the focused Telegram tests that cover message resource extraction and repository search behavior.
