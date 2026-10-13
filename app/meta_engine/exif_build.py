# -*- coding: utf-8 -*-
"""
exif_build.py —— TIFF/EXIF 结构的解析与重建（纯标准库）。

用途：往 JPEG / WebP 里写 UserComment（生成参数的载体），同时**完整保留
源文件里原有的 EXIF**——光圈、快门、ISO、相机型号、GPS、缩略图，一个不丢。

为什么要自己写一个 TIFF 重排器，而不是"拼一个只有 UserComment 的新 EXIF"：
后者实现起来五行就够，但会把相机信息整段抹掉。对一张手机拍的照片来说，
"改了下备注，拍摄参数没了"是不可接受的数据损失。

做法是老老实实的重排：把所有 IFD（IFD0 / ExifIFD / GPS / Interop / IFD1
缩略图）的条目原样搬过来，只替换 UserComment 那一条，然后按新布局重新
计算所有 value offset。缩略图的 JPEGInterchangeFormat 偏移也跟着修。
条目的类型、数量一律不动，所以任何我们不认识的厂商私有标签也能完整带过去。
"""
import struct

TAG_EXIF_IFD = 0x8769
TAG_GPS_IFD = 0x8825
TAG_INTEROP_IFD = 0xA005
TAG_USER_COMMENT = 0x9286
TAG_THUMB_OFFSET = 0x0201
TAG_THUMB_LENGTH = 0x0202

_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}

_SUB_IFD_TAGS = (TAG_EXIF_IFD, TAG_GPS_IFD, TAG_INTEROP_IFD)


class TiffError(Exception):
    pass


# ============================================================
# 解析
# ============================================================

