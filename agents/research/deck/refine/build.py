"""Refine the Canva-exported Alibi deck: slide 4 becomes the architecture diagram, slides 5+
are rebuilt as native shapes in the landing page's branding, 12 slides become 10.

usage: python build.py src.pptx assets_dir out.pptx
"""
import copy
import sys

from lxml import etree
from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

SRC, ASSETS, OUT = sys.argv[1:4]

# ---------------------------------------------------------------- brand tokens (landing site.css)
PAPER, PAPER2, CARD, CARD2 = 'F7F4EE', 'EFE9DF', 'FFFFFF', 'FDFBF7'
INK, SOFT, FAINT, RULE = '1F1B16', '5E554A', '7B7266', 'E4DED3'
ACC, ACCS, OK, BAD, BADS = '437C90', 'E3EEF1', '1F7A4A', 'B4322D', 'FBEAE8'
NIGHT, NINK, NSOFT, NFAINT = '17140F', 'EFE9DE', 'AAA398', '8A847A'
NCARD, NBORDER = '221F19', '2E2A24'
TITLE, BODY, SEMI, MONO = 'Arimo Bold', 'Inter', 'Inter Semi-Bold', 'IBM Plex Mono'


def rgb(h):
    return RGBColor.from_string(h)


# ---------------------------------------------------------------- primitives
def _no_shadow(shape):
    shape.shadow.inherit = False


def _shadow(shape, blur=0.22, dist=0.06, alpha=9):
    sp = shape._element.spPr
    for e in sp.findall(qn('a:effectLst')):
        sp.remove(e)
    eff = etree.SubElement(sp, qn('a:effectLst'))
    sh = etree.SubElement(eff, qn('a:outerShdw'), blurRad=str(int(Inches(blur))), dist=str(int(Inches(dist))),
                          dir='5400000', algn='t', rotWithShape='0')
    c = etree.SubElement(sh, qn('a:srgbClr'), val=INK)
    etree.SubElement(c, qn('a:alpha'), val=str(alpha * 1000))


def box(s, x, y, w, h, fill=None, line=None, lw=1.0, r=None, dash=False, shadow=False, name=None, shape=None):
    kind = shape or (MSO_SHAPE.ROUNDED_RECTANGLE if r else MSO_SHAPE.RECTANGLE)
    sp = s.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    if r and kind == MSO_SHAPE.ROUNDED_RECTANGLE:
        sp.adjustments[0] = min(0.5, r / min(w, h))
    if fill:
        sp.fill.solid()
        sp.fill.fore_color.rgb = rgb(fill)
    else:
        sp.fill.background()
    if line:
        sp.line.color.rgb = rgb(line)
        sp.line.width = Pt(lw)
        if dash:
            sp.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    else:
        sp.line.fill.background()
    _no_shadow(sp)
    if shadow:
        _shadow(sp, **(shadow if isinstance(shadow, dict) else {}))
    if name:
        sp.name = name
    return sp


def R(t, size, color=INK, font=BODY, strike=False, spc=None, italic=False):
    return dict(t=t, size=size, color=color, font=font, strike=strike, spc=spc, italic=italic)


def WM(size, color=INK):
    """The alibi wordmark as a text run: lowercase, Inter Semi-Bold, tight tracking (as on slide 1)."""
    return R('alibi', size, color, SEMI, spc=-0.06 * size)


def _fill_frame(tf, paras, align, anchor, space_after, line_spacing):
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    tf.auto_size = None
    for i, runs in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        if space_after:
            p.space_after = Pt(space_after)
        if line_spacing:
            p.line_spacing = line_spacing
        if isinstance(runs, dict):
            runs = [runs]
        for rd in runs:
            r = p.add_run()
            r.text = rd['t']
            f = r.font
            f.size = Pt(rd['size'])
            f.name = rd['font']
            f.color.rgb = rgb(rd['color'])
            f.bold = False
            f.italic = rd['italic']
            rpr = r._r.get_or_add_rPr()
            for tag in ('a:ea', 'a:cs'):
                e = etree.SubElement(rpr, qn(tag))
                e.set('typeface', rd['font'])
            if rd['strike']:
                rpr.set('strike', 'sngStrike')
            if rd['spc'] is not None:
                rpr.set('spc', str(int(rd['spc'] * 100)))


def text(s, x, y, w, h, paras, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, space_after=0, line_spacing=None, name=None):
    tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    if isinstance(paras, dict):
        paras = [[paras]]
    _fill_frame(tb.text_frame, paras, align, anchor, space_after, line_spacing)
    if name:
        tb.name = name
    return tb


