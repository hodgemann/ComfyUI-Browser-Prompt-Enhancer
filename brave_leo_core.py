"""
Browser Prompt Enhancer for ComfyUI :: brave_leo_core.py
=====================================

Platform-agnostic core for driving the *free* Brave Leo assistant through
browser automation, with **no ComfyUI dependencies** so that this module can be
imported and exercised by ``selftest.py`` (or any unit test) outside of ComfyUI.

What lives here
---------------
* ``PathDetector``            - finds the local Brave binary on Win/macOS/Linux/WSL2.
* ``BraveAutomationService``  - launches Brave, drives ``brave://leo-ai``, scrapes the reply.
* ``MODE_HANDLERS``           - Strategy/Command registry mapping a mode name -> query builder.
* ``build_query`` / ``clean_response`` / ``run_leo`` - small pure helpers + one entry point.

Why the full-page view (brave://leo-ai) and not the sidebar
-----------------------------------------------------------
The Leo *sidebar* is part of the browser **chrome** (the application UI), which
Playwright cannot reach with page locators. Leo's **full-page view** at
``brave://leo-ai`` is ordinary (shadow) DOM that Playwright *can* drive. All of
the fragile, version-sensitive selectors are centralised in ``SELECTORS`` below
so they are trivial to update if a future Brave release changes the markup.

Required reading / repositories (per project spec)
--------------------------------------------------
* Playwright (Python) : https://github.com/microsoft/playwright-python
* Brave download      : https://brave.com/download/
* ComfyUI V3 API docs : https://docs.comfy.org/custom-nodes/v3_migration
"""

from __future__ import annotations

import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

__version__ = "1.0.0"

# Default instruction prepended to every request to Leo. Override via the
# `leo_personality` input.
DEFAULT_PERSONALITY = (
    "Act as an expert Prompt Engineer for image generation. Your task is to "
    "expand my basic concept into a highly "
    'detailed, "long-form" positive text-to-image prompt. Assume the model renders '
    "exactly what is described physically. Do not rely on it to infer missing "
    "details; every texture, light source, and material "
    "property must be explicitly stated in the positive prompt. Please follow "
    "this structure for the enhanced prompt: "
    "1. Subject & Micro-Details: Define the main subject with intricate physical "
    "descriptions (e.g., pore structure, fabric weave, material imperfections, "
    "subsurface scattering). "
    "2. Environment & Atmosphere: Describe the setting with depth, specific "
    "atmospheric particles (haze, dust, humidity), and background geometry. "
    "3. Lighting Physics: Specify the light source (type, color temperature, "
    "angle), interaction with surfaces (specular highlights, ray-traced "
    "reflections, volumetric fog), and shadow density. "
    "4. Composition & Camera: Define the lens focal length (e.g., 35mm, 85mm, "
    "macro), aperture (depth of field), camera angle, and framing rules. "
    "5. Style & Render Quality: Explicitly state the rendering style (e.g., film "
    "grain, halftone, oil paint texture) and specific surface qualities (matte, "
    "glossy, rough, wet). "
    "Output ONLY the enhanced positive prompt, ready to be copied and pasted "
    "directly into an image model. Do not include any negative constraints, "
    "introductory text, or explanations."
)


# ---------------------------------------------------------------------------
# Exceptions (typed so the node can surface a clear, friendly status string)
# ---------------------------------------------------------------------------
class BraveLeoError(Exception):
    """Base class for all errors raised by this package."""


class BraveNotFoundError(BraveLeoError):
    """The Brave executable could not be located on this machine."""


class PlaywrightMissingError(BraveLeoError):
    """The ``playwright`` package or its browser deps are not installed."""


class DisplayUnavailableError(BraveLeoError):
    """No display is available (e.g. headless Linux / WSL2 without WSLg)."""


class BraveLaunchError(BraveLeoError):
    """Brave could not be launched or attached to."""


class LeoUIError(BraveLeoError):
    """Leo's UI did not match any known selector (Brave likely changed markup)."""


class LeoTimeoutError(BraveLeoError):
    """Leo did not produce a response within the allotted time."""


