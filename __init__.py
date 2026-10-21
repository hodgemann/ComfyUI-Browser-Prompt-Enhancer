"""
Browser Prompt Enhancer for ComfyUI
================
A modular ComfyUI custom-node package that enhances image-generation prompts
using the *free* Brave Leo assistant (with real-time web search) running in a
local Brave browser -- no paid API.

ComfyUI's V3 loader discovers extensions via ``comfy_entrypoint``; we re-export
it here so it is visible at the package top level.

Repos:
* Playwright     : https://github.com/microsoft/playwright-python
* Brave download : https://brave.com/download/
* ComfyUI V3 API : https://docs.comfy.org/custom-nodes/v3_migration
"""

from .brave_leo_node import BraveLeoNode, comfy_entrypoint

__all__ = ["BraveLeoNode", "comfy_entrypoint"]
