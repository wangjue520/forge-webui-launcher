# -*- coding: utf-8 -*-
"""查看启动器使用计数（usage-ping Release 里两个小文件的下载次数）。

用法：python tools/usage_stats.py
每次运行把结果追加到 tools/usage_stats.csv，隔几天跑一次就能看出走势。
"""
import csv
import os
import sys
import time

import requests

API = "https://api.github.com/repos/wangjue520/wwy-launcher/releases/tags/usage-ping"
CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_stats.csv")


def main():
    resp = requests.get(API, headers={"Accept": "application/vnd.github+json"}, timeout=20)
    resp.raise_for_status()
    counts = {a["name"]: a["download_count"] for a in resp.json().get("assets", [])}
    users, updates = counts.get("user.txt", 0), counts.get("update.txt", 0)
    now = time.strftime("%Y-%m-%d %H:%M")
    print(f"{now}  用户 {users}  更新人次 {updates}")

    prev = None
    if os.path.isfile(CSV):
        with open(CSV, encoding="utf-8") as f:
            rows = list(csv.reader(f))[1:]
        prev = rows[-1] if rows else None
    if prev:
        print(f"比上次（{prev[0]}）：用户 +{users - int(prev[1])}  更新 +{updates - int(prev[2])}")
    new = not os.path.isfile(CSV)
    with open(CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "users", "updates"])
        w.writerow([now, users, updates])


if __name__ == "__main__":
    sys.exit(main())