# ---------------------------------------------------------------------------
# Centralised, update-in-one-place selectors / patterns.
#
# These are intentionally *lists* of candidates tried in order. When Brave
# changes its markup, add the new selector to the appropriate list -- you should
# not need to touch any logic. ``selftest.py --debug`` dumps the live DOM to
# help you discover the right values.
# ---------------------------------------------------------------------------
SELECTORS: Dict[str, List[str]] = {
    # Leo's chat composer. VERIFIED against brave://leo-ai (June 2026): it is a
    # contenteditable <span data-testid="leo-input"
    # data-placeholder="How can I help you today?" contenteditable="plaintext-only">.
    # CRITICAL: these must NOT match the left-panel conversation-history search
    # box, which is a <leo-input placeholder="Filter conversations"> custom
    # element. Keying off data-testid / data-placeholder / data-editor (stable)
    # rather than the build-specific hashed CSS class names.
    "composer": [
        '[data-testid="leo-input"][contenteditable]',
        'span[data-editor="true"][contenteditable]',
        '[data-placeholder="How can I help you today?"]',
        '[contenteditable="plaintext-only"]',
    ],
    # Send button: a <leo-button> wrapping <leo-icon name="arrow-up">.
    "send_button": [
        'leo-button:has(leo-icon[name="arrow-up"])',
        'leo-icon[name="arrow-up"]',
        'button[aria-label*="send" i]',
        'button[title*="send" i]',
        'button[type="submit"]',
    ],
    # Containers holding an assistant (Leo) reply. The *last* match is taken.
    # Leans on semantic/stable hooks. If your build doesn't surface the reply,
    # run `selftest.py --full --debug` (it now reaches Leo, so the dump contains a
    # real answer) and add the right selector here -- see BEST_PRACTICES.md s.3.
    "assistant_message": [
        '[data-testid*="assistant" i]',
        '[data-testid*="response" i]',
        '[data-testid*="turn" i]',
        '[aria-live="polite"]',
        '[aria-live="assertive"]',
        "leo-conversation-entry",
        '[data-testid*="message" i]',
    ],
}

# Regex of button labels that dismiss Leo's one-time onboarding / consent.
ONBOARDING_BUTTON_RE = re.compile(
    r"\b(accept|agree|i understand|understand|get started|continue|got it|"
    r"enable|allow|okay|ok|start chatting|dismiss)\b",
    re.IGNORECASE,
)

# Role-based send button names (used via get_by_role).
SEND_BUTTON_RE = re.compile(r"\b(send|submit)\b", re.IGNORECASE)

LEO_URL = "brave://leo-ai/"

# Leo renders the conversation (user + assistant turns) inside an IFRAME. Its
# content is a separate document that page-level locators cannot see, so replies
# must be read by scoping into this frame. Verified June 2026.
CONVERSATION_IFRAME = 'iframe[data-testid="conversation-entries-iframe"]'

# Sentinel markers we ask Leo to wrap the final prompt in, so we can extract it
# cleanly regardless of "Improving answer with Brave Search…" status lines,
# citations/sources, URLs, or conversational preamble. Plain ASCII words (no
# markdown-special characters) so they survive Leo's rendering verbatim.
PROMPT_START_MARKER = "BRAVELEO_PROMPT_START"
PROMPT_END_MARKER = "BRAVELEO_PROMPT_END"

# Preambles we politely strip from Leo's reply so the prompt is clean.
_PREAMBLE_RE = re.compile(
    r"^\s*(sure|certainly|of course|absolutely|here(?:'s| is| you go)|"
    r"below is|here are|happy to help)[^\n:]{0,80}[:\n]",
    re.IGNORECASE,
)


# ===========================================================================
# Environment / platform helpers
# ===========================================================================
def running_os() -> str:
    """Return a normalised OS name: 'windows', 'macos', or 'linux'."""
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "macos"
    return "linux"


def is_wsl() -> bool:
    """True when running inside Windows Subsystem for Linux (WSL1/WSL2)."""
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        rel = platform.uname().release.lower()
        if "microsoft" in rel or "wsl" in rel:
            return True
    except Exception:
        pass
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except Exception:
        return False


