# -*- coding: utf-8 -*-
"""
container_writer.py —— 把元数据写回图片容器（纯标准库，不重编码像素）。

跟原项目的做法不一样，这里要说明一下为什么：

原项目用 Pillow 打开图片 → 改 info → 重新保存。这条路能顺带做格式转换，
但代价是**像素被重新编码**：
  · PNG 会被重新压缩，文件大小变、字节流全变
  · JPEG 会被重新有损编码，每保存一次画质掉一点
  · 动图要逐帧解码再逐帧写回，所以原项目不得不设一个"总像素超 8000 万
    就拒绝保存"的上限，否则内存会炸

这里改成**容器块手术**：只增删改元数据块，像素数据的字节原样复制。
好处是实打实的：
  · 真·无损，JPEG 反复保存也不掉画质
  · 动图天然安全（帧数据根本没被碰过），那个 8000 万像素的限制可以取消
  · "外来数据保全"变成默认行为 —— 我们没动的块本来就还在那儿，
    不需要专门去"收集并带回"，也就不存在收集遗漏
  · 不依赖 Pillow，启动器的依赖表不用动

代价是没法做格式转换（PNG 另存成 JPEG 要真的编码像素）。那条路单独走
隔离 venv 里的 Pillow，见 image_convert.py。
"""
import os
import struct
import zlib

from . import exif_build

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# PNG 里承载文本的三种块
_PNG_TEXT_CHUNKS = (b"tEXt", b"iTXt", b"zTXt")
# 清理元数据时一并干掉的非文本元数据块
_PNG_META_CHUNKS = (b"eXIf", b"tIME", b"pHYs", b"iCCP")

# JPEG 段标记
_JPEG_APP1 = 0xE1
_JPEG_COM = 0xFE
# APP1 段的数据区上限（段长字段是 16 位，含自身 2 字节）
_APP1_MAX = 65533


class WriteError(Exception):
    pass


def _atomic_replace(tmp, target):
    os.replace(tmp, target)


def _tmp_for(target):
    return f"{target}.tmp_{os.getpid()}"


# ============================================================
# PNG
# ============================================================

def _png_text_chunk(keyword, text):
    """
    生成一个文本块。纯 latin-1 能表示的短文本用 tEXt（跟 A1111 写出来的一致，
    最大兼容）；含中文等非 latin-1 字符时必须用 iTXt（规范里只有它是 UTF-8）。
    """
    kw = keyword.encode("latin-1", "replace")
    try:
        body = text.encode("latin-1")
        return _chunk(b"tEXt", kw + b"\x00" + body)
    except UnicodeEncodeError:
        # iTXt: keyword\0 compression_flag compression_method lang\0 translated\0 text
        body = (kw + b"\x00\x00\x00" + b"\x00" + b"\x00"
                + text.encode("utf-8"))
        return _chunk(b"iTXt", body)


def _chunk(ctype, data):
    return (struct.pack(">I", len(data)) + ctype + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF))


