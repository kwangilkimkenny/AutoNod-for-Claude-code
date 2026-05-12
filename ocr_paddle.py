"""PaddleOCR wrapper. Cached singleton — init is expensive (~3s)."""

from __future__ import annotations

import os
import warnings

from PIL import Image

# Suppress noisy logs from PaddlePaddle / PaddleX
warnings.filterwarnings("ignore")
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("GLOG_minloglevel", "3")

_engine = None


def _get_engine(lang: str = "en"):
    global _engine
    if _engine is None:
        from paddleocr import PaddleOCR
        _engine = PaddleOCR(
            lang=lang,
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name=(
                "en_PP-OCRv5_mobile_rec" if lang == "en"
                else None  # let PaddleOCR pick by lang
            ),
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    return _engine


def recognize_lines(img: Image.Image, lang: str = "en") -> list[str]:
    """Run PaddleOCR. Returns recognized text lines."""
    import numpy as np

    if img.mode != "RGB":
        img = img.convert("RGB")
    arr = np.asarray(img)

    engine = _get_engine(lang)
    res = engine.predict(arr)
    if not res:
        return []
    page = res[0]
    return list(page.get("rec_texts", []))


if __name__ == "__main__":
    import sys
    import time
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/shot.png"
    img = Image.open(path)
    # Warm
    recognize_lines(img)
    t0 = time.time()
    lines = recognize_lines(img)
    print(f"PaddleOCR: {time.time() - t0:.2f}s — {len(lines)} lines")
    for ln in lines:
        print(f"  | {ln}")
