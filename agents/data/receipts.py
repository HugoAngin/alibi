"""Synthetic receipt and confirmation images for testing self-supplied imports (vision).

    uv run --frozen python -m agents.data.receipts

Writes PNGs to agents/data/out/receipts/ and the true fields of each image to
agents/data/out/receipts/receipts.json. Every image is labelled SYNTHETIC on the image itself.
Where an image shows a record that exists in a development case, `links` names it, so an
import can be checked against the case's records (amounts, dates, references).

Rendering uses Pillow's bundled font, so it works without system fonts; the handwritten image
uses a handwriting font if the machine has one (macOS: Bradley Hand) and falls back otherwise.
Pixels may differ slightly between machines; the true fields never do.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent / "out" / "receipts"
STAMP = "SYNTHETIC TEST DOCUMENT - NOT A REAL RECEIPT"
HAND_FONTS = ["/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf",
              "/System/Library/Fonts/Supplemental/Chalkboard.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"]


TEXT_FONTS = ["/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf",
              "/System/Library/Fonts/Helvetica.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
_TEXT_FONT = next((p for p in TEXT_FONTS if Path(p).exists()), None)


def pounds(minor: int) -> str:
    # Pillow's bundled font has no pound sign; without a system font, write GBP instead.
    sign = "£" if _TEXT_FONT else "GBP "
    return f"{sign}{minor // 100:,}.{minor % 100:02d}"


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_TEXT_FONT, size) if _TEXT_FONT else ImageFont.load_default(size=size)


def hand_font(size: int):
    for path in HAND_FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


# --------------------------------------------------------------------------
# document specs: what each image shows, and the true fields behind it
# --------------------------------------------------------------------------

CASE1_SALES = [  # (record_id, order shown, date, amount, item)
    ("rec-0001-001", "MM-48001", "2026-10-05", 30000, "Vintage denim jacket"),
    ("rec-0001-003", "MM-48017", "2026-10-06", 25000, "Wool coat, size 10"),
    ("rec-0001-006", "MM-48042", "2026-10-07", 40000, "Leather boots and two scarves"),
    ("rec-0001-013", "MM-48107", "2026-10-11", 45000, "Bundle: 5 summer dresses"),
]


def specs() -> list[dict]:
    out = []
    for i, (rec, order, date, amount, item) in enumerate(CASE1_SALES, 1):
        fee = (amount * 25 + 500) // 1000
        out.append({
            "id": f"rcpt-{i:02d}", "kind": "order_confirmation", "layout": "web_screenshot",
            "distortion": "none", "issuer": "Mock Marketplace", "document_date": date,
            "reference": order, "currency": "GBP", "total_minor": amount,
            "items": [{"description": item, "amount_minor": amount}],
            "extra_fields": {"seller_fee_minor": fee, "seller": "maya-001", "status": "Completed"},
            "links": {"case_number": 1, "record_id": rec},
            "notes": "Sale confirmation as the seller sees it. The order number is the platform's "
                     "display reference; the case record ID is in links."})
    out.append({
        "id": "rcpt-05", "kind": "refund_confirmation", "layout": "web_screenshot", "distortion": "none",
        "issuer": "Mock Marketplace", "document_date": "2026-10-07", "reference": "MM-48017-R1",
        "currency": "GBP", "total_minor": 10000,
        "items": [{"description": "Partial refund: Wool coat, size 10 (button missing)", "amount_minor": 10000}],
        "extra_fields": {"original_order": "MM-48017"},
        "links": {"case_number": 1, "record_id": "rec-0001-005"},
        "notes": "Refund against the sale rec-0001-003."})
    out.append({
        "id": "rcpt-06", "kind": "payout_confirmation", "layout": "app_screenshot", "distortion": "none",
        "issuer": "Mock Marketplace", "document_date": "2026-10-27", "reference": "PO-0001-01",
        "currency": "GBP", "total_minor": 216000,
        "items": [{"description": "Sales", "amount_minor": 240000},
                  {"description": "Refunds", "amount_minor": -18000},
                  {"description": "Fees", "amount_minor": -6000}],
        "extra_fields": {"status": "Paid to bank", "settled_at": "2026-10-27T16:00:00Z",
                         "bank_account": "ending 00"},
        "links": {"case_number": 1, "record_id": "rec-0001-021"},
        "notes": "Holder's screenshot of the payout that settles case 1's credit. As an import it is "
                 "self_supplied; the issuer-verified record is only available by lookup."})
    out.append({
        "id": "rcpt-07", "kind": "payout_confirmation", "layout": "app_screenshot", "distortion": "none",
        "issuer": "Mock Marketplace", "document_date": "2026-10-26", "reference": "PO-0002-01",
        "currency": "GBP", "total_minor": 216000,
        "items": [{"description": "Withdrawal to bank", "amount_minor": 216000}],
        "extra_fields": {"status": "Processing", "settled_at": None, "bank_account": "ending 00"},
        "links": {"case_number": 2, "record_id": "rec-0002-021"},
        "notes": "Case 2's payout while still processing. It does not show money reaching the bank."})
    out.append({
        "id": "rcpt-08", "kind": "till_receipt", "layout": "thermal_print", "distortion": "faded",
        "issuer": "Synthetic Opticians", "document_date": "2026-10-14", "reference": "TILL 0412-7781",
        "currency": "GBP", "total_minor": 4500,
        "items": [{"description": "Prescription eyewear", "amount_minor": 4500}],
        "extra_fields": {"payment": "Card ending 00"},
        "links": {"case_number": 1, "record_id": "rec-0001-019"},
        "notes": "SENSITIVE: health-category purchase. It is a forbidden item in case 1 and must "
                 "never be disclosed, even if imported."})
    out.append({
        "id": "rcpt-09", "kind": "handwritten_receipt", "layout": "handwritten", "distortion": "lined_paper",
        "issuer": "Private buyer (handwritten)", "document_date": "2026-10-03", "reference": None,
        "currency": "GBP", "total_minor": 4500,
        "items": [{"description": "Denim jacket (private sale)", "amount_minor": 4500}],
        "extra_fields": {"payer": "J. Smith", "payment": "cash"},
        "links": None,
        "notes": "A cash private sale with no platform record. It supports nothing in any case and "
                 "must stay self_supplied and unlinked."})
    out.append({
        "id": "rcpt-10", "kind": "payout_confirmation", "layout": "printed_letter", "distortion": "photo_at_angle",
        "issuer": "Mock Marketplace", "document_date": "2026-10-27", "reference": "PO-0001-01",
        "currency": "GBP", "total_minor": 216000,
        "items": [{"description": "Withdrawal PO-0001-01", "amount_minor": 216000}],
        "extra_fields": {"status": "Sent to your bank", "bank_account": "ending 00"},
        "links": {"case_number": 1, "record_id": "rec-0001-021"},
        "notes": "The same payout as rcpt-06, printed and photographed on a desk at an angle."})
    out.append({
        "id": "rcpt-11", "kind": "order_confirmation", "layout": "email", "distortion": "none",
        "issuer": "Second Platform", "document_date": None, "reference": None, "currency": "GBP",
        "total_minor": None, "items": [], "extra_fields": {}, "links": None,
        "notes": "Filled in from case 71 by agents/data/receipts.py once the second platform exists."})
    out.append({
        "id": "rcpt-12", "kind": "weekly_statement", "layout": "pdf_like", "distortion": "none",
        "issuer": "Second Platform", "document_date": None, "reference": None, "currency": "GBP",
        "total_minor": None, "items": [], "extra_fields": {}, "links": None,
        "notes": "Filled in from case 71 by agents/data/receipts.py once the second platform exists."})
    return out


# --------------------------------------------------------------------------
# renderers
# --------------------------------------------------------------------------

def stamp(draw: ImageDraw.ImageDraw, w: int, h: int) -> None:
    draw.text((12, h - 22), STAMP, font=font(13), fill=(170, 30, 30))


def lines_for(spec: dict) -> list[tuple[str, int, bool]]:
    """(text, size, bold-ish) lines shared by the typed layouts."""
    rows = [(spec["issuer"], 30, True)]
    title = {"order_confirmation": "Order confirmation", "refund_confirmation": "Refund confirmation",
             "payout_confirmation": "Payout details", "till_receipt": "Receipt",
             "weekly_statement": "Weekly earnings statement"}.get(spec["kind"], spec["kind"])
    rows.append((title, 22, True))
    if spec["reference"]:
        rows.append((f"Reference: {spec['reference']}", 18, False))
    if spec["document_date"]:
        rows.append((f"Date: {spec['document_date']}", 18, False))
    for k, v in spec["extra_fields"].items():
        if v is not None and k != "seller_fee_minor":
            rows.append((f"{k.replace('_', ' ').capitalize()}: {v}", 18, False))
    rows.append(("", 10, False))
    for it in spec["items"]:
        amt = it["amount_minor"]
        rows.append((f"{it['description']}  {'-' if amt < 0 else ''}{pounds(abs(amt))}", 18, False))
    if "seller_fee_minor" in spec["extra_fields"]:
        rows.append((f"Seller fee (2.5%)  -{pounds(spec['extra_fields']['seller_fee_minor'])}", 18, False))
    if spec["total_minor"] is not None:
        rows.append(("", 10, False))
        label = "Order total" if spec["kind"] == "order_confirmation" else "Total"
        rows.append((f"{label}  {pounds(spec['total_minor'])}", 24, True))
    return rows


def typed(spec: dict, width: int = 820, bg=(255, 255, 255), header=None) -> Image.Image:
    rows = lines_for(spec)
    h = 90 + sum(s + 14 for _, s, _ in rows) + 40
    img = Image.new("RGB", (width, h), bg)
    d = ImageDraw.Draw(img)
    y = 24
    if header:
        d.rectangle([0, 0, width, 56], fill=header)
        d.text((24, 14), spec["issuer"], font=font(26), fill=(255, 255, 255))
        y = 76
        rows = rows[1:]
    for text, size, bold in rows:
        f = font(size)
        d.text((32, y), text, font=f, fill=(20, 20, 20))
        if bold:
            d.text((33, y), text, font=f, fill=(20, 20, 20))
        y += size + 14
    stamp(d, width, h)
    return img


def thermal(spec: dict, rng: random.Random) -> Image.Image:
    rows = lines_for(spec)
    w = 460
    h = 80 + sum(s + 12 for _, s, _ in rows) + 60
    img = Image.new("RGB", (w, h), (250, 248, 240))
    d = ImageDraw.Draw(img)
    y = 20
    for text, size, _ in rows:
        d.text((20, y), text[:40], font=font(min(size, 20)), fill=(60, 60, 60))
        y += min(size, 20) + 12
        if size >= 22:
            d.text((20, y - 6), "-" * 46, font=font(14), fill=(120, 120, 120))
    # faded thermal print: lighten random horizontal bands
    px = img.load()
    for _ in range(4):
        y0 = rng.randrange(0, h - 20)
        lift = rng.randint(25, 45)
        for yy in range(y0, min(h, y0 + rng.randint(2, 6))):
            for xx in range(w):
                r, g, b = px[xx, yy]
                px[xx, yy] = (min(255, r + lift), min(255, g + lift), min(255, b + lift))
    stamp(d, w, h)
    return img


def handwritten(spec: dict, rng: random.Random) -> Image.Image:
    w, h = 820, 560
    img = Image.new("RGB", (w, h), (252, 250, 236))
    d = ImageDraw.Draw(img)
    for y in range(80, h - 40, 44):
        d.line([(0, y), (w, y)], fill=(170, 200, 230), width=1)
    d.line([(70, 0), (70, h)], fill=(230, 160, 160), width=2)
    text = ["Receipt", f"3rd Oct 2026", f"Received {pounds(spec['total_minor'])} cash",
            "from J. Smith for 1 denim jacket", "(private sale)", "Signed:  M."]
    y = 44
    for line in text:
        x = 90
        for word in line.split(" "):
            f = hand_font(rng.randint(30, 34))
            layer = Image.new("RGBA", (int(f.getlength(word)) + 20, 60), (0, 0, 0, 0))
            ImageDraw.Draw(layer).text((5, 5), word, font=f, fill=(25, 45, 140, 255))
            layer = layer.rotate(rng.uniform(-4, 4), expand=True, resample=Image.BICUBIC)
            img.paste(layer, (x, y + rng.randint(-3, 3)), layer)
            x += layer.width - 6
        y += 44 * 2 if line == "Receipt" else 44 * 1 + 44 * 0
    stamp(ImageDraw.Draw(img), w, h)
    return img


def photographed(doc: Image.Image, rng: random.Random) -> Image.Image:
    """Place the document on a desk, rotated and in perspective, with a shadow and soft focus."""
    W, H = int(doc.width * 1.45), int(doc.height * 1.6)
    desk = Image.new("RGB", (W, H), (96, 74, 56))
    dd = ImageDraw.Draw(desk)
    for _ in range(40):  # wood grain
        y = rng.randrange(H)
        dd.line([(0, y), (W, y + rng.randint(-20, 20))], fill=(86, 66, 50), width=rng.randint(1, 3))
    w, h = doc.size
    # perspective: top edge narrower than bottom
    inset = int(w * 0.09)
    quad_src = [(0, 0), (w, 0), (w, h), (0, h)]
    quad_dst = [(inset, 0), (w - inset // 2, int(h * 0.03)), (w, h), (0, int(h * 0.97))]
    coeffs = _perspective_coeffs(quad_dst, quad_src)
    warped = doc.convert("RGBA").transform((w, h), Image.PERSPECTIVE, coeffs, Image.BICUBIC)
    warped = warped.rotate(rng.uniform(6, 10), expand=True, resample=Image.BICUBIC)
    shadow = Image.new("RGBA", warped.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 110), mask=warped.split()[3])
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    ox, oy = (W - warped.width) // 2, (H - warped.height) // 2
    desk.paste(shadow, (ox + 18, oy + 22), shadow)
    desk.paste(warped, (ox, oy), warped)
    # uneven light and slight blur, like a phone photo
    light = Image.new("L", (W, H), 0)
    ImageDraw.Draw(light).ellipse([-W // 3, -H // 3, W, H], fill=60)
    light = light.filter(ImageFilter.GaussianBlur(120))
    desk = Image.composite(Image.new("RGB", (W, H), (255, 255, 240)), desk, light)
    return desk.filter(ImageFilter.GaussianBlur(0.8))


def _perspective_coeffs(src, dst):
    """Coefficients mapping output (src) points back to input (dst) points for Image.transform."""
    import numpy as np
    rows = []
    for (x, y), (u, v) in zip(src, dst):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    a = np.array(rows, dtype=float)
    b = np.array([c for p in dst for c in p], dtype=float)
    return list(np.linalg.solve(a, b))


def render(spec: dict, rng: random.Random) -> Image.Image:
    layout = spec["layout"]
    if layout == "web_screenshot":
        return typed(spec, header=(31, 78, 140))
    if layout == "app_screenshot":
        return typed(spec, width=560, header=(40, 40, 48))
    if layout == "thermal_print":
        return thermal(spec, rng)
    if layout == "handwritten":
        return handwritten(spec, rng)
    if layout == "printed_letter":
        return photographed(typed(spec, bg=(246, 246, 242)), rng)
    if layout == "email":
        return typed(spec, header=(90, 90, 90))
    return typed(spec, bg=(250, 250, 250), header=(20, 110, 90))


def build(all_specs: list[dict], seed: int = 20261108) -> list[dict]:
    OUT.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    manifest = []
    for spec in all_specs:
        if spec["total_minor"] is None:
            continue
        name = f"{spec['id']}_{spec['kind']}.png"
        render(spec, random.Random(rng.random())).save(OUT / name, optimize=True)
        manifest.append({"file": name, **spec})
    (OUT / "receipts.json").write_text(json.dumps(
        {"label": "SYNTHETIC - labelled test images, not real receipts", "count": len(manifest),
         "currency_note": "amounts are integer pence; negative item amounts are deductions",
         "receipts": manifest}, indent=2, ensure_ascii=False) + "\n")
    return manifest


def main() -> None:
    base = specs()
    try:
        from agents.data.ledgers import second_platform_receipt_specs
        extra = {s["id"]: s for s in second_platform_receipt_specs()}
    except ImportError:          # second platform not built yet
        extra = {}
    full = [extra.get(s["id"], s) for s in base]
    m = build(full)
    print(json.dumps({"receipts": len(m), "dir": str(OUT.relative_to(OUT.parents[3]))}))


if __name__ == "__main__":
    main()
