"""
common.py - 通用辅助函数与类型转换
"""
import os
from typing import Any, Optional

def safe_int(val: Any) -> Optional[int]:
    # 先走精确 int() 通道: int/bool/整数字符串 (含超大整数如 ComfyUI 64-bit seed) 直接精确转换,
    # 避免旧实现 int(float(val)) 对 > 2^53 的整数因 float64 尾数只有 53 位而静默丢精度
    # (真实场景: seed=12345678901234567890 被读成/回写成 12345678901234567168)。
    if val is None: return None
    try: return int(val)
    except Exception: pass
    # 回退: 浮点字符串 ("42.7"/"1e3") 与 float 值仍按截断取整 (与历史行为一致)
    try: return int(float(val))
    except Exception: return None

def safe_float(val: Any) -> Optional[float]:
    try: return float(val) if val is not None else None
    except Exception: return None

def valid_safetensors_header(header_size: Any, file_size: int, max_bytes: int) -> bool:
    """三处重复的 header 三重守卫 (==0 / >上限 / 物理截断) 的唯一真相源: read/save/autov2 对称复用"""
    try: return bool(header_size) and header_size <= max_bytes and 8 + header_size <= file_size
    except Exception: return False

def cleanup_tmp(path: str) -> None:
    """三处重复的 temp 残留清理 (save/strip/lora) 的唯一实现: 存在即删, 异常吞掉"""
    try:
        if path and os.path.exists(path): os.remove(path)
    except Exception: pass