def label_in(sp, paras, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE):
    """Text inside a shape (pills, buttons)."""
    if isinstance(paras, dict):
        paras = [[paras]]
    _fill_frame(sp.text_frame, paras, align, anchor, 0, None)
    sp.text_frame.word_wrap = False


def _arrow(ln, end='tail', size='med'):
    for e in ln.findall(qn(f'a:{end}End')):
        ln.remove(e)
    etree.SubElement(ln, qn(f'a:{end}End'), type='triangle', w=size, len=size)


def path(s, pts, color=INK, lw=1.5, dash=None, end_arrow=True, start_arrow=False, arrow_size='med', name=None):
    """Open polyline (editable freeform) with optional arrowheads."""
    if len(pts) == 2:
        (x1, y1), (x2, y2) = pts
        sp = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    else:
        fb = s.shapes.build_freeform(Inches(pts[0][0]), Inches(pts[0][1]), scale=1.0)
        fb.add_line_segments([(Inches(px), Inches(py)) for px, py in pts[1:]], close=False)
        sp = fb.convert_to_shape()
        sp.fill.background()
    sp.line.color.rgb = rgb(color)
    sp.line.width = Pt(lw)
    if dash == 'dash':
        sp.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    elif dash == 'dot':
        sp.line.dash_style = MSO_LINE_DASH_STYLE.ROUND_DOT
    ln = sp.line._get_or_add_ln()
    if end_arrow:
        _arrow(ln, 'tail', arrow_size)
    if start_arrow:
        _arrow(ln, 'head', arrow_size)
    _no_shadow(sp)
    if name:
        sp.name = name
    return sp


def rail(s, pts, name='Bank line'):
    """The landing page's encrypted bank line: a dark double rail."""
    outer = path(s, pts, INK, 5.0, arrow_size='sm', name=name)
    # stop the inner line short of the arrowhead
    (ax, ay), (bx, by) = pts[-2], pts[-1]
    d = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
    k = max(0.0, (d - 0.16) / d)
    inner = pts[:-1] + [(ax + (bx - ax) * k, ay + (by - ay) * k)]
    path(s, inner, PAPER, 1.6, end_arrow=False, name=name + ' (inner)')
    return outer


def image(s, name, x, y, w=None, h=None, box_w=None, box_h=None, align='center'):
    """Place an image; with box_w/box_h it is fitted (contained) and centred in that box."""
    f = f'{ASSETS}/{name}'
    iw, ih = Image.open(f).size
    if box_w:
        scale = min(box_w / iw, box_h / ih)
        w, h = iw * scale, ih * scale
        ox = (box_w - w) / 2 if align == 'center' else 0
        x, y = x + ox, y + (box_h - h) / 2
    elif w and not h:
        h = w * ih / iw
    elif h and not w:
        w = h * iw / ih
    return s.shapes.add_picture(f, Inches(x), Inches(y), Inches(w), Inches(h))


def clear(s):
    for sp in list(s.shapes):
        sp._element.getparent().remove(sp._element)


def title(s, runs, sub=None):
    text(s, 1.15, 0.85, 17.7, 0.85, [runs if isinstance(runs, list) else [runs]], name='Title')
    if sub:
        text(s, 1.15, 1.76, 17.7, 0.45, [sub if isinstance(sub, list) else [sub]], name='Subtitle')


def T(t, size=54):
    return R(t, size, INK, TITLE)


def SUB(t):
    return R(t, 22.5, SOFT, 'Arimo')


def pill(s, x, y, w, h, t, fill, color, size=12, font=MONO, line=None):
    sp = box(s, x, y, w, h, fill=fill, line=line, lw=0.75, r=h / 2)
    label_in(sp, R(t, size, color, font))
    return sp


def circle(s, x, y, d, fill, t, color, size=14, font=SEMI, line=None):
    sp = box(s, x, y, d, d, fill=fill, line=line, shape=MSO_SHAPE.OVAL)
    label_in(sp, R(t, size, color, font))
    return sp


# ---------------------------------------------------------------- open, reorder
prs = Presentation(SRC)
slides = list(prs.slides)
S = {i + 1: sl for i, sl in enumerate(slides)}

# delete 9 (money in/out, speculative) and 11 (limits, merged into results)
sldIdLst = prs.slides._sldIdLst
for n in (11, 9):
    sid = sldIdLst[n - 1]
    rid = sid.get(qn('r:id'))
    sldIdLst.remove(sid)
    prs.part.drop_rel(rid)

for n in (4, 5, 6, 7, 8, 10):
    clear(S[n])
    S[n].background.fill.solid()
    S[n].background.fill.fore_color.rgb = rgb(PAPER)

