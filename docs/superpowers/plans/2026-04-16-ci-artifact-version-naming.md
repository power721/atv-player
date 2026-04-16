# CI Artifact Version Naming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a version segment to CI artifact filenames, using the pushed Git tag without the leading `v` on release builds and `dev` on non-tag runs.

**Architecture:** Keep artifact filename construction centralized in `build.py` and make GitHub Actions pass the raw CI-derived version value into that naming API. Cover the change with focused tests in `tests/test_build.py` and workflow contract assertions so local naming logic and CI orchestration cannot drift apart.

**Tech Stack:** Python 3.12, pytest, GitHub Actions, uv

---

### Task 1: Add Failing Naming Tests For Versioned Artifacts

**Files:**
- Modify: `tests/test_build.py`
- Reference: `build.py`

- [ ] **Step 1: Write the failing tests**

Add these tests near the existing archive naming assertions in `tests/test_build.py`:

```python
def test_resolve_artifact_version_prefers_explicit_value() -> None:
    assert build.resolve_artifact_version("1.2.3") == "1.2.3"
    assert build.resolve_artifact_version(" 2.0.0 ") == "2.0.0"


def test_resolve_artifact_version_falls_back_to_default() -> None:
    assert build.resolve_artifact_version(None) == "dev"
    assert build.resolve_artifact_version("") == "dev"
    assert build.resolve_artifact_version("   ") == "dev"


def test_build_archive_name_includes_explicit_version() -> None:
    assert build.build_archive_name("linux", "x86_64", "1.2.3") == "atv-player-1.2.3-linux-x64.AppImage"
    assert build.build_archive_name("Darwin", "aarch64", "2.0.0") == "atv-player-2.0.0-macos-arm64.zip"
    assert build.build_archive_name("windows", "AMD64", "3.4.5") == "atv-player-3.4.5-windows-x64.exe"


def test_build_archive_name_uses_default_version_when_missing() -> None:
    assert build.build_archive_name("linux", "x86_64") == "atv-player-dev-linux-x64.AppImage"
    assert build.build_archive_name("windows", "AMD64", "   ") == "atv-player-dev-windows-x64.exe"


def test_release_artifact_path_for_target_includes_version() -> None:
    assert build.release_artifact_path_for_target("linux", "x86_64", "1.2.3") == build.DIST_DIR / "atv-player-1.2.3-linux-x64.AppImage"
    assert build.release_artifact_path_for_target("macos", "arm64") == build.DIST_DIR / "atv-player-dev-macos-arm64.zip"
    assert build.release_artifact_path_for_target("windows", "AMD64", "9.9.9") == build.DIST_DIR / "atv-player-9.9.9-windows-x64.exe"
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `uv run pytest tests/test_build.py -k "artifact_version or build_archive_name or release_artifact_path_for_target" -q`

Expected: FAIL because `build.py` does not yet define `resolve_artifact_version()` and current archive names do not include a version segment.

- [ ] **Step 3: Commit the red test state**

```bash
git add tests/test_build.py
git commit -m "test: cover versioned CI artifact names"
```

### Task 2: Implement Version-Aware Artifact Naming In `build.py`

**Files:**
- Modify: `build.py`
- Test: `tests/test_build.py`

- [ ] **Step 1: Add the minimal naming implementation**

Update `build.py` so the naming API accepts an optional version and normalizes blanks to `dev`:

```python
APP_NAME = "atv-player"
DEFAULT_ARTIFACT_VERSION = "dev"
PROJECT_ROOT = Path(__file__).resolve().parent
```

Add the helper below `normalize_arch()`:

```python
def resolve_artifact_version(value: str | None = None) -> str:
    normalized = (value or "").strip()
    return normalized or DEFAULT_ARTIFACT_VERSION
```

Change the naming functions to include the version token:

```python
def build_archive_name(target_platform: str, arch: str | None = None, version: str | None = None) -> str:
    target = build_target(target_platform)
    return (
        f"{APP_NAME}-"
        f"{resolve_artifact_version(version)}-"
        f"{target.platform_id}-"
        f"{normalize_arch(arch)}."
        f"{target.archive_ext}"
    )


def release_artifact_path_for_target(
    target_platform: str,
    arch: str | None = None,
    version: str | None = None,
) -> Path:
    return DIST_DIR / build_archive_name(target_platform, arch, version)
