# BEST_PRACTICES.md — developer & "revisit later" notes for Browser Prompt Enhancer

> Read this first if you come back to this package. It captures the *why*
> behind the design so you don't have to re-derive it, plus exactly where to look when something
> breaks.

---

## 1. Mental model in one paragraph

We drive the **free Brave Leo full-page assistant** (`brave://leo-ai`) with **Playwright**, type a
prompt, wait for the streamed reply to stop changing, scrape it, and hand it back to ComfyUI.
Everything browser-related lives in `brave_leo_core.py` (no ComfyUI imports). The thin ComfyUI
**V3** wrapper is `brave_leo_node.py`. `selftest.py` exercises the core standalone. This separation
is deliberate: it keeps the fragile, testable part runnable without spinning up ComfyUI.

---

## 2. Key design decisions (don't undo these without reading why)

- **Full-page Leo, not the sidebar.** The sidebar is browser *chrome* — unreachable by page
  automation. `brave://leo-ai` is real (shadow) DOM. This is non-negotiable; the original spec's
  `#leo-sidebar` selector approach cannot work.
- **Playwright runs in a CHILD PROCESS (`run_leo_subprocess` + `_worker_main`).** The sync
  Playwright API throws "Sync API inside the asyncio loop" if it runs in a thread that has a live
  event loop — and depending on the ComfyUI build, the node's async `execute` can be in exactly that
  situation even through `asyncio.to_thread` (this was a real field failure). A separate `python`
  process is guaranteed to have no event loop, so the sync API always works there. The node calls
  `run_leo_subprocess` (still via `to_thread`, but only to block on the worker's pipes without freezing
  ComfyUI). The worker is this module's `__main__` block; it talks to the parent over stdout using
  `__BRAVELEO_RESULT__` / `__BRAVELEO_LOG__` line prefixes, and typed errors are mapped back via
  `_ERROR_CLASSES`. Do NOT move Playwright back in-process.

- **Versioning.** Bump `__version__` in `brave_leo_core.py` AND `version` in `pyproject.toml`
  together, and add a `CHANGELOG.md` entry, for every release. Pre-publication builds are `0.9.x`;
  the first GitHub/Registry publish should be `1.0.0`.

- **Window visibility = `window_mode` (default `headless`).** `headless` runs a real browser engine
  with no window (fully invisible, and it skips the display check so it works even without WSLg).
  `visible` shows the window (debugging). `hidden` pushes it off-screen, but note off-screen positioning
  is unreliable on Wayland/WSLg (compositors ignore client positioning), so it may still show — that's
  why `headless` is the default rather than `hidden`. If a user reports headless returns the *original*
  prompt (i.e. Leo didn't render), the fallbacks are `visible`, or a virtual display (Xvfb) on Linux.
  Combined with ComfyUI's node caching (Leo is queried once per batch when inputs don't change), headless
  gives the desired quiet, rate-limit-friendly behavior.
- **Locators, not `page.evaluate(querySelectorAll)`.** Leo's WebUI is heavy on shadow DOM.
  Playwright **locators pierce open shadow roots**; raw `querySelectorAll` does **not**. All scraping
  goes through locators for this reason.
- **`get_by_role("textbox")` first.** Most stable way to find the composer across UI revisions.
- **Graceful degradation.** On any known error the node returns the *original* prompt + a `status`
  string and never raises, so a workflow run is never killed by a flaky browser.
- **Dedicated persistent profile** (`~/.comfyui-braveleo/profile`) so Leo's one-time consent sticks
  and we never touch the user's real browser/profile in managed mode.
