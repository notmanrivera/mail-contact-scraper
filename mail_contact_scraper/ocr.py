"""OCR for signatures that are pasted as images (logo + phone numbers in a PNG/JPG).

Needs the Tesseract binary plus the `pytesseract` and `Pillow` packages. Without
them, image signatures are skipped and a warning is logged once.
"""
import base64
import hashlib
import io
import logging
import re
import shutil
import urllib.request

log = logging.getLogger(__name__)

MAX_DOWNLOAD = 2 * 1024 * 1024
MIN_WIDTH, MIN_HEIGHT = 80, 20          # smaller = social icons / tracking pixels
MAX_PIXELS = 3000 * 1500                 # bigger = photos, flyers, scanned docs


class SignatureOCR:
    def __init__(self, lang="spa+eng", allow_remote=False):
        self.lang = lang
        self.allow_remote = allow_remote
        self.cache = {}
        self.enabled = self._check()

    def _check(self):
        try:
            import PIL  # noqa: F401
            import pytesseract  # noqa: F401
        except ImportError:
            log.warning("OCR disabled: pip install pytesseract pillow")
            return False
        if not shutil.which("tesseract"):
            log.warning("OCR disabled: Tesseract is not installed (see README)")
            return False
        return True

    def load(self, src, images):
        """Return image bytes for an <img src>, or None."""
        if src.lower().startswith("cid:"):
            return images.get(src[4:].strip("<>").lower())
        if src.lower().startswith("data:image"):
            try:
                return base64.b64decode(src.split(",", 1)[1])
            except Exception:
                return None
        if src.lower().startswith(("http://", "https://")) and self.allow_remote:
            try:
                req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.read(MAX_DOWNLOAD + 1)[:MAX_DOWNLOAD]
            except Exception as exc:
                log.debug("Could not download %s: %s", src, exc)
                return None
        # Outlook sometimes references parts by bare file name.
        return images.get(src.rsplit("/", 1)[-1].lower())

    def read(self, data):
        if not self.enabled or not data:
            return ""
        key = hashlib.sha1(data).hexdigest()
        if key not in self.cache:
            self.cache[key] = self._ocr(data)
        return self.cache[key]

    def _ocr(self, data):
        from PIL import Image, ImageOps
        import pytesseract

        try:
            img = Image.open(io.BytesIO(data))
            img.seek(0)
        except Exception:
            return ""
        w, h = img.size
        if w < MIN_WIDTH or h < MIN_HEIGHT or w * h > MAX_PIXELS:
            return ""
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            background = Image.new("RGBA", img.size, "white")
            img = Image.alpha_composite(background, img)
        img = ImageOps.grayscale(img)
        if h < 300:  # signatures are small; tesseract likes ~30px tall letters
            scale = max(2, 300 // max(h, 1))
            img = img.resize((w * min(scale, 4), h * min(scale, 4)), Image.LANCZOS)
        if img.resize((1, 1)).getpixel((0, 0)) < 110:  # light text on dark background
            img = ImageOps.invert(img)
        try:
            text = pytesseract.image_to_string(img, lang=self.lang, config="--psm 6")
        except Exception as exc:
            log.debug("OCR failed: %s", exc)
            return ""
        lines = [re.sub(r"\s+", " ", l).strip(" |·•") for l in text.splitlines()]
        return "\n".join(l for l in lines if len(re.sub(r"\W", "", l)) >= 3)