def display_available() -> bool:
    """
    Best-effort check that a GUI display is reachable.

    On Windows/macOS this is always True. On Linux/WSL2 we require an X11
    (DISPLAY) or Wayland (WAYLAND_DISPLAY) server -- WSLg provides both on
    Windows 11 automatically inside an interactive WSL shell.
    """
    if running_os() != "linux":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def default_profile_dir() -> Path:
    """
    Dedicated, persistent Brave profile used by 'managed' mode.

    Persisting it means Leo's one-time consent screen only has to be handled
    once. Override with the ``BRAVE_LEO_PROFILE_DIR`` environment variable.
    """
    override = os.environ.get("BRAVE_LEO_PROFILE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".comfyui-braveleo" / "profile"


# ===========================================================================
# Brave executable detection (no hard-coded single path; WSL2-aware)
# ===========================================================================
class PathDetector:
    """Locate the Brave browser binary across operating systems."""

    # Standard install locations per platform.
    _WINDOWS = [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    _MACOS = [
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        str(Path.home() / "Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
    ]
    _LINUX = [
        "/usr/bin/brave-browser",
        "/usr/bin/brave-browser-stable",
        "/usr/bin/brave",
        "/snap/bin/brave",
        "/opt/brave.com/brave/brave-browser",
        "/opt/brave.com/brave/brave",
        "/var/lib/flatpak/exports/bin/com.brave.Browser",
    ]
    _LINUX_WHICH = [
        "brave-browser",
        "brave-browser-stable",
        "brave",
        "com.brave.Browser",
    ]

    @classmethod
    def detect(cls, explicit_path: Optional[str] = None) -> str:
        """
        Return the path to a Brave binary or raise :class:`BraveNotFoundError`.

        Resolution order:
          1. ``explicit_path`` argument (node input ``brave_path``)
          2. ``BRAVE_LEO_EXECUTABLE`` environment variable
          3. Platform default locations + ``shutil.which`` (Linux)

        On WSL2 we deliberately search the *Linux* locations and never return a
        Windows ``/mnt/c/...brave.exe`` path -- a Linux Playwright cannot drive a
        Windows binary.
        """
        for candidate in (explicit_path, os.environ.get("BRAVE_LEO_EXECUTABLE")):
            if candidate:
                p = Path(candidate).expanduser()
                if p.exists():
                    return str(p)
                raise BraveNotFoundError(
                    f"Brave path was given as '{candidate}' but no file exists there."
                )

        osname = running_os()
        wsl = is_wsl()

        if osname == "windows":
            search = list(cls._WINDOWS)
            local = os.environ.get("LOCALAPPDATA")
            if local:
                search.append(
                    str(Path(local) / "BraveSoftware/Brave-Browser/Application/brave.exe")
                )
        elif osname == "macos":
            search = cls._MACOS
        else:  # linux (this branch also covers WSL2)
            search = list(cls._LINUX)
            for name in cls._LINUX_WHICH:
                found = shutil.which(name)
                if found:
                    search.append(found)

        for path in search:
            if path and Path(path).exists():
                return str(path)

        raise BraveNotFoundError(cls._not_found_message(osname, wsl))

    @staticmethod
    def _not_found_message(osname: str, wsl: bool) -> str:
        if wsl:
            return (
                "Brave was not found inside your WSL2 distro. ComfyUI runs in WSL2, "
                "so it needs a *Linux* Brave (your Windows Brave cannot be driven from "
                "WSL2). Install it inside WSL2, e.g.:\n"
                "  sudo apt install -y curl\n"
                "  curl -fsS https://dl.brave.com/install.sh | sh\n"
                "then re-run. See https://brave.com/linux/ . "
                "(Advanced alternative: use connection_mode='attach' to reach your "
                "Windows Brave -- see the README.)"
            )
        if osname == "windows":
            return (
                "Brave was not found. Install it from https://brave.com/download/ "
                "(expected at C:\\Program Files\\BraveSoftware\\Brave-Browser\\"
                "Application\\brave.exe)."
            )
        if osname == "macos":
            return (
                "Brave was not found. Install it from https://brave.com/download/ "
                "(expected at /Applications/Brave Browser.app)."
            )
        return (
            "Brave was not found. Install it from https://brave.com/download/ "
            "or set BRAVE_LEO_EXECUTABLE to the binary path."
        )


# ===========================================================================
# Strategy pattern: pluggable "modes". Add a new mode with @register_mode("x").
# ===========================================================================
# A mode handler takes (original_prompt, search_topic, leo_personality) and
# returns the full query string that will be typed into Leo.
ModeHandler = Callable[[str, str, str], str]
MODE_HANDLERS: Dict[str, ModeHandler] = {}


def register_mode(name: str) -> Callable[[ModeHandler], ModeHandler]:
    """Decorator to register a new mode. This is the ONLY thing you need to add
    a mode (e.g. 'translate', 'critique') -- no core logic has to change."""

    def _wrap(fn: ModeHandler) -> ModeHandler:
        MODE_HANDLERS[name] = fn
        return fn

    return _wrap


def _persona(leo_personality: str) -> str:
    persona = (leo_personality or "").strip()
    return f"{persona}\n\n" if persona else ""


@register_mode("append")
def build_append_query(original_prompt: str, search_topic: str, leo_personality: str) -> str:
    """Fetch real-time context and append it WITHOUT altering the original intent."""
    return (
        f"{_persona(leo_personality)}"
        f"Search the web for current, real-world details about '{search_topic}'. "
        f"Then take the following image-generation prompt and APPEND a single, vivid "
        f"sentence of concrete visual details to the end of it. Do NOT change, reword, "
        f"or remove any of the original text. "
        f"Output ONLY the final prompt: no commentary, no explanation, no sources, no "
        f"citations, no URLs, no status text. "
        f"Put the line {PROMPT_START_MARKER} by itself, then the final prompt, then the "
        f"line {PROMPT_END_MARKER} by itself.\n\n"
        f"PROMPT: {original_prompt}"
    )


@register_mode("rewrite")
def build_rewrite_query(original_prompt: str, search_topic: str, leo_personality: str) -> str:
    """Completely rewrite/optimise the prompt for AI image generation."""
    return (
        f"{_persona(leo_personality)}"
        f"Search the web for current, real-world details about '{search_topic}'. "
        f"Then completely rewrite the following idea into one highly detailed, vivid "
        f"prompt optimised for AI image generation (subject, composition, lighting, "
        f"style, medium, mood, quality tags). Incorporate the latest real-world details "
        f"you found. "
        f"Output ONLY the final prompt: no commentary, no explanation, no sources, no "
        f"citations, no URLs, no status text. "
        f"Put the line {PROMPT_START_MARKER} by itself, then the final prompt, then the "
        f"line {PROMPT_END_MARKER} by itself.\n\n"
        f"IDEA: {original_prompt}"
    )


def build_query(mode: str, original_prompt: str, search_topic: str, leo_personality: str) -> str:
    """Look up the registered handler for ``mode`` and build the Leo query."""
    handler = MODE_HANDLERS.get(mode)
    if handler is None:
        raise BraveLeoError(
            f"Unknown mode '{mode}'. Registered modes: {sorted(MODE_HANDLERS)}"
        )
    topic = (search_topic or "").strip() or original_prompt
    return handler(original_prompt, topic, leo_personality)


# ===========================================================================
# Response cleanup
# ===========================================================================
def clean_response(text: str) -> str:
    """Conservatively strip code fences, a leading preamble line, and wrapping
    quotes so the returned prompt is ready to feed a sampler. Non-destructive:
    if nothing matches, the text is returned essentially unchanged."""
    if not text:
        return ""
    cleaned = text.strip()

    # Whole reply wrapped in a fenced code block -> take the inside.
    fence = re.match(r"^```[a-zA-Z0-9_-]*\s*\n(.*)\n```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()

    # Drop a single leading "Sure, here's ...:" style preamble line.
    cleaned = _PREAMBLE_RE.sub("", cleaned, count=1).strip()

    # Strip a single layer of matching wrapping quotes.
    if len(cleaned) >= 2 and cleaned[0] in "\"'“”" and cleaned[-1] in "\"'“”":
        cleaned = cleaned[1:-1].strip()

    return cleaned


# ===========================================================================
# The browser automation service (synchronous Playwright)
#
# NOTE ON CONCURRENCY: this uses Playwright's *sync* API on purpose. The node
# calls it via ``asyncio.to_thread`` so it runs in a worker thread that has no
# running event loop -- which is exactly what the sync API requires and which
# avoids the "Sync API inside the asyncio loop" error you would hit if you ran
# it directly inside ComfyUI's async execution loop.
# ===========================================================================
class BraveAutomationService:
    def __init__(
        self,
        *,
        connection_mode: str = "managed",
        brave_path: str = "",
        window_mode: str = "headless",
        timeout_seconds: int = 90,
        profile_dir: Optional[str] = None,
        debug_host: str = "127.0.0.1",
        debug_port: int = 9222,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.connection_mode = connection_mode
        self.brave_path = brave_path
        # window_mode: "headless" (no window), "hidden" (off-screen), "visible".
        self.window_mode = window_mode if window_mode in ("headless", "hidden", "visible") else "headless"
        self.timeout_seconds = max(5, int(timeout_seconds))
        self.profile_dir = Path(profile_dir).expanduser() if profile_dir else default_profile_dir()
        self.debug_host = debug_host
        self.debug_port = int(debug_port)
        self._log = on_log or (lambda _msg: None)

    # -- public API ---------------------------------------------------------
    def run(self, query: str) -> str:
        """Open Leo, send ``query``, return Leo's raw reply text. Always tidies up."""
        try:
            from playwright.sync_api import sync_playwright  # lazy import
        except ImportError as exc:  # pragma: no cover - depends on user env
            raise PlaywrightMissingError(
                "The 'playwright' package is not installed. Run:\n"
                "  pip install playwright\n"
                "(On Linux/WSL2 you may also need: playwright install-deps chromium)"
            ) from exc

        if (
            self.connection_mode == "managed"
            and self.window_mode != "headless"
            and not display_available()
        ):
            raise DisplayUnavailableError(
                "No display detected for visible Brave. Use headless mode, or start "
                "ComfyUI with DISPLAY or WAYLAND_DISPLAY set."
            )

        playwright = sync_playwright().start()
        context = None
        browser = None
        try:
            context, browser = self._open_context(playwright)
            context.set_default_timeout(self.timeout_seconds * 1000)
            page = context.pages[0] if context.pages else context.new_page()
            self._goto_leo(page)
            composer = self._find_composer(page)
            baseline = self._conversation_text(page)  # what's there before we send
            self._submit(page, composer, query)
            reply = self._wait_for_response(page, query, baseline)
            return reply
        finally:
            # Always tidy up. 'attach' mode only disconnects (never closes the
            # user's own browser); 'managed' mode closes the dedicated browser.
            try:
                if context is not None and self.connection_mode == "managed":
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None and self.connection_mode == "managed":
                    browser.close()
            except Exception:
                pass
            try:
                playwright.stop()
            except Exception:
                pass

    # -- launch / attach ----------------------------------------------------
    def _open_context(self, playwright):
        if self.connection_mode == "attach":
            return self._attach(playwright)
        return self._launch_managed(playwright)

    def _launch_managed(self, playwright):
        brave = PathDetector.detect(self.brave_path)
        headless = self.window_mode == "headless"
        self._log(
            f"Launching Brave (window_mode={self.window_mode}, headless={headless}, "
            f"DISPLAY={os.environ.get('DISPLAY')}, "
            f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY')}): {brave}"
        )
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        # Window placement (irrelevant in headless mode, which has no window):
        #   visible -> on-screen at a fixed spot
        #   hidden  -> pushed off-screen (NOTE: Wayland/WSLg compositors may ignore
        #              positioning and still show it; use headless for a guaranteed
        #              no-window run)
        if self.window_mode == "visible":
            window_args = ["--window-position=60,60", "--window-size=1100,850"]
        elif self.window_mode == "hidden":
            window_args = ["--window-position=-32000,-32000", "--window-size=1100,850"]
        else:  # headless
            window_args = []

        args = [
            "--no-sandbox",              # required in many WSL2 / container setups
            "--disable-gpu",
            "--disable-dev-shm-usage",   # avoid small /dev/shm crashes in WSL2/docker
            # Keep the page fully "awake" so a backgrounded/occluded window can't
            # throttle Leo's streamed reply.
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            *window_args,
        ]
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                executable_path=brave,
                headless=headless,
                args=args,
                ignore_default_args=["--enable-automation"],
            )
        except Exception as exc:
            raise BraveLaunchError(
                f"Failed to launch Brave at '{brave}'. If the binary path is wrong, set "
                f"the 'brave_path' input or BRAVE_LEO_EXECUTABLE. Underlying error: {exc}"
            ) from exc
        return context, context.browser

    def _attach(self, playwright):
        url = f"http://{self.debug_host}:{self.debug_port}"
        self._log(f"Attaching to running Brave via CDP: {url}")
        try:
            browser = playwright.chromium.connect_over_cdp(url)
        except Exception as exc:
            raise BraveLaunchError(
                f"Could not attach to Brave at {url}. Launch Brave first with:\n"
                f"  brave-browser --remote-debugging-port={self.debug_port} "
                f"--remote-allow-origins=*\n"
                f"On WSL2 -> Windows, also enable mirrored networking "
                f"(networkingMode=mirrored in .wslconfig). Underlying error: {exc}"
            ) from exc
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        return context, browser

    # -- navigation & interaction ------------------------------------------
    def _goto_leo(self, page) -> None:
        try:
            page.goto(LEO_URL, wait_until="domcontentloaded", timeout=self.timeout_seconds * 1000)
        except Exception as exc:
            raise LeoUIError(
                f"Could not open {LEO_URL}. Make sure this Brave build has Leo enabled. "
                f"Underlying error: {exc}"
            ) from exc
        page.wait_for_timeout(800)  # let the WebUI hydrate

    def _find_composer(self, page):
        """Find Leo's text box, dismissing the one-time consent screen if needed."""
        loc = self._try_composer(page, quick=True)
        if loc is not None:
            return loc

        # Not found yet -> probably the onboarding / consent overlay. Try to clear it.
        self._dismiss_onboarding(page)
        loc = self._try_composer(page, quick=False)
        if loc is not None:
            return loc

        raise LeoUIError(
            "Could not find Leo's text input box. Brave may have changed its Leo UI, or "
            "a consent screen is blocking it. Run 'python selftest.py --window-mode visible' once and "
            "complete any Leo setup by hand (the dedicated profile will remember it), or "
            "update SELECTORS['composer'] in brave_leo_core.py."
        )

    def _try_composer(self, page, *, quick: bool):
        deadline = time.time() + (4 if quick else max(8, self.timeout_seconds // 2))
        while time.time() < deadline:
            # Use ONLY the composer-specific selectors. We deliberately do not fall
            # back to a generic role=textbox first-match, because Leo's left-panel
            # "Filter conversations" search box is also a textbox and would win.
            for sel in SELECTORS["composer"]:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        return loc.first
                except Exception:
                    continue
            page.wait_for_timeout(400)
        return None

    def _dismiss_onboarding(self, page) -> None:
        """Best-effort: click an Accept/Continue/Get-started style button. Never fatal."""
        try:
            btns = page.get_by_role("button", name=ONBOARDING_BUTTON_RE)
            count = min(btns.count(), 4)
            for i in range(count):
                try:
                    b = btns.nth(i)
                    if b.is_visible():
                        self._log("Dismissing a Leo onboarding/consent button.")
                        b.click(timeout=1500)
                        page.wait_for_timeout(600)
                except Exception:
                    continue
        except Exception:
            pass

    def _submit(self, page, composer, query: str) -> None:
        composer.click()
        try:
            composer.fill(query)
        except Exception:
            # contenteditable: fill() can be rejected; insert the text directly.
            try:
                page.keyboard.insert_text(query)
            except Exception:
                composer.type(query)
        page.wait_for_timeout(300)  # let the send button become enabled
        # Leo submits via the arrow-up send button. Prefer clicking it; only fall
        # back to Enter if no send button can be found/clicked.
        if self._click_send_fallback(page):
            self._log("Prompt submitted to Leo (send button).")
        else:
            composer.press("Enter")
            self._log("Prompt submitted to Leo (Enter fallback).")

    def _click_send_fallback(self, page) -> bool:
        """If Enter didn't submit, try clicking a send button once."""
        try:
            role_loc = page.get_by_role("button", name=SEND_BUTTON_RE)
            if role_loc.count() > 0 and role_loc.first.is_visible():
                role_loc.first.click(timeout=1500)
                return True
        except Exception:
            pass
        for sel in SELECTORS["send_button"]:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=1500)
                    return True
            except Exception:
                continue
        return False

    # -- response scraping (iframe-aware) -----------------------------------
    def _conversation_text(self, page) -> str:
        """Return the full text of Leo's conversation. The conversation lives in
        an iframe whose document is invisible to page-level locators, so we scope
        into it; we fall back to the main region for builds that don't use one."""
        try:
            fl = page.frame_locator(CONVERSATION_IFRAME)
            body = fl.locator("body")
            if body.count() > 0:
                return (body.first.inner_text(timeout=1500) or "").strip()
        except Exception:
            pass
        for sel in ('[data-testid="main"]', "main", "body"):
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    return (loc.first.inner_text(timeout=1500) or "").strip()
            except Exception:
                continue
        return ""

    # Trailing UI that Leo appends under a finished reply (action toolbar, model
    # name like "Qwen VL 235B", etc.). Stripped line-by-line from the end.
    _UI_TRAILING_RE = re.compile(
        r"^(copy|edit|like|dislike|good response|bad response|regenerate|retry|"
        r"stop|share|read aloud|read|sources?|automatic|ask follow up|"
        r"new conversation|[\w .+\-/]*\b\d+\s*[bBkKmM]\b[\w .+\-/]*)$",
        re.IGNORECASE,
    )

    def _strip_trailing_ui(self, text: str) -> str:
        lines = text.splitlines()
        while lines and (not lines[-1].strip() or self._UI_TRAILING_RE.match(lines[-1].strip())):
            lines.pop()
        return "\n".join(lines).strip()

    # Transient status lines Leo shows WHILE searching (e.g. "Improving answer
    # with Brave Search…"). If the reply is only these, it isn't finished yet.
    _STATUS_RE = re.compile(
        r"^(improving answer with brave search"
        r"|searching( the web| with brave[\w ]*)?"
        r"|looking (up|for)[\w ]*"
        r"|browsing[\w ]*|reading[\w ]*|gathering[\w ]*"
        r"|generating[\w ]*|thinking[\w ]*|analy[sz]ing[\w ]*"
        r"|working on it|one moment|please wait)[.\s\u2026]*$",
        re.IGNORECASE,
    )
    # Start of a citations/sources block; everything from here on is dropped.
    _SOURCES_RE = re.compile(
        r"^(sources?|citations?|references?|related|learn more|see more|"
        r"footnotes?|read more)\b",
        re.IGNORECASE,
    )

    def _strip_leading_status(self, text: str) -> str:
        lines = text.splitlines()
        i = 0
        while i < len(lines) and (not lines[i].strip() or self._STATUS_RE.match(lines[i].strip())):
            i += 1
        return "\n".join(lines[i:]).strip()

    def _strip_sources(self, text: str) -> str:
        out = []
        for ln in text.splitlines():
            stripped = ln.strip()
            if self._SOURCES_RE.match(stripped) or re.search(r"https?://", stripped):
                break  # citations/URLs begin here -> the prompt is everything above
            out.append(ln)
        return "\n".join(out).strip()

    def _clean_candidate(self, text: str) -> str:
        """Strip search-status lines, a citations/URL tail, and the action toolbar."""
        return self._strip_trailing_ui(self._strip_sources(self._strip_leading_status(text)))

    def _after_query(self, text: str, query: str) -> Optional[str]:
        """Return the text that follows the echoed query in the conversation,
        matching the query whitespace-insensitively (the bubble may re-wrap it)."""
        tokens = [re.escape(tok) for tok in query.split() if tok]
        if not tokens:
            return None
        pat = re.compile(r"\s+".join(tokens), re.IGNORECASE | re.DOTALL)
        matches = list(pat.finditer(text))
        if matches:
            return text[matches[-1].end():]
        return None

    def _extract_reply(self, full_text: str, query: str, baseline: str) -> str:
        """Isolate Leo's final prompt from the full conversation text.

        Returns "" when the reply is not finished yet (only a search-status line is
        showing, or the start marker has appeared but not the end marker). The wait
        loop treats "" as 'keep waiting', which is what makes web-search runs robust.
        """
        if not full_text:
            return ""
        after = self._after_query(full_text, query)
        if after is None:
            after = full_text[len(baseline):] if (baseline and full_text.startswith(baseline)) else full_text

        # Preferred: the explicitly delimited block. Search only AFTER the echoed
        # query, since the query text itself names the markers.
        m = re.search(
            re.escape(PROMPT_START_MARKER) + r"(.*?)" + re.escape(PROMPT_END_MARKER),
            after, re.DOTALL,
        )
        if m:
            return self._clean_candidate(m.group(1))
        if PROMPT_START_MARKER in query:
            return ""  # search activity and incomplete replies are not prompts

        # Queries without markers still use heuristic cleanup.
        return self._clean_candidate(after)

    def _wait_for_response(self, page, query: str, baseline: str = "") -> str:
        """Poll until Leo's reply stops growing (streaming finished), then return it."""
        deadline = time.time() + self.timeout_seconds
        last = ""
        last_change = time.time()
        seen = False
        stable_for = 2.5

        while time.time() < deadline:
            cur = self._extract_reply(self._conversation_text(page), query, baseline)
            if cur and cur != last:
                last, last_change, seen = cur, time.time(), True
            # Reply present and unchanged for a beat -> streaming finished.
            if seen and last and (time.time() - last_change) >= stable_for:
                self._log("Leo response complete.")
                return last
            page.wait_for_timeout(700)

        # Timed out. Salvage whatever we can: Leo may have written the prompt but
        # never closed the marker, or only produced a partial answer.
        salvage = self._salvage_reply(self._conversation_text(page), query, baseline)
        if salvage:
            self._log("Timeout reached; salvaged a partial response.")
            return salvage
        if last.strip():
            self._log("Timeout reached; returning partial response.")
            return last
        raise LeoTimeoutError(
            f"Leo did not respond within {self.timeout_seconds}s. Try raising "
            f"'timeout_seconds', or run 'python selftest.py --full --window-mode visible' to watch it."
        )

    def _salvage_reply(self, full_text: str, query: str, baseline: str) -> str:
        """Best-effort extraction on timeout: drop any stray marker tokens and clean."""
        if not full_text:
            return ""
        after = self._after_query(full_text, query)
        if after is None:
            after = full_text[len(baseline):] if (baseline and full_text.startswith(baseline)) else full_text
        if PROMPT_START_MARKER not in after:
            return ""
        after = after.split(PROMPT_START_MARKER, 1)[1].replace(PROMPT_END_MARKER, " ")
        return self._clean_candidate(after)

    # -- debugging aid ------------------------------------------------------
    def dump_debug(self, page, out_dir: str = ".") -> Tuple[str, str]:
        """Save the live HTML (page + every iframe) and a screenshot to help
        update selectors. Iframe HTML is dumped separately because the
        conversation lives in one and is absent from the top-level page.content()."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        html_path = out / "brave_leo_debug.html"
        png_path = out / "brave_leo_debug.png"
        try:
            parts = ["<!-- ===== TOP-LEVEL PAGE ===== -->", page.content()]
            for i, frame in enumerate(page.frames):
                if frame == page.main_frame:
                    continue
                try:
                    parts.append(f"\n<!-- ===== IFRAME {i}: {frame.url} ===== -->")
                    parts.append(frame.content())
                except Exception:
                    parts.append(f"\n<!-- IFRAME {i}: could not read content -->")
            html_path.write_text("\n".join(parts), encoding="utf-8")
        except Exception:
            pass
        try:
            page.screenshot(path=str(png_path), full_page=True)
        except Exception:
            pass
        return str(html_path), str(png_path)


# ===========================================================================
# One-shot convenience entry point used by the node and the self-test.
# ===========================================================================
def run_leo(
    *,
    mode: str,
    original_prompt: str,
    search_topic: str = "",
    leo_personality: str = DEFAULT_PERSONALITY,
    connection_mode: str = "managed",
    brave_path: str = "",
    window_mode: str = "headless",
    timeout_seconds: int = 90,
    debug_host: str = "127.0.0.1",
    debug_port: int = 9222,
    on_log: Optional[Callable[[str], None]] = None,
) -> Tuple[str, str]:
    """Build the query, drive Leo, and return ``(enhanced_prompt, raw_response)``.

    Raises a :class:`BraveLeoError` subclass on failure (the node catches these
    and degrades gracefully by passing the original prompt through)."""
    query = build_query(mode, original_prompt, search_topic, leo_personality)
    service = BraveAutomationService(
        connection_mode=connection_mode,
        brave_path=brave_path,
        window_mode=window_mode,
        timeout_seconds=timeout_seconds,
        debug_host=debug_host,
        debug_port=debug_port,
        on_log=on_log,
    )
    raw = service.run(query)
    enhanced = clean_response(raw) or original_prompt
    return enhanced, raw


# ===========================================================================
# Subprocess runner.
#
# Why a whole separate process? Playwright's *sync* API refuses to run inside a
# thread that has a live asyncio event loop ("Sync API inside the asyncio loop").
# Depending on the ComfyUI build, the node's async execute can run in exactly
# such a context even via asyncio.to_thread. A fresh child process is GUARANTEED
# to have no event loop, so the sync API always works there. The node calls
# run_leo_subprocess(); this module's __main__ block is the worker.
# ===========================================================================
_RESULT_PREFIX = "__BRAVELEO_RESULT__ "
_LOG_PREFIX = "__BRAVELEO_LOG__ "

# Map worker error-type names back to real exception classes for a clean status.
_ERROR_CLASSES = {
    c.__name__: c
    for c in (
        BraveNotFoundError, PlaywrightMissingError, DisplayUnavailableError,
        BraveLaunchError, LeoUIError, LeoTimeoutError, BraveLeoError,
    )
}


def run_leo_subprocess(
    *,
    mode: str,
    original_prompt: str,
    search_topic: str = "",
    leo_personality: str = DEFAULT_PERSONALITY,
    connection_mode: str = "managed",
    brave_path: str = "",
    window_mode: str = "headless",
    timeout_seconds: int = 90,
    debug_host: str = "127.0.0.1",
    debug_port: int = 9222,
    on_log: Optional[Callable[[str], None]] = None,
) -> Tuple[str, str]:
    """Run the Leo automation in a child process and return (enhanced, raw)."""
    log = on_log or (lambda _m: None)
    payload = {
        "mode": mode, "original_prompt": original_prompt, "search_topic": search_topic,
        "leo_personality": leo_personality, "connection_mode": connection_mode,
        "brave_path": brave_path, "window_mode": window_mode,
        "timeout_seconds": int(timeout_seconds), "debug_host": debug_host,
        "debug_port": int(debug_port),
    }
    cmd = [sys.executable, os.path.abspath(__file__)]
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=os.environ.copy(),
        )
    except Exception as exc:  # pragma: no cover
        raise BraveLaunchError(f"Could not start the BraveLeo worker process: {exc}") from exc

    try:
        proc.stdin.write(json.dumps(payload))
        proc.stdin.flush()
        proc.stdin.close()
    except Exception:
        pass

    out_q: "queue.Queue[Optional[str]]" = queue.Queue()
    err_lines: List[str] = []

    def _read_out():
        try:
            for line in proc.stdout:
                out_q.put(line.rstrip("\n"))
        except Exception:
            pass
        finally:
            out_q.put(None)

    def _read_err():
        try:
            for line in proc.stderr:
                err_lines.append(line)
        except Exception:
            pass

    threading.Thread(target=_read_out, daemon=True).start()
    threading.Thread(target=_read_err, daemon=True).start()

    result: Optional[dict] = None
    deadline = time.time() + int(timeout_seconds) + 45  # buffer over the inner timeout
    while time.time() < deadline:
        try:
            line = out_q.get(timeout=1.0)
        except queue.Empty:
            continue
        if line is None:  # EOF: worker exited
            break
        if line.startswith(_RESULT_PREFIX):
            try:
                result = json.loads(line[len(_RESULT_PREFIX):])
            except Exception:
                result = {"ok": False, "error_type": "ParseError", "error": line}
            break
        if line.startswith(_LOG_PREFIX):
            log(line[len(_LOG_PREFIX):])
        else:
            log(line)

    if result is None:
        try:
            proc.kill()
        except Exception:
            pass
        tail = "".join(err_lines)[-800:].strip()
        raise LeoTimeoutError(
            "The BraveLeo worker process did not return a result in time."
            + (f" Worker stderr:\n{tail}" if tail else "")
        )

    if not result.get("ok"):
        et = result.get("error_type", "")
        msg = result.get("error", "Unknown error")
        cls = _ERROR_CLASSES.get(et, BraveLeoError)
        raise cls(msg)

    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

    return result.get("enhanced", original_prompt), result.get("raw", "")


def _worker_main() -> int:
    """Child-process entry point. Runs the sync Playwright work (no event loop
    here) and emits a single result line on stdout."""
    def emit_log(msg: str) -> None:
        sys.stdout.write(_LOG_PREFIX + str(msg).replace("\n", " ") + "\n")
        sys.stdout.flush()

    def emit_result(obj: dict) -> None:
        sys.stdout.write(_RESULT_PREFIX + json.dumps(obj) + "\n")
        sys.stdout.flush()

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as exc:
        emit_result({"ok": False, "error_type": "PayloadError", "error": str(exc)})
        return 1

    emit_log(
        f"worker env: DISPLAY={os.environ.get('DISPLAY')} "
        f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY')} "
        f"XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR')} "
        f"window_mode={payload.get('window_mode', 'headless')}"
    )
    try:
        query = build_query(
            payload["mode"], payload["original_prompt"],
            payload.get("search_topic", ""), payload.get("leo_personality", ""),
        )
        service = BraveAutomationService(
            connection_mode=payload.get("connection_mode", "managed"),
            brave_path=payload.get("brave_path", ""),
            window_mode=payload.get("window_mode", "headless"),
            timeout_seconds=int(payload.get("timeout_seconds", 90)),
            debug_host=payload.get("debug_host", "127.0.0.1"),
            debug_port=int(payload.get("debug_port", 9222)),
            on_log=emit_log,
        )
        raw = service.run(query)
        enhanced = clean_response(raw) or payload.get("original_prompt", "")
        emit_result({"ok": True, "enhanced": enhanced, "raw": raw, "status": "Success"})
        ok = True
    except BraveLeoError as exc:
        emit_result({"ok": False, "error_type": type(exc).__name__, "error": str(exc)})
        ok = False
    except Exception as exc:  # noqa: BLE001
        emit_result({"ok": False, "error_type": "BraveLeoError", "error": f"Unexpected error: {exc}"})
        ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_worker_main())
