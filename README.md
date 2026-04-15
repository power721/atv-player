# atv-player

Linux-first `PySide6` desktop player for `alist-tvbox`.

## Packaging

Local packaging uses the same `build.py` entrypoint as GitHub Actions.

Install the packaging dependencies:

```bash
uv sync --group dev --group package
```

Build the current platform bundle:

```bash
uv run python build.py current
```

GitHub Actions builds Linux, macOS, and Windows artifacts for pull requests and manual runs. Pushing a tag that starts with `v` also creates a GitHub Release and uploads the generated archives.
