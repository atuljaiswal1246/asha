"""Jarvis's camera — see what the user points at, on demand.

One frame at a time, exactly like the screen: a tiny Swift helper (compiled
once and cached, the same pattern as ``screen_tool``'s OCR helper) starts an
``AVCaptureSession``, grabs a single frame as JPEG, and exits. Nothing streams
and nothing records. Reading the frame then reuses ``screen_tool``: on-device
OCR always, the configured vision model only when a question is asked.

Everything degrades honestly: no camera, no permission, or a refused TCC prompt
becomes a sentence Jarvis can say, never a raw AVFoundation error.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import screen_tool

_CAPTURE_SRC = r'''
import AVFoundation
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

func fail(_ code: Int32, _ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(code)
}

final class Box<T> {
    var value: T
    init(_ value: T) { self.value = value }
}

let arguments = CommandLine.arguments
let checkOnly = arguments.contains("--check")

guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .unspecified)
        ?? AVCaptureDevice.default(for: .video) else {
    fail(2, "no camera")
}

if checkOnly {
    print("camera=\(device.localizedName)")
    exit(0)
}

guard arguments.count >= 2 else {
    fail(4, "usage: jarvis_camera <output.jpg>")
}
let outputPath = arguments[1]

var status = AVCaptureDevice.authorizationStatus(for: .video)
if status == .notDetermined {
    let semaphore = DispatchSemaphore(value: 0)
    let granted = Box(false)
    AVCaptureDevice.requestAccess(for: .video) { ok in
        granted.value = ok
        semaphore.signal()
    }
    semaphore.wait()
    status = granted.value ? .authorized : .denied
}
if status != .authorized {
    fail(3, "camera permission not granted")
}

guard let input = try? AVCaptureDeviceInput(device: device) else {
    fail(4, "cannot open the camera")
}

let session = AVCaptureSession()
session.sessionPreset = .photo
guard session.canAddInput(input) else { fail(4, "cannot add camera input") }
session.addInput(input)

let photoOutput = AVCapturePhotoOutput()
guard session.canAddOutput(photoOutput) else { fail(4, "cannot add photo output") }
session.addOutput(photoOutput)

final class CaptureDelegate: NSObject, AVCapturePhotoCaptureDelegate {
    let done: DispatchSemaphore
    var imageData: Data?
    var failure: String?
    init(_ done: DispatchSemaphore) { self.done = done }
    func photoOutput(_ output: AVCapturePhotoOutput,
                     didFinishProcessingPhoto photo: AVCapturePhoto,
                     error: Error?) {
        if let error = error {
            failure = error.localizedDescription
        } else if let cg = photo.cgImageRepresentation() {
            let data = NSMutableData()
            if let dest = CGImageDestinationCreateWithData(
                data, UTType.jpeg.identifier as CFString, 1, nil) {
                CGImageDestinationAddImage(dest, cg, [
                    kCGImageDestinationLossyCompressionQuality: 0.8
                ] as CFDictionary)
                if CGImageDestinationFinalize(dest) {
                    imageData = data as Data
                } else {
                    failure = "could not encode JPEG"
                }
            } else {
                failure = "could not create JPEG encoder"
            }
        } else {
            failure = "camera returned no image"
        }
        done.signal()
    }
}

let done = DispatchSemaphore(value: 0)
let delegate = CaptureDelegate(done)

session.startRunning()
Thread.sleep(forTimeInterval: 0.7)
photoOutput.capturePhoto(with: AVCapturePhotoSettings(), delegate: delegate)

var timedOut = false
if done.wait(timeout: .now() + 8.0) == .timedOut {
    timedOut = true
}
session.stopRunning()

if timedOut {
    fail(4, "capture timed out")
}
guard let data = delegate.imageData, !data.isEmpty else {
    fail(4, delegate.failure ?? "capture produced no image")
}

do {
    try data.write(to: URL(fileURLWithPath: outputPath))
} catch {
    fail(4, "could not write \(outputPath): \(error.localizedDescription)")
}
print(outputPath)
exit(0)
'''

_EXIT_OK = 0
_EXIT_NO_CAMERA = 2
_EXIT_NO_PERMISSION = 3
_EXIT_CAPTURE_FAILED = 4


class CameraError(RuntimeError):
    """Anything that stops us seeing through the camera, with a fixable message."""


def _camera_binary() -> Path:
    """Compile the camera helper once, then reuse it (same as the OCR helper)."""
    cache = screen_tool._data_dir() / "tools"
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / "jarvis_camera"
    source = cache / "jarvis_camera.swift"
    if binary.exists() and source.exists() and source.read_text() == _CAPTURE_SRC:
        return binary
    if not shutil.which("swiftc"):
        raise CameraError("swiftc not found — the camera needs the Xcode command line tools")
    source.write_text(_CAPTURE_SRC)
    proc = subprocess.run(
        ["swiftc", "-O", "-o", str(binary), str(source)],
        capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise CameraError(f"could not build the camera helper: {proc.stderr.strip()[:300]}")
    return binary


def _message(returncode: int, stderr: str) -> str:
    detail = (stderr or "").strip()
    if returncode == _EXIT_NO_CAMERA:
        return "no camera found on this Mac"
    if returncode == _EXIT_NO_PERMISSION:
        return ("camera access was not granted — turn it on in System Settings → "
                "Privacy & Security → Camera")
    if returncode == _EXIT_CAPTURE_FAILED:
        return f"the camera could not take a frame{': ' + detail if detail else ''}"
    return f"the camera helper failed{': ' + detail if detail else ''}"


def available() -> bool:
    """Is a camera present and is the helper buildable? Never raises."""
    try:
        binary = _camera_binary()
    except Exception:  # noqa: BLE001 - availability is a yes/no question
        return False
    try:
        proc = subprocess.run([str(binary), "--check"], capture_output=True,
                              text=True, timeout=15)
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == _EXIT_OK


def capture(path: str | None = None) -> Path:
    """Take one frame and write it as a JPEG. Returns the JPEG path."""
    out = Path(path) if path else Path(tempfile.gettempdir()) / "jarvis-camera.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    binary = _camera_binary()
    try:
        proc = subprocess.run([str(binary), str(out)], capture_output=True,
                              text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise CameraError("the camera did not answer in time") from exc
    if proc.returncode != _EXIT_OK or not out.exists() or not out.stat().st_size:
        raise CameraError(_message(proc.returncode, proc.stderr))
    return out


def look(question: str = "") -> str:
    """Look through the camera and read it — OCR always, vision model when asked.

    Reuses ``screen_tool`` entirely: ``ocr`` for the on-device text and
    ``screen_tool.look`` for the vision-model answer. The report always carries
    the frame's path as ``[camera: ...]``.
    """
    try:
        shot = capture()
    except CameraError as exc:
        return f"[camera] could not look: {exc}"
    text = ""
    try:
        text = screen_tool.ocr(shot)
    except Exception as exc:  # noqa: BLE001 - degrade to the vision path
        text = f"(ocr unavailable: {exc})"
    if question and screen_tool.vision_ready():
        try:
            answer = screen_tool.look(shot, question)
            return f"{answer}\n\n[camera: {shot}]"
        except Exception as exc:  # noqa: BLE001 - fall back to OCR text
            text = f"{text}\n(vision model failed: {exc})"
    if not text:
        return f"[camera] no text found in the frame (image: {shot})"
    return f"{text[:4000]}\n\n[camera: {shot}]"
