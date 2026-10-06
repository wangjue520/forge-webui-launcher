# -*- coding: utf-8 -*-
"""WWY Bauform —— 「蓝图 · 机能包豪斯」主题的自制西文装饰字体生成器

设计思路
  * 包豪斯：每个字母只用三种几何体拼成——矩形（竖干 / 横杠）、椭圆环扇形（碗 / 弧）、
    等宽斜笔画；圆形字母用整圆 / 半圆，直线字母严格贴网格（大写高 700 / em 1000）
  * 未来复古：宽体比例、拱形 A、平直切口；Line 变体在 0.47 高度切一道水平细缝
    （70 年代「赛车条纹」式的切线字）
  * 机能风：笔画端点一律水平 / 垂直平切（斜笔画先延长再用字框裁平），没有圆头，
    数字等宽（时钟 / 计数不跳动），小写映射到大写（单一大小写，跟 Bayer 的 universal 同一思路）

生成：python tools/build_bauform_font.py
输出：web/fonts/WWYBauform-Light/Regular/Bold.woff2、WWYBauformLine-Bold.woff2
依赖：fonttools、skia-pathops、brotli（只是生成时需要，启动器运行不需要）
字体随本项目以 MIT 许可分发。
"""
import math
import os
import sys

import pathops
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.transformPen import TransformPen

H = 700          # 大写高度
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "web", "fonts")


# ---------------------------------------------------------------- 几何工具
def _ccw(path):
    """统一成逆时针（CFF 外轮廓方向），叠加后用非零规则合并"""
    if path.clockwise:
        path.reverse()
    return path


