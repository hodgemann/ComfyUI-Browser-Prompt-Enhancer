# 🪿 Browser Prompt Enhancer for ComfyUI

Enhance your image-generation prompts with the **free Brave Leo** assistant — which has **real-time web search** — running in a local Brave browser. This is an unofficial integration, unaffiliated with Brave. **No API key, no API cost.**

Two modes:

- **append** — Leo searches the web and adds fresh, concrete visual detail to the *end* of your prompt, keeping your original intent intact.
- **rewrite** — Leo searches the web and rewrites your idea into a fully optimized image-generation prompt.

> Built on the ComfyUI **V3** node API. Designed to be modular and extensible (new modes are a one-line registration).

---

## How it works (and honest limitations — please read)

This node uses [Playwright](https://github.com/microsoft/playwright-python) to drive Leo's **full-page view at `brave://leo-ai`**, type your request, wait for the streamed answer, and return it to your workflow.

A few things to understand up front:

- **It automates the full-page Leo, not the sidebar.** The Leo *sidebar* is part of the browser's chrome (its app UI) and cannot be reached by page automation. The full-page view is normal DOM, so that's what we drive.
- **Runs invisibly by default.** `window_mode` defaults to `headless` (a real browser engine with no window), so nothing pops up. Set it to `visible` if you want to watch, or `hidden` to try an off-screen window. (Off-screen hiding is unreliable on Wayland/WSLg, which is why headless is the default.)
- **Visible mode needs a display.** The default headless mode does not. On **WSL2 + Windows 11**, WSLg provides a display for visible mode (see below).
- **This is UI automation of an app not built to be automated, so it is inherently fragile.** A Brave update can change Leo's markup. When that happens you'll get a clear error, and the fix is usually a selector update in `brave_leo_core.py` (`SELECTORS`). The browser flow was tested on WSL2 in October 2026.
- **Graceful by default.** If Leo can't be reached, the node returns your **original prompt unchanged** plus a `status` message, so your workflow keeps running.

---

## Prerequisites

1. **Brave Browser** — install from <https://brave.com/download/>. Free Leo requires **no account**. (On WSL2, install the *Linux* Brave inside WSL2 — see below.)
2. **Playwright** — installed automatically via `requirements.txt`. The node launches your installed Brave, so Playwright's bundled Chromium download is not needed.

---

## Installation

### Option A — ComfyUI Manager (easiest, once published)

1. Open **ComfyUI → Manager → Custom Nodes Manager**.
2. Search for **Browser Prompt Enhancer** and click **Install**.
3. Open a terminal **in the same Python environment ComfyUI uses** and run:
   ```bash
   # Linux/WSL2 only, if the self-test reports missing browser libraries:
   playwright install-deps chromium
   ```
4. Restart ComfyUI.

### Option B — Manual (git clone)

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/hodgemann/ComfyUI-Browser-Prompt-Enhancer
cd ComfyUI-Browser-Prompt-Enhancer
pip install -r requirements.txt
# Linux/WSL2 only, if needed:
playwright install-deps chromium
```

Restart ComfyUI. The node appears under **Add Node → Browser Prompt Enhancer → Browser Prompt Enhancer**.

---

## WSL2 + Windows 11 setup (important if that's you)

ComfyUI runs inside WSL2 (Linux), but your everyday Brave is a *Windows* app — and a Linux Playwright process **cannot drive a Windows `.exe`**. The reliable approach is to install a **Linux Brave inside WSL2** and let **WSLg** (built into Windows 11) display it on your Windows desktop. The node then launches/closes its own visible Brave window.

```bash
# Inside your WSL2 shell:

# 1) Install the Linux Brave
sudo apt update && sudo apt install -y curl
curl -fsS https://dl.brave.com/install.sh | sh
#    (alternative methods: https://brave.com/linux/)

# 2) Make sure ComfyUI's Python env has Playwright + browser deps
pip install -r requirements.txt
playwright install-deps chromium      # pulls the system libraries Chromium needs

# 3) Verify everything end-to-end
python selftest.py --full
```

Notes:
- **Start ComfyUI from an interactive WSL shell** so the WSLg display variables (`DISPLAY` / `WAYLAND_DISPLAY`) are present. If you run ComfyUI as a background service without them, the node can't open a window. `selftest.py` checks this for you.
- The dedicated Brave profile is created at `~/.comfyui-braveleo/profile`. **On the very first run**, Leo may show a one-time consent screen. The node tries to dismiss it automatically; if it can't, run `python selftest.py --window-mode visible` once and click through by hand — the profile remembers it afterward.

---

## Usage

Add **Browser Prompt Enhancer**, type a prompt, pick a mode, and connect `enhanced_prompt` to your CLIP Text Encode (or wherever your prompt goes).

The [example workflow](example_workflows/brave_leo_to_clip.json) uses “A puppy dog sniffing a flower.” (the maintainer's preferred public SFW sample prompt) and the model-agnostic default personality. Queue it to preview Leo's result. It contains no checkpoint or LoRA names. To use it for images, connect your existing CLIP output to `CLIP Text Encode` and route its conditioning into your image workflow.

### Inputs

| Input | Required | Description |
|---|---|---|
| `original_prompt` | ✅ | Your base prompt (multiline). Returned unchanged if Leo can't be reached. |
| `mode` | ✅ | `rewrite` (full optimization; default) or `append` (add one sentence, keep intent). |
| `search_topic` | optional | What Leo should search for. Empty → uses the original prompt. |
| `leo_personality` | optional | A model-agnostic instruction prepended to the request (multiline; edit freely). |
| `connection_mode` | advanced | `managed` (launch a dedicated Brave window — default) or `attach` (use a running Brave). |
| `window_mode` | advanced | `headless` (no window — default, fully invisible), `hidden` (off-screen; may still show on Wayland/WSLg), or `visible` (debugging). `managed` only. |
| `timeout_seconds` | advanced | How long to wait for Leo's full response (default 90). |
| `brave_path` | advanced | Override the Brave executable path. Empty → auto-detect. |
| `debug_host` / `debug_port` | advanced | `attach` mode only (see below). |

### Outputs

| Output | Description |
|---|---|
| `enhanced_prompt` | The cleaned result (or your original prompt on failure). Wire this onward. |
| `raw_response` | Leo's full, unmodified reply — handy for debugging. |
| `status` | `Success` or a clear error message. |

### Examples

**append** — `original_prompt: "A futuristic city street."`, `search_topic: "2026 solarpunk city design"`
→ *"A futuristic city street, lined with solar-panel vertical gardens, holographic transit signage, and rain-slick reflective pavement."*

**rewrite** — `original_prompt: "Cat warrior."`
→ *"A hyper-detailed portrait of a feline knight in ornate engraved armor, battle-worn fur, glowing amber eyes, dramatic cinematic lighting, intricate metalwork, fantasy concept art, highly detailed, 8k."*

---

## Recovering past prompts from Leo's history

Every run is saved as a conversation in Leo's history inside the dedicated profile, so older enhanced prompts aren't lost. Two ways to get them back:

Open the dedicated profile directly to browse Leo's full conversation history:
  ```bash
  brave-browser --user-data-dir="$HOME/.comfyui-braveleo/profile" "brave://leo-ai/"
  ```

## Verifying it works (self-test)

Run this from the node folder, in ComfyUI's Python environment:

```bash
python selftest.py            # environment + Playwright + Brave + launch + open Leo
python selftest.py --full     # also send a tiny prompt and print Leo's reply
python selftest.py --full --window-mode visible   # watch the window while it happens
python selftest.py --debug    # on failure, dump live HTML + a screenshot for selector fixes
```

A green **PASS** on every line means the node should work in ComfyUI.

---

## Advanced: `attach` mode (reuse your own Brave session)

Instead of a dedicated profile, you can attach to a Brave you launched yourself:

```bash
brave-browser --remote-debugging-port=9222 --remote-allow-origins=*
```

Then set the node's `connection_mode` to `attach` (and `debug_port` to match). `--remote-allow-origins=*` is required by modern Chromium for CDP connections.

**WSL2 → Windows Brave:** this only works if WSL2 can reach Windows' `localhost`. Enable **mirrored networking** (Windows 11): put the following in `C:\Users\<you>\.wslconfig`, then `wsl --shutdown`:

```ini
[wsl2]
networkingMode=mirrored
```

This path is experimental and finicky; **managed mode is recommended.**

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `BraveNotFoundError` | Install Brave (<https://brave.com/download/>); on WSL2 install the *Linux* Brave inside WSL2. Or set `brave_path`. |
| `PlaywrightMissingError` | `pip install playwright`. |
| `DisplayUnavailableError` | Use `headless` mode or start ComfyUI with a working display. |
| Driver fails to start (Linux/WSL2) | `playwright install-deps chromium`. |
| `LeoUIError: could not find Leo's input` | A consent screen is up, or Brave changed its UI. Run `selftest.py --window-mode visible` (click through once) or `--debug` (then update `SELECTORS` in `brave_leo_core.py`). |
| `LeoTimeoutError` | Raise `timeout_seconds`; check your network; run `selftest.py --full --window-mode visible` to watch. |
| Leo asks you to sign in | Free Leo doesn't require an account; just complete any first-run prompt once in the dedicated profile. |

---

## Publishing (maintainer notes)

See **BEST_PRACTICES.md** for the full developer guide, and the *Publishing to the Comfy Registry* section there for step-by-step instructions to get this into ComfyUI Manager.

---

## Links

- Playwright (Python): <https://github.com/microsoft/playwright-python>
- Brave download: <https://brave.com/download/>
- ComfyUI V3 API docs: <https://docs.comfy.org/custom-nodes/v3_migration>
- Comfy Registry publishing: <https://docs.comfy.org/registry/publishing>

## License

MIT — see [LICENSE](LICENSE).
