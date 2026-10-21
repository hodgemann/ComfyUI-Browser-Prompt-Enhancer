"""
Browser Prompt Enhancer for ComfyUI :: brave_leo_node.py
========================================================

The ComfyUI **V3 schema** node that exposes the Brave Leo prompt enhancer.

This file is the only one that imports ``comfy_api``. All browser/automation
logic lives in ``brave_leo_core.py`` (which has no ComfyUI dependency), so the
self-test and unit tests can exercise the engine without ComfyUI installed.

References:
* ComfyUI V3 API : https://docs.comfy.org/custom-nodes/v3_migration
* Playwright     : https://github.com/microsoft/playwright-python
* Brave download : https://brave.com/download/
"""

from __future__ import annotations

import asyncio

from comfy_api.latest import ComfyExtension, io, ui

from .brave_leo_core import (
    DEFAULT_PERSONALITY,
    MODE_HANDLERS,
    BraveLeoError,
    __version__,
    run_leo_subprocess,
)

# Modes are sourced from the core registry, so adding a new @register_mode in
# brave_leo_core.py automatically makes it selectable here -- no edits needed.
_MODE_OPTIONS = list(MODE_HANDLERS.keys()) or ["append", "rewrite"]


class BraveLeoNode(io.ComfyNode):
    """Enhance an image prompt using the free Brave Leo assistant (web search,
    zero API cost) via local browser automation."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="BraveLeo_EnhancePrompt",
            display_name="Browser Prompt Enhancer",
            category="Browser Prompt Enhancer",
            description=(
                "Unofficial integration that drives Brave Leo in a local browser to "
                "append real-time context to, or fully rewrite, your prompt. "
                "No API key required."
            ),
            inputs=[
                io.String.Input(
                    "original_prompt",
                    multiline=True,
                    default="",
                    tooltip="Your base prompt. Returned unchanged if Leo can't be reached.",
                ),
                io.Combo.Input(
                    "mode",
                    options=_MODE_OPTIONS,
                    default="rewrite" if "rewrite" in _MODE_OPTIONS else _MODE_OPTIONS[0],
                    tooltip=(
                        "append: search and add details to the end (keeps your intent). "
                        "rewrite: search and rewrite the whole prompt for image generation."
                    ),
                ),
                io.String.Input(
                    "search_topic",
                    optional=True,
                    default="",
                    tooltip="What Leo should search for. Empty = use the original prompt.",
                ),
                io.String.Input(
                    "leo_personality",
                    optional=True,
                    multiline=True,
                    default=DEFAULT_PERSONALITY,
                    tooltip="A system-style instruction prepended to the request to Leo.",
                ),
                # ---- advanced (hidden behind the node's Advanced toggle) ----
                io.Combo.Input(
                    "connection_mode",
                    options=["managed", "attach"],
                    default="managed",
                    optional=True,
                    advanced=True,
                    tooltip=(
                        "managed: launch a dedicated Brave profile (recommended). "
                        "attach: connect to a Brave you started with --remote-debugging-port."
                    ),
                ),
                io.Combo.Input(
                    "window_mode",
                    options=["headless", "hidden", "visible"],
                    default="headless",
                    optional=True,
                    advanced=True,
                    tooltip=(
                        "headless: no window at all (recommended, fully invisible). "
                        "hidden: real window pushed off-screen (may still show on Wayland/WSLg). "
                        "visible: show the window (for debugging). managed mode only."
                    ),
                ),
                io.Int.Input(
                    "timeout_seconds",
                    default=90,
                    min=10,
                    max=300,
                    optional=True,
                    advanced=True,
                    tooltip="How long to wait for Leo's full response.",
                ),
                io.String.Input(
                    "brave_path",
                    optional=True,
                    default="",
                    advanced=True,
                    tooltip="Override the Brave executable path. Empty = auto-detect.",
                ),
                io.String.Input(
                    "debug_host",
                    optional=True,
                    default="127.0.0.1",
                    advanced=True,
                    tooltip="attach mode only: host of the running Brave's debug port.",
                ),
                io.Int.Input(
                    "debug_port",
                    default=9222,
                    min=1,
                    max=65535,
                    optional=True,
                    advanced=True,
                    tooltip="attach mode only: the --remote-debugging-port you launched Brave with.",
                ),
            ],
            outputs=[
                io.String.Output(display_name="enhanced_prompt"),
                io.String.Output(display_name="raw_response"),
                io.String.Output(display_name="status"),
            ],
        )

    @classmethod
    async def execute(
        cls,
        original_prompt: str,
        mode: str,
        search_topic: str = "",
        leo_personality: str = DEFAULT_PERSONALITY,
        connection_mode: str = "managed",
        window_mode: str = "headless",
        timeout_seconds: int = 90,
        brave_path: str = "",
        debug_host: str = "127.0.0.1",
        debug_port: int = 9222,
    ) -> io.NodeOutput:
        logs: list[str] = []

        def _log(msg: str) -> None:
            logs.append(msg)
            print(f"[BraveLeo] {msg}")

        original_prompt = original_prompt or ""

        try:
            # Run the Playwright work in a CHILD PROCESS. The sync Playwright API
            # cannot run inside a thread that has a live asyncio loop (which the
            # ComfyUI execution context can have); a separate process is guaranteed
            # to have no loop. asyncio.to_thread keeps ComfyUI responsive while we
            # block on the worker's pipes.
            enhanced, raw = await asyncio.to_thread(
                run_leo_subprocess,
                mode=mode,
                original_prompt=original_prompt,
                search_topic=search_topic,
                leo_personality=leo_personality,
                connection_mode=connection_mode,
                brave_path=brave_path,
                window_mode=window_mode,
                timeout_seconds=timeout_seconds,
                debug_host=debug_host,
                debug_port=debug_port,
                on_log=_log,
            )
            status = "Success"
        except BraveLeoError as exc:
            # Known, friendly failure -> degrade gracefully, keep the workflow alive.
            enhanced, raw, status = original_prompt, "", f"{type(exc).__name__}: {exc}"
            _log(status)
        except Exception as exc:  # noqa: BLE001 - never let the node hard-crash a run
            enhanced, raw, status = original_prompt, "", f"Unexpected error: {exc}"
            _log(status)

        preview = f"[{status}]\n\n{enhanced}"
        return io.NodeOutput(enhanced, raw, status, ui=ui.PreviewText(preview))


class BraveLeoExtension(ComfyExtension):
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [BraveLeoNode]


async def comfy_entrypoint() -> BraveLeoExtension:
    print(f"[BraveLeo] Browser Prompt Enhancer v{__version__} loaded.")
    return BraveLeoExtension()