class Glyph:
    def __init__(self, S, w, ylo=0, yhi=H, xlo=0, xhi=None):
        self.S = S                      # 竖笔画宽
        self.Sh = round(S * 0.86)       # 横笔画略细（视觉等粗）
        self.Sd = round(S * 0.96)       # 斜笔画
        self.w = w
        self.box = (xlo, ylo, w if xhi is None else xhi, yhi)
        self.parts = []
        self.cuts = []

    # 矩形
    def rect(self, x0, y0, x1, y1):
        if x1 < x0: x0, x1 = x1, x0
        if y1 < y0: y0, y1 = y1, y0
        p = pathops.Path()
        p.moveTo(x0, y0); p.lineTo(x1, y0); p.lineTo(x1, y1); p.lineTo(x0, y1); p.close()
        self.parts.append(_ccw(p))

    def poly(self, pts):
        p = pathops.Path()
        p.moveTo(*pts[0])
        for q in pts[1:]:
            p.lineTo(*q)
        p.close()
        self.parts.append(_ccw(p))

    # 椭圆环扇形：外椭圆 (rx, ry)，内椭圆 (rx - sx, ry - sy)，角度单位度、逆时针 a0 → a1
    def ring(self, cx, cy, rx, ry, a0, a1, sx=None, sy=None):
        sx = self.S if sx is None else sx
        sy = self.Sh if sy is None else sy
        if a1 - a0 >= 359.9:            # 整环拆成两半（单轮廓扇形，方向好控制）
            self.ring(cx, cy, rx, ry, a0, a0 + 181, sx, sy)
            self.ring(cx, cy, rx, ry, a0 + 180, a0 + 361, sx, sy)
            return
        irx, iry = rx - sx, ry - sy
        p = pathops.Path()
        segs = max(1, int(math.ceil((a1 - a0) / 90.0)))

        def arc(rx_, ry_, b0, b1, first):
            n = segs
            for i in range(n):
                t0 = math.radians(b0 + (b1 - b0) * i / n)
                t1 = math.radians(b0 + (b1 - b0) * (i + 1) / n)
                k = 4.0 / 3.0 * math.tan((t1 - t0) / 4.0)
                p0 = (cx + rx_ * math.cos(t0), cy + ry_ * math.sin(t0))
                p1 = (cx + rx_ * math.cos(t1), cy + ry_ * math.sin(t1))
                d0 = (-rx_ * math.sin(t0), ry_ * math.cos(t0))
                d1 = (-rx_ * math.sin(t1), ry_ * math.cos(t1))
                if i == 0:
                    (p.moveTo if first else p.lineTo)(*p0)
                p.cubicTo(p0[0] + k * d0[0], p0[1] + k * d0[1],
                          p1[0] - k * d1[0], p1[1] - k * d1[1], p1[0], p1[1])

        arc(rx, ry, a0, a1, True)
        arc(irx, iry, a1, a0, False)
        p.close()
        self.parts.append(_ccw(p))

    # 实心椭圆（点、小圆）
    def disc(self, cx, cy, rx, ry=None):
        ry = rx if ry is None else ry
        self.ring(cx, cy, rx, ry, 0, 360, rx, ry)

    # 斜笔画：中心线 p0 → p1，两端各延长 ext，最后被字框裁平
    def stroke(self, x0, y0, x1, y1, w=None, ext=None, ext0=None, ext1=None):
        w = self.Sd if w is None else w
        ext = w * 1.2 if ext is None else ext
        e0 = ext if ext0 is None else ext0
        e1 = ext if ext1 is None else ext1
        dx, dy = x1 - x0, y1 - y0
        L = math.hypot(dx, dy)
        ux, uy = dx / L, dy / L
        nx, ny = -uy * w / 2, ux * w / 2
        a = (x0 - ux * e0, y0 - uy * e0)
        b = (x1 + ux * e1, y1 + uy * e1)
        self.poly([(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny),
                   (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)])

    def cut(self, x0, y0, x1, y1):
        p = pathops.Path()
        p.moveTo(x0, y0); p.lineTo(x1, y0); p.lineTo(x1, y1); p.lineTo(x0, y1); p.close()
        self.cuts.append(_ccw(p))

    def build(self, extra_cut=None, mirror=False):
        acc = pathops.Path()
        for q in self.parts:
            acc.addPath(q)
        acc.simplify(fix_winding=True, clockwise=False)
        x0, y0, x1, y1 = self.box
        box = pathops.Path()
        box.moveTo(x0, y0); box.lineTo(x1, y0); box.lineTo(x1, y1); box.lineTo(x0, y1); box.close()
        acc = pathops.op(acc, _ccw(box), pathops.PathOp.INTERSECTION, clockwise=False)
        cuts = list(self.cuts) + ([extra_cut] if extra_cut is not None else [])
        for c in cuts:
            acc = pathops.op(acc, c, pathops.PathOp.DIFFERENCE, clockwise=False)
        if mirror:   # 180° 旋转（9 = 倒过来的 6）
            out = pathops.Path()
            acc.draw(TransformPen(out.getPen(), (-1, 0, 0, -1, self.w, H)))
            out.simplify(fix_winding=True, clockwise=False)
            acc = out
        return acc


# ---------------------------------------------------------------- 字形定义
# 每个函数返回 Glyph；sb = 左右留白系数（圆形字母小一点）
G = {}


def glyph(name, uni=None, sb=1.0, tab=False, line=True, rsb=None):
    # rsb：右侧留白系数（右边开口的字母如 C / L 视觉上已经很空，右留白要收紧）
    def deco(fn):
        G[name] = dict(fn=fn, uni=uni, sb=sb, tab=tab, line=line, rsb=sb if rsb is None else rsb)
        return fn
    return deco


@glyph("A", "A")
def _A(S):
    w = 660; g = Glyph(S, w); r = w / 2; cy = H - r
    g.ring(r, cy, r, r, 0, 180)                      # 拱顶（半圆）
    g.rect(0, 0, S, cy + 1); g.rect(w - S, 0, w, cy + 1)
    y0 = round(H * 0.30); g.rect(0, y0, w, y0 + g.Sh)
    return g


@glyph("B", "B")
def _B(S):
    w = 600; g = Glyph(S, w); Sh = round(S * .86)
    m = H * 0.54
    ryu = (H - (m - Sh / 2)) / 2; ryl = (m + Sh / 2) / 2
    rxu, rxl = min(ryu * 1.08, 230), min(ryl * 1.0, 250)
    cxu, cxl = w - 26 - rxu, w - rxl
    g.ring(cxu, H - ryu, rxu, ryu, -90, 90); g.ring(cxl, ryl, rxl, ryl, -90, 90)
    g.rect(0, 0, S, H)
    g.rect(0, H - Sh, cxu + 1, H); g.rect(0, m - Sh / 2, max(cxu, cxl) + 1, m + Sh / 2); g.rect(0, 0, cxl + 1, Sh)
    return g


