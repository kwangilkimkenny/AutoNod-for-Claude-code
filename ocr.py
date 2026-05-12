"""macOS Vision-based OCR. Returns recognized text lines from a PIL Image.

The returned list is ordered top-to-bottom (visual order). Vision returns
observations in detection order, which is *not* guaranteed to match the
visual layout, so we sort by bounding-box position. Fragments that share
a row (similar y) are concatenated left-to-right so a line that Vision
split into multiple observations (e.g. "1. Yes" + "(recommended)") reads
back as a single line.
"""

from __future__ import annotations

import io

from Foundation import NSData
from PIL import Image
from Quartz import (  # type: ignore
    CGImageSourceCreateImageAtIndex,
    CGImageSourceCreateWithData,
)
from Vision import (  # type: ignore
    VNImageRequestHandler,
    VNRecognizeTextRequest,
    VNRequestTextRecognitionLevelAccurate,
    VNRequestTextRecognitionLevelFast,
)


def _pil_to_cgimage(img: Image.Image):
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    nsdata = NSData.dataWithBytes_length_(buf.getvalue(), len(buf.getvalue()))
    src = CGImageSourceCreateWithData(nsdata, None)
    if src is None:
        raise RuntimeError("CGImageSourceCreateWithData failed")
    cg = CGImageSourceCreateImageAtIndex(src, 0, None)
    if cg is None:
        raise RuntimeError("CGImageSourceCreateImageAtIndex failed")
    return cg


def _bbox(obs):
    """Return (y_top, y_bottom, x_left, height) in Vision-normalized coords.
    Vision uses bottom-left origin with values in [0, 1]; larger y = higher
    on screen.
    """
    bb = obs.boundingBox()
    # NSRect / CGRect — accessible as .origin / .size on pyobjc.
    y0 = float(bb.origin.y)
    h = float(bb.size.height)
    x0 = float(bb.origin.x)
    return y0 + h, y0, x0, h


def _cluster_rows(items, row_tol_factor: float = 0.6):
    """Cluster (y_top, y_bottom, x_left, text, height) into rows.

    Two observations are the same row if their vertical centers are closer
    than row_tol_factor * average height. Within a row, sort by x_left and
    join with a single space.
    """
    if not items:
        return []
    # Sort by y_top descending (visual top → bottom).
    items = sorted(items, key=lambda it: -it[0])
    rows: list[list[tuple]] = [[items[0]]]
    avg_h = items[0][4] or 0.02
    for it in items[1:]:
        prev_row = rows[-1]
        prev_center = sum((p[0] + p[1]) / 2 for p in prev_row) / len(prev_row)
        cur_center = (it[0] + it[1]) / 2
        tol = row_tol_factor * max(avg_h, it[4] or 0.02)
        if abs(prev_center - cur_center) <= tol:
            prev_row.append(it)
        else:
            rows.append([it])
        # Running average of row heights, biased to recent.
        avg_h = (avg_h * 0.7) + ((it[4] or avg_h) * 0.3)
    out: list[str] = []
    for row in rows:
        row.sort(key=lambda it: it[2])  # x_left
        out.append(" ".join(it[3] for it in row).rstrip())
    return out


def recognize_lines(img: Image.Image,
                    accurate: bool = True,
                    languages: list[str] | None = None) -> list[str]:
    """Run Apple Vision OCR. Returns top candidate string per detected line,
    sorted top-to-bottom and with fragments on the same row merged.
    """
    cg = _pil_to_cgimage(img)
    handler = VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)

    request = VNRecognizeTextRequest.alloc().init()
    level = (VNRequestTextRecognitionLevelAccurate if accurate
             else VNRequestTextRecognitionLevelFast)
    request.setRecognitionLevel_(level)
    request.setUsesLanguageCorrection_(False)
    if languages:
        request.setRecognitionLanguages_(languages)

    success, error = handler.performRequests_error_([request], None)
    if not success:
        raise RuntimeError(f"Vision OCR failed: {error}")

    items: list[tuple] = []
    for obs in (request.results() or []):
        cands = obs.topCandidates_(1)
        if not cands or len(cands) == 0:
            continue
        text = str(cands[0].string())
        try:
            y_top, y_bot, x_left, h = _bbox(obs)
        except Exception:
            # If bbox is unavailable for some reason, fall back to detection
            # order by parking the obs near the top.
            y_top, y_bot, x_left, h = 1.0, 1.0, 0.0, 0.02
        items.append((y_top, y_bot, x_left, text, h))

    return _cluster_rows(items)


if __name__ == "__main__":
    import sys
    import time
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/shot.png"
    t0 = time.time()
    lines = recognize_lines(Image.open(path))
    print(f"OCR took {time.time() - t0:.2f}s — {len(lines)} lines")
    for ln in lines:
        print(f"  | {ln}")
