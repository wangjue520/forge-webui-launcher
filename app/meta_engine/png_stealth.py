"""
png_stealth.py - 隐写 PNG (Stealth PNG Info) 元数据解码器

解码写入像素通道 LSB 的隐写元数据 (WebUI stealth-pnginfo / NovelAI 隐写下载), 4 种签名:
  stealth_pnginfo / stealth_pngcomp  -> alpha 通道 (comp = gzip 压缩)
  stealth_rgbinfo  / stealth_rgbcomp -> RGB 通道

设计要点 (性能红线: 正常 PNG 读取 0.18ms 不得回退):
- probe_stealth_signature 为字节级探测: 只扫描 PNG chunk 头定位 IHDR/IDAT,
  解压"签名所需的最少扫描行前缀"并逆 PNG 滤波, 全程不经 PIL 全图解码;
- decode_stealth_text 仅在签名确认后打开图像读 LSB (调用方需静默捕获一切异常);
- 位序与 stealth-pnginfo 写入端一致: 列主序 (x 外层, y 内层), 每字节 MSB 先出。
"""
import json
import re
import struct
import zlib
from typing import Any, Dict, Iterator, List, Optional, Tuple

# ── 常量 ──
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SIGNATURE_BYTES = 15
LENGTH_BYTES = 4
SIGNATURE_BITS = SIGNATURE_BYTES * 8                      # 120
MAX_PROBE_DECOMPRESSED_BYTES = 1 << 22                    # 探测解压上限 4MB
MAX_PAYLOAD_BYTES = 8 << 20                               # 隐写载荷上限 8MB
MAX_DECOMPRESSED_BYTES = 16 << 20                         # gzip 解压输出上限 16MB
_MAX_SCAN_CHUNKS = 65536                                  # chunk 头扫描防失控

# 签名 -> (通道, 是否 gzip 压缩)
_SIGNATURE_CARRIERS: Dict[bytes, Tuple[str, bool]] = {
    b"stealth_pnginfo": ("alpha", False),
    b"stealth_pngcomp": ("alpha", True),
    b"stealth_rgbinfo": ("rgb", False),
    b"stealth_rgbcomp": ("rgb", True),
}

# 解码文本若以 {/[ 开头但不是合法 JSON 且形似 WebUI 参数行, 仍按 parameters 处理
_WEBUI_SHAPE_RE = re.compile(r"(?m)^Steps[:：]\s*\d+\s*,[^\r\n]*\bSampler[:：]")


# ────────────────────────── 字节级 PNG 扫描与探测 ──────────────────────────

def _scan_png_layout(file_path: str) -> Optional[Tuple[int, int, int, List[Tuple[int, int]]]]:
    """扫描 PNG chunk 头, 返回 (width, height, color_type, idat_ranges)。

    只读 IHDR 与各 chunk 的 8 字节头, 数据区 seek 跳过; 任何异常/不一致返回 None。
    """
    try:
        with open(file_path, "rb") as f:
            if f.read(8) != PNG_SIGNATURE:
                return None
            width = height = color_type = -1
            bit_depth = -1
            interlace = -1
            idat_ranges: List[Tuple[int, int]] = []
            chunks = 0
            while True:
                header = f.read(8)
                if len(header) < 8:
                    break
                chunk_length, chunk_type = struct.unpack(">I4s", header)
                if chunk_length > 0x7FFFFFFF:
                    return None
                data_offset = f.tell()
                if chunk_type == b"IHDR":
                    if chunk_length != 13:
                        return None
                    ihdr = f.read(13)
                    if len(ihdr) != 13:
                        return None
                    f.seek(4, 1)  # CRC
                    width, height = struct.unpack(">II", ihdr[:8])
                    bit_depth, color_type, interlace = ihdr[8], ihdr[9], ihdr[12]
                elif chunk_type == b"IDAT":
                    idat_ranges.append((data_offset, chunk_length))
                    f.seek(chunk_length + 4, 1)
                else:
                    f.seek(chunk_length + 4, 1)  # 数据 + CRC
                    if chunk_type == b"IEND":
                        break
                chunks += 1
                if chunks > _MAX_SCAN_CHUNKS:
                    return None
            if width <= 0 or height <= 0 or bit_depth != 8 or interlace != 0:
                return None
            if not idat_ranges:
                return None
            return width, height, color_type, idat_ranges
    except OSError:
        return None


