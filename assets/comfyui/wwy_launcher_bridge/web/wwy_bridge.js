// WWY 启动器桥接插件：每次加载 / 切换工作流时检查缺节点、缺模型，结果发给后端打日志，启动器的报错诊断接住
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const MODEL_EXT = /\.(safetensors|ckpt|pt|pth|bin|gguf|sft|onnx)$/i;
const SKIP_MODES = new Set([2, 4]);     // 2 = 静音，4 = 绕过：不参与运行，缺了也不碍事

const norm = (s) => String(s).replace(/\\/g, "/").toLowerCase();
// 网页加载缺失节点时会把类型名里的 & < > 引号等去掉（sanitizeNodeName），扫画布时拿到的是去掉后的名字
const sanitized = (s) => String(s).replace(/[&<>"'`=]/g, "");

function graphNodes(graph) {
    const out = [...(graph?._nodes || graph?.nodes || [])];
    const subs = graph?.subgraphs || graph?._subgraphs;
    if (subs && typeof subs.values === "function") {
        for (const sg of subs.values()) out.push(...(sg?._nodes || sg?.nodes || []));
    }
    return out;
}

function embeddedModels(node, wf) {
    // 官方模板等会在节点 properties.models 里写好下载地址：[{name, url, directory}]
    const list = [...(node?.properties?.models || []), ...(wf?.extra?.models || []), ...(wf?.models || [])];
    const map = {};
    for (const m of list) if (m?.name && m?.url) map[norm(m.name)] = m;
    return map;
}

function check(missingTypes) {
    const graph = app.rootGraph || app.graph;
    let wf = null;
    try { wf = graph?.serialize?.(); } catch (e) { /* 序列化失败也照样检查 */ }
    const registered = globalThis.LiteGraph?.registered_node_types || {};
    const nodes = new Map();
    for (const t of missingTypes || []) {
        const type = typeof t === "string" ? t : t?.type;
        if (type) nodes.set(type, { type, cnr_id: (typeof t === "object" && t?.cnrId) || "" });
    }
    const known = new Set([...nodes.keys()].map(sanitized));
    const models = [];
    const seen = new Set();
    for (const node of graphNodes(graph)) {
        if (!node || SKIP_MODES.has(node.mode)) continue;
        if (node.type && !(node.type in registered) && !nodes.has(node.type) && !known.has(sanitized(node.type))) {
            nodes.set(node.type, { type: node.type, cnr_id: node.properties?.cnr_id || "" });
        }
        const emb = embeddedModels(node, wf);
        for (const w of node.widgets || []) {
            const v = w?.value;
            if (w?.type !== "combo" || typeof v !== "string" || !MODEL_EXT.test(v)) continue;
            let values = w.options?.values;
            if (typeof values === "function") {
                try { values = values(w, node); } catch (e) { values = null; }
            }
            if (!Array.isArray(values) || values.some((x) => norm(x) === norm(v))) continue;
            const key = norm(v) + "|" + w.name;
            if (seen.has(key)) continue;
            seen.add(key);
            const base = norm(v).split("/").pop();
            const e = emb[norm(v)] || emb[base];
            models.push({ name: v, node_type: node.type, node_title: node.title || "", input: w.name,
                          url: e?.url || "", directory: e?.directory || "" });
        }
    }
    let name = "";
    try { name = app.extensionManager?.workflow?.activeWorkflow?.filename || ""; } catch (e) { /* 老版本没有 */ }
    return { workflow_name: name, missing_nodes: [...nodes.values()], missing_models: models, workflow: wf };
}

let timer = null;
function report(missingTypes) {
    clearTimeout(timer);
    // 等网页把下拉框的选项、子图都装好再查
    timer = setTimeout(() => {
        try {
            const body = check(missingTypes);
            api.fetchApi("/wwy_bridge/report", { method: "POST", body: JSON.stringify(body) }).catch(() => {});
        } catch (e) {
            console.warn("[WWY诊断] 检查工作流出错", e);
        }
    }, 800);
}

// ---- 启动器诊断「执行修复」发来的指令：改当前工作流（改完标成未保存，用户自己 Ctrl+S）----
const MODES = { enable: 0, mute: 2, bypass: 4 };
const MODE_TEXT = { enable: "恢复", mute: "静音", bypass: "绕过" };
const modeName = (m) => (m === 4 ? "bypass" : m === 2 ? "mute" : "enable");

function findNode(graph, id) {
    const n = graph?.getNodeById?.(Number(id)) ?? graph?.getNodeById?.(id);
    return n || graphNodes(graph).find((x) => String(x?.id) === String(id)) || null;
}

function markChanged(graph) {
    try { graph?.setDirtyCanvas?.(true, true); } catch (e) { /* 只是重画 */ }
    try { app.canvas?.setDirty?.(true, true); } catch (e) { /* 同上 */ }
    try { app.extensionManager?.workflow?.activeWorkflow?.changeTracker?.checkState?.(); } catch (e) { /* 老版本没有 */ }
}

function applyCommand(cmd) {
    const graph = app.rootGraph || app.graph;
    const label = (n) => `#${n.id}「${n.title || n.type}」`;
    if (cmd.op === "set_mode") {
        if (!(cmd.mode in MODES)) throw new Error(`不认识的模式 ${cmd.mode}`);
        const nodes = (cmd.node_ids || []).map((id) => {
            const n = findNode(graph, id);
            if (!n) throw new Error(`当前工作流里没有节点 #${id}（网页里打开的可能不是同一个工作流）`);
            return n;
        });
        const prev = {};
        for (const n of nodes) { prev[n.id] = modeName(n.mode); n.mode = MODES[cmd.mode]; }
        markChanged(graph);
        return { ok: true, prev, text: `已${MODE_TEXT[cmd.mode]}节点 ${nodes.map(label).join("、")}（网页里还没保存，确认没问题按 Ctrl+S）` };
    }
    if (cmd.op === "set_widget") {
        const n = findNode(graph, cmd.node_id);
        if (!n) throw new Error(`当前工作流里没有节点 #${cmd.node_id}（网页里打开的可能不是同一个工作流）`);
        const w = (n.widgets || []).find((x) => x?.name === cmd.widget);
        if (!w) throw new Error(`节点 ${label(n)} 没有参数「${cmd.widget}」（有：${(n.widgets || []).map((x) => x.name).join("、")}）`);
        let values = w.options?.values;
        if (typeof values === "function") { try { values = values(w, n); } catch (e) { values = null; } }
        if (w.type === "combo" && Array.isArray(values) && !values.includes(cmd.value)) {
            throw new Error(`「${cmd.value}」不在节点 ${label(n)} 的「${cmd.widget}」可选项里`);
        }
        const prev = w.value;
        w.value = cmd.value;
        try { w.callback?.(w.value, app.canvas, n); } catch (e) { /* 有的控件回调要更多参数，值已经改上了 */ }
        markChanged(graph);
        return { ok: true, prev, text: `节点 ${label(n)} 的「${cmd.widget}」：${prev} → ${cmd.value}（网页里还没保存，确认没问题按 Ctrl+S）` };
    }
    throw new Error(`不认识的指令 ${cmd.op}`);
}

api.addEventListener("wwy_bridge_command", (e) => {
    const cmd = e.detail || {};
    let res;
    try { res = applyCommand(cmd); } catch (err) { res = { ok: false, text: String(err?.message || err) }; }
    api.fetchApi("/wwy_bridge/ack", { method: "POST", body: JSON.stringify({ id: cmd.id, ...res }) }).catch(() => {});
    if (res.ok) report([]);
});

app.registerExtension({
    name: "wwy.launcher.bridge",
    async afterConfigureGraph(missingNodeTypes) {
        report(missingNodeTypes);
    },
    async refreshComboInNodes() {
        // 按 R 刷新模型列表（比如刚下好缺的模型）后再查一次
        report([]);
    },
});