# ---------------------------------------------------------------- slide 3: wordmark, not "Alibi"
for sp in S[3].shapes:
    stack = [sp]
    while stack:
        cur = stack.pop()
        if cur.shape_type == 6:
            stack.extend(cur.shapes)
            continue
        if not cur.has_text_frame:
            continue
        for p in cur.text_frame.paragraphs:
            for r in p.runs:
                if 'Alibi' in r.text:
                    before, after = r.text.split('Alibi', 1)
                    r.text = before
                    new = copy.deepcopy(r._r)
                    new.find(qn('a:t')).text = 'alibi'
                    rpr = new.find(qn('a:rPr'))
                    for tag in ('a:latin', 'a:ea', 'a:cs', 'a:sym'):
                        e = rpr.find(qn(tag))
                        if e is not None:
                            e.set('typeface', SEMI)
                    rpr.attrib.pop('b', None)
                    sz = int(rpr.get('sz', '2250'))
                    rpr.set('spc', str(int(-0.06 * sz)))
                    tail = copy.deepcopy(r._r)
                    tail.find(qn('a:t')).text = after
                    r._r.addnext(new)
                    new.addnext(tail)

# ================================================================= slide 4: architecture
s = S[4]
title(s, [T('Gemini investigates. ', 50), R('Code decides.', 50, ACC, TITLE), T(' Maya approves.', 50)])

cols = dict(c1=(1.10, 2.70), c2=(4.35, 2.30), c3=(7.20, 5.60), c4=(13.35, 2.60), c5=(16.50, 2.40))
top, bot = 3.15, 10.55
for key, (num, word) in zip(cols, [('01', 'Connect'), ('02', 'Remember'), ('03', 'Investigate'), ('04', 'Approve'), ('05', 'Verify')]):
    x, _ = cols[key]
    text(s, x, 2.72, 2.4, 0.3, [[R(num + '  ', 13, ACC, MONO), R(word, 15, SOFT)]], name=f'Step {num}')