@glyph("C", "C", sb=.7, rsb=.15)
def _C(S):
    w = 690; g = Glyph(S, w)
    g.ring(w / 2, H / 2, w / 2, H / 2, 45, 315)
    return g


@glyph("D", "D", sb=.85)
def _D(S):
    w = 670; g = Glyph(S, w); Sh = round(S * .86)
    rx = 330; cx = w - rx
    g.ring(cx, H / 2, rx, H / 2, -90, 90)
    g.rect(0, 0, S, H); g.rect(0, H - Sh, cx + 1, H); g.rect(0, 0, cx + 1, Sh)
    return g


@glyph("E", "E")
def _E(S):
    w = 540; g = Glyph(S, w); Sh = round(S * .86)
    g.rect(0, 0, S, H); g.rect(0, H - Sh, w, H); g.rect(0, 0, w, Sh)
    g.rect(0, H / 2 - Sh / 2, w - 40, H / 2 + Sh / 2)
    return g


@glyph("F", "F", rsb=.6)
def _F(S):
    w = 520; g = Glyph(S, w); Sh = round(S * .86)
    g.rect(0, 0, S, H); g.rect(0, H - Sh, w, H)
    g.rect(0, H * .47 - Sh / 2, w - 40, H * .47 + Sh / 2)
    return g


@glyph("G", "G", sb=.7, rsb=.5)
def _G(S):
    w = 710; g = Glyph(S, w); Sh = round(S * .86)
    g.ring(w / 2, H / 2, w / 2, H / 2, 45, 360)
    g.rect(w * .5, H / 2 - Sh, w, H / 2)
    return g


@glyph("H", "H")
def _H(S):
    w = 640; g = Glyph(S, w); Sh = round(S * .86)
    g.rect(0, 0, S, H); g.rect(w - S, 0, w, H); g.rect(0, H / 2 - Sh / 2, w, H / 2 + Sh / 2)
    return g


@glyph("I", "I")
def _I(S):
    g = Glyph(S, S); g.rect(0, 0, S, H); return g


@glyph("J", "J", sb=.85)
def _J(S):
    w = 540; g = Glyph(S, w); r = w / 2
    g.rect(w - S, r - 1, w, H); g.ring(r, r, r, r, 180, 360)
    return g


@glyph("K", "K")
def _K(S):
    w = 630; g = Glyph(S, w)
    g.rect(0, 0, S, H)
    ax0, ay0, ax1, ay1 = S * .4, H * .30, w, H + 40
    g.stroke(ax0, ay0, ax1, ay1)
    t = .36
    jx, jy = ax0 + t * (ax1 - ax0), ay0 + t * (ay1 - ay0)
    g.stroke(jx, jy, w - g.Sd * .5, 0, ext0=0)
    return g


@glyph("L", "L", rsb=.35)
def _L(S):
    w = 500; g = Glyph(S, w); Sh = round(S * .86)
    g.rect(0, 0, S, H); g.rect(0, 0, w, Sh)
    return g


@glyph("M", "M")
def _M(S):
    w = 820; g = Glyph(S, w)
    g.rect(0, 0, S, H); g.rect(w - S, 0, w, H)
    g.stroke(S * .5, H, w / 2, S * .1); g.stroke(w - S * .5, H, w / 2, S * .1)
    return g


@glyph("N", "N")
def _N(S):
    w = 650; g = Glyph(S, w)
    g.rect(0, 0, S, H); g.rect(w - S, 0, w, H)
    g.stroke(S * .5, H, w - S * .5, 0)
    return g


@glyph("O", "O", sb=.7)
def _O(S):
    w = 730; g = Glyph(S, w); g.ring(w / 2, H / 2, w / 2, H / 2, 0, 360); return g


