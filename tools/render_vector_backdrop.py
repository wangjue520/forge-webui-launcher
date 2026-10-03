"""render_vector_backdrop.py — 把「矢量突破」主界面的立体背景烘焙成三张透明图层（开发者工具）

主界面原来是实时的 CSS 3D 场景（十几个物体、每个方块 6 个面、景深模糊、湍流等高环）。
烘焙成图片后，运行时只剩三张图：
    media/vector-bg-far.webp    远景：柔光 + 等高环 + 虚化的远处方块 / 软球
    media/vector-bg-mid.webp    焦平面：清楚的方块 / 软球
    media/vector-bg-front.webp  前景：压在边角、很虚的大方块 / 大球
翻页时三层以不同幅度横移（视差），毛玻璃透出的就是它们；空闲时完全静止、零开销。

改了 js/vector-scene.js 的 MAIN 预设或 themes/vector-scene.css 后重新跑：
  python tools/render_vector_backdrop.py
"""
import asyncio
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_vector_splash import WEB, OUT_DIR, serve  # noqa: E402

W, H, DPR = 1600, 1112, 1.25        # 与默认窗口同比例（≈1.44），放大到大窗口 / 高分屏也够清楚

ISOLATE = """
  html, body { background: transparent !important; }
  #layout, #vb-front, #splash, #toasts, #loadbar, body > div[style*="99999"] { display: none !important; }
  #vb-deco { display: block !important; }
  #vb-deco .vb3-it, #vb-deco .vb3-haze, #vb-deco .vb-rings { animation: none !important; transition: none !important; }
"""

LAYERS = {
    # 每层只显示自己的东西（visibility 不改变布局，三层叠回去和原场景完全一致）
    "far": "#vb-deco .vb3-it:not([data-layer='far']) { visibility: hidden !important; }",
    "mid": "#vb-deco .vb3-haze, #vb-deco .vb-rings, #vb-deco .vb3-it:not([data-layer='mid']) { visibility: hidden !important; }",
    "front": "#vb-deco .vb3-haze, #vb-deco .vb-rings, #vb-deco .vb3-it:not([data-layer='front']) { visibility: hidden !important; }",
}


async def render():
    from playwright.async_api import async_playwright
    from PIL import Image
    srv = serve(WEB)
    url = f"http://127.0.0.1:{srv.server_address[1]}/index.html?mock=1&splash=live&bg=live"
    async with async_playwright() as p:
        kw = {}
        if os.environ.get("CHROME"):
            kw["executable_path"] = os.environ["CHROME"]
        browser = await p.chromium.launch(**kw)
        page = await browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=DPR)
        await page.add_init_script("window.WWY_THEME_ID = 'vector'; try { sessionStorage.setItem('skip-splash', '1'); } catch (e) {}")
        await page.goto(url)
        await page.wait_for_function("window.App && App.cfg")
        await page.wait_for_timeout(800)            # 等 core.js 按配置套完风格，再强制切到矢量突破
        await page.evaluate("App.cfg.ui_theme = 'vector'; WWYThemes.apply('vector')")
        await page.evaluate("document.documentElement.classList.remove('vb-splash')")
        await page.add_style_tag(content=ISOLATE)
        await page.evaluate("document.fonts.ready.then(() => 1)")
        await page.wait_for_timeout(1500)
        for name, css in LAYERS.items():
            tag = await page.add_style_tag(content=css)
            await page.wait_for_timeout(300)
            png = await page.screenshot(omit_background=True)
            await tag.evaluate("n => n.remove()")
            im = Image.open(io.BytesIO(png)).convert("RGBA")
            out = OUT_DIR / f"vector-bg-{name}.webp"
            im.save(out, "WEBP", quality=82, method=6)
            print(f"  {out.name}: {im.size[0]}×{im.size[1]}  {out.stat().st_size / 1024:.0f} KB")
        await browser.close()
    srv.shutdown()


if __name__ == "__main__":
    OUT_DIR.mkdir(exist_ok=True)
    asyncio.run(render())
