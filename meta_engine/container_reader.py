# -*- coding: utf-8 -*-
"""
container_reader.py —— 用标准库把图片容器读成"PIL 风格"的 info 字典。

上游那套解析器（webui/comfyui/novelai/alt_generators）全部是以
`img.info` 这个字典 + EXIF 字节 为输入写的。原版靠 Pillow 提供这两样东西，
但这个启动器坚持不引入 Pillow（理由见 wd14_venv_manager 顶部那段：
numpy/PIL 这类库跟用户机器上其他 AI 工具抢版本，改一个坏一个）。

所以这里用 struct + zlib 重新实现"取 info"这一步，产出的字典跟 Pillow
的 img.info 键名完全对齐：
    PNG   -> tEXt / iTXt / zTXt 文本块，键名原样（parameters / prompt /
             workflow / Description / Comment / Software / Source ...）
    JPEG  -> APP1 里的 EXIF，解出 UserComment / ImageDescription / Software /
             Make / Model / Artist / Copyright，并把原始 TIFF 字节一并带出
    WebP  -> RIFF 容器里的 EXIF chunk（内部同样是 TIFF），另外解 XMP
另外统一给出 width / height / format。

跟原版的差异只有一处：exif_raw 给的是**去掉 "Exif\\0\\0" 头之后的 TIFF 字节**，
而 Pillow 给的是带头的。上游只在 piexif 可用时才碰这个字段，而 piexif 在这个
启动器里本来就不存在，所以这点差异不会影响任何行为——EXIF 里的字符串字段
由本模块自己解出来、直接并进 info，走的是"info 自有键优先"那条路。
"""
import json
import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# EXIF 里需要提取的字符串标签 -> 上游探测器使用的键名
_EXIF_STRING_TAGS = {
    0x010E: "ImageDescription",
    0x010F: "Make",
    0x0110: "Model",
    0x0131: "Software",
    0x013B: "Artist",
    0x8298: "Copyright",
}
_TAG_USER_COMMENT = 0x9286
_TAG_EXIF_IFD_PTR = 0x8769
_TAG_XMP = 0x02BC

_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8, 11: 4, 12: 8}

# 单个文本块上限：正常的 parameters/workflow 再大也就几百 KB，
# 超过这个数基本是坏文件或攻击构造，直接跳过
_MAX_TEXT_CHUNK = 32 << 20


# ============================================================
# PNG
# ============================================================

def _read_png(path):
    info = {}
    width = height = 0
    try:
        with open(path, "rb") as f:
            if f.read(8) != PNG_SIGNATURE:
                return None
            for _ in range(65536):
                head = f.read(8)
                if len(head) < 8:
                    break
                length, ctype = struct.unpack(">I4s", head)
                if length > _MAX_TEXT_CHUNK:
                    f.seek(length + 4, 1)
                    continue
                if ctype == b"IHDR":
                    data = f.read(length)
                    if len(data) >= 8:
                        width, height = struct.unpack(">II", data[:8])
                    f.seek(4, 1)
                    continue
                if ctype in (b"tEXt", b"iTXt", b"zTXt", b"eXIf"):
                    data = f.read(length)
                    f.seek(4, 1)
                    _absorb_png_text(info, ctype, data)
                    continue
                if ctype == b"IEND":
                    break
                f.seek(length + 4, 1)
    except OSError:
        return None
    return {"info": info, "width": width, "height": height, "format": "PNG"}


