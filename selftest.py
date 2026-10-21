#!/usr/bin/env python3
"""
Browser Prompt Enhancer for ComfyUI :: selftest.py
===============================

A standalone, neat diagnostic you can run *outside* ComfyUI to confirm the whole
chain works on this machine before wiring the node into a workflow. It imports
only ``brave_leo_core`` (no ComfyUI required).

Usage
-----
    python selftest.py                 # checks + a launch smoke test (no Leo query)
    python selftest.py --full          # also sends a tiny prompt and prints Leo's reply
    python selftest.py --full --window-mode visible   # watch the Brave window
    python selftest.py --debug         # on failure, dump live HTML + screenshot for selectors
    python selftest.py --attach        # test 'attach' mode against a running Brave
    python selftest.py --mode rewrite --prompt "cat warrior"

Exit code is 0 only if every selected check passes.
"""

from __future__ import annotations

import argparse
import sys
import time

import brave_leo_core as core

# ---- tiny, dependency-free pretty printing --------------------------------
_USE_COLOR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def ok(msg: str) -> None:
    print(f"  {_c('32', 'PASS')}  {msg}")


def warn(msg: str) -> None:
    print(f"  {_c('33', 'WARN')}  {msg}")


def fail(msg: str) -> None:
    print(f"  {_c('31', 'FAIL')}  {msg}")


def head(msg: str) -> None:
    print("\n" + _c("1", msg))


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def add_fail(self, msg: str) -> None:
        self.failures.append(msg)
        fail(msg)

    def add_warn(self, msg: str) -> None:
        self.warnings.append(msg)
        warn(msg)


def check_environment(rep: Report, args) -> None:
    head("1. Environment")
    py = sys.version_info
    if py >= (3, 9):
        ok(f"Python {py.major}.{py.minor}.{py.micro}")
    else:
        rep.add_fail(f"Python {py.major}.{py.minor} -- ComfyUI needs 3.9+")

    osname = core.running_os()
    wsl = core.is_wsl()
    print(f"        OS detected: {osname}{' (WSL2)' if wsl else ''}")

    if core.display_available():
        ok("A display is available (GUI apps can render)")
    else:
        if osname == "linux" and args.window_mode != "headless" and not args.attach:
            rep.add_fail(
                "No DISPLAY / WAYLAND_DISPLAY for visible Brave. Use --window-mode "
                "headless or start ComfyUI with a working display."
            )
        elif args.window_mode == "headless":
            ok("Headless mode does not need a display")
        else:
            rep.add_warn("Could not confirm a display.")


def check_playwright(rep: Report) -> None:
    head("2. Playwright")
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401

        ok("'playwright' package importable")
    except ImportError:
        rep.add_fail(
            "'playwright' not installed. Run: pip install playwright"
        )
        return

    # Confirm the driver actually starts (catches broken installs early).
    try:
        from playwright.sync_api import sync_playwright

        p = sync_playwright().start()
        p.stop()
        ok("Playwright driver starts")
    except Exception as exc:  # noqa: BLE001
        rep.add_fail(
            f"Playwright driver failed to start ({exc}). On Linux/WSL2 try: "
            f"playwright install-deps chromium"
        )


def check_brave(rep: Report) -> str | None:
    head("3. Brave browser")
    try:
        path = core.PathDetector.detect()
        ok(f"Brave found: {path}")
        return path
    except core.BraveNotFoundError as exc:
        rep.add_fail(str(exc))
        return None