def _iter_png_chunks(raw):
    pos = 8
    while pos + 8 <= len(raw):
        length = struct.unpack_from(">I", raw, pos)[0]
        ctype = raw[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > len(raw):
            break
        yield ctype, raw[pos + 8:pos + 8 + length], raw[pos:end]
        pos = end
        if ctype == b"IEND":
            break


def _png_chunk_keyword(ctype, data):
    """取出文本块的 keyword，非文本块返回 None。"""
    if ctype not in _PNG_TEXT_CHUNKS:
        return None
    try:
        return data.split(b"\x00", 1)[0].decode("latin-1")
    except (ValueError, UnicodeDecodeError):
        return None


def write_png(src, target, new_texts=None, owned_keys=(), strip=False):
    """
    重写 PNG 的元数据块。

    new_texts : {keyword: text}，要写入的文本块
    owned_keys: 本次要重建的 keyword 集合 —— 源文件里这些同名旧块一律丢弃，
                这样用户清空某个字段时不会被旧块"复活"。
                其余文本块（第三方工具写的）原样保留。
    strip     : 真时丢弃所有元数据块，new_texts 被忽略。
    """
    new_texts = new_texts or {}
    owned = {k.lower() for k in owned_keys} | {k.lower() for k in new_texts}
    with open(src, "rb") as f:
        raw = f.read()
    if not raw.startswith(PNG_SIGNATURE):
        raise WriteError("不是有效的 PNG 文件")

    head, body, tail = bytearray(PNG_SIGNATURE), bytearray(), bytearray()
    seen_idat = False
    for ctype, data, whole in _iter_png_chunks(raw):
        if ctype == b"IEND":
            continue
        if ctype == b"IDAT":
            seen_idat = True
        kw = _png_chunk_keyword(ctype, data)
        if strip:
            if kw is not None or ctype in _PNG_META_CHUNKS:
                continue
        elif kw is not None and kw.lower() in owned:
            continue   # 本次要重建的字段，旧块丢弃
        (tail if seen_idat else body).extend(whole)

    out = bytearray(head)
    out += body
    if not strip:
        # 文本块放在 IDAT 之前：规范允许前后都行，但放前面读取端更容易拿到
        for kw, text in new_texts.items():
            if text is None:
                continue
            out += _png_text_chunk(kw, text)
    out += tail
    out += _chunk(b"IEND", b"")

    tmp = _tmp_for(target)
    try:
        with open(tmp, "wb") as f:
            f.write(bytes(out))
        _atomic_replace(tmp, target)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def png_text_keys(src):
    """列出 PNG 里现有的文本块 keyword（损失预告要用）。"""
    try:
        with open(src, "rb") as f:
            raw = f.read()
    except OSError:
        return []
    if not raw.startswith(PNG_SIGNATURE):
        return []
    keys = []
    for ctype, data, _ in _iter_png_chunks(raw):
        kw = _png_chunk_keyword(ctype, data)
        if kw:
            keys.append(kw)
    return keys


# ============================================================
# JPEG
# ============================================================

def _iter_jpeg_segments(raw):
    """产出 (marker, 整段字节, 数据区字节)；SOS 之后的压缩数据整块产出一次。"""
    pos = 2
    n = len(raw)
    while pos < n:
        if raw[pos] != 0xFF:
            pos += 1
            continue
        m = pos + 1
        while m < n and raw[m] == 0xFF:
            m += 1
        if m >= n:
            break
        mk = raw[m]
        if mk in (0xD8, 0x01) or 0xD0 <= mk <= 0xD7:
            pos = m + 1
            continue
        if mk == 0xD9:
            yield mk, raw[pos:m + 1], b""
            pos = m + 1
            continue
        if m + 3 > n:
            break
        size = struct.unpack_from(">H", raw, m + 1)[0]
        end = m + 1 + size
        if end > n:
            break
        if mk == 0xDA:   # SOS：后面直到文件尾都是压缩数据，整块带走
            yield mk, raw[pos:], raw[m + 3:end]
            return
        yield mk, raw[pos:end], raw[m + 3:end]
        pos = end


def write_jpeg(src, target, user_comment=None, strip=False):
    """
    重写 JPEG 的 EXIF UserComment。源图已有 EXIF 时完整保留
    （相机型号、光圈快门 ISO、GPS、缩略图都在），只替换 UserComment 这一条。
    """
    with open(src, "rb") as f:
        raw = f.read()
    if not raw.startswith(b"\xff\xd8"):
        raise WriteError("不是有效的 JPEG 文件")

    # 先找出源 EXIF
    src_tiff = None
    for mk, _whole, data in _iter_jpeg_segments(raw):
        if mk == _JPEG_APP1 and data.startswith(b"Exif\x00\x00"):
            src_tiff = data[6:]
            break

    new_app1 = None
    if not strip and user_comment is not None:
        parsed = exif_build.parse(src_tiff) if src_tiff else None
        if parsed is None:
            parsed = exif_build.new_tiff()
        # APP1 总长上限 64KB，UTF-16 会把体积翻倍，超了就退回 UTF-8
        uc = exif_build.encode_user_comment(user_comment, prefer_ascii_limit=_APP1_MAX - 4096)
        tiff = exif_build.build(parsed, uc)
        payload = b"Exif\x00\x00" + tiff
        if len(payload) + 2 > _APP1_MAX:
            # 还是太长：丢掉缩略图再试一次（缩略图通常占几 KB 到几十 KB）
            parsed["thumb"] = None
            parsed["ifd1"] = {}
            tiff = exif_build.build(parsed, uc)
            payload = b"Exif\x00\x00" + tiff
        if len(payload) + 2 > _APP1_MAX:
            raise WriteError("生成参数太长，超出 JPEG 元数据段 64KB 上限。"
                             "建议另存为 PNG（PNG 没有这个限制）。")
        new_app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload

    out = bytearray(b"\xff\xd8")
    inserted = False
    for mk, whole, data in _iter_jpeg_segments(raw):
        if mk == _JPEG_APP1 and data.startswith(b"Exif\x00\x00"):
            if new_app1 and not inserted:
                out += new_app1
                inserted = True
            continue           # 旧 EXIF 段丢弃
        if strip and mk == _JPEG_APP1 and (
                data.startswith(b"http://ns.adobe.com/xap/") or b"<x:xmpmeta" in data[:200]):
            continue           # XMP
        if strip and mk == _JPEG_COM:
            continue
        if mk == 0xDA and new_app1 and not inserted:
            out += new_app1    # 源图没有 EXIF：插在图像数据之前
            inserted = True
        out += whole
    if new_app1 and not inserted:
        out[2:2] = new_app1

    tmp = _tmp_for(target)
    try:
        with open(tmp, "wb") as f:
            f.write(bytes(out))
        _atomic_replace(tmp, target)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# ============================================================
# WebP
# ============================================================

def _riff_chunks(raw):
    pos = 12
    while pos + 8 <= len(raw):
        fourcc = raw[pos:pos + 4]
        size = struct.unpack_from("<I", raw, pos + 4)[0]
        padded = size + (size & 1)
        end = pos + 8 + padded
        if end > len(raw) + 1:
            break
        yield fourcc, raw[pos + 8:pos + 8 + size], raw[pos:min(end, len(raw))]
        pos = end


def _riff_chunk(fourcc, data):
    out = fourcc + struct.pack("<I", len(data)) + data
    if len(data) & 1:
        out += b"\x00"
    return out


def write_webp(src, target, user_comment=None, strip=False, width=0, height=0):
    """
    重写 WebP 的 EXIF chunk。

    简单格式的 WebP（只有 VP8/VP8L，没有 VP8X）本身装不下 EXIF，
    这时会自动升级成扩展格式：补一个 VP8X 头并打上 EXIF 标志位。
    像素数据（VP8/VP8L/ANMF 帧）原样搬运，动图不受影响。
    """
    with open(src, "rb") as f:
        raw = f.read()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
        raise WriteError("不是有效的 WebP 文件")

    src_tiff = None
    chunks = []
    for fourcc, data, _whole in _riff_chunks(raw):
        if fourcc == b"EXIF":
            body = data[6:] if data.startswith(b"Exif\x00\x00") else data
            src_tiff = src_tiff or body
            continue
        if strip and fourcc in (b"XMP ", b"ICCP"):
            continue
        chunks.append((fourcc, data))

    exif_bytes = None
    if not strip and user_comment is not None:
        parsed = exif_build.parse(src_tiff) if src_tiff else None
        if parsed is None:
            parsed = exif_build.new_tiff()
        exif_bytes = exif_build.build(parsed, exif_build.encode_user_comment(user_comment))

    has_vp8x = any(fc == b"VP8X" for fc, _ in chunks)
    if exif_bytes and not has_vp8x:
        if not (width and height):
            raise WriteError("无法读取 WebP 尺寸，不能补写扩展头")
        flags = 0b00001000   # EXIF present
        vp8x = (struct.pack("<B", flags) + b"\x00\x00\x00"
                + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little"))
        chunks.insert(0, (b"VP8X", vp8x))
    elif has_vp8x:
        for i, (fc, data) in enumerate(chunks):
            if fc == b"VP8X" and len(data) >= 1:
                flags = data[0]
                flags = (flags | 0b00001000) if exif_bytes else (flags & ~0b00001000)
                chunks[i] = (fc, bytes([flags]) + data[1:])
                break

    body = bytearray()
    for fc, data in chunks:
        body += _riff_chunk(fc, data)
    if exif_bytes:
        body += _riff_chunk(b"EXIF", exif_bytes)   # EXIF 按规范放在末尾

    out = b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + bytes(body)
    tmp = _tmp_for(target)
    try:
        with open(tmp, "wb") as f:
            f.write(out)
        _atomic_replace(tmp, target)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# ============================================================
# 格式探测
# ============================================================

def detect_format(path):
    try:
        with open(path, "rb") as f:
            magic = f.read(16)
    except OSError:
        return None
    if magic.startswith(PNG_SIGNATURE):
        return "PNG"
    if magic.startswith(b"\xff\xd8"):
        return "JPEG"
    if magic[:4] == b"RIFF" and magic[8:12] == b"WEBP":
        return "WEBP"
    return None


def count_png_frames(path):
    """PNG 的帧数（APNG 的 acTL 里写着）。不是动图返回 1。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return 1
    if not raw.startswith(PNG_SIGNATURE):
        return 1
    for ctype, data, _ in _iter_png_chunks(raw):
        if ctype == b"acTL" and len(data) >= 4:
            return struct.unpack(">I", data[:4])[0]
        if ctype == b"IDAT":
            break
    return 1


def count_webp_frames(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return 1
    if raw[:4] != b"RIFF":
        return 1
    n = sum(1 for fc, _d, _w in _riff_chunks(raw) if fc == b"ANMF")
    return n or 1