```

- [ ] **Step 2: Run the targeted tests to verify they pass**

Run: `uv run pytest tests/test_build.py -k "artifact_version or build_archive_name or release_artifact_path_for_target" -q`

Expected: PASS with versioned archive names and `dev` fallback behavior.

- [ ] **Step 3: Run the full packaging test file**

Run: `uv run pytest tests/test_build.py -q`

Expected: PASS so the naming change does not break existing packaging contracts.

- [ ] **Step 4: Commit the build.py implementation**

```bash
git add build.py tests/test_build.py
git commit -m "feat: version CI artifact filenames"
```

### Task 3: Thread CI Version Context Into Workflow Naming

**Files:**
- Modify: `.github/workflows/build.yml`
- Modify: `tests/test_build.py`
- Reference: `build.py`

- [ ] **Step 1: Add the failing workflow contract test**

Append this assertion near the existing workflow tests in `tests/test_build.py`:

```python
def test_github_workflow_resolves_versioned_artifact_names() -> None:
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    assert 'if [[ "${GITHUB_REF}" == refs/tags/v* ]]; then' in workflow
    assert 'ARTIFACT_VERSION="${GITHUB_REF_NAME#v}"' in workflow
    assert 'ARTIFACT_VERSION=dev' in workflow
    assert "build.build_archive_name('${{ matrix.platform }}', version=os.environ['ARTIFACT_VERSION'])" in workflow
    assert "build.release_artifact_path_for_target('${{ matrix.platform }}', version=os.environ['ARTIFACT_VERSION'])" in workflow
```

- [ ] **Step 2: Run the workflow-specific test to verify it fails**

Run: `uv run pytest tests/test_build.py::test_github_workflow_resolves_versioned_artifact_names -q`

Expected: FAIL because the workflow does not yet set `ARTIFACT_VERSION` or pass it into the Python naming helpers.

- [ ] **Step 3: Update the workflow to pass the version into naming helpers**

Add a shared POSIX step before archive resolution:

```yaml
      - name: Resolve artifact version (POSIX)
        if: matrix.platform != 'windows'
        run: |
          if [[ "${GITHUB_REF}" == refs/tags/v* ]]; then
            ARTIFACT_VERSION="${GITHUB_REF_NAME#v}"
          else
            ARTIFACT_VERSION=dev
          fi
          echo "ARTIFACT_VERSION=$ARTIFACT_VERSION" >> "$GITHUB_ENV"
```

Add a Windows PowerShell equivalent before the Windows archive-name step:

```yaml
      - name: Resolve artifact version (Windows)
        if: matrix.platform == 'windows'
        shell: pwsh
        run: |
          if ($env:GITHUB_REF -like 'refs/tags/v*') {
            $artifactVersion = $env:GITHUB_REF_NAME.Substring(1)
          } else {
            $artifactVersion = 'dev'
          }
          "ARTIFACT_VERSION=$artifactVersion" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
```

Update the existing Python helper calls so they pass the environment version:

```yaml
          ARCHIVE_NAME=$(uv run python -c "import build, os; print(build.build_archive_name('${{ matrix.platform }}', version=os.environ['ARTIFACT_VERSION']))")
          RELEASE_ASSET_PATH=$(uv run python -c \"import build, os; print(build.release_artifact_path_for_target('${{ matrix.platform }}', version=os.environ['ARTIFACT_VERSION']))\")
```

and on Windows:

```yaml
          $archiveName = uv run python -c "import build, os; print(build.build_archive_name('${{ matrix.platform }}', version=os.environ['ARTIFACT_VERSION']))"
```

- [ ] **Step 4: Run the workflow-specific test to verify it passes**

Run: `uv run pytest tests/test_build.py::test_github_workflow_resolves_versioned_artifact_names -q`

Expected: PASS with explicit `v` stripping and `dev` fallback encoded in the workflow.

- [ ] **Step 5: Run the full packaging test file again**

Run: `uv run pytest tests/test_build.py -q`

Expected: PASS with the new workflow assertions and all existing packaging checks still green.

- [ ] **Step 6: Commit the workflow integration**

```bash
git add .github/workflows/build.yml tests/test_build.py
git commit -m "ci: version packaged artifact names"
```

### Task 4: Final Verification And Review

**Files:**
- Modify: `build.py`
- Modify: `.github/workflows/build.yml`
- Modify: `tests/test_build.py`

- [ ] **Step 1: Inspect the final diff for scope control**

Run: `git diff -- build.py .github/workflows/build.yml tests/test_build.py`

Expected: only artifact-version naming changes plus their tests are present.

- [ ] **Step 2: Run the complete verification command**

Run: `uv run pytest tests/test_build.py -q`

Expected: PASS with all packaging tests green.

- [ ] **Step 3: Commit the verified result**

```bash
git add build.py .github/workflows/build.yml tests/test_build.py
git commit -m "feat: add version to CI artifact filenames"
```