# 01 connect
box(s, 1.10, top, 2.70, 3.70, CARD, r=0.18, shadow=True, name='Merchants')
text(s, 1.35, 3.40, 2.2, 0.4, R('Merchants', 20, INK, SEMI))
for i, (logo, sq) in enumerate([('amazon.png', 0), ('deliveroo.png', 0), ('uniqlo.png', 1), ('ucl.png', 0)]):
    tx, ty = 1.35 + (i % 2) * 1.15, 3.98 + (i // 2) * 0.90
    box(s, tx, ty, 1.05, 0.80, CARD2, RULE, 0.75, r=0.1)
    image(s, logo, tx + 0.12, ty + 0.12, box_w=0.81, box_h=0.56 if sq else 0.34)
pill(s, 1.35, 6.22, 1.60, 0.38, 'signed records', PAPER2, SOFT)

box(s, 1.10, 7.05, 2.70, 3.50, CARD, r=0.18, shadow=True, name='Her banks')
text(s, 1.35, 7.30, 2.2, 0.4, R('Her banks', 20, INK, SEMI))
for i, logo in enumerate(['santander.png', 'natwest.png']):
    ty = 7.88 + i * 0.92
    box(s, 1.35, ty, 2.20, 0.82, CARD2, RULE, 0.75, r=0.1)
    image(s, logo, 1.55, ty + 0.2, box_w=1.8, box_h=0.42)
pill(s, 1.35, 9.92, 1.60, 0.38, 'read-only feed', PAPER2, SOFT)

# 02 passport
box(s, 4.35, top, 2.30, bot - top, ACC, r=0.2, shadow=True, name='Passport')
text(s, 4.60, 3.42, 1.9, 0.45, [[WM(24, '000000'), R('  passport', 15, '0B1A1F')]])
text(s, 4.60, 4.10, 1.9, 1.4, R('Living financial passport', 26, '000000', TITLE), line_spacing=0.95)
text(s, 4.60, 5.75, 1.85, 2.2, [[R('Owned by Maya', 15, '000000')], [R('Versioned: nothing is overwritten', 15, '000000')],
                                 [R('Encrypted at rest', 15, '000000')]], space_after=9)
text(s, 4.60, 8.52, 1.9, 0.3, R('Trust, per record', 11.5, '0B1A1F', MONO))
for i, t in enumerate(['issuer_verified', 'bank_observed', 'self_supplied']):
    b = box(s, 4.60, 8.85 + i * 0.47, 1.80, 0.38, 'DCE6EA', r=0.08)
    label_in(b, R(t, 12, INK, MONO), align=PP_ALIGN.LEFT)
    b.text_frame.margin_left = Inches(0.12)

# 03 engine
box(s, 7.20, top, 5.60, bot - top, NIGHT, r=0.22, shadow=dict(blur=0.4, dist=0.12, alpha=18), name='Engine')
text(s, 7.50, 3.42, 2.3, 0.55, [[WM(28, NINK), R(' engine', 28, NSOFT)]], anchor=MSO_ANCHOR.MIDDLE)
pl = box(s, 9.90, 3.43, 2.60, 0.52, '221F1A', '38332C', 0.75, r=0.26, name='Reasoning with Gemini')
text(s, 10.08, 3.43, 1.2, 0.52, R('Reasoning with', 12, NSOFT), anchor=MSO_ANCHOR.MIDDLE)
image(s, 'gemini-white.png', 11.30, 3.56, h=0.26)

gw = box(s, 7.50, 4.22, 5.00, 0.78, None, '4D4840', 1.0, r=0.1, dash=True, name='Tool gateway')
text(s, 7.70, 4.32, 4.7, 0.6, [[R('Tool gateway', 13, NINK, SEMI)],
                               [R('Scoped, budgeted reads. Text never instructs.', 12.5, NSOFT)]], space_after=2)
agents = [('Matcher', '3.8 Flash', 'Links payouts to credits', '+£2,160 → PO-0001-01', NINK),
          ('Detective', '3.8 Flash', 'Spots what’s missing', '£2,160 unexplained', 'F0B37A'),
          ('Challenger', '4 Argon', 'Fetches the proof', 'Release ≠ transfer', '6FD39B'),
          ('Curator', '3.8 Flash', 'Picks minimal evidence', '3 shared · 3 private', NINK)]
for i, (nm, model, job, ex, exc) in enumerate(agents):
    ax, ay = 7.50 + (i % 2) * 2.56, 5.18 + (i // 2) * 1.72
    box(s, ax, ay, 2.44, 1.60, NCARD, NBORDER, 0.75, r=0.12, name=f'Agent {nm}')
    text(s, ax + 0.2, ay + 0.2, 1.5, 0.38, R(nm, 16.5, NINK, SEMI))
    frontier = model == '4 Argon'
    cw = 0.74 if frontier else 0.86
    pill(s, ax + 2.44 - 0.2 - cw, ay + 0.23, cw, 0.3, model, '969DFF' if frontier else '16213F',
         '0B1020' if frontier else 'C9D3FF', size=10)
    text(s, ax + 0.2, ay + 0.66, 2.1, 0.35, R(job, 13, NSOFT))
    text(s, ax + 0.2, ay + 1.12, 2.1, 0.3, R(ex, 11.5, exc, MONO))
text(s, 7.50, 8.66, 5.0, 0.3, [[R('Gemma', 12.5, NSOFT, SEMI), R(' cross-checks Matcher and Detective', 12.5, NFAINT)]])
box(s, 7.50, 9.08, 5.00, 1.17, '1F2726', '414D4F', 0.75, r=0.12, name='Code decides')
text(s, 7.72, 9.22, 1.8, 0.42, R('Code decides', 19, 'FFFFFF', SEMI))
text(s, 9.55, 9.20, 2.8, 0.5, R('Checks every reference and penny. Traces every claim.', 12, 'CFE0E6'))
path(s, [(7.72, 9.72), (12.28, 9.72)], '35474A', 0.75, end_arrow=False)
text(s, 7.72, 9.83, 3.4, 0.3, R('240000 − 18000 − 6000 = 216000', 12.5, NINK, MONO))
text(s, 10.8, 9.83, 1.48, 0.3, [[R('✓ ', 12.5, '6FD39B', SEMI), R('supported', 12.5, '6FD39B', MONO)]], align=PP_ALIGN.RIGHT)

# 04 approve
box(s, 13.35, top, 2.60, bot - top, CARD, r=0.18, shadow=True, name='Maya approves')
text(s, 13.60, 3.40, 2.1, 0.4, R('Bank A gets', 19, INK, SEMI))
text(s, 13.60, 4.05, 2.1, 0.25, R('SHARED', 11, FAINT, MONO))
text(s, 13.60, 4.40, 2.15, 1.3, [[R('✓  ', 14.5, OK, SEMI), R(t, 14.5)] for t in ['£2,160 credit', 'Payout PO-0001-01', '18 signed records']], space_after=7)
text(s, 13.60, 5.85, 2.1, 0.25, R('KEPT PRIVATE', 11, FAINT, MONO))
text(s, 13.60, 6.20, 2.1, 1.2, [[R(t, 14.5, FAINT)] for t in ['Rent', 'Two card payments', 'An eyewear purchase']], space_after=7)
path(s, [(13.60, 7.62), (15.70, 7.62)], RULE, 0.75, end_arrow=False)
image(s, 'gemini.png', 13.60, 7.86, h=0.24)
text(s, 14.72, 7.84, 1.1, 0.3, R('privacy rules', 12, SOFT))
text(s, 13.60, 8.25, 2.15, 0.9, R('“Never share my rent” becomes an enforced rule.', 14, INK))
btn = box(s, 13.60, 9.73, 2.10, 0.52, ACC, r=0.26, name='Approve button')
label_in(btn, R('Approve exactly this', 14, '000000', SEMI))

# 05 verify
pk = box(s, 16.50, top, 2.40, 0.95, INK, r=0.16, name='Signed package')
circle(s, 16.70, 3.38, 0.48, ACC, '✓', '000000', 16)
text(s, 17.32, 3.33, 1.55, 0.3, R('Signed package', 14.5, PAPER, SEMI))
text(s, 17.32, 3.66, 1.5, 0.25, R('Ed25519', 11, 'B9B1A4', MONO))
box(s, 16.50, 4.65, 2.40, 3.05, CARD, r=0.18, shadow=True, name='Bank A analyst')
text(s, 16.75, 4.88, 1.95, 0.4, R('Bank A analyst', 19, INK, SEMI))
text(s, 16.75, 5.38, 1.95, 0.6, [[R('✓ ', 14, OK, SEMI), R('Every signature checked', 14, SOFT)]])
pill(s, 16.75, 6.36, 0.93, 0.38, 'Clear', INK, PAPER, 12, SEMI)
pill(s, 17.77, 6.36, 0.93, 0.38, 'Escalate', CARD, INK, 12, SEMI, line='CFC8BC')
pill(s, 16.75, 6.84, 1.95, 0.38, 'Ask for more', CARD, INK, 12, SEMI, line='CFC8BC')
box(s, 16.50, 8.05, 2.40, 2.50, CARD2, RULE, 0.75, r=0.18, name='Bank B')
text(s, 16.75, 8.28, 1.95, 0.4, R('Bank B', 19, INK, SEMI))
text(s, 16.75, 8.78, 1.95, 1.5, R('Asks next. Same passport, fresh consent, no access to Bank A’s package.', 14, SOFT))

# wires
path(s, [(3.80, 5.00), (4.33, 5.00)], SOFT, 1.5, dash='dash', name='Signed records wire')
rail(s, [(3.80, 8.80), (4.35, 8.80)], name='Bank feed wire')
path(s, [(6.67, 4.61), (7.18, 4.61)], INK, 1.5, start_arrow=True, name='Gateway reads')
t = text(s, 6.62, 4.20, 0.62, 0.25, R('reads', 10.5, SOFT, MONO), align=PP_ALIGN.CENTER)
rail(s, [(18.90, 5.10), (19.32, 5.10), (19.32, 2.20), (11.40, 2.20), (11.40, 3.15)], name='Request for Context')
rp = box(s, 12.55, 1.95, 5.05, 0.50, CARD, r=0.25, shadow=True, name='RFC label')
pill(s, 12.70, 2.04, 0.62, 0.32, 'RFC', INK, PAPER, 10.5)
text(s, 13.45, 1.95, 4.1, 0.50, [[R('Bank A asks  ', 14, INK), R('“Explain the £2,160 credit”', 14, SOFT)]], anchor=MSO_ANCHOR.MIDDLE)
path(s, [(12.80, 9.66), (13.07, 9.66), (13.07, 6.50), (13.33, 6.50)], INK, 1.5, name='Draft wire')
tg = box(s, 12.83, 7.92, 0.48, 0.26, PAPER)
label_in(tg, R('draft', 10.5, SOFT, MONO))
path(s, [(15.95, 9.99), (16.22, 9.99), (16.22, 3.62), (16.48, 3.62)], ACC, 2.5, arrow_size='sm', name='Approved package wire')
path(s, [(17.70, 4.10), (17.70, 4.63)], ACC, 2.5, arrow_size='sm', name='Delivery wire')

# ================================================================= slide 5: evidence already there
s = S[5]
title(s, T('The evidence was already there'),
      [WM(22.5, INK), SUB(' builds Maya’s history in the background, from marketplace and bank connections, before any bank asks.')])
events = [('5–14 OCT', '8 sales, 2 refunds, 8 fees, each signed', 'm'),
          ('26 OCT', 'Released to seller balance', 'm'),
          ('27 OCT', 'Payout PO-0001-01 settles', 'm'),
          ('28 OCT', '£2,160 credit lands', 'b'),
          ('BEFORE ANY REQUEST', 'Flagged as unexplained', 'alert'),
          ('2 NOV', 'Bank A asks for Maya’s evidence', 'ask')]
cw = 17.7 / 6
cx = [1.15 + cw * (i + 0.5) for i in range(6)]
ly = 3.55
path(s, [(cx[0], ly), (cx[2], ly)], SOFT, 1.5, dash='dash', end_arrow=False, name='Merchant records')
rail(s, [(cx[2], ly), (cx[3] - 0.22, ly)], name='Bank line')
path(s, [(cx[3], ly), (cx[5], ly)], 'CFC8BC', 1.5, end_arrow=False)
for (date, what, kind), x in zip(events, cx):
    if kind == 'alert':
        circle(s, x - 0.22, ly - 0.22, 0.44, BAD, '!', 'FFFFFF', 15)
    elif kind == 'ask':
        circle(s, x - 0.22, ly - 0.22, 0.44, ACC, '?', '000000', 15)
    else:
        circle(s, x - 0.16, ly - 0.16, 0.32, CARD, '', INK, line=SOFT)
    dc = BAD if kind == 'alert' else ACC
    text(s, x - 1.35, ly + 0.45, 2.7, 0.3, R(date, 13, dc, MONO), align=PP_ALIGN.CENTER)
    text(s, x - 1.3, ly + 0.85, 2.6, 0.9, R(what, 16, INK), align=PP_ALIGN.CENTER)
pill(s, cx[1] - 0.95, 2.62, 1.9, 0.36, 'merchant records', PAPER2, SOFT, 11.5)
pill(s, cx[3] - 0.7, 2.62, 1.4, 0.36, 'bank feed', PAPER2, SOFT, 11.5)

# the reconciliation as stat blocks
blocks = [('£2,400', '8 sales', ''), ('£180', '2 refunds', '−'), ('£60', '8 fees', '−'), ('£2,160', 'the credit', '=')]
bw, gap, by, bh = 3.6, 0.9, 6.35, 2.55
x0 = (20 - (4 * bw + 3 * gap)) / 2
for i, (amt, what, op) in enumerate(blocks):
    bx = x0 + i * (bw + gap)
    final = i == 3
    box(s, bx, by, bw, bh, ACC if final else CARD, r=0.2, shadow=True, name=f'Sum {what}')
    text(s, bx + 0.35, by + 0.45, bw - 0.7, 1.0, R(amt, 54, '000000' if final else INK, TITLE))
    text(s, bx + 0.35, by + 1.55, bw - 0.7, 0.5, R(what, 18, '0B1A1F' if final else SOFT))
    if op:
        text(s, bx - gap, by + 0.75, gap, 0.8, R(op, 40, FAINT, TITLE), align=PP_ALIGN.CENTER)
text(s, x0, by + bh + 0.4, 4 * bw + 3 * gap, 0.35,
     [[R('✓  ', 14, OK, SEMI), R('Reconciled in code from 18 signed records:  240000 − 18000 − 6000 = 216000 pence', 14, SOFT, MONO)]],
     align=PP_ALIGN.CENTER)

# ================================================================= slide 6: matching amount is not proof
s = S[6]
title(s, T('Matching amounts are not proof'), SUB('From a live Gemini run on Maya’s £2,160 credit.'))
cw, ch, cy = 5.2, 5.6, 3.45
xs = [1.15, 7.40, 13.65]
# 1 merchant says: a paper receipt
box(s, xs[0], cy, cw, ch, CARD2, SOFT, 1.25, r=0.2, dash=True, name='Merchant says')
circle(s, xs[0] + 0.45, cy + 0.5, 0.5, INK, '1', PAPER, 15)
text(s, xs[0] + 1.15, cy + 0.52, 3.5, 0.45, R('Merchant says', 22, INK, SEMI))
text(s, xs[0] + 0.45, cy + 1.45, cw - 0.9, 1.6, R('“£2,160 released to your seller balance.”', 26, INK), line_spacing=1.05)
text(s, xs[0] + 0.45, cy + 3.55, cw - 0.9, 0.9, R('The amount matches the credit.', 18, SOFT))
# 2 challenger: the engine
box(s, xs[1], cy, cw, ch, NIGHT, r=0.2, shadow=dict(blur=0.4, dist=0.12, alpha=18), name='Challenger')
circle(s, xs[1] + 0.45, cy + 0.5, 0.5, ACC, '2', '000000', 15)
text(s, xs[1] + 1.15, cy + 0.52, 2.2, 0.45, R('Challenger', 22, NINK, SEMI))
pill(s, xs[1] + cw - 1.3, cy + 0.6, 0.85, 0.32, '4 Argon', '969DFF', '0B1020', 11)
text(s, xs[1] + 0.45, cy + 1.45, cw - 0.9, 1.6, R('“A balance release is not a bank transfer.”', 26, NINK), line_spacing=1.05)
text(s, xs[1] + 0.45, cy + 3.55, cw - 0.9, 0.9, R('It fetches the PO-5001 settlement.', 18, NSOFT))
image(s, 'gemini-white.png', xs[1] + 0.45, cy + ch - 0.62, h=0.26)
# 3 code verdict
box(s, xs[2], cy, cw, ch, CARD, r=0.2, shadow=True, name='Code verdict')
circle(s, xs[2] + 0.45, cy + 0.5, 0.5, BAD, '3', 'FFFFFF', 15)
text(s, xs[2] + 1.15, cy + 0.52, 3.5, 0.45, R('Code verdict', 22, INK, SEMI))
text(s, xs[2] + 0.45, cy + 1.45, cw - 0.9, 1.9, R('Settlement still pending two hours after the credit was booked.', 26, INK), line_spacing=1.05)
pill(s, xs[2] + 0.45, cy + 3.62, 2.6, 0.48, 'claim contradicted', BADS, BAD, 14)
for i in range(2):
    path(s, [(xs[i] + cw + 0.12, cy + ch / 2), (xs[i + 1] - 0.12, cy + ch / 2)], INK, 2.0, name='Flow')

# ================================================================= slide 7: what the bank will see
s = S[7]
title(s, T('Maya sees exactly what the bank will see'),
      SUB('Code builds the smallest package that answers the question. Nothing leaves until Maya approves it.'))
px, py, pw, ph = 2.3, 2.75, 4.5, 8.1
box(s, px, py, pw, ph, INK, r=0.55, shadow=dict(blur=0.4, dist=0.12, alpha=16), name='Phone')
box(s, px + 0.16, py + 0.16, pw - 0.32, ph - 0.32, PAPER, r=0.42, name='Screen')
text(s, px + 0.5, py + 0.5, 2.0, 0.45, WM(24, INK), name='Wordmark')
pill(s, px + pw - 1.6, py + 0.55, 1.1, 0.34, 'Bank A', ACCS, ACC, 11.5, SEMI)
cxp, cyp, cwp = px + 0.4, py + 1.25, pw - 0.8
box(s, cxp, cyp, cwp, 6.15, CARD, r=0.2, shadow=True, name='Share card')
text(s, cxp + 0.3, cyp + 0.3, cwp - 0.6, 0.45, R('Share with Bank A', 20, INK, SEMI))
text(s, cxp + 0.3, cyp + 0.98, 2.0, 0.25, R('INCLUDED', 11, OK, MONO))
text(s, cxp + 0.3, cyp + 1.32, cwp - 0.5, 1.6, [[R('✓  ', 14.5, OK, SEMI), R(t, 14.5)] for t in
     ['The £2,160 credit', 'Payout PO-0001-01', '18 sale, refund and fee records']], space_after=8)
text(s, cxp + 0.3, cyp + 2.9, 2.0, 0.25, R('STAYS PRIVATE', 11, FAINT, MONO))
text(s, cxp + 0.3, cyp + 3.24, cwp - 0.5, 1.4, [[R(t, 14.5, FAINT)] for t in
     ['Rent', 'Two card payments', 'An eyewear purchase']], space_after=8)
b = box(s, cxp + 0.3, cyp + 5.25, cwp - 0.6, 0.55, ACC, r=0.275, name='Approve and send')
label_in(b, R('Approve and send', 15, '000000', SEMI))
facts = [('Built by code, not the model', 'Gemini proposes the evidence. Code filters it against Maya’s rules, and deny wins.'),
         ('Approval covers this exact package', 'It is bound to these contents, Bank A and this request. Any edit voids it.'),
         ('Private records stay out', 'Rent and personal purchases are excluded from every package.')]
for i, (h, d) in enumerate(facts):
    fy = 3.55 + i * 2.15
    circle(s, 8.6, fy, 0.72, ACCS, str(i + 1), ACC, 20)
    text(s, 9.65, fy - 0.02, 8.6, 0.5, R(h, 26, INK, SEMI))
    text(s, 9.65, fy + 0.6, 8.4, 0.9, R(d, 18, SOFT))

# ================================================================= slide 8: a second bank asks
s = S[8]
title(s, T('Bank B asks next. Maya doesn’t start over.'), SUB('Both questions are answered from one persisted passport.'))
# passport centre
ppx, ppy, ppw, pph = 8.0, 3.0, 4.0, 4.6
box(s, ppx, ppy, ppw, pph, ACC, r=0.22, shadow=True, name='Passport')
text(s, ppx + 0.4, ppy + 0.4, 3.2, 0.5, [[WM(28, '000000'), R('  passport', 18, '0B1A1F')]])
text(s, ppx + 0.4, ppy + 1.55, 3.2, 2.4, [[R(t, 20, '000000')] for t in ['Same passport', 'Same source records', 'No re-import']], space_after=12)
# banks
for side, (nm, q, extra, pkg) in enumerate([
        ('Bank A', 'Where did the £2,160 come from?', [('the credit', '£2,160'), ('payout', 'PO-0001-01')], 'Package 1 · Maya approved'),
        ('Bank B', 'What were her marketplace sales and refunds, 1 Oct to 1 Nov?', [('8 sales', '£2,400'), ('2 refunds', '£180')], 'Package 2 · fresh approval')]):
    bx = 1.15 if side == 0 else 13.0
    bw_ = 5.85
    box(s, bx, ppy, bw_, pph, CARD, r=0.2, shadow=True, name=nm)
    text(s, bx + 0.4, ppy + 0.4, 3, 0.5, R(nm, 26, INK, SEMI))
    text(s, bx + 0.4, ppy + 1.1, bw_ - 0.8, 1.2, R(q, 19, SOFT))
    if extra:
        for j, (lab, amt) in enumerate(extra):
            ex = bx + 0.4 + j * 2.6
            box(s, ex, ppy + 2.45, 2.4, 1.0, CARD2, RULE, 0.75, r=0.12)
            text(s, ex + 0.2, ppy + 2.52, 2.0, 0.5, R(amt, 24, INK, TITLE))
            text(s, ex + 0.2, ppy + 3.02, 2.0, 0.3, R(lab, 13, SOFT))
    pill(s, bx + 0.4, ppy + pph - 0.85, 3.1, 0.42, pkg, ACCS, ACC, 12)
path(s, [(ppx - 0.02, ppy + pph / 2), (1.15 + 5.85 + 0.04, ppy + pph / 2)], ACC, 3, arrow_size='med', name='To Bank A')
path(s, [(ppx + ppw + 0.02, ppy + pph / 2), (13.0 - 0.04, ppy + pph / 2)], ACC, 3, arrow_size='med', name='To Bank B')
# refusals
for j, t in enumerate(['Reusing Bank A’s approval for Bank B', 'Bank A opening Bank B’s package']):
    rx = 1.15 + j * 9.0
    box(s, rx, 8.35, 8.7, 1.1, CARD, RULE, 0.75, r=0.16, name='Refusal')
    circle(s, rx + 0.3, 8.6, 0.6, BADS, '⊘', BAD, 18)
    text(s, rx + 1.15, 8.35, 5.6, 1.1, R(t, 18, INK), anchor=MSO_ANCHOR.MIDDLE)
    pill(s, rx + 8.7 - 1.55, 8.69, 1.25, 0.42, 'refused', BADS, BAD, 12.5)

# ================================================================= slide 9 (was 10): measured, and limits
s = S[10]
title(s, T('What we measured, and where it stops'), SUB('We built on data we could see. Then we scored on data we had never seen.'))
for j, (tag, what, num, hi) in enumerate([('SEEN', 'What we built and tuned on', '73 / 80', False),
                                          ('UNSEEN', 'What we never saw while building', '19 / 44', True)]):
    sx = 1.15 + j * 9.0
    box(s, sx, 2.75, 8.7, 2.9, ACCS if hi else CARD, ACC if hi else None, 1.0, r=0.2, shadow=not hi, name=tag)
    text(s, sx + 0.45, 3.2, 3.8, 0.3, R(tag, 12, ACC, MONO))
    text(s, sx + 0.45, 3.6, 4.3, 1.2, R(what, 22, INK, SEMI))
    text(s, sx + 4.85, 3.25, 3.4, 1.9, R(num, 80, ACC if hi else INK, TITLE), align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
text(s, 1.15, 6.2, 6, 0.5, R('Limits', 24, INK, SEMI))
limits = ['Synthetic records and simulated bank and marketplace services only',
          'Monitoring is not anti-money-laundering screening or scam detection',
          'A signature proves a package wasn’t altered, not that its claims are true',
          'Whether a real bank would accept these packages is unmeasured']
for i, t in enumerate(limits):
    lx = 1.15 + i * 4.5
    box(s, lx, 6.9, 4.2, 2.9, CARD2, RULE, 0.75, r=0.18, name=f'Limit {i + 1}')
    circle(s, lx + 0.35, 7.25, 0.5, PAPER2, str(i + 1), SOFT, 14)
    text(s, lx + 0.35, 8.0, 3.5, 1.6, R(t, 17, INK))

prs.save(OUT)
print('saved', OUT, len(prs.slides), 'slides')
