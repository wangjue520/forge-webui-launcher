"""
hash_utils.py - 模型与文件哈希计算工具
支持 Civitai AutoV2 (safetensors SHA256 前10位)、标准 SHA256 与 CRC32
"""
import os
import struct
import zlib
from .common import valid_safetensors_header

class HashUtils:
    """哈希计算引擎 (全部方法异常安全, 失败返回空串)"""

    _CHUNK = 4 * 1024 * 1024
    # safetensors header 上限守卫 (与 lora_parser._MAX_HEADER_BYTES 对称): 声明超过此值视为损坏/恶意,
    # 拒绝按超大偏移 seek, 否则 f.seek(8+header_size) 会因偏移溢出抛 ValueError 而非被 OSError 捕获
    _MAX_HEADER_BYTES = 100 * 1024 * 1024

    @staticmethod
    def calculate_sha256(file_path: str, chunk_size: int = 4 * 1024 * 1024) -> str:
        """计算完整文件的 SHA256"""
        if not isinstance(file_path, str): return ""
        if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
            chunk_size = HashUtils._CHUNK
        try:
            import hashlib  # 延迟加载: 仅真正计算哈希时需要 (省启动关键路径开销)
            sha256 = hashlib.sha256()
            with open(file_path, "rb") as f:
                while chunk := f.read(chunk_size):
                    sha256.update(chunk)
            return sha256.hexdigest()
        except (OSError, ValueError, TypeError):
            return ""

    @staticmethod
    def calculate_autov2_hash(file_path: str) -> str:
        """Civitai AutoV2: safetensors 跳过头部(8字节长度+header JSON)后对数据区计算 SHA256 前10位。
        非 safetensors 文件返回空串 (避免给出与 Civitai 不匹配的错值)。"""
        if not isinstance(file_path, str) or os.path.splitext(file_path)[1].lower() != ".safetensors":
            return ""
        try:
            import hashlib  # 延迟加载: 同 calculate_sha256
            file_size = os.path.getsize(file_path)
            if file_size < 8: return ""
            with open(file_path, "rb") as f:
                header_len_bytes = f.read(8)
                if len(header_len_bytes) < 8:
                    return ""
                header_size = struct.unpack("<Q", header_len_bytes)[0]
                # 头部长度上限与物理大小守卫: 损坏/截断/恶意文件的超大 header_size 拒绝处理
                if not valid_safetensors_header(header_size, file_size, HashUtils._MAX_HEADER_BYTES):
                    return ""
                f.seek(8 + header_size)
                sha256 = hashlib.sha256()
                while chunk := f.read(HashUtils._CHUNK):
                    sha256.update(chunk)
            return sha256.hexdigest()[:10]
        except (OSError, ValueError, OverflowError, struct.error, TypeError):
            return ""

    @staticmethod
    def calculate_file_crc32(file_path: str) -> str:
        """计算文件 CRC32"""
        if not isinstance(file_path, str): return ""
        try:
            crc = 0
            with open(file_path, "rb") as f:
                while chunk := f.read(HashUtils._CHUNK):
                    crc = zlib.crc32(chunk, crc)
            return f"{crc & 0xFFFFFFFF:08x}"
        except (OSError, ValueError, TypeError):
            return ""