def _probe_layout(height: int, color_type: int) -> Optional[Tuple[int, int, int]]:
    """返回签名探测所需的 (扫描行数, 每行前缀字节数, 每像素字节数)。仅支持 RGB/RGBA。"""
    if height <= 0:
        return None
    if color_type == 6:
        bpp = 4
        required_pixels = SIGNATURE_BITS              # alpha 每 1 像素 1 bit
    elif color_type == 2:
        bpp = 3
        required_pixels = SIGNATURE_BITS // 3         # RGB 每 1 像素 3 bit
    else:
        return None
    rows_to_decode = min(height, required_pixels)
    columns_to_decode = (required_pixels + height - 1) // height
    return rows_to_decode, columns_to_decode * bpp, bpp


def _paeth_predictor(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    dl, da, dul = abs(estimate - left), abs(estimate - above), abs(estimate - upper_left)
    if dl <= da and dl <= dul:
        return left
    if da <= dul:
        return above
    return upper_left


def unfilter_png_scanline_prefix(filter_type: int, filtered_prefix: bytes,
                                 previous_prefix: bytes, bytes_per_pixel: int) -> bytes:
    """对单行扫描线前缀逆 PNG 滤波 (0 None / 1 Sub / 2 Up / 3 Average / 4 Paeth)。"""
    if len(filtered_prefix) != len(previous_prefix):
        raise ValueError("PNG probe scanline prefixes have different lengths")
    if filter_type not in (0, 1, 2, 3, 4):
        raise ValueError(f"Invalid PNG scanline filter type: {filter_type}")
    reconstructed = bytearray(len(filtered_prefix))
    for i, raw_value in enumerate(filtered_prefix):
        left = reconstructed[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
        above = previous_prefix[i]
        upper_left = previous_prefix[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
        if filter_type == 0:
            predictor = 0
        elif filter_type == 1:
            predictor = left
        elif filter_type == 2:
            predictor = above
        elif filter_type == 3:
            predictor = (left + above) // 2
        else:
            predictor = _paeth_predictor(left, above, upper_left)
        reconstructed[i] = (raw_value + predictor) & 0xFF
    return bytes(reconstructed)


def _read_idat_prefix(file_path: str, idat_ranges: List[Tuple[int, int]],
                      required_output_bytes: int) -> Optional[bytes]:
    """流式解压 IDAT 直到凑够所需解压字节 (探测只需前几行, 不解全图)。"""
    decompressor = zlib.decompressobj()
    output = bytearray()
    try:
        with open(file_path, "rb") as f:
            for data_offset, data_length in idat_ranges:
                f.seek(data_offset)
                remaining = data_length
                while remaining > 0 and len(output) < required_output_bytes:
                    compressed = f.read(min(64 * 1024, remaining))
                    if not compressed:
                        return None
                    remaining -= len(compressed)
                    output.extend(decompressor.decompress(compressed, required_output_bytes - len(output)))
                if len(output) >= required_output_bytes:
                    return bytes(output)
    except (OSError, zlib.error):
        return None
    return None


def _signature_from_channel(scanline_prefixes: List[bytes], height: int,
                            bytes_per_pixel: int, channel_indexes: Tuple[int, ...]) -> Optional[bytes]:
    """按列主序 (x 外层 y 内层) 从重建前缀收集 LSB, 组出 15 字节签名。"""
    bits: List[int] = []
    column_count = min((len(row) // bytes_per_pixel for row in scanline_prefixes), default=0)
    for column in range(column_count):
        for row in scanline_prefixes[:height]:
            pixel_offset = column * bytes_per_pixel
            for channel_index in channel_indexes:
                bits.append(row[pixel_offset + channel_index] & 1)
                if len(bits) == SIGNATURE_BITS:
                    return _bits_to_bytes(bits)
    return None


def probe_stealth_signature(file_path: str) -> Optional[bytes]:
    """廉价探测隐写签名: 仅解压 IDAT 前缀; 无隐写/畸形/非 8bit 非交错 RGB(A) 一律 None。"""
    layout_info = _scan_png_layout(file_path)
    if layout_info is None:
        return None
    width, height, color_type, idat_ranges = layout_info
    layout = _probe_layout(height, color_type)
    if layout is None:
        return None
    rows_to_decode, prefix_bytes, bytes_per_pixel = layout
    row_bytes = width * bytes_per_pixel
    required_output = rows_to_decode * (row_bytes + 1)
    if required_output > MAX_PROBE_DECOMPRESSED_BYTES:
        return None

    decompressed = _read_idat_prefix(file_path, idat_ranges, required_output)
    if decompressed is None or len(decompressed) < required_output:
        return None

    previous_prefix = bytes(prefix_bytes)
    scanline_prefixes: List[bytes] = []
    for row_index in range(rows_to_decode):
        scanline_offset = row_index * (row_bytes + 1)
        filter_type = decompressed[scanline_offset]
        filtered_prefix = decompressed[scanline_offset + 1: scanline_offset + 1 + prefix_bytes]
        try:
            reconstructed = unfilter_png_scanline_prefix(filter_type, filtered_prefix,
                                                         previous_prefix, bytes_per_pixel)
        except ValueError:
            return None
        scanline_prefixes.append(reconstructed)
        previous_prefix = reconstructed

    # color_type 6 先查 alpha 签名, 未中再查 RGB; color_type 2 只查 RGB
    if color_type == 6:
        alpha_sig = _signature_from_channel(scanline_prefixes, height, 4, (3,))
        if alpha_sig in (b"stealth_pnginfo", b"stealth_pngcomp"):
            return alpha_sig
    if color_type in (2, 6):
        bpp = 3 if color_type == 2 else 4
        rgb_sig = _signature_from_channel(scanline_prefixes, height, bpp, (0, 1, 2))
        if rgb_sig in (b"stealth_rgbinfo", b"stealth_rgbcomp"):
            return rgb_sig
    return None


# ────────────────────────── 载荷解码 (签名确认后才调用) ──────────────────────────

def _bits_to_bytes(bits: List[int]) -> bytes:
    if len(bits) % 8 != 0:
        raise ValueError("Bit sequence is not byte-aligned")
    return bytes(
        sum(bits[offset + bit_index] << (7 - bit_index) for bit_index in range(8))
        for offset in range(0, len(bits), 8)
    )


def _iter_channel_bits(raw: bytes, width: int, height: int, channel: str, bpp: int) -> Iterator[int]:
    """按写入端位序产出 LSB: 列主序 (x 外层, y 内层); RGB 通道每像素 r,g,b 三位。

    基于 PIL tobytes() 的行主序字节流做列切片 (C 级 strided slice), 避免逐像素取值。
    """
    if channel == "alpha":
        off = bpp - 1
        for x in range(width):
            column = raw[x * bpp + off:: width * bpp][:height]
            for b in column:
                yield b & 1
    else:
        for x in range(width):
            cr = raw[x * bpp + 0:: width * bpp][:height]
            cg = raw[x * bpp + 1:: width * bpp][:height]
            cb = raw[x * bpp + 2:: width * bpp][:height]
            for i in range(height):
                yield cr[i] & 1
                yield cg[i] & 1
                yield cb[i] & 1


def _read_exact_bytes(bits: Iterator[int], byte_count: int) -> bytes:
    output = bytearray(byte_count)
    for byte_index in range(byte_count):
        value = 0
        for _ in range(8):
            value = (value << 1) | next(bits)
        output[byte_index] = value
    return bytes(output)


def _gzip_decompress_limited(payload: bytes, max_output_bytes: int) -> bytes:
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output = decompressor.decompress(payload, max_output_bytes + 1)
    if len(output) > max_output_bytes or decompressor.unconsumed_tail:
        raise ValueError("gzip payload exceeds decompressed payload limit")
    remaining = max_output_bytes + 1 - len(output)
    if remaining > 0:
        output += decompressor.flush(remaining)
    if not decompressor.eof or decompressor.unused_data:
        raise ValueError("invalid gzip payload")
    if len(output) > max_output_bytes:
        raise ValueError("gzip payload exceeds decompressed payload limit")
    return output


def decode_stealth_text(file_path: str, expected_signature: bytes) -> Optional[str]:
    """解码已确认签名的隐写载荷, 返回 UTF-8 文本; 任何异常由调用方静默处理。"""
    carrier = _SIGNATURE_CARRIERS.get(expected_signature)
    if carrier is None:
        return None
    channel, compressed = carrier
    # [启动器改动] 原实现硬依赖 PIL 取像素。本启动器不引入 Pillow, 故改为
    # "有 PIL 就用 PIL, 没有就用 png_raw 的纯标准库解码器" —— 两条路输出的
    # 字节布局一致 (行优先, 每像素 bpp 字节), 下游位提取逻辑完全不用改。
    raw = width = height = bpp = None
    try:
        from PIL import Image as _PILImage
    except Exception:
        _PILImage = None
    if _PILImage is not None:
        try:
            with _PILImage.open(file_path) as img:
                # 载体通道与色彩模式的联合守卫 (channel 只能为 alpha/rgb, 无需第三次单独判 mode)
                if channel == "alpha" and img.mode != "RGBA":
                    return None
                if channel == "rgb" and img.mode not in ("RGB", "RGBA"):
                    return None
                bpp = 4 if img.mode == "RGBA" else 3
                width, height = img.width, img.height
                raw = img.tobytes()
        except Exception:
            raw = None
    if raw is None:
        from .png_raw import decode_rgb_bytes
        decoded = decode_rgb_bytes(file_path)
        if decoded is None:
            return None
        raw, width, height, bpp = decoded
        if channel == "alpha" and bpp != 4:
            return None

    bits = _iter_channel_bits(raw, width, height, channel, bpp)
    try:
        signature = _read_exact_bytes(bits, SIGNATURE_BYTES)
        if signature != expected_signature:
            return None
        length_bytes = _read_exact_bytes(bits, LENGTH_BYTES)
        payload_bit_length = int.from_bytes(length_bytes, byteorder="big")
        if payload_bit_length <= 0 or payload_bit_length % 8 != 0:
            return None
        payload_byte_length = payload_bit_length // 8
        channel_count = 1 if channel == "alpha" else 3
        available_bits = width * height * channel_count - (SIGNATURE_BYTES + LENGTH_BYTES) * 8
        if payload_bit_length > available_bits or payload_byte_length > MAX_PAYLOAD_BYTES:
            return None
        payload = _read_exact_bytes(bits, payload_byte_length)
    except (StopIteration, ValueError):
        return None

    if compressed:
        try:
            data = _gzip_decompress_limited(payload, MAX_DECOMPRESSED_BYTES)
        except (ValueError, zlib.error):
            return None
    else:
        data = payload
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if text.strip() else None


def payload_to_metadata(text: str) -> Optional[Dict[str, Any]]:
    """把解码文本归一为调度用 dict: {"parameters": 文本} 或 JSON 对象; 无法归类返回 None。"""
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            if _WEBUI_SHAPE_RE.search(text):
                return {"parameters": text}
            return None
        if isinstance(parsed, dict):
            if not all(isinstance(k, str) for k in parsed):
                return None
            return parsed
        if isinstance(parsed, str) and parsed.strip():
            return {"parameters": parsed}
        return None
    return {"parameters": text}
