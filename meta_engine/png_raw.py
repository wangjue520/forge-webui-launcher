# -*- coding: utf-8 -*-
"""
png_raw.py —— 纯标准库 PNG 像素解码（只服务于隐写 PNG 读取）。

为什么要有这个文件：原版 png_stealth.decode_stealth_text 用 PIL 打开图片拿
img.tobytes()。而这个启动器刻意只依赖 pywebview + requests——为了读一张隐写图
去硬性引入 Pillow，会把"依赖打架"这个坑重新挖开（见 wd14_venv_manager 里记的
那段血泪）。

隐写 PNG 的载体条件本来就很窄（stealth-pnginfo 写入端只产出 8bit、非隔行的
RGB/RGBA PNG），所以这里只需要覆盖这一种情况：zlib 解压 IDAT -> 逐行逆滤波，
一共不到百行。遇到任何超出范围的情况（16bit、隔行、调色板）直接返回 None，
调用方自然降级成"这张图没有隐写元数据"，不会误判。

优先级：有 PIL 就用 PIL（更快），没有就用这里的纯实现。两条路的输出字节布局
完全一致（行优先、每像素 bpp 字节）。
"""
import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# 只解这两种色彩类型：2 = truecolor(RGB), 6 = truecolor+alpha(RGBA)
_CHANNELS = {2: 3, 6: 4}

# 解压输出上限，防止恶意构造的 PNG 撑爆内存（8K x 8K RGBA 约 268MB，取 320MB 留余量）
_MAX_RAW_BYTES = 320 << 20


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw, width, height, bpp):
    """逐扫描行逆 PNG 滤波，返回去掉滤波字节后的纯像素数据。"""
    stride = width * bpp
    out = bytearray(stride * height)
    pos = 0
    prev_start = 0
    for y in range(height):
        if pos >= len(raw):
            return None
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        if len(line) < stride:
            return None
        pos += stride
        cur_start = y * stride

        if ftype == 0:
            pass
        elif ftype == 1:  # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:  # Up
            if y:
                for i in range(stride):
                    line[i] = (line[i] + out[prev_start + i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up = out[prev_start + i] if y else 0
                line[i] = (line[i] + ((left + up) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up = out[prev_start + i] if y else 0
                ul = out[prev_start + i - bpp] if (y and i >= bpp) else 0
                line[i] = (line[i] + _paeth(left, up, ul)) & 0xFF
        else:
            return None

        out[cur_start:cur_start + stride] = line
        prev_start = cur_start
    return bytes(out)


def decode_rgb_bytes(file_path):
    """
    返回 (raw_bytes, width, height, bpp)；不是受支持的 PNG 时返回 None。
    raw_bytes 的布局与 PIL 的 img.tobytes() 一致。
    """
    try:
        with open(file_path, "rb") as f:
            if f.read(8) != PNG_SIGNATURE:
                return None
            width = height = -1
            bit_depth = color_type = interlace = -1
            idat = bytearray()
            seen_ihdr = False
            for _ in range(65536):
                head = f.read(8)
                if len(head) < 8:
                    break
                length, ctype = struct.unpack(">I4s", head)
                if length > (1 << 30):
                    return None
                if ctype == b"IHDR":
                    data = f.read(length)
                    if len(data) < 13:
                        return None
                    width, height, bit_depth, color_type, _comp, _filt, interlace = \
                        struct.unpack(">IIBBBBB", data[:13])
                    seen_ihdr = True
                    if bit_depth != 8 or interlace != 0 or color_type not in _CHANNELS:
                        return None
                    if width <= 0 or height <= 0:
                        return None
                    if width * height * _CHANNELS[color_type] > _MAX_RAW_BYTES:
                        return None
                elif ctype == b"IDAT":
                    idat += f.read(length)
                    if len(idat) > _MAX_RAW_BYTES:
                        return None
                elif ctype == b"IEND":
                    break
                else:
                    f.seek(length, 1)
                f.seek(4, 1)  # CRC
            if not seen_ihdr or not idat:
                return None
    except OSError:
        return None

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None

    bpp = _CHANNELS[color_type]
    pixels = _unfilter(raw, width, height, bpp)
    if pixels is None:
        return None
    return pixels, width, height, bpp
