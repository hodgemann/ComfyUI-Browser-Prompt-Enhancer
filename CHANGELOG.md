# Changelog

All notable changes to **Browser Prompt Enhancer for ComfyUI** are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
[Semantic Versioning](https://semver.org/) (MAJOR.MINOR.PATCH). The version in
`pyproject.toml` and `__version__` in `brave_leo_core.py` are kept in sync; bump
both and add an entry here for every release.

## [Unreleased]

## [1.0.0] - 2026-10-21

- Make `rewrite` the default mode.
- Use a model-agnostic default prompt instruction.
- Wait for Leo's prompt markers before returning an enhanced prompt.
- Remove automatic screenshots from normal runs.
- Prepare GitHub and Comfy Registry metadata for the first public release.

## [0.9.1] - 2026-06-28

### Added
- `window_mode` input (`headless` | `hidden` | `visible`), replacing the separate
  `show_window` and `headless` booleans. Defaults to **headless** (no window at all).
- `AI_HANDOFF.md` — a project brief for resuming work in a fresh session.

### Changed
- The browser now runs **headless by default**, so nothing pops up on screen. Headless
  also skips the display requirement (works even without WSLg).

### Removed
- `show_window` and `headless` boolean inputs (folded into `window_mode`).

## [0.9.0] - 2026-06-28
First feature-complete pre-release (not yet published to GitHub / the Comfy Registry).

### Added
- Added a configurable default `leo_personality` instruction.
- `CHANGELOG.md` and a version-bump step in the release checklist.

### Changed
- The browser window always opens on-screen at a fixed position; off-screen hiding was
  removed because it is unreliable on Wayland/WSLg.
- Default response timeout raised to 90s to accommodate Leo's web-search latency.

### Removed
- The `keep_browser_open` toggle (and all of its plumbing). It was unreliable, and node
  caching already gives "request Leo once per batch" behavior. Recover past prompts by
  opening the profile manually (see the README).

### Fixed
- **Playwright now runs in a child process** (`run_leo_subprocess`), eliminating the
  "Sync API inside the asyncio loop" failure seen in some ComfyUI builds.
- **Web-search scraping robustness:** Leo is asked to wrap the final prompt in sentinel
  markers; completion is gated on the closing marker, so transient status lines
  ("Improving answer with Brave Search…") and citation/URL blocks no longer leak in.
- Correct selectors for Leo's real UI: contenteditable composer (`data-testid="leo-input"`),
  `arrow-up` send button, and reading replies from inside the `conversation-entries` iframe.
- WSL2-aware Brave path detection.

[Unreleased]: https://github.com/hodgemann/ComfyUI-Browser-Prompt-Enhancer/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/hodgemann/ComfyUI-Browser-Prompt-Enhancer/releases/tag/v1.0.0
