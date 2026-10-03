"""render_vector_splash.py — 把「矢量突破」开屏动画离线逐帧渲染成视频（开发者工具，不随启动器运行）

为什么要预渲染：开屏要同时跑立体场景、光条、软球、唱片、逐字动画，实时渲染在核显 /
主线程正忙着初始化的机器上容易掉帧。预渲染成视频后，启动器只需要「播放一个视频图层」，
解码走硬件，主线程再忙也不卡。

原理：
  1. 用 Playwright 打开 web/index.html?mock=1（矢量突破风格），注入一个「假时钟」：
     接管 performance.now / Date.now / setTimeout / setInterval / requestAnimationFrame，
     并把页面上所有 CSS 动画 / 过渡暂停，每一帧手动把它们的 currentTime 设到假时钟的时刻。
     这样每一帧都是精确的 1/60 秒，和电脑快慢无关，画面完全确定。
  2. 逐帧截图（设备像素比 1.5，按 1180×820 的默认窗口排版，输出 1770×1230）。
  3. 用 ffmpeg 编码成 H.264 mp4 + VP9 webm（WebView2 都支持硬件解码）。
  4. 导出 web/media/vector-splash.json / .js：视频时长、叠加层（版本号等不进视频的内容）
     在画面里的位置和出现时刻，供 js/splash.js 播放时对齐。

改了开屏的 CSS / 编排后重新跑一遍即可：
  pip install playwright imageio-ffmpeg && python -m playwright install chromium
  python tools/render_vector_splash.py
"""
import asyncio
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
OUT_DIR = WEB / "media"
W, H, DPR = 1180, 820, 1.5          # 启动器默认窗口尺寸；1.5 倍 = 150% 缩放的屏幕上也清晰
FPS = 60
DURATION = 2.65                     # 秒：入场全部完成后停在最后一帧（撤场由 CSS 在视频图层上做）

FAKE_CLOCK = r"""
(() => {
  // ---------- 假时钟：页面里所有「时间」都由渲染脚本一帧一帧推进 ----------
  let now = 0, seq = 1;
  const timers = [];
  let rafs = [];
  const T0 = 1767225600000;
  performance.now = () => now;
  Date.now = () => T0 + now;
  window.setTimeout = (fn, ms, ...a) => { const id = seq++; timers.push({ id, t: now + (+ms || 0), fn, a }); return id; };
  window.setInterval = (fn, ms, ...a) => { const id = seq++; timers.push({ id, t: now + Math.max(1, +ms || 0), fn, a, iv: Math.max(1, +ms || 0) }); return id; };
  window.clearTimeout = window.clearInterval = (id) => { const k = timers.findIndex(x => x.id === id); if (k >= 0) timers.splice(k, 1); };
  window.requestAnimationFrame = (fn) => { rafs.push(fn); return seq++; };
  window.cancelAnimationFrame = () => {};
  // CSS 动画 / 过渡：第一次见到时记下「诞生时刻」并暂停，之后每帧手动设 currentTime
  const seen = new WeakMap();
  function syncAnims() {
    for (const an of document.getAnimations()) {
      if (!seen.has(an)) { seen.set(an, now); try { an.pause(); } catch (e) {} }
      try { an.currentTime = now - seen.get(an); } catch (e) {}
    }
  }
  window.__clock = {
    advance(dt) {
      const target = now + dt;
      for (;;) {
        timers.sort((a, b) => a.t - b.t || a.id - b.id);
        const t = timers[0];
        if (!t || t.t > target) break;
        now = t.t;
        if (t.iv) t.t += t.iv; else timers.shift();
        try { t.fn(...t.a); } catch (e) { console.error(e); }
        syncAnims();                // 定时器里加的 class 产生的动画，诞生时刻要记准
      }
      now = target;
      const fs = rafs; rafs = [];
      for (const f of fs) { try { f(now); } catch (e) { console.error(e); } }
      syncAnims();
    },
    now: () => now,
  };
})();
"""

