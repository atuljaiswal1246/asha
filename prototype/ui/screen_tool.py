"""Jarvis's eyes — see the screen (and any image) without a vendor's permission.

Two layers, both on-device by default:

* **capture** — `/usr/sbin/screencapture` grabs the screen (or one window) to a
  PNG. No API, no OAuth, no allowlist: unlike a provider's MCP server, this
  needs nothing from anybody.
* **ocr** — macOS's own Vision framework (compiled once into a tiny cached
  helper) turns the pixels into text. Free, offline, instant.
* **look** — when a question needs real understanding (a design, a chart, a
  screenshot of a bug), the image goes to a vision model through the normal
  provider stack (`JARVIS_VISION_MODEL`, default OpenRouter `openai/gpt-4o`).

Everything degrades honestly: if Screen Recording isn't granted we say so
instead of returning a black rectangle, and if no vision model is configured we
fall back to OCR text rather than pretending.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCREENCAPTURE = "/usr/sbin/screencapture"
DEFAULT_VISION_MODEL = "openai/gpt-4o"
DEFAULT_VISION_PROVIDER = "openrouter"

_OCR_SRC = r'''
import AppKit
import Foundation
import Vision

let args = CommandLine.arguments
guard args.count >= 2, let image = NSImage(contentsOfFile: args[1]),
      let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot read image\n".data(using: .utf8)!)
    exit(2)
}
var lines: [String] = []
let request = VNRecognizeTextRequest { req, _ in
    guard let observations = req.results as? [VNRecognizedTextObservation] else { return }
    for obs in observations {
        if let best = obs.topCandidates(1).first { lines.append(best.string) }
    }
}
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do { try handler.perform([request]) } catch {
    FileHandle.standardError.write("ocr failed: \(error)\n".data(using: .utf8)!)
    exit(3)
}
print(lines.joined(separator: "\n"))
'''


class ScreenError(RuntimeError):
    """Anything that stops us seeing the screen, with a fixable message."""


def _data_dir() -> Path:
    try:
        import jarvis_paths

        return Path(jarvis_paths.data_dir())
    except Exception:  # noqa: BLE001 - standalone use
        root = Path(__file__).resolve().parent.parent
        return root / "data"


def _ocr_binary() -> Path:
    """Compile the Vision OCR helper once, then reuse it."""
    cache = _data_dir() / "tools"
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / "jarvis_ocr"
    source = cache / "jarvis_ocr.swift"
    if binary.exists() and source.exists() and source.read_text() == _OCR_SRC:
        return binary
    if not shutil.which("swiftc"):
        raise ScreenError("swiftc not found — OCR needs the Xcode command line tools")
    source.write_text(_OCR_SRC)
    proc = subprocess.run(
        ["swiftc", "-O", "-o", str(binary), str(source)],
        capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise ScreenError(f"could not build the OCR helper: {proc.stderr.strip()[:300]}")
    return binary




_WIN_SRC = r'''
import CoreGraphics
import Foundation

let opts = CGWindowListOption(arrayLiteral: .optionOnScreenOnly, .excludeDesktopElements)
guard let list = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else {
    exit(1)
}
for w in list {
    let layer = (w[kCGWindowLayer as String] as? Int) ?? 0
    let alpha = (w[kCGWindowAlpha as String] as? Double) ?? 0
    guard layer == 0, alpha > 0.05 else { continue }
    let num = (w[kCGWindowNumber as String] as? Int) ?? 0
    let owner = (w[kCGWindowOwnerName as String] as? String) ?? ""
    let name = (w[kCGWindowName as String] as? String) ?? ""
    let b = (w[kCGWindowBounds as String] as? [String: Any]) ?? [:]
    let bw = (b["Width"] as? NSNumber)?.intValue ?? 0
    let bh = (b["Height"] as? NSNumber)?.intValue ?? 0
    print("\(num)\t\(owner)\t\(name)\t\(bw)x\(bh)")
}
'''


def _window_binary() -> Path:
    """Compile the window-list helper once, then reuse it."""
    cache = _data_dir() / "tools"
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / "jarvis_windows"
    source = cache / "jarvis_windows.swift"
    if binary.exists() and source.exists() and source.read_text() == _WIN_SRC:
        return binary
    if not shutil.which("swiftc"):
        raise ScreenError("swiftc not found — listing windows needs the Xcode command line tools")
    source.write_text(_WIN_SRC)
    proc = subprocess.run(["swiftc", "-O", "-o", str(binary), str(source)],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise ScreenError(f"could not build the window helper: {proc.stderr.strip()[:300]}")
    return binary


def list_windows() -> list[dict]:
    """Every ordinary on-screen window: {id, owner, title, width, height}.

    On-screen only and no raising: this is how we look at a window WITHOUT
    stealing the user's focus (``screencapture -l <id>`` captures it in place,
    even when it is behind other windows).
    """
    proc = subprocess.run([str(_window_binary())], capture_output=True,
                          text=True, timeout=60)
    if proc.returncode != 0:
        raise ScreenError(proc.stderr.strip() or "could not list windows")
    out: list[dict] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        size = parts[3].split("x")
        out.append({
            "id": int(parts[0]),
            "owner": parts[1],
            "title": parts[2],
            "width": int(size[0] or 0),
            "height": int(size[1] or 0),
        })
    return out


def find_window(match: str) -> dict | None:
    """The first window whose owner or title contains *match* (case-insensitive)."""
    want = (match or "").lower()
    best = None
    for w in list_windows():
        hay = f"{w['owner']} {w['title']}".lower()
        if want and want in hay:
            # prefer the largest match (the real window, not a tooltip/panel)
            if best is None or w["width"] * w["height"] > best["width"] * best["height"]:
                best = w
    return best


def capture(path: str | None = None, *, window: bool | str | int = False,
            display: int | None = None) -> Path:
    """Screenshot the screen, or one window, to a PNG.

    ``window`` may be True (frontmost), a window id, or a name/title match such
    as "Jarvis" — a named window is captured **in place, without raising it to
    the front**, so we never take over the user's screen to look at something.
    """
    out = Path(path) if path else Path(tempfile.gettempdir()) / "jarvis-screen.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [SCREENCAPTURE, "-x", "-t", "png"]
    win_id = None
    # NOTE: bool is a subclass of int in Python, so `isinstance(False, int)` is
    # True - checking int first turned the default `window=False` into
    # `screencapture -l False`, which broke every plain screen capture. Test the
    # boolean case explicitly, before the integer case.
    if isinstance(window, bool):
        win_id = None
    elif isinstance(window, int):
        win_id = window
    elif isinstance(window, str) and window:
        found = find_window(window)
        if not found:
            raise ScreenError(f"no window matching {window!r} is on screen")
        win_id = found["id"]
    if window:
        cmd.append("-o")  # no window shadow
    if win_id is not None:
        cmd += ["-l", str(win_id)]
    if display is not None:
        cmd += ["-D", str(display)]
    cmd.append(str(out))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0 or not out.exists():
        raise ScreenError(
            "screen capture failed — grant Screen Recording to the app running "
            "Jarvis (System Settings → Privacy & Security → Screen Recording), "
            "then try again")
    return out


def _is_blank(path: Path) -> bool:
    """A denied capture is a valid PNG of nothing but the wallpaper/black."""
    try:
        proc = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001
        return False
    return "pixelWidth" not in proc.stdout


def ocr(path: str | Path) -> str:
    """Read the text on an image with macOS Vision."""
    resolved = Path(path).expanduser().resolve()
    binary = _ocr_binary()
    proc = subprocess.run([str(binary), str(resolved)], capture_output=True,
                          text=True, timeout=120)
    if proc.returncode != 0:
        raise ScreenError(proc.stderr.strip() or "OCR failed")
    return proc.stdout.strip()


def _data_url(path: Path) -> str:
    raw = path.read_bytes()
    suffix = path.suffix.lower().lstrip(".") or "png"
    mime = "image/jpeg" if suffix in ("jpg", "jpeg") else f"image/{suffix}"
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _shrink(path: Path, max_edge: int = 1400) -> Path:
    """A smaller copy: fewer tokens, same readability. Falls back to the original."""
    out = path.with_name(path.stem + f"-{max_edge}.png")
    try:
        subprocess.run(["sips", "-Z", str(max_edge), str(path), "--out", str(out)],
                       capture_output=True, text=True, timeout=60)
        if out.exists() and out.stat().st_size:
            return out
    except Exception:  # noqa: BLE001
        pass
    return path


def vision_ready() -> bool:
    try:
        from providers import get_provider

        provider = get_provider(os.environ.get(
            "JARVIS_VISION_PROVIDER", DEFAULT_VISION_PROVIDER))
        return bool(provider.is_configured())
    except Exception:  # noqa: BLE001
        return False


def look(path: str | Path, question: str = "") -> str:
    """Answer a question about an image with a vision model."""
    from providers import chat

    provider_id = os.environ.get("JARVIS_VISION_PROVIDER", DEFAULT_VISION_PROVIDER)
    model = os.environ.get("JARVIS_VISION_MODEL", DEFAULT_VISION_MODEL)
    cap = int(os.environ.get("JARVIS_VISION_MAX_TOKENS", "900"))
    prompt = question or (
        "Describe this image precisely: any text, UI, layout, code or data in "
        "it. Be concrete and complete.")
    resolved = Path(path).expanduser().resolve()
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": _data_url(_shrink(resolved))}},
        ],
    }]
    resp = chat(provider_id, model, messages, timeout=180, max_tokens=cap)
    try:
        return resp["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        raise ScreenError(f"vision model returned nothing usable: {exc}") from exc


def see_screen(question: str = "", *, path: str | None = None) -> str:
    """Capture the screen and read it — OCR always, vision model when asked.

    Returns a short report. With ``question`` set (and a vision model
    available) the answer is the model's reading of the screen; otherwise the
    on-device OCR text.
    """
    try:
        shot = capture(path, window=False)
    except ScreenError as exc:
        # Degrade honestly: the tool returns text the model can act on instead
        # of raising, so Jarvis can tell the user what to do about it.
        return f"[screen] could not look: {exc}"
    if _is_blank(shot):
        return ("[screen] captured an empty image — Screen Recording permission "
                "is likely missing for the app running Jarvis.")
    text = ""
    try:
        text = ocr(shot)
    except ScreenError as exc:
        text = f"(ocr unavailable: {exc})"
    if question and vision_ready():
        try:
            answer = look(shot, question)
            return f"{answer}\n\n[screen: {shot}]"
        except Exception as exc:  # noqa: BLE001 - fall back to OCR
            text = f"{text}\n(vision model failed: {exc})"
    if not text:
        return f"[screen] no text found on screen (image: {shot})"
    return f"{text[:4000]}\n\n[screen: {shot}]"


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Jarvis's eyes")
    ap.add_argument("action", choices=["capture", "ocr", "look", "see"])
    ap.add_argument("path", nargs="?", default=None)
    ap.add_argument("--question", "-q", default="")
    ap.add_argument("--window", action="store_true")
    a = ap.parse_args()
    try:
        if a.action == "capture":
            print(capture(a.path, window=a.window))
        elif a.action == "ocr":
            print(ocr(a.path))
        elif a.action == "look":
            print(look(a.path, a.question))
        else:
            print(see_screen(a.question, path=a.path))
    except ScreenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