@glyph("P", "P", sb=.9)
def _P(S):
    w = 590; g = Glyph(S, w); Sh = round(S * .86)
    yb = H * .38; ry = (H - yb) / 2; rx = min(ry * 1.06, 250); cx = w - rx
    g.ring(cx, H - ry, rx, ry, -90, 90)
    g.rect(0, 0, S, H); g.rect(0, H - Sh, cx + 1, H); g.rect(0, yb, cx + 1, yb + Sh)
    return g


@glyph("Q", "Q", sb=.7)
def _Q(S):
    w = 730; g = Glyph(S, w, ylo=-90, xhi=w + 40); g.ring(w / 2, H / 2, w / 2, H / 2, 0, 360)
    g.stroke(w * .56, H * .34, w + 20, -70)
    return g


@glyph("R", "R")
def _R(S):
    w = 620; g = Glyph(S, w); Sh = round(S * .86)
    yb = H * .38; ry = (H - yb) / 2; rx = min(ry * 1.06, 250); cx = w - 30 - rx
    g.ring(cx, H - ry, rx, ry, -90, 90)
    g.rect(0, 0, S, H); g.rect(0, H - Sh, cx + 1, H); g.rect(0, yb, cx + 1, yb + Sh)
    g.stroke(cx - 40, yb + Sh * .4, w - g.Sd * .5, 0, ext0=0)
    return g


@glyph("S", "S", sb=.85)
def _S(S):
    w = 600; g = Glyph(S, w); Sh = round(S * .86)
    tot = (H + Sh) / 2; ryl = tot * .515; ryu = tot - ryl
    rxu, rxl = ryu * 1.05, ryl * 1.05
    cxu, cxl = rxu, w - rxl
    g.ring(cxu, H - ryu, rxu, ryu, 90, 270)
    g.ring(cxl, ryl, rxl, ryl, -90, 90)
    my = H - 2 * ryu
    if cxl > cxu: g.rect(cxu - 1, my, cxl + 1, my + Sh)
    g.rect(cxu - 1, H - Sh, w, H); g.rect(0, 0, cxl + 1, Sh)
    return g


@glyph("T", "T")
def _T(S):
    w = 620; g = Glyph(S, w); Sh = round(S * .86)
    g.rect(0, H - Sh, w, H); g.rect(w / 2 - S / 2, 0, w / 2 + S / 2, H)
    return g


@glyph("U", "U", sb=.9)
def _U(S):
    w = 640; g = Glyph(S, w); r = w / 2
    g.rect(0, r - 1, S, H); g.rect(w - S, r - 1, w, H); g.ring(r, r, r, r, 180, 360)
    return g


@glyph("V", "V")
def _V(S):
    w = 680; g = Glyph(S, w)
    g.stroke(S * .5, H, w / 2, 0); g.stroke(w - S * .5, H, w / 2, 0)
    return g


@glyph("W", "W")
def _W(S):
    w = 960; g = Glyph(S, w)
    q1, q3 = w * .27, w * .73
    g.stroke(S * .5, H, q1, 0); g.stroke(w / 2, H, q1, 0)
    g.stroke(w / 2, H, q3, 0); g.stroke(w - S * .5, H, q3, 0)
    return g


@glyph("X", "X")
def _X(S):
    w = 650; g = Glyph(S, w)
    g.stroke(S * .5, H, w - S * .5, 0); g.stroke(w - S * .5, H, S * .5, 0)
    return g


@glyph("Y", "Y")
def _Y(S):
    w = 670; g = Glyph(S, w); yj = H * .42
    g.stroke(S * .5, H, w / 2, yj, ext1=0); g.stroke(w - S * .5, H, w / 2, yj, ext1=0)
    g.rect(w / 2 - S / 2, 0, w / 2 + S / 2, yj + 10)
    return g


@glyph("Z", "Z")
def _Z(S):
    w = 590; g = Glyph(S, w); Sh = round(S * .86)
    g.rect(0, H - Sh, w, H); g.rect(0, 0, w, Sh)
    g.stroke(w - S * .62, H - Sh, S * .62, Sh)
    return g


# ---- 数字（等宽）
DW = 580


@glyph("zero", "0", tab=True)
def _0(S):
    g = Glyph(S, DW); g.ring(DW / 2, H / 2, DW / 2, H / 2, 0, 360)
    # 机能风：零里一道短斜杠，和字母 O 区分
    g.stroke(DW * .40, H * .38, DW * .60, H * .62, w=S * .62, ext=0)
    return g