def parse(tiff):
    """
    把 TIFF 字节解析成可编辑的结构：
        {"endian": "<", "ifd0": {tag: (type, count, raw_bytes)},
         "exif": {...}, "gps": {...}, "interop": {...},
         "ifd1": {...}, "thumb": bytes 或 None}
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

    def u16(o):
        return struct.unpack_from(endian + "H", tiff, o)[0]

    def u32(o):
        return struct.unpack_from(endian + "I", tiff, o)[0]

    def read_ifd(off):
        """返回 ({tag: (type, count, raw)}, next_ifd_offset)"""
        entries = {}
        if off <= 0 or off + 2 > len(tiff):
            return entries, 0
        try:
            count = u16(off)
        except struct.error:
            return entries, 0
        if count > 4096:
            return entries, 0
        for i in range(count):
            e = off + 2 + i * 12
            if e + 12 > len(tiff):
                break
            tag, ftype, num = u16(e), u16(e + 2), u32(e + 4)
            size = _TYPE_SIZES.get(ftype, 1) * num
            if size <= 4:
                raw = tiff[e + 8:e + 8 + size]
            else:
                vo = u32(e + 8)
                if vo + size > len(tiff):
                    continue
                raw = tiff[vo:vo + size]
            entries[tag] = (ftype, num, raw)
        nxt_off = off + 2 + count * 12
        nxt = u32(nxt_off) if nxt_off + 4 <= len(tiff) else 0
        return entries, nxt

    try:
        ifd0, next_off = read_ifd(u32(4))
    except struct.error:
        return None

    out = {"endian": endian, "ifd0": ifd0, "exif": {}, "gps": {},
           "interop": {}, "ifd1": {}, "thumb": None}

    for tag, key in ((TAG_EXIF_IFD, "exif"), (TAG_GPS_IFD, "gps")):
        if tag in ifd0:
            ftype, num, raw = ifd0.pop(tag)
            if len(raw) >= 4:
                sub, _ = read_ifd(struct.unpack(endian + "I", raw[:4])[0])
                out[key] = sub

    if TAG_INTEROP_IFD in out["exif"]:
        _t, _n, raw = out["exif"].pop(TAG_INTEROP_IFD)
        if len(raw) >= 4:
            sub, _ = read_ifd(struct.unpack(endian + "I", raw[:4])[0])
            out["interop"] = sub

    # IFD1 = 缩略图
    if next_off:
        ifd1, _ = read_ifd(next_off)
        thumb_off = ifd1.pop(TAG_THUMB_OFFSET, None)
        thumb_len = ifd1.pop(TAG_THUMB_LENGTH, None)
        out["ifd1"] = ifd1
        if thumb_off and thumb_len:
            try:
                o = struct.unpack(endian + "I", thumb_off[2][:4])[0]
                n = struct.unpack(endian + "I", thumb_len[2][:4])[0]
                if 0 < n <= len(tiff) and o + n <= len(tiff):
                    out["thumb"] = tiff[o:o + n]
            except (struct.error, IndexError):
                pass
    return out


# ============================================================
# 重建
# ============================================================

def _pack_ifd(entries, endian, base, value_pool, extra_pointers=None):
    """
    序列化一个 IFD。
    entries: {tag: (type, count, raw)}
    base:    本 IFD 在 TIFF 里的起始偏移
    value_pool: 用来收集超过 4 字节的值（list of (占位标记, bytes)）
    extra_pointers: {tag: 待回填的子 IFD 指针占位} —— 返回时告知各占位所在字节位置
    返回 (ifd_bytes, {tag: 该 tag 的 value 字段在 ifd_bytes 内的相对位置})
    """
    extra_pointers = extra_pointers or {}
    all_tags = sorted(set(entries) | set(extra_pointers))   # TIFF 要求 tag 升序
    out = bytearray(struct.pack(endian + "H", len(all_tags)))
    value_slots = {}
    for tag in all_tags:
        if tag in extra_pointers:
            ftype, num, raw = 4, 1, b"\x00\x00\x00\x00"
        else:
            ftype, num, raw = entries[tag]
        size = _TYPE_SIZES.get(ftype, 1) * num
        out += struct.pack(endian + "HHI", tag, ftype, num)
        pos = len(out)
        if size <= 4:
            out += raw[:4].ljust(4, b"\x00")
        else:
            value_pool.append((tag, raw))
            out += b"\x00\x00\x00\x00"   # 稍后回填
        value_slots[tag] = (pos, size)
    return out, value_slots


def build(parsed, user_comment_bytes=None):
    """
    按解析结果重建 TIFF 字节。user_comment_bytes 非 None 时替换（或新建）
    ExifIFD 里的 UserComment。
    """
    endian = parsed.get("endian", "<")
    ifd0 = dict(parsed.get("ifd0") or {})
    exif = dict(parsed.get("exif") or {})
    gps = dict(parsed.get("gps") or {})
    interop = dict(parsed.get("interop") or {})
    ifd1 = dict(parsed.get("ifd1") or {})
    thumb = parsed.get("thumb")

    if user_comment_bytes is not None:
        exif[TAG_USER_COMMENT] = (7, len(user_comment_bytes), user_comment_bytes)

    # 布局：header(8) | IFD0 | [IFD0 values] | ExifIFD | [values] | GPS | ... | IFD1 | thumb
    # 先算各 IFD 的字节长度（条目数固定，长度可预知），再依次分配 value 区
    def ifd_len(entry_count):
        return 2 + entry_count * 12 + 4

    has_exif, has_gps, has_interop = bool(exif), bool(gps), bool(interop)
    ptr_tags = {}
    if has_exif:
        ptr_tags[TAG_EXIF_IFD] = None
    if has_gps:
        ptr_tags[TAG_GPS_IFD] = None

    def big_values(entries):
        return [(t, r) for t, (ft, n, r) in sorted(entries.items())
                if _TYPE_SIZES.get(ft, 1) * n > 4]

    def big_size(entries):
        total = 0
        for _t, r in big_values(entries):
            total += len(r) + (len(r) & 1)   # 值区按偶数字节对齐
        return total

    interop_ptr_in_exif = {TAG_INTEROP_IFD: None} if has_interop else {}

    n0 = len(set(ifd0) | set(ptr_tags))
    off_ifd0 = 8
    off_ifd0_vals = off_ifd0 + ifd_len(n0)
    off_exif = off_ifd0_vals + big_size(ifd0)

    n_exif = len(set(exif) | set(interop_ptr_in_exif))
    off_exif_vals = off_exif + ifd_len(n_exif) if has_exif else off_exif
    off_gps = off_exif_vals + (big_size(exif) if has_exif else 0)

    off_gps_vals = off_gps + ifd_len(len(gps)) if has_gps else off_gps
    off_interop = off_gps_vals + (big_size(gps) if has_gps else 0)

    off_interop_vals = off_interop + ifd_len(len(interop)) if has_interop else off_interop
    off_ifd1 = off_interop_vals + (big_size(interop) if has_interop else 0)

    if ifd1 or thumb:
        n1 = len(ifd1) + (2 if thumb else 0)
        off_ifd1_vals = off_ifd1 + ifd_len(n1)
        off_thumb = off_ifd1_vals + big_size(ifd1)
    else:
        off_ifd1 = 0
        off_ifd1_vals = off_thumb = 0

    # 真正开始拼
    buf = bytearray(b"II" if endian == "<" else b"MM")
    buf += struct.pack(endian + "H", 42)
    buf += struct.pack(endian + "I", off_ifd0)

    def emit(entries, base, extra_ptrs, next_ifd):
        pool = []
        body, slots = _pack_ifd(entries, endian, base, pool, extra_ptrs)
        body += struct.pack(endian + "I", next_ifd)
        val_base = base + len(body)
        vals = bytearray()
        for tag, raw in pool:
            pos = slots[tag][0]
            struct.pack_into(endian + "I", body, pos, val_base + len(vals))
            vals += raw
            if len(raw) & 1:
                vals += b"\x00"
        return body, vals, slots

    ifd0_body, ifd0_vals, ifd0_slots = emit(
        ifd0, off_ifd0, {t: None for t in ptr_tags}, off_ifd1)
    if has_exif:
        struct.pack_into(endian + "I", ifd0_body, ifd0_slots[TAG_EXIF_IFD][0], off_exif)
    if has_gps:
        struct.pack_into(endian + "I", ifd0_body, ifd0_slots[TAG_GPS_IFD][0], off_gps)
    buf += ifd0_body + ifd0_vals

    if has_exif:
        exif_body, exif_vals, exif_slots = emit(exif, off_exif, interop_ptr_in_exif, 0)
        if has_interop:
            struct.pack_into(endian + "I", exif_body, exif_slots[TAG_INTEROP_IFD][0], off_interop)
        buf += exif_body + exif_vals
    if has_gps:
        b_, v_, _ = emit(gps, off_gps, {}, 0)
        buf += b_ + v_
    if has_interop:
        b_, v_, _ = emit(interop, off_interop, {}, 0)
        buf += b_ + v_

    if off_ifd1:
        e1 = dict(ifd1)
        if thumb:
            e1[TAG_THUMB_OFFSET] = (4, 1, struct.pack(endian + "I", off_thumb))
            e1[TAG_THUMB_LENGTH] = (4, 1, struct.pack(endian + "I", len(thumb)))
        b_, v_, _ = emit(e1, off_ifd1, {}, 0)
        buf += b_ + v_
        if thumb:
            buf += thumb

    return bytes(buf)


def encode_user_comment(text, prefer_ascii_limit=None):
    """
    把文本编成 UserComment 的字节形态。

    默认走 UNICODE + UTF-16LE（A1111 就是这么写的，读取端兼容性最好）。
    prefer_ascii_limit 给了值且 UTF-16 超出这个字节数时，改用 ASCII 前缀 +
    UTF-8 并截断 —— 这是 JPEG 专用的保险：APP1 段总长受 64KB 限制，
    UTF-16 正好把体积翻倍，长提示词很容易撞上限。
    """
    uc = b"UNICODE\x00" + text.encode("utf-16le", "surrogatepass")
    if prefer_ascii_limit is not None and len(uc) > prefer_ascii_limit:
        body = text.encode("utf-8", "ignore")[:prefer_ascii_limit]
        uc = b"ASCII\x00\x00\x00" + body
    return uc


def new_tiff(endian="<"):
    """没有源 EXIF 时用的空壳。"""
    return {"endian": endian, "ifd0": {}, "exif": {}, "gps": {},
            "interop": {}, "ifd1": {}, "thumb": None}
