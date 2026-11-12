// WWY 启动器桥接扩展：接住启动器「发送到 WebUI」发来的生成参数，用 WebUI 自己的 ↙ 粘贴按钮套用
(function () {
    "use strict";
    if (window.__wwyLauncherBridge) return;
    window.__wwyLauncherBridge = true;

    const root = () => (typeof gradioApp === "function" ? gradioApp() : document);
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const PASTE_SYMBOL = "↙";      // ↙

    async function waitUi() {
        for (let i = 0; i < 1200; i++) {
            if (root().querySelector("#txt2img_prompt textarea")) return true;
            await sleep(500);
        }
        return false;
    }

    function switchTab(tab) {
        if (tab === "img2img" && typeof switch_to_img2img === "function") { switch_to_img2img(); return; }
        if (typeof switch_to_txt2img === "function") { switch_to_txt2img(); return; }
        // 兜底：顶部第一个 / 第二个标签按钮
        const btns = root().querySelectorAll("#tabs > .tab-nav > button, #tabs > div > button");
        const b = btns[tab === "img2img" ? 1 : 0];
        if (b) b.click();
    }

    function pasteButton(tab) {
        const r = root();
        const inTools = r.querySelector(`#${tab}_tools #paste`) || r.querySelector(`#${tab}_paste`);
        if (inTools) return inTools;
        const all = Array.from(r.querySelectorAll("#paste"));
        if (all.length) return all[tab === "img2img" && all.length > 1 ? 1 : 0];
        // 再兜底：按钮上的 ↙ 图标，在对应页面里找
        const page = r.querySelector(`#tab_${tab}`) || r;
        return Array.from(page.querySelectorAll("button")).find((b) => (b.textContent || "").includes(PASTE_SYMBOL)) || null;
    }

    function notice(text, ok) {
        const el = document.createElement("div");
        el.textContent = text;
        el.style.cssText = "position:fixed;right:18px;bottom:18px;z-index:99999;max-width:420px;padding:10px 14px;" +
            "border-radius:8px;font-size:14px;line-height:1.5;box-shadow:0 4px 16px rgba(0,0,0,.35);" +
            (ok ? "background:#14532d;color:#dcfce7;" : "background:#7f1d1d;color:#fee2e2;");
        document.body.appendChild(el);
        setTimeout(() => el.remove(), ok ? 4000 : 9000);
    }

    async function apply(item) {
        const tab = item.tab === "img2img" ? "img2img" : "txt2img";
        switchTab(tab);
        await sleep(150);
        const ta = root().querySelector(`#${tab}_prompt textarea`);
        if (!ta) throw new Error(`页面上找不到${tab === "img2img" ? "图生图" : "文生图"}的提示词框`);
        const btn = pasteButton(tab);
        if (!btn) throw new Error("页面上找不到 ↙ 粘贴参数按钮（这个 WebUI 版本可能改过界面）");
        const input = (el) => {
            if (typeof updateInput === "function") updateInput(el);
            else el.dispatchEvent(new Event("input", { bubbles: true }));
        };
        // Forge Neo：负向提示词框里有字时 ↙ 按钮是灰的（防止误粘贴覆盖）。先清空负向，
        // 等它把按钮放开再按——反正粘贴会把负向提示词换成图里的
        const neg = root().querySelector(`#${tab}_neg_prompt textarea`);
        if (neg && neg.value.trim()) { neg.value = ""; input(neg); }
        ta.value = item.infotext;
        input(ta);
        for (let i = 0; i < 40 && btn.disabled; i++) await sleep(100);
        if (btn.disabled) throw new Error("↙ 粘贴参数按钮是灰的按不了：把负向提示词框清空后再发一次");
        await sleep(80);
        btn.click();
        const where = tab === "img2img" ? "图生图" : "文生图";
        return { ok: true, text: `已套用到 WebUI 的${where}${item.title ? `（${item.title}）` : ""}` };
    }

    async function loop() {
        if (!(await waitUi())) return;
        let fails = 0;
        for (;;) {
            let d = null;
            try {
                const r = await fetch("./wwy_bridge/poll", { cache: "no-store" });
                if (r.status === 404) { await sleep(60000); continue; }   // 扩展没加载上接口
                d = await r.json();
                fails = 0;
            } catch (e) {
                fails++;
                await sleep(Math.min(30000, 2000 * fails));               // WebUI 重启中
                continue;
            }
            const item = d && d.item;
            if (!item) continue;
            let res;
            try { res = await apply(item); } catch (err) { res = { ok: false, text: String((err && err.message) || err) }; }
            notice((res.ok ? "WWY 启动器：" : "WWY 启动器发送失败：") + res.text, res.ok);
            fetch("./wwy_bridge/ack", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(Object.assign({ id: item.id }, res)),
            }).catch(() => {});
        }
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", loop);
    else loop();
})();
