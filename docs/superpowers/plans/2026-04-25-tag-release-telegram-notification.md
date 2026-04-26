# Tag Release Telegram Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update the GitHub Actions Telegram notification so tag releases send a version/release message instead of a commit message.

**Architecture:** Keep the existing `release` job and `appleboy/telegram-action` step in `.github/workflows/build.yml`. Replace commit-oriented fields with tag/release-oriented fields derived from the GitHub Actions context so the notification matches release tags such as `v0.13.0`.

**Tech Stack:** GitHub Actions workflow YAML, GitHub Actions context expressions, `appleboy/telegram-action`

---

### Task 1: Replace commit notification fields with release notification fields

**Files:**
- Modify: `.github/workflows/build.yml`

- [ ] **Step 1: Inspect the existing Telegram step**

Read the `send telegram message` step in `.github/workflows/build.yml` and confirm it currently uses commit-specific fields such as `${{ github.event.commits[0].message }}` and a commit URL.

- [ ] **Step 2: Update the Telegram message body**

Replace the existing message body with:

```yaml
          message: |
            atv-player 发布新版本
            版本: ${{ github.ref_name }}
            仓库: ${{ github.repository }}
            发布人: ${{ github.actor }}
            Release: ${{ github.server_url }}/${{ github.repository }}/releases/tag/${{ github.ref_name }}
```

- [ ] **Step 3: Review the workflow diff**

Run: `git diff -- .github/workflows/build.yml`
Expected: only the Telegram message content changes, with commit-specific fields removed and tag/release fields added.

- [ ] **Step 4: Sanity-check the rendered values**

Confirm the final expressions map as follows for a tag like `v0.13.0`:

```text
${{ github.ref_name }} -> v0.13.0
${{ github.repository }} -> power721/atv-player
${{ github.actor }} -> the workflow initiator username
${{ github.server_url }}/${{ github.repository }}/releases/tag/${{ github.ref_name }} -> https://github.com/power721/atv-player/releases/tag/v0.13.0
```