@glyph("one", "1", tab=True)
def _1(S):
    g = Glyph(S, DW); Sh = round(S * .86); x0 = DW * .56 - S / 2
    g.rect(x0, 0, x0 + S, H); g.rect(x0 - 170, H - Sh, x0 + 1, H)
    return g


@glyph("two", "2", tab=True)
def _2(S):
    g = Glyph(S, DW); Sh = round(S * .86)
    ry = 260; cy = H - ry; rx = DW / 2
    a = -32
    g.ring(rx, cy, rx, ry, a, 180)
    t = math.radians(a)
    px = rx + (rx - S / 2) * math.cos(t); py = cy + (ry - Sh / 2) * math.sin(t)
    g.stroke(px, py, S * .55, Sh * .5)
    g.rect(0, 0, DW, Sh)
    return g


@glyph("three", "3", tab=True)
def _3(S):
    g = Glyph(S, DW); Sh = round(S * .86)
    tot = (H + Sh) / 2; ryl = tot * .52; ryu = tot - ryl
    rxu, rxl = ryu * 1.08, ryl * 1.08
    cxu, cxl = DW - 24 - rxu, DW - rxl
    g.ring(cxu, H - ryu, rxu, ryu, -90, 90); g.ring(cxl, ryl, rxl, ryl, -90, 90)
    my = H - 2 * ryu
    g.rect(0, H - Sh, cxu + 1, H); g.rect(0, 0, cxl + 1, Sh)
    g.rect(DW * .28, my, max(cxu, cxl) + 1, my + Sh)
    return g


@glyph("four", "4", tab=True)
def _4(S):
    g = Glyph(S, DW); Sh = round(S * .86)
    y0 = H * .25; xs = DW * .64
    g.rect(0, y0, DW, y0 + Sh); g.rect(xs, 0, xs + S, H)
    g.stroke(xs + S * .5, H + 20, S * .45, y0 + Sh * .5)
    return g


@glyph("five", "5", tab=True)
def _5(S):
    g = Glyph(S, DW); Sh = round(S * .86)
    hb = H * .6; ry = hb / 2; rx = 250; cx = DW - rx
    g.ring(cx, ry, rx, ry, -90, 90)
    g.rect(0, hb - Sh, cx + 1, hb); g.rect(0, 0, cx + 1, Sh)
    g.rect(0, hb - Sh, S, H); g.rect(0, H - Sh, DW, H)
    return g


@glyph("six", "6", tab=True)
def _6(S):
    g = Glyph(S, DW)
    ry = 240; r = DW / 2
    g.ring(r, ry, r, ry, 0, 360)
    g.rect(0, ry, S, H - r + 1)
    g.ring(r, H - r, r, r, 28, 180)
    return g


@glyph("seven", "7", tab=True)
def _7(S):
    g = Glyph(S, DW); Sh = round(S * .86)
    g.rect(0, H - Sh, DW, H)
    g.stroke(DW - S * .6, H - Sh * .5, DW * .24, 0)
    return g


@glyph("eight", "8", tab=True)
def _8(S):
    g = Glyph(S, DW); Sh = round(S * .86)
    tot = (H + Sh) / 2; ryl = tot * .53; ryu = tot - ryl
    g.ring(DW / 2, H - ryu, DW / 2 - 28, ryu, 0, 360)
    g.ring(DW / 2, ryl, DW / 2, ryl, 0, 360)
    return g


@glyph("nine", "9", tab=True)
def _9(S):
    g = _6(S); g._mirror = True; return g


# ---- 标点 / 符号
@glyph("space", " ", line=False)
def _space(S):
    g = Glyph(S, 200); return g


@glyph("period", ".", sb=1.0, line=False)
def _period(S):
    g = Glyph(S, S); g.rect(0, 0, S, S); return g


@glyph("comma", ",", line=False)
def _comma(S):
    g = Glyph(S, S, ylo=-170, xlo=-60)
    g.rect(0, 0, S, S); g.poly([(0, S * .5), (S, S * .5), (S * .45, -160), (-S * .45, -160)])
    return g