def check_launch_and_leo(rep: Report, args) -> None:
    head("4. Launch Brave + open Leo")
    if not core.display_available() and not args.attach and args.window_mode != "headless":
        rep.add_warn("Skipping launch test -- no display available.")
        return

    service = core.BraveAutomationService(
        connection_mode="attach" if args.attach else "managed",
        window_mode=args.window_mode,
        timeout_seconds=args.timeout,
        debug_host=args.debug_host,
        debug_port=args.debug_port,
        on_log=lambda m: print(f"        · {m}"),
    )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        rep.add_warn("Skipping launch test -- Playwright missing (see step 2).")
        return

    playwright = sync_playwright().start()
    context = browser = page = None
    try:
        context, browser = service._open_context(playwright)
        context.set_default_timeout(args.timeout * 1000)
        page = context.pages[0] if context.pages else context.new_page()
        ok("Brave launched / attached")

        service._goto_leo(page)
        ok(f"Opened {core.LEO_URL}")

        composer = service._find_composer(page)
        ok("Found Leo's text input box")

        if args.full:
            head("5. Round-trip (sending a tiny prompt to Leo)")
            query = core.build_query(
                args.mode, args.prompt, "", "Reply with as few words as possible."
            )
            print(f"        Sending: {args.prompt!r} (mode={args.mode})")
            t0 = time.time()
            service._submit(page, composer, query)
            reply = service._wait_for_response(page, query)
            dt = time.time() - t0
            cleaned = core.clean_response(reply)
            if cleaned.strip():
                ok(f"Leo replied in {dt:.1f}s")
                print(_c("36", "\n        ---- Leo says ----"))
                for line in cleaned.splitlines() or [cleaned]:
                    print(f"        {line}")
                print(_c("36", "        ------------------"))
            else:
                rep.add_fail("Leo returned an empty response.")
    except core.BraveLeoError as exc:
        rep.add_fail(str(exc))
        if args.debug and page is not None:
            h, s = service.dump_debug(page, ".")
            print(f"        Debug dumped -> {h} , {s}")
    except Exception as exc:  # noqa: BLE001
        rep.add_fail(f"Unexpected error: {exc}")
        if args.debug and page is not None:
            h, s = service.dump_debug(page, ".")
            print(f"        Debug dumped -> {h} , {s}")
    finally:
        try:
            if context is not None and not args.attach:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None and not args.attach:
                browser.close()
        except Exception:
            pass
        try:
            playwright.stop()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Self-test for Browser Prompt Enhancer.")
    ap.add_argument("--full", action="store_true", help="Send a tiny prompt and verify a reply.")
    ap.add_argument("--window-mode", default="visible", choices=["headless", "hidden", "visible"],
                    help="headless (no window), hidden (off-screen), or visible (default for testing).")
    ap.add_argument("--debug", action="store_true", help="Dump HTML + screenshot on failure.")
    ap.add_argument("--attach", action="store_true", help="Test attach mode (running Brave + debug port).")
    ap.add_argument("--timeout", type=int, default=90, help="Response timeout in seconds.")
    ap.add_argument("--mode", default="append", help="Mode to use for --full (append/rewrite/...).")
    ap.add_argument("--prompt", default="A futuristic city street.", help="Prompt for --full.")
    ap.add_argument("--debug-host", default="127.0.0.1")
    ap.add_argument("--debug-port", type=int, default=9222)
    args = ap.parse_args()

    print(_c("1", f"Browser Prompt Enhancer self-test  (core v{core.__version__})"))
    print(f"Registered modes: {sorted(core.MODE_HANDLERS)}")

    rep = Report()
    check_environment(rep, args)
    check_playwright(rep)
    check_brave(rep)
    check_launch_and_leo(rep, args)

    head("Summary")
    if rep.failures:
        fail(f"{len(rep.failures)} check(s) failed:")
        for f in rep.failures:
            print(f"        - {f.splitlines()[0]}")
    if rep.warnings:
        warn(f"{len(rep.warnings)} warning(s).")
    if not rep.failures:
        ok("All selected checks passed. The node should work in ComfyUI. 🎉"
           if _USE_COLOR else "All selected checks passed. The node should work in ComfyUI.")
        if not args.full:
            print("        Tip: re-run with --full to verify an actual Leo response.")
    return 1 if rep.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