HIDE_CHROME = """
  body > div[style*="99999"], #toasts { display: none !important; }
"""


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(directory):
    handler = functools.partial(_QuietHandler, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        exe = shutil.which("ffmpeg")
        if not exe:
            sys.exit("需要 ffmpeg：pip install imageio-ffmpeg")
        return exe


async def render(frames_dir, meta_only=False):
    from playwright.async_api import async_playwright
    srv = serve(WEB)
    url = f"http://127.0.0.1:{srv.server_address[1]}/index.html?mock=1&splash=live"
    meta = {}
    async with async_playwright() as p:
        kw = {}
        if os.environ.get("CHROME"):
            kw["executable_path"] = os.environ["CHROME"]
        browser = await p.chromium.launch(**kw)
        page = await browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=DPR)
        await page.add_init_script("window.WWY_THEME_ID = 'vector';")
        await page.add_init_script(FAKE_CLOCK)
        await page.goto(url)
        await page.add_style_tag(content=HIDE_CHROME)
        await page.evaluate("document.documentElement.classList.add('vs-render')")   # 版本号等不进视频
        await page.evaluate("document.fonts.ready.then(() => 1)")
        await page.wait_for_timeout(800)            # 真实时间：等字体、图片解码；假时钟此时停在 0
        n = int(round(DURATION * FPS))
        for i in range(n):
            if i:
                await page.evaluate(f"__clock.advance({1000 / FPS})")
            if meta_only:
                continue
            await page.screenshot(path=str(frames_dir / f"f{i:04d}.png"))
            if i % 30 == 0:
                print(f"  frame {i}/{n}")
        # 叠加层位置（按 1180×820 的比例存百分比；播放时盒子按视频比例铺满窗口）
        meta = await page.evaluate("""() => {
          const r = (sel) => { const e = document.querySelector(sel); if (!e) return null;
            const b = e.getBoundingClientRect();
            return { x: b.left / innerWidth, y: b.top / innerHeight, w: b.width / innerWidth, h: b.height / innerHeight }; };
          const big = document.querySelector('.vs-big.r2');
          const under = document.querySelector('.vs-under');
          return {
            num: r('.vs-num'),
            ver: r('.vs-verw'),
            bigFont: parseFloat(getComputedStyle(big).fontSize) / innerWidth,
            underFont: parseFloat(getComputedStyle(under).fontSize) / innerWidth,
            node: r('.vs-node'),
            log: r('.vs-log'),
            bridge: r('.vs-bridge em'),
          };
        }""")
        await browser.close()
    srv.shutdown()
    return meta


def encode(frames_dir, out):
    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(frames_dir / "f%04d.png"),
           "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-tune", "animation",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", str(out)]
    subprocess.run(cmd, check=True)
    # 同时出一份 VP9 webm：WebView2 两种都能硬解；不带 H.264 的 Chromium 也能播 webm
    webm = [ffmpeg_exe(), "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(frames_dir / "f%04d.png"),
            "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-row-mt", "1", "-deadline", "good", "-cpu-used", "2",
            "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", str(out.with_suffix(".webm"))]
    subprocess.run(webm, check=True)


def main():
    OUT_DIR.mkdir(exist_ok=True)
    meta_only = "--meta-only" in sys.argv       # 只重新量叠加层位置（不重渲视频）
    tmp = Path(tempfile.mkdtemp(prefix="vsplash_"))
    try:
        print("量叠加层位置…" if meta_only else "渲染帧…")
        meta = asyncio.run(render(tmp, meta_only))
        if not meta_only:
            print("编码视频…")
            encode(tmp, OUT_DIR / "vector-splash.mp4")
            # 首帧做海报（视频解码出来之前先显示它，避免一闪），末帧留作预览
            from PIL import Image
            n = int(round(DURATION * FPS))
            for src, name in ((0, "vector-splash-first.jpg"), (n - 1, "vector-splash-last.jpg")):
                Image.open(tmp / f"f{src:04d}.png").convert("RGB").save(OUT_DIR / name, quality=88, optimize=True)
        meta.update({
            "duration": DURATION, "fps": FPS, "width": round(W * DPR), "height": round(H * DPR), "aspect": W / H,
            # 叠加层出现时刻（秒）：与 js/splash.js 里 vectorSplash 的编排一致（vs-6 = 1.06s，引号是第 11 个字）
            "numAt": 1.06 + 11 * 0.022, "verAt": 1.06 + 0.4,
        })
        (OUT_DIR / "vector-splash.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        # 同一份数据再写成 JS（开屏脚本同步读取，不用等 fetch）
        (OUT_DIR / "vector-splash.js").write_text(
            "/* 由 tools/render_vector_splash.py 生成，勿手改 */\nwindow.VB_SPLASH = " + json.dumps(meta) + ";\n", encoding="utf-8")
        size = (OUT_DIR / "vector-splash.mp4").stat().st_size
        print(f"完成：web/media/vector-splash.mp4（{size / 1024:.0f} KB）+ .webm + .json/.js")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