@glyph("colon", ":", line=False)
def _colon(S):
    g = Glyph(S, S); g.rect(0, 0, S, S); y = H * .46; g.rect(0, y, S, y + S); return g


@glyph("semicolon", ";", line=False)
def _semicolon(S):
    g = _comma(S); y = H * .46; g.rect(0, y, S, y + S); return g


@glyph("hyphen", "-", line=False)
def _hyphen(S):
    g = Glyph(S, 300); Sh = round(S * .86); y = H * .42; g.rect(0, y, 300, y + Sh); return g


@glyph("endash", "–", line=False)
def _endash(S):
    g = Glyph(S, 500); Sh = round(S * .86); y = H * .42; g.rect(0, y, 500, y + Sh); return g


@glyph("emdash", "—", line=False)
def _emdash(S):
    g = Glyph(S, 860); Sh = round(S * .86); y = H * .42; g.rect(0, y, 860, y + Sh); return g


@glyph("underscore", "_", line=False)
def _underscore(S):
    g = Glyph(S, 540, ylo=-160); Sh = round(S * .86); g.rect(0, -150, 540, -150 + Sh); return g


@glyph("slash", "/", line=False)
def _slash(S):
    w = 400; g = Glyph(S, w, ylo=-80, yhi=H + 40)
    g.stroke(S * .5, -80, w - S * .5, H + 40, w=S * .8); return g


@glyph("backslash", "\\", line=False)
def _backslash(S):
    w = 400; g = Glyph(S, w, ylo=-80, yhi=H + 40)
    g.stroke(w - S * .5, -80, S * .5, H + 40, w=S * .8); return g


@glyph("bar", "|", line=False)
def _bar(S):
    g = Glyph(S, S * .7, ylo=-120, yhi=H + 100); g.rect(0, -120, S * .7, H + 100); return g


@glyph("plus", "+", line=False)
def _plus(S):
    w = 460; g = Glyph(S, w); Sh = round(S * .86); cy = H * .44
    g.rect(w / 2 - S / 2, cy - w / 2, w / 2 + S / 2, cy + w / 2)
    g.rect(0, cy - Sh / 2, w, cy + Sh / 2); return g


@glyph("equal", "=", line=False)
def _equal(S):
    w = 460; g = Glyph(S, w); Sh = round(S * .86); cy = H * .44; gap = max(70, S * .7)
    g.rect(0, cy + gap / 2, w, cy + gap / 2 + Sh); g.rect(0, cy - gap / 2 - Sh, w, cy - gap / 2); return g


@glyph("multiply", "×", line=False)
def _multiply(S):
    w = 420; g = Glyph(S, w, ylo=H * .44 - 210, yhi=H * .44 + 210); cy = H * .44
    g.stroke(0, cy + w / 2, w, cy - w / 2, w=S * .8); g.stroke(0, cy - w / 2, w, cy + w / 2, w=S * .8); return g


@glyph("numbersign", "#", line=False)
def _hash(S):
    w = 600; g = Glyph(S, w); t = S * .72; th = round(t * .9)
    for x in (w * .27, w * .73): g.rect(x - t / 2, 40, x + t / 2, H - 40)
    for y in (H * .33, H * .67): g.rect(0, y - th / 2, w, y + th / 2)
    return g


@glyph("percent", "%", line=False)
def _percent(S):
    w = 680; g = Glyph(S, w); r = 125; t = S * .72
    g.ring(r, H - r, r, r, 0, 360, t, t * .9); g.ring(w - r, r, r, r, 0, 360, t, t * .9)
    g.stroke(w * .80, H, w * .20, 0, w=S * .8); return g


@glyph("parenleft", "(", line=False)
def _parenleft(S):
    w = 230; g = Glyph(S, w, ylo=-110, yhi=H + 110)
    g.ring(300, H / 2, 300, 520, 118, 242); return g


@glyph("parenright", ")", line=False)
def _parenright(S):
    w = 230; g = Glyph(S, w, ylo=-110, yhi=H + 110)
    g.ring(w - 300, H / 2, 300, 520, -62, 62); return g


