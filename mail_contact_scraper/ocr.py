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
    def __init__(self, lang="spa+eng", allow_remote=False, vision=None):
        self.lang = lang
        self.allow_remote = allow_remote
        self.vision = vision  # optional ClaudeVision for signatures Tesseract can't read
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
        if not data or not (self.enabled or self.vision):
            return ""
        key = hashlib.sha1(data).hexdigest()
        if key not in self.cache:
            if self.enabled:
                text, quality = self._ocr(data)
            else:  # no Tesseract: Claude reads every image that could be a signature
                text, quality = "", (0.0 if _open(data) else None)
            if self.vision and quality is not None and self.vision.wanted(quality):
                text = self.vision.read(key, data) or text
            self.cache[key] = text
        return self.cache[key]

    def _ocr(self, data):
        """Return (text, quality): quality is the share of the image's text Tesseract trusts."""
        from PIL import Image, ImageOps
        import pytesseract

        img = _open(data)
        if not img:
            return "", None
        w, h = img.size
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            background = Image.new("RGBA", img.size, "white")
            img = Image.alpha_composite(background, img)
        img = ImageOps.grayscale(img)
        if h < 300:  # signatures are small; tesseract likes ~30px tall letters
            scale = min(4, max(2, 300 // max(h, 1)))
            img = img.resize((w * scale, h * scale), Image.LANCZOS)
        if img.resize((1, 1)).getpixel((0, 0)) < 110:  # light text on dark background
            img = ImageOps.invert(img)
        kept, total = [], 0
        # Sparse mode finds text scattered around logos and photos; block mode keeps whole
        # lines such as an address or an email that sparse mode splits.
        for psm in (11, 6):
            try:
                data_ = pytesseract.image_to_data(img, lang=self.lang, config=f"--psm {psm}",
                                                  output_type=pytesseract.Output.DICT)
            except Exception as exc:
                log.debug("OCR failed: %s", exc)
                return "", None
            lines, chars = _lines(data_)
            total = max(total, chars)
            for top, line in lines:
                _merge_line(kept, top, line)
        text = "\n".join(line for _, line in sorted(kept))
        good = len(re.sub(r"\W", "", text))
        return text, (good / total if total else None)


def _open(data):
    """The image, or None if it can't be a signature (icon, tracking pixel, photo)."""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.seek(0)
    except Exception:
        return None
    w, h = img.size
    if w < MIN_WIDTH or h < MIN_HEIGHT or w * h > MAX_PIXELS:
        return None
    return img


MIN_CONF = 60
MIN_CONF_DIGITS = 10    # phone numbers often come out with low confidence but right digits;
                        # find_phones() still has to validate them
JUNK_RE = re.compile(r"[<>»«”“\"|_=~^\\{}\[\]¡¿©®]")


def _lines(data):
    """Words Tesseract is sure about, grouped into (top, line) pairs."""
    groups, chars = {}, 0
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        if not word:
            continue
        chars += len(re.sub(r"\W", "", word))
        conf = float(data["conf"][i])
        if sum(c.isdigit() for c in word) >= 3:
            if conf < MIN_CONF_DIGITS:
                continue
            word = re.sub(r"[^\d()+\-./ ]", "", word)
            word = re.sub(r"^(\d{2,3}\))", r"(\1", word)  # "|442)": the "(" was read as "|"
        elif conf < MIN_CONF:
            continue
        word = word.strip("<>»«”“\"'|_=~^`*:;,") if "@" not in word else word.lstrip("@©«<(").rstrip(">)»,;")
        if not re.search(r"\w", word):
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        groups.setdefault(key, []).append((data["top"][i], word))
    lines = []
    for words in groups.values():
        tokens = [w for _, w in words]
        # A lone character at either end is almost always a piece of an icon.
        while tokens and len(tokens[0]) == 1:
            tokens.pop(0)
        while tokens and len(tokens[-1]) == 1:
            tokens.pop()
        line = " ".join(tokens)
        if len(re.sub(r"\W", "", line)) >= 3 and (re.search(r"\w{3}", line) or len(re.findall(r"\d", line)) >= 7):
            lines.append((min(t for t, _ in words), line))
    return lines, chars


def _norm(line):
    return re.sub(r"\W", "", line).lower()


def _digits(line):
    digits = re.sub(r"\D", "", line)
    return digits[-7:] if len(digits) >= 7 else ""


def _merge_line(kept, top, line):
    """Add a line from another OCR pass unless it repeats one already kept."""
    norm, digits = _norm(line), _digits(line)
    for i, (_, other) in enumerate(kept):
        other_norm = _norm(other)
        if digits and digits == _digits(other):
            return
        if norm in other_norm:
            return
        if other_norm in norm:
            extra = line
            for word in other.split():
                extra = extra.replace(word, " ", 1)
            if re.search(r"\w{3}", extra):  # the longer line adds a real word: keep that one
                kept[i] = (kept[i][0], line)
            return
    kept.append((top, line))