def _absorb_png_text(info, ctype, data):
    try:
        if ctype == b"tEXt":
            k, v = data.split(b"\x00", 1)
            # 先按 UTF-8 试（ComfyUI/部分工具即使用 tEXt 也写 UTF-8），
            # 失败再退回规范要求的 latin-1，避免中文提示词变乱码
            info[k.decode("latin-1")] = _best_text(v)
        elif ctype == b"iTXt":
            # keyword\0 comp_flag(1) comp_method(1) lang\0 translated\0 text
            k, rest = data.split(b"\x00", 1)
            comp_flag = rest[0]
            rest = rest[2:]
            _lang, rest = rest.split(b"\x00", 1)
            _trans, text = rest.split(b"\x00", 1)
            if comp_flag:
                text = zlib.decompressobj().decompress(text, _MAX_TEXT_CHUNK)
            info[k.decode("latin-1")] = text.decode("utf-8", "replace")
        elif ctype == b"zTXt":
            k, rest = data.split(b"\x00", 1)
            text = zlib.decompressobj().decompress(rest[1:], _MAX_TEXT_CHUNK)
            info[k.decode("latin-1")] = _best_text(text)
        elif ctype == b"eXIf":
            # PNG 也可以带 EXIF（少见，但 NovelAI 的部分导出会用）
            parsed = parse_tiff(data)
            if parsed:
                for key, val in parsed["strings"].items():
                    info.setdefault(key, val)
                if parsed.get("usercomment"):
                    info.setdefault("_usercomment", parsed["usercomment"])
                info.setdefault("_exif_tiff", data)
    except (ValueError, IndexError, zlib.error, UnicodeDecodeError):
        pass