- **`window_mode` controls visibility.** `headless` (default) = no window, and it bypasses the display
  check. `visible` = on-screen at a fixed position. `hidden` = off-screen (unreliable on Wayland/WSLg).
  The `_launch_managed` args map window_mode → `headless=` flag + `--window-position`. If you ever need
  guaranteed-invisible-AND-headed (e.g. headless can't render Leo on some build), wrap the worker in an
  Xvfb virtual display rather than reintroducing `-32000` positioning.

---

## 3. The fragile parts (where breakage will happen)

All of it is centralized in `brave_leo_core.py`:

- **`SELECTORS`** — `composer`, `assistant_message`, `send_button` candidate lists. **This is the #1
  thing that rots** when Brave updates Leo. Each is a list tried in order; add new values, keep old
  ones for back-compat.
- **`ONBOARDING_BUTTON_RE` / `SEND_BUTTON_RE`** — text patterns for consent and send buttons.
- **`LEO_URL`** — if Brave moves the page.
- **`_wait_for_response` heuristic** — "reply stopped growing for `stable_for` seconds." If Leo's
  streaming behavior changes (e.g. a reliable "done"/"regenerate"/"stop" affordance appears), prefer
  switching to detecting *that* element over the polling heuristic.

### Verified DOM facts (Brave Leo, June 2026)

Confirmed by dumping the live `brave://leo-ai` DOM. If a future Brave build breaks things, re-confirm
these first:

- **Composer** is a contenteditable span, *not* a textarea:
  `<span data-testid="leo-input" data-placeholder="How can I help you today?" contenteditable="plaintext-only">`.
- **Do NOT** match the left-panel history search box: `<leo-input placeholder="Filter conversations">`
  (a custom element). A generic `role=textbox` first-match grabs *this*, not the composer — that bug
  cost a debugging cycle, which is why the composer selectors are deliberately specific.
- **Send button** is `<leo-button>` wrapping `<leo-icon name="arrow-up">`. Enter may also submit, but
  clicking the button is what we rely on.
- **The conversation (both user and assistant turns) is inside an IFRAME**:
  `<iframe data-testid="conversation-entries-iframe">`. This is the big one: Playwright locators pierce
  *shadow DOM* automatically but **not iframes**, and `page.content()` does **not** serialize iframe
  documents — so the reply is invisible to page-level locators and absent from a naive HTML dump. We
  read replies via `page.frame_locator(CONVERSATION_IFRAME)`, and `dump_debug` now dumps every frame.
- **CSS class names are build-specific hashes** (e.g. `L606BboVFOz8tuU0ttdqsA==`). Never select on them;
  use `data-testid` / `data-placeholder` / `data-editor` / custom tag names instead.
- The reply is extracted from the iframe's text by anchoring on the echoed prompt (whitespace-tolerant
  regex via `_after_query`) and stripping the trailing action toolbar + model-name line (e.g.
  "Qwen VL 235B"). If Leo stops echoing the prompt, it falls back to a baseline diff.

### Web-search streaming robustness (the "50% of the time" bug)

When Leo runs a Brave web search, two things used to corrupt the scrape:
1. A transient status line ("Improving answer with Brave Search…") sat unchanged
   long enough that the stability check accepted it as the final answer.
2. The citations/sources block (URLs + summaries) bled into the captured text.

Fixes (all in `brave_leo_core.py`):
- The mode builders ask Leo to wrap the final prompt between `PROMPT_START_MARKER`
  and `PROMPT_END_MARKER` and to omit sources/URLs/status. `_extract_reply` pulls
  exactly the marked block (searched only AFTER the echoed query, since the query
  text names the markers).
- Completion is gated on the **closing marker**: if only the start marker (or only a
  status line) is present, `_extract_reply` returns `""`, which the wait loop reads
  as "keep waiting" — so we never accept a mid-search state. `_STATUS_RE` enumerates
  the known status phrases.
- `_clean_candidate` strips leading status lines, citations, URLs, and the trailing toolbar
  from the marked prompt.
- On timeout, `_salvage_reply` can return text after an opening marker. Without a
  marker, the node reports a timeout rather than returning search activity as a prompt.
- Default timeout raised to 90s because search adds latency.

If Leo stops honoring the markers, capture a `--debug` dump of a search-grounded
reply before changing the extraction logic.

### How to fix a selector break (fast loop)
```bash
python selftest.py --window-mode visible  # watch what actually happens
python selftest.py --debug         # dumps brave_leo_debug.html + .png on failure
PWDEBUG=1 python selftest.py --full --window-mode visible  # Playwright Inspector: hover elements to get selectors
```
Then update the relevant list in `SELECTORS`, re-run `selftest.py --full` until green.

---

## 4. Extending: add a new mode (the whole point of the Strategy pattern)

Adding `translate`, `critique`, `negative_prompt`, etc. is **one function** in `brave_leo_core.py`:

```python
@register_mode("critique")
def build_critique_query(original_prompt, search_topic, leo_personality):
    return (f"{_persona(leo_personality)}Critique this image prompt and return an improved "
            f"version only:\n\nPROMPT: {original_prompt}")
```

That's it. The node reads `MODE_HANDLERS.keys()` to populate the `mode` dropdown, so the new mode
shows up automatically after a restart. No edits to `brave_leo_node.py` required.

