"""
netspeed.py — 给部署心跳用的整机下载网速

pip / git 下载大文件时输出被启动器接管，不显示进度条，日志只剩「仍在执行…」，
用户分不清是在下载还是卡死了。这里读整机网卡的累计接收字节数，两次心跳之间
相减得出网速（整机的，浏览器等其他程序的流量也算在内，但足够判断「有没有在下」）。

Windows：`netstat -e` 第一行带数字的就是「字节 接收 发送」（中文/英文系统都这样排，
不依赖本地化的标签文字）；其他系统读 /proc/net/dev（开发调试用）。
"""

import os
import re
import subprocess
import time

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def total_rx_bytes():
    """整机累计接收字节数；读不到返回 None"""
    try:
        if os.name == "nt":
            r = subprocess.run(["netstat", "-e"], capture_output=True, timeout=10,
                               creationflags=_NO_WINDOW)
            for line in (r.stdout or b"").decode("gbk", "replace").splitlines():
                nums = re.findall(r"\d+", line)
                if len(nums) >= 2:
                    return int(nums[0])
            return None
        total = 0
        with open("/proc/net/dev", "r") as f:
            for line in f.readlines()[2:]:
                name, data = line.split(":", 1)
                if name.strip() == "lo":
                    continue
                total += int(data.split()[0])
        return total
    except Exception:
        return None


def fmt_rate(bps):
    if bps >= 1024 * 1024:
        return f"{bps / 1048576:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps:.0f} B/s"


class NetMeter:
    """开始时记一次，之后每次 text() 给出距上次的平均网速描述"""

    def __init__(self):
        self.t = time.monotonic()
        self.rx = total_rx_bytes()
        self.idle = 0   # 连续几次几乎没流量

    def text(self):
        now, rx = time.monotonic(), total_rx_bytes()
        if rx is None or self.rx is None or rx < self.rx or now <= self.t:
            # 读不到 / 计数器回绕：这次不报，重新起算
            self.t, self.rx = now, rx
            return ""
        rate = (rx - self.rx) / (now - self.t)
        self.t, self.rx = now, rx
        if rate < 2048:
            self.idle += 1
            if self.idle >= 3:
                return ("，网速几乎为 0（已持续约 1 分半）——如果这一步是在下载，多半卡住了，"
                        "可以点「取消」后重新部署；解压、安装、编译这类本地步骤没有网速是正常的")
            return "，当前网速几乎为 0"
        self.idle = 0
        return f"，当前网速 {fmt_rate(rate)}（整机）"