@glyph("bracketleft", "[", line=False)
def _bl(S):
    w = 240; g = Glyph(S, w, ylo=-110, yhi=H + 110); Sh = round(S * .86)
    g.rect(0, -110, S, H + 110); g.rect(0, H + 110 - Sh, w, H + 110); g.rect(0, -110, w, -110 + Sh); return g


@glyph("bracketright", "]", line=False)
def _br(S):
    w = 240; g = Glyph(S, w, ylo=-110, yhi=H + 110); Sh = round(S * .86)
    g.rect(w - S, -110, w, H + 110); g.rect(0, H + 110 - Sh, w, H + 110); g.rect(0, -110, w, -110 + Sh); return g


@glyph("quotesingle", "'", line=False)
def _qs(S):
    g = Glyph(S, S * .8); g.rect(0, H - 230, S * .8, H); return g


@glyph("quotedbl", '"', line=False)
def _qd(S):
    w = S * .8 * 2 + 70; g = Glyph(S, w)
    g.rect(0, H - 230, S * .8, H); g.rect(w - S * .8, H - 230, w, H); return g


@glyph("exclam", "!", line=False)
def _exclam(S):
    g = Glyph(S, S); g.rect(0, S + 90, S, H); g.rect(0, 0, S, S); return g


@glyph("question", "?", line=False)
def _question(S):
    w = 520; g = Glyph(S, w); r = w / 2; ry = 220; cy = H - ry; Sh = round(S * .86)
    g.ring(r, cy, r, ry, -90, 180)
    g.rect(r - S / 2, S + 80, r + S / 2, cy - ry + Sh)
    g.rect(r - S / 2, 0, r + S / 2, S); return g


@glyph("asterisk", "*", line=False)
def _ast(S):
    w = 420; g = Glyph(S, w, ylo=H - 400, yhi=H + 10); cx, cy, L = w / 2, H - 200, 190; t = S * .62
    for a in (90, 30, 150):
        dx, dy = L * math.cos(math.radians(a)), L * math.sin(math.radians(a))
        g.stroke(cx - dx, cy - dy, cx + dx, cy + dy, w=t, ext=0)
    return g


@glyph("less", "<", line=False)
def _less(S):
    w = 440; g = Glyph(S, w); cy = H * .44; t = S * .84
    g.stroke(w, cy + 230, S * .3, cy, w=t); g.stroke(S * .3, cy, w, cy - 230, w=t); return g


@glyph("greater", ">", line=False)
def _greater(S):
    w = 440; g = Glyph(S, w); cy = H * .44; t = S * .84
    g.stroke(0, cy + 230, w - S * .3, cy, w=t); g.stroke(w - S * .3, cy, 0, cy - 230, w=t); return g


@glyph("periodcentered", "·", line=False)
def _middot(S):
    g = Glyph(S, S); y = H * .44 - S / 2; g.rect(0, y, S, y + S); return g


@glyph("degree", "°", line=False)
def _deg(S):
    g = Glyph(S, 300); g.ring(150, H - 150, 150, 150, 0, 360, S * .7, S * .7); return g


@glyph("arrowright", "→", line=False)
def _arrow(S):
    w = 700; g = Glyph(S, w); cy = H * .44; Sh = round(S * .86); t = S * .86
    g.rect(0, cy - Sh / 2, w - 60, cy + Sh / 2)
    g.stroke(w - 250, cy + 250, w - S * .35, cy, w=t); g.stroke(w - S * .35, cy, w - 250, cy - 250, w=t)
    return g


@glyph("ampersand", "&", line=False)
def _amp(S):
    # 几何 & ：上小环 + 下大环开口 + 斜尾
    w = 700; g = Glyph(S, w); Sh = round(S * .86)
    g.ring(w * .36, H - 160, 160, 160, 0, 360, S * .9, Sh * .9)
    g.ring(w * .40, 230, w * .40, 230, 40, 330)
    g.stroke(w * .24, H - 300, w - S * .5, 0)
    g.rect(w * .62, H * .38 - Sh / 2, w, H * .38 + Sh / 2)
    return g


# ---------------------------------------------------------------- 组装字体
LOWER = {chr(c): chr(c - 32) for c in range(ord("a"), ord("z") + 1)}