To add a new *node*, write another `io.ComfyNode` subclass and append it to
`BraveLeoExtension.get_node_list()`.

---

## 5. WSL2 / Windows 11 specifics (the primary target environment)

- ComfyUI runs in WSL2 (Linux); the node must launch the **Linux Brave inside WSL2**, displayed via
  **WSLg**. A Linux Playwright cannot control a Windows `.exe`, so we never return `/mnt/c/...brave.exe`
  from `PathDetector` (it searches Linux paths when `is_wsl()` is true).
- WSLg supplies `DISPLAY`/`WAYLAND_DISPLAY` in interactive shells; `display_available()` checks for them.
- `--no-sandbox` and `--disable-dev-shm-usage` matter in WSL2/containers (sandbox + tiny `/dev/shm`).
- `attach` mode to the *Windows* Brave needs mirrored networking (`networkingMode=mirrored`) **and**
  `--remote-allow-origins=*` on the Brave launch (Chromium ≥111 CDP origin check). Treat as experimental.

---

## 6. Concurrency & lifecycle invariants

- The browser **must always close**. `BraveAutomationService.run` does cleanup in `finally`. In
  `attach` mode we **disconnect only** — never close the user's browser.
- Don't store mutable state on the node class — V3 nodes are stateless classmethods (the sanitizer
  enforces this). Per-call state belongs in `BraveAutomationService` instances.

---

## 7. Publishing to the Comfy Registry (→ ComfyUI Manager)

Full official docs: <https://docs.comfy.org/registry/publishing>.

**One-time:**
1. `pip install comfy-cli`
2. Register at <https://registry.comfy.org> (sign in with GitHub/Google) and **create a Publisher**;
   note the **Publisher ID**.
3. Keep the publisher ID, author, and repository URL in `pyproject.toml` current.
4. On the registry site, create an **API key**. In the GitHub repo: **Settings → Secrets and
   variables → Actions → New repository secret**, name it **`REGISTRY_ACCESS_TOKEN`**, value = the API key.
   (The name must match `.github/workflows/publish.yml`.)
5. Push the repository, then run the `Publish to Comfy registry` workflow manually from GitHub Actions.

**Publish a version:**
- *GitHub Actions:* bump `version` in `pyproject.toml`, push, then dispatch the publish workflow.
- *Manual:* from the repo root, `comfy node publish` (it publishes the version in `pyproject.toml`).

**Validation gotchas:** the registry runs security/format checks. `license` must be
`{file = "LICENSE"}` or `{text = "..."}`. Keep `name` lowercase/hyphenated. Don't rely on the install
directory matching the repo name — Manager derives it from the `name` field, so keep imports relative
(we do).

---

## 8. Versioning & release checklist

- [ ] Bump `__version__` in `brave_leo_core.py` **and** `version` in `pyproject.toml` (keep them in sync).
- [ ] `python selftest.py --full` passes on at least one real machine (ideally WSL2 + a desktop OS).
- [ ] `python -m py_compile brave_leo_core.py brave_leo_node.py __init__.py selftest.py` is clean.
- [ ] README inputs/outputs table still matches `define_schema`.
- [ ] If selectors changed, note the Brave version you validated against.

---

## 9. "Revisit later" checklist (start here when you come back)

1. Read sections 2 and 3 above.
2. Run `python selftest.py --full --window-mode visible` and see what, if anything, broke.
3. If it's a UI break → update `SELECTORS` (section 3). If it's behavior → check `_wait_for_response`.
4. Re-validate against the current ComfyUI V3 API — `comfy_api.latest` evolves; confirm
   `io.Schema`/`io.Combo.Input(options=...)`/`io.String.Input(multiline=...)`/`ui.PreviewText` still
   exist. Docs: <https://docs.comfy.org/custom-nodes/v3_migration>.
5. Re-validate the publishing flow against <https://docs.comfy.org/registry/publishing> (it changes too).
6. Bump versions, run the release checklist, publish.

---

## 10. Things intentionally *not* done (and why)

- **No Brave/Leo paid API.** The entire premise is zero-cost via the local browser. If you ever want
  reliability over zero-cost, the honest alternative is a local LLM (e.g. Ollama) or Brave's paid API
  — but that's a different node, not this one.
- **No bundled Chromium reliance.** We launch *Brave* via `executable_path`, not Playwright's bundled
  Chromium, so Leo is present. Install system browser libraries separately if the self-test reports them missing.
- **No hard failures.** Every error path degrades to "return original prompt + status."