def _best_text(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


# ============================================================
# TIFF / EXIF
# ============================================================

def parse_tiff(tiff):
    """
    解析 TIFF（EXIF 主体）字节，返回
        {"strings": {标签名: 文本}, "usercomment": 文本或 None, "xmp": 文本或 None}
    不是合法 TIFF 时返回 None。
    """
    if len(tiff) < 8:
        return None
    if tiff[:2] == b"II":
        endian = "<"
    elif tiff[:2] == b"MM":
        endian = ">"
    else:
        return None

    def u16(off):
        return struct.unpack_from(endian + "H", tiff, off)[0]

    def u32(off):
        return struct.unpack_from(endian + "I", tiff, off)[0]

    strings, raw_values = {}, {}

    def read_ifd(ifd_off, depth=0):
        if depth > 4 or ifd_off <= 0 or ifd_off + 2 > len(tiff):
            return
        try:
            count = u16(ifd_off)
        except struct.error:
            return
        if count > 4096:
            return
        for i in range(count):
            entry = ifd_off + 2 + i * 12
            if entry + 12 > len(tiff):
                return
            try:
                tag, ftype, num = u16(entry), u16(entry + 2), u32(entry + 4)
            except struct.error:
                return
            size = _TYPE_SIZES.get(ftype, 1) * num
            if size > len(tiff):
                continue
            value_off = entry + 8 if size <= 4 else (u32(entry + 8) if entry + 12 <= len(tiff) else -1)
            if value_off < 0 or value_off + size > len(tiff):
                if tag != _TAG_EXIF_IFD_PTR:
                    continue
            if tag == _TAG_EXIF_IFD_PTR:
                try:
                    read_ifd(u32(entry + 8), depth + 1)
                except struct.error:
                    pass
                continue
            raw = tiff[value_off:value_off + size]
            if tag in _EXIF_STRING_TAGS:
                text = raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
                if text:
                    strings.setdefault(_EXIF_STRING_TAGS[tag], text)
            elif tag in (_TAG_USER_COMMENT, _TAG_XMP):
                raw_values.setdefault(tag, raw)

    try:
        read_ifd(u32(4))
    except struct.error:
        return None

    uc = _decode_user_comment(raw_values.get(_TAG_USER_COMMENT), endian)
    xmp = None
    if raw_values.get(_TAG_XMP):
        xmp = raw_values[_TAG_XMP].split(b"\x00", 1)[0].decode("utf-8", "replace").strip() or None
    return {"strings": strings, "usercomment": uc, "xmp": xmp}


def _decode_user_comment(raw, endian):
    """
    UserComment 前 8 字节是字符集声明。这里把 piexif 那套解码规则原样实现了一遍，
    额外兼容两种常见的野路子写法：
      - ComfyUI 的 JpegExport 等节点会写 'charset="UTF-8" ' 这种 8 字节前缀
      - 有些工具干脆不写前缀，整段就是 UTF-8
    """
    if not raw:
        return None
    prefix, body = raw[:8], raw[8:]
    text = None
    if prefix.startswith(b"UNICODE"):
        # A1111 写 UTF-16，字节序理论上跟 TIFF 一致；但实测有反着写的，
        # 所以按 BOM / 零字节分布先猜一次，再拿另一个兜底
        if body.startswith(b"\xfe\xff") or (len(body) >= 2 and body[0] == 0 and body[1] != 0):
            order = ("utf-16-be", "utf-16-le")
        elif body.startswith(b"\xff\xfe"):
            order = ("utf-16-le", "utf-16-be")
        else:
            order = ("utf-16-le", "utf-16-be") if endian == "<" else ("utf-16-be", "utf-16-le")
        for enc in order:
            try:
                cand = body.decode(enc).strip("\x00\ufeff").strip()
            except UnicodeDecodeError:
                continue
            if cand:
                text = cand
                break
    elif prefix.startswith(b"ASCII") or prefix == b"\x00" * 8 or prefix.lower().startswith(b"charset"):
        text = body.decode("utf-8", "replace").strip("\x00").strip()
    if not text:
        text = raw.decode("utf-8", "replace").strip("\x00").strip()
    return text or None


# ============================================================
# JPEG
# ============================================================

def _read_jpeg(path):
    info = {}
    width = height = 0
    exif_tiff = None
    comments = []
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None
            while True:
                b = f.read(1)
                if not b:
                    break
                if b != b"\xff":
                    continue
                # 连续的 0xFF 是填充字节，跳到真正的 marker
                marker = f.read(1)
                while marker == b"\xff":
                    marker = f.read(1)
                if not marker:
                    break
                mk = marker[0]
                if mk in (0xD8, 0x01) or 0xD0 <= mk <= 0xD7:
                    continue
                if mk == 0xD9:  # EOI
                    break
                size_raw = f.read(2)
                if len(size_raw) < 2:
                    break
                size = struct.unpack(">H", size_raw)[0] - 2
                if size < 0:
                    break
                if mk == 0xDA:  # SOS：后面全是压缩数据，元数据到此为止
                    break
                data = f.read(size)
                if mk == 0xE1:
                    if data.startswith(b"Exif\x00\x00"):
                        exif_tiff = exif_tiff or data[6:]
                    elif b"<x:xmpmeta" in data[:200] or data.startswith(b"http://ns.adobe.com/xap/"):
                        info.setdefault("XML:com.adobe.xmp",
                                        data.decode("utf-8", "replace"))
                elif mk == 0xFE:  # COM 注释段，部分工具把参数写这里
                    txt = data.decode("utf-8", "replace").strip("\x00").strip()
                    if txt:
                        comments.append(txt)
                elif 0xC0 <= mk <= 0xCF and mk not in (0xC4, 0xC8, 0xCC):
                    if len(data) >= 5:
                        height, width = struct.unpack(">HH", data[1:5])
    except OSError:
        return None

    if exif_tiff:
        parsed = parse_tiff(exif_tiff)
        if parsed:
            info.update(parsed["strings"])
            if parsed.get("usercomment"):
                info["_usercomment"] = parsed["usercomment"]
            if parsed.get("xmp"):
                info.setdefault("XML:com.adobe.xmp", parsed["xmp"])
        info["_exif_tiff"] = exif_tiff
    if comments:
        info.setdefault("_jpeg_comment", "\n".join(comments))
    return {"info": info, "width": width, "height": height, "format": "JPEG"}


# ============================================================
# WebP
# ============================================================

def _read_webp(path):
    info = {}
    width = height = 0
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
                return None
            for _ in range(4096):
                chdr = f.read(8)
                if len(chdr) < 8:
                    break
                fourcc, size = chdr[:4], struct.unpack("<I", chdr[4:])[0]
                padded = size + (size & 1)
                if fourcc in (b"EXIF", b"XMP "):
                    data = f.read(padded)[:size]
                    if fourcc == b"EXIF":
                        body = data[6:] if data.startswith(b"Exif\x00\x00") else data
                        parsed = parse_tiff(body)
                        if parsed:
                            info.update(parsed["strings"])
                            if parsed.get("usercomment"):
                                info["_usercomment"] = parsed["usercomment"]
                        info["_exif_tiff"] = body
                    else:
                        info.setdefault("XML:com.adobe.xmp", data.decode("utf-8", "replace"))
                    continue
                if fourcc == b"VP8X":
                    data = f.read(padded)
                    if len(data) >= 10:
                        width = int.from_bytes(data[4:7], "little") + 1
                        height = int.from_bytes(data[7:10], "little") + 1
                    continue
                if fourcc == b"VP8 " and not width:
                    data = f.read(padded)
                    if len(data) >= 10 and data[3:6] == b"\x9d\x01\x2a":
                        width = struct.unpack_from("<H", data, 6)[0] & 0x3FFF
                        height = struct.unpack_from("<H", data, 8)[0] & 0x3FFF
                    continue
                if fourcc == b"VP8L" and not width:
                    data = f.read(padded)
                    if len(data) >= 5 and data[0] == 0x2F:
                        bits = int.from_bytes(data[1:5], "little")
                        width = (bits & 0x3FFF) + 1
                        height = ((bits >> 14) & 0x3FFF) + 1
                    continue
                f.seek(padded, 1)
    except OSError:
        return None
    return {"info": info, "width": width, "height": height, "format": "WEBP"}


# ============================================================
# 视频：MP4 / MOV（ISO-BMFF）和 MKV / WebM（Matroska）
# ============================================================
# Forge Neo（含 H3 分支）保存视频时用 ffmpeg -metadata description=<A1111 参数>，
# mp4 里落在 moov/udta/meta/ilst/desc，mkv 里落在 Tags/SimpleTag(DESCRIPTION)。
# ComfyUI 的视频则把 prompt / workflow 写进元数据键（或 comment 里的一段 JSON）。
# 这里统一抠成 PNG 那样的 info 键：参数文本 → "parameters"，ComfyUI → "prompt"/"workflow"，
# 后面的解析调度和门禁跟图片完全共用。
#
# 只按需跳着读：moov 经常在文件末尾（在几百 MB 的 mdat 后面），不能整个文件读进内存。

_MP4_TEXT_KEYS = (            # 按优先顺序：越靠前越可能是生成参数
    "desc", "\xa9des", "ldes", "description", "\xa9cmt", "comment", "parameters", "\xa9inf",
)
_MAX_MOOV = 64 * 1024 * 1024


def _iter_boxes(buf, start, end):
    """在内存缓冲里遍历 ISO-BMFF box，产出 (类型, 内容起点, 内容终点)"""
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        typ = buf[pos + 4:pos + 8].decode("latin-1")
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield typ, pos + hdr, pos + size
        pos += size


def _mp4_data_text(buf, start, end):
    """ilst 条目里的 data 子 box → 文本"""
    for typ, s, e in _iter_boxes(buf, start, end):
        if typ == "data" and e - s >= 8:
            return _best_text(bytes(buf[s + 8:e]))
    return None


def _mp4_meta_items(buf, start, end):
    """meta（full box）→ {键: 文本}。同时支持 iTunes 风格（©des、desc）和 mdta keys 风格"""
    out = {}
    keys, handler, ilst = [], "", None
    for typ, s, e in _iter_boxes(buf, start + 4, end):          # 跳过 version/flags
        if typ == "hdlr" and e - s >= 12:
            handler = buf[s + 8:s + 12].decode("latin-1")
        elif typ == "keys" and e - s >= 8:
            n = struct.unpack(">I", buf[s + 4:s + 8])[0]
            p = s + 8
            for _ in range(n):
                if p + 8 > e:
                    break
                ks = struct.unpack(">I", buf[p:p + 4])[0]
                keys.append(buf[p + 8:p + ks].decode("utf-8", "replace"))
                p += ks
        elif typ == "ilst":
            ilst = (s, e)
    if ilst:
        for typ, s, e in _iter_boxes(buf, ilst[0], ilst[1]):
            name = typ
            if handler == "mdta" and keys:
                # mdta 风格的条目类型是 1 起的整数下标
                idx = struct.unpack(">I", typ.encode("latin-1"))[0]
                if 1 <= idx <= len(keys):
                    name = keys[idx - 1].split(".")[-1] if keys[idx - 1].startswith("com.") else keys[idx - 1]
            text = _mp4_data_text(buf, s, e)
            if text:
                out.setdefault(name, text)
    return out


def _qt_udta_text(buf, s, e):
    """QuickTime 老式 udta 文本（©cmt 等直接挂在 udta 下）：[len16][lang16][text]"""
    if e - s >= 4:
        n = struct.unpack(">H", buf[s:s + 2])[0]
        if 0 < n <= e - s - 4:
            return _best_text(bytes(buf[s + 4:s + 4 + n]))
    return None


def _read_mp4(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            pos, moov = 0, None
            while pos + 8 <= fsize:
                f.seek(pos)
                head = f.read(16)
                if len(head) < 8:
                    break
                size = struct.unpack(">I", head[:4])[0]
                typ = head[4:8]
                if size == 1 and len(head) >= 16:
                    size = struct.unpack(">Q", head[8:16])[0]
                elif size == 0:
                    size = fsize - pos
                if size < 8:
                    break
                if typ == b"moov":
                    if size > _MAX_MOOV:
                        return None
                    f.seek(pos)
                    moov = f.read(size)
                    break
                pos += size
    except OSError:
        return None
    if not moov:
        return None
    return _parse_moov(moov)


def _parse_moov(moov):
    """moov box 的完整字节 → 容器结构（前端 JS 版本同一套逻辑）"""
    buf = bytes(moov)
    texts = {}
    width = height = 0
    duration = None
    hdr = 16 if struct.unpack(">I", moov[:4])[0] == 1 else 8
    for typ, s, e in _iter_boxes(buf, hdr, len(moov)):
        if typ == "mvhd" and e - s >= 20:
            ver = buf[s]
            if ver == 1 and e - s >= 32:
                ts, dur = struct.unpack(">IQ", buf[s + 20:s + 32])
            else:
                ts, dur = struct.unpack(">II", buf[s + 12:s + 20])
            if ts:
                duration = dur / ts
        elif typ == "trak":
            for t2, s2, e2 in _iter_boxes(buf, s, e):
                if t2 == "tkhd" and e2 - s2 >= 84:
                    off = s2 + (88 if buf[s2] == 1 else 76)
                    if off + 8 <= e2:
                        w, h = struct.unpack(">II", buf[off:off + 8])
                        w, h = w >> 16, h >> 16
                        if w * h > width * height:
                            width, height = w, h
        elif typ == "udta":
            for t2, s2, e2 in _iter_boxes(buf, s, e):
                if t2 == "meta":
                    for k, v in _mp4_meta_items(buf, s2, e2).items():
                        texts.setdefault(k, v)
                elif t2.startswith("\xa9"):
                    v = _qt_udta_text(buf, s2, e2)
                    if v:
                        texts.setdefault(t2, v)
        elif typ == "meta":
            for k, v in _mp4_meta_items(buf, s, e).items():
                texts.setdefault(k, v)
    return {"info": _video_info(texts), "width": width, "height": height,
            "format": _video_format("MP4", duration)}


# ---- Matroska / WebM（EBML）----

def _ebml_vint(f):
    """读一个 EBML 变长整数，返回 (值, 长度, 是否「未知长度」)；读不到返回 (None, 0, False)"""
    b = f.read(1)
    if not b:
        return None, 0, False
    first = b[0]
    mask, length = 0x80, 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8:
        return None, 0, False
    val = first & (mask - 1)
    rest = f.read(length - 1)
    if len(rest) < length - 1:
        return None, 0, False
    for c in rest:
        val = (val << 8) | c
    unknown = val == (1 << (7 * length)) - 1
    return val, length, unknown


def _ebml_id(f):
    b = f.read(1)
    if not b:
        return None
    first = b[0]
    mask, length = 0x80, 1
    while length <= 4 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 4:
        return None
    rest = f.read(length - 1)
    val = first
    for c in rest:
        val = (val << 8) | c
    return val


def _ebml_children(f, start, end):
    """产出 (id, 内容起点, 内容长度)；长度未知时 size=None"""
    pos = start
    while end is None or pos < end:
        f.seek(pos)
        eid = _ebml_id(f)
        if eid is None:
            return
        size, _n, unknown = _ebml_vint(f)
        if size is None:
            return
        dstart = f.tell()
        yield eid, dstart, (None if unknown else size)
        if unknown:
            return
        pos = dstart + size


def _ebml_uint(f, s, n):
    f.seek(s)
    v = 0
    for c in f.read(min(n, 8)):
        v = (v << 8) | c
    return v


def _read_mkv(path):
    texts, width, height = {}, 0, 0
    duration, scale = None, 1000000
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            seg = None
            for eid, s, n in _ebml_children(f, 0, fsize):
                if eid == 0x18538067:                 # Segment
                    seg = (s, (s + n) if n is not None else fsize)
                    break
            if not seg:
                return None
            seg_start, seg_end = seg
            todo, seen = [], set()

            def visit(eid, s, n):
                nonlocal width, height, duration, scale
                if eid == 0x114D9B74:                 # SeekHead → 跳到 Tags / Tracks / Info
                    for sid, ss, sn in _ebml_children(f, s, s + (n or 0)):
                        if sid != 0x4DBB:
                            continue
                        target, tpos = None, None
                        for cid, cs, cn in _ebml_children(f, ss, ss + (sn or 0)):
                            if cid == 0x53AB:
                                f.seek(cs)
                                target = int.from_bytes(f.read(cn or 0), "big")
                            elif cid == 0x53AC:
                                tpos = _ebml_uint(f, cs, cn or 0)
                        if target in (0x1254C367, 0x1654AE6B, 0x1549A966) and tpos is not None:
                            todo.append(seg_start + tpos)
                elif eid == 0x1549A966:               # Info
                    for cid, cs, cn in _ebml_children(f, s, s + (n or 0)):
                        if cid == 0x2AD7B1:
                            scale = _ebml_uint(f, cs, cn) or scale
                        elif cid == 0x4489 and cn in (4, 8):
                            f.seek(cs)
                            duration = struct.unpack(">f" if cn == 4 else ">d", f.read(cn))[0]
                elif eid == 0x1654AE6B:               # Tracks
                    for tid, ts, tn in _ebml_children(f, s, s + (n or 0)):
                        if tid != 0xAE:
                            continue
                        for vid, vs, vn in _ebml_children(f, ts, ts + (tn or 0)):
                            if vid == 0xE0:
                                w = h = 0
                                for pid, ps, pn in _ebml_children(f, vs, vs + (vn or 0)):
                                    if pid == 0xB0:
                                        w = _ebml_uint(f, ps, pn)
                                    elif pid == 0xBA:
                                        h = _ebml_uint(f, ps, pn)
                                if w * h > width * height:
                                    width, height = w, h
                elif eid == 0x1254C367:               # Tags
                    for tid, ts, tn in _ebml_children(f, s, s + (n or 0)):
                        if tid != 0x7373:
                            continue
                        for sid, ss, sn in _ebml_children(f, ts, ts + (tn or 0)):
                            if sid != 0x67C8:
                                continue
                            name = val = None
                            for cid, cs, cn in _ebml_children(f, ss, ss + (sn or 0)):
                                if cid in (0x45A3, 0x4487) and cn is not None and cn <= 32 * 1024 * 1024:
                                    f.seek(cs)
                                    txt = f.read(cn).decode("utf-8", "replace").rstrip("\x00")
                                    if cid == 0x45A3:
                                        name = txt
                                    else:
                                        val = txt
                            if name and val:
                                texts.setdefault(name.lower(), val)

            for eid, s, n in _ebml_children(f, seg_start, seg_end):
                seen.add(s)
                if eid == 0x1F43B675:                 # Cluster：后面全是画面数据，交给 SeekHead
                    break
                visit(eid, s, n)
            for pos in todo:
                if pos in seen:
                    continue
                for eid, s, n in _ebml_children(f, pos, None):
                    visit(eid, s, n)
                    break
    except (OSError, struct.error, ValueError):
        return None
    if duration is not None:
        duration = duration * scale / 1e9
    fmt = "WEBM" if path.lower().endswith(".webm") else "MKV"
    return {"info": _video_info(texts), "width": width, "height": height,
            "format": _video_format(fmt, duration)}


def _video_format(name, duration):
    return f"{name} 视频 {duration:.1f} 秒" if duration else f"{name} 视频"


def _video_info(texts):
    """视频里抠出来的元数据文本 → PNG 风格的 info 键"""
    info = {}
    low = {k.lower().lstrip("\xa9"): v for k, v in texts.items()}
    # ComfyUI：直接的 prompt / workflow 键
    for k in ("prompt", "workflow"):
        if isinstance(low.get(k), str) and low[k].strip().startswith("{"):
            info[k] = low[k]
    # ComfyUI（VideoHelperSuite 等）：comment 里一整段 JSON
    for k in ("comment", "cmt", "description", "des", "desc"):
        v = low.get(k)
        if isinstance(v, str) and v.strip().startswith("{") and ("prompt" not in info):
            try:
                obj = json.loads(v)
            except ValueError:
                obj = None
            if isinstance(obj, dict) and ("prompt" in obj or "workflow" in obj):
                for kk in ("prompt", "workflow"):
                    if kk in obj:
                        info[kk] = obj[kk] if isinstance(obj[kk], str) else json.dumps(obj[kk], ensure_ascii=False)
    if "prompt" in info or "workflow" in info:
        return info
    # WebUI / Forge：描述里的 A1111 参数文本
    for k in _MP4_TEXT_KEYS:
        kk = k.lower().lstrip("\xa9")
        v = low.get(kk)
        if isinstance(v, str) and v.strip():
            info["parameters"] = v
            break
    return info


VIDEO_EXTS = (".mp4", ".m4v", ".mov", ".mkv", ".webm")


# ============================================================
# 统一入口
# ============================================================

def read_container(path):
    """
    返回 {"info": dict, "width": int, "height": int, "format": str}。
    格式按魔数判断（不信任扩展名——用户手动改过扩展名的图很常见）。
    完全无法识别时返回 None。
    """
    try:
        with open(path, "rb") as f:
            magic = f.read(16)
    except OSError:
        return None
    if magic.startswith(PNG_SIGNATURE):
        return _read_png(path)
    if magic.startswith(b"\xff\xd8"):
        return _read_jpeg(path)
    if magic[:4] == b"RIFF" and magic[8:12] == b"WEBP":
        return _read_webp(path)
    if magic[4:8] == b"ftyp":
        return _read_mp4(path)
    if magic.startswith(b"\x1a\x45\xdf\xa3"):
        return _read_mkv(path)
    return None