def build_font(family, style, weight, S, line_cut=False):
    order = [".notdef"] + list(G.keys())
    cmap, charstrings, metrics = {}, {}, {}
    base_sb = 52 + S * 0.14
    # .notdef
    pen = T2CharStringPen(500, None)
    pen.moveTo((40, 0)); pen.lineTo((460, 0)); pen.lineTo((460, H)); pen.lineTo((40, H)); pen.closePath()
    pen.moveTo((100, 60)); pen.lineTo((100, H - 60)); pen.lineTo((400, H - 60)); pen.lineTo((400, 60)); pen.closePath()
    charstrings[".notdef"] = pen.getCharString(); metrics[".notdef"] = (500, 40)

    slit = None
    if line_cut:
        y0 = H * 0.47; slit = pathops.Path()
        slit.moveTo(-500, y0); slit.lineTo(2000, y0); slit.lineTo(2000, y0 + S * 0.2); slit.lineTo(-500, y0 + S * 0.2); slit.close()
        slit = _ccw(slit)

    for name, d in G.items():
        g = d["fn"](S)
        mirror = getattr(g, "_mirror", False)
        path = g.build(extra_cut=slit if (line_cut and d["line"]) else None, mirror=mirror)
        sb = round(base_sb * d["sb"])
        if name == "space":
            adv = 240 + S * 0.4; sb = 0
        elif d["tab"]:
            adv = DW + 2 * round(base_sb * .9); sb = round(base_sb * .9)
        else:
            adv = g.w + sb + round(base_sb * d["rsb"])
        adv = int(round(adv))
        pen = T2CharStringPen(adv, None)
        path.draw(TransformPen(pen, (1, 0, 0, 1, sb, 0)))
        charstrings[name] = pen.getCharString()
        bounds = path.bounds if path.contours else (0, 0, 0, 0)
        lsb = int(round(bounds[0] + sb)) if path.contours else 0
        metrics[name] = (adv, lsb)
        if d["uni"]:
            cmap[ord(d["uni"])] = name
    for lo, up in LOWER.items():
        cmap[ord(lo)] = cmap[ord(up)]
    cmap[0xA0] = "space"

    fb = FontBuilder(1000, isTTF=False)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(cmap)
    ps = (family + "-" + style).replace(" ", "")
    fb.setupCFF(ps, {"FullName": family + " " + style}, charstrings, {})
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=900, descent=-240)
    fb.setupNameTable({
        "familyName": family, "styleName": style,
        "uniqueFontIdentifier": ps + "-1.0", "fullName": family + " " + style, "psName": ps,
        "version": "Version 1.000", "copyright": "(c) 2026 WWY. Released under the MIT License.",
        "designer": "WWY", "description": "Geometric display face for the WWY launcher blueprint theme.",
    })
    fb.setupOS2(version=4, sTypoAscender=900, sTypoDescender=-240, sTypoLineGap=0,
                usWinAscent=960, usWinDescent=280, sCapHeight=H, sxHeight=H,
                usWeightClass=weight, fsType=0, achVendID="WWY ",
                fsSelection=0x40 | 0x80)
    fb.setupPost()
    return fb.font


def main():
    os.makedirs(OUT, exist_ok=True)
    jobs = [
        ("WWY Bauform", "Light", 300, 46, False, "WWYBauform-Light"),
        ("WWY Bauform", "Regular", 400, 84, False, "WWYBauform-Regular"),
        ("WWY Bauform", "Bold", 700, 134, False, "WWYBauform-Bold"),
        ("WWY Bauform Line", "Bold", 700, 134, True, "WWYBauformLine-Bold"),
    ]
    for fam, sty, wt, S, cut, fn in jobs:
        f = build_font(fam, sty, wt, S, cut)
        if "--otf" in sys.argv:
            f.save(os.path.join(OUT, fn + ".otf"))
        f.flavor = "woff2"
        f.save(os.path.join(OUT, fn + ".woff2"))
        print("wrote", fn, os.path.getsize(os.path.join(OUT, fn + ".woff2")), "bytes")


if __name__ == "__main__":
    main()
