# Live EPG Channel Alias Design

## Summary

Add a small explicit alias-matching layer to custom live EPG lookup so a live channel name can match an XMLTV display name even when normalization alone is not enough.

The initial required alias is:

- `CCTV-1综合高清` -> `CCTV1综合`

This change is scoped strictly to EPG lookup. It must not rename channels in the live source list, player title, merged-channel keys, or stored source data.

## Goals

- Let custom live playback find EPG data for channels whose source name differs from XMLTV naming by a known alias
- Keep the current channel display name unchanged everywhere outside EPG matching
- Keep the matching rules deterministic and easy to extend with a few future aliases

## Non-Goals

- Renaming channel cards or player playlist items
- Rewriting source playlists or manual channel names
- Broad fuzzy matching across unrelated channels
- Adding user-editable alias configuration in this change

## User Experience

When a custom live source contains a channel named `CCTV-1综合高清` and the cached XMLTV data exposes the same channel as `CCTV1综合`, playback should show the matched EPG rows exactly as if the names had matched directly.

The visible channel name should remain `CCTV-1综合高清` in:

- custom live lists
- player title
- playlist line titles

Only the behind-the-scenes EPG lookup may translate the name through the alias table.

## Architecture

Keep the alias logic inside [`src/atv_player/live_epg_service.py`](/home/harold/workspace/atv-player/src/atv_player/live_epg_service.py).

`CustomLiveService` should continue passing the original merged channel name into `LiveEpgService.get_schedule()`. `LiveEpgService` remains the only layer responsible for turning that name into an XMLTV channel match.

Add a small internal alias map in `LiveEpgService`, for example:

- key: source-side live channel name
- value: XMLTV-side canonical channel name

The alias map should be code-owned and in-memory only.

## Matching Flow

Keep the existing matching order and add alias fallback after the current rules fail.

Recommended order:

1. Exact match between the input channel name and XMLTV display names
2. Normalized match between the input channel name and XMLTV display names
3. Exact match using alias candidates derived from the input channel name
4. Normalized match using alias candidates derived from the input channel name

Alias candidates should include:

- the raw input channel name if it exists in the alias map
- the normalized input channel name if a normalized alias key exists

This keeps the behavior narrow:

- existing direct matches still win
- aliases are only consulted on lookup failure
- aliases stay one-way and do not change stored data

## Normalization

Keep the existing normalization behavior unchanged for non-alias cases.

The alias layer should reuse the same normalization function already used by the regular matching path so alias handling does not invent a second incompatible comparison rule.

## Failure Handling

- If no direct or alias match is found, return no schedule exactly as today
- If an alias target does not exist in the XMLTV names, return no schedule
- If a future alias overlaps with an existing direct match, the direct match should continue to win because alias lookup runs later

No logging, persistence, or UI error state is needed for alias misses.

## Testing

Add coverage in [`tests/test_live_epg_service.py`](/home/harold/workspace/atv-player/tests/test_live_epg_service.py) for:

- the existing normalization-only case staying valid, such as `CCTV-1综合` matching `CCTV1综合`
- the new alias-only case where `CCTV-1综合高清` matches `CCTV1综合`
- direct matches continuing to win before alias lookup

The new alias test should be structured so it would fail under the current implementation, proving the alias table is the behavior change being exercised.

## Risks

- Risk: alias rules grow into an unstructured bag of special cases.
  Mitigation: keep the map small, explicit, and scoped to proven mismatches only.

- Risk: alias matching accidentally renames visible channel text.
  Mitigation: confine the change to `LiveEpgService` and do not modify `CustomLiveService`, playlist parsing, or `VodItem` naming.
