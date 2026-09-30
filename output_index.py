# -*- coding: utf-8 -*-
"""
输出管理器（V3 阶段 5）：把所有实例的出图目录建成一个可筛选的 SQLite 索引。

  - 只扫实例自己的输出目录，不收录外部图片；原文件一律不移动
  - 不收录阵列图：Forge/WebUI 的 grids 目录（txt2img-grids 等）扫描时跳过，
    已收录的记录在下次扫描时清掉
  - 增量扫描：按 (大小, 修改时间) 判断新增/变化，文件被改名/挪动时尽量把原记录接上（收藏不丢）
  - 生成参数复用 meta_engine 的解析器（A1111 / Forge / ComfyUI / NovelAI / 隐写 PNG 都认）
  - 角色识别：正向提示词拆成 tag，对照 WD14 selected_tags.csv 的角色类（category 4）
  - 收藏夹只存在数据库里
  - 缩略图由前端画好传回来缓存（不依赖 Pillow），视频缩略图同样由浏览器截帧

纯标准库；所有数据库访问都串行在一把锁后面（SQLite 单连接跨线程）。
"""
import csv
import glob
import http.server
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "launcher_data")
DB_PATH = os.path.join(DATA_DIR, "index.db")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi"}
# 「其他」：音频、3D 等；配置/缓存类文件不算输出
IGNORE_EXTS = {".txt", ".json", ".yaml", ".yml", ".tmp", ".part", ".db", ".ini", ".log",
               ".csv", ".py", ".pyc", ".lnk", ".url", ".civitai"}
IGNORE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outputs(
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE,
  name TEXT,
  iid TEXT,
  kind TEXT,
  size INTEGER,
  mtime REAL,
  width INTEGER,
  height INTEGER,
  prompt TEXT,
  negative TEXT,
  model TEXT,
  seed TEXT,
  sampler TEXT,
  source TEXT,
  has_meta INTEGER DEFAULT 0,
  missing INTEGER DEFAULT 0,
  added REAL
);
CREATE INDEX IF NOT EXISTS idx_out_mtime ON outputs(mtime);
CREATE INDEX IF NOT EXISTS idx_out_iid ON outputs(iid);
CREATE INDEX IF NOT EXISTS idx_out_model ON outputs(model);
CREATE INDEX IF NOT EXISTS idx_out_fp ON outputs(size, mtime);
CREATE TABLE IF NOT EXISTS output_tags(oid INTEGER, tag TEXT, is_char INTEGER);
CREATE INDEX IF NOT EXISTS idx_tag ON output_tags(tag);
CREATE INDEX IF NOT EXISTS idx_tag_oid ON output_tags(oid);
CREATE TABLE IF NOT EXISTS output_loras(oid INTEGER, name TEXT, weight TEXT);
CREATE INDEX IF NOT EXISTS idx_lora ON output_loras(name);
CREATE INDEX IF NOT EXISTS idx_lora_oid ON output_loras(oid);
CREATE TABLE IF NOT EXISTS collections(id INTEGER PRIMARY KEY, name TEXT UNIQUE, created REAL);
CREATE TABLE IF NOT EXISTS collection_items(cid INTEGER, oid INTEGER, added REAL,
  PRIMARY KEY(cid, oid));
"""

_LORA_TAG_RE = re.compile(r"<(?:lora|lyco):([^:>]+)(?::([^:>]+))?[^>]*>", re.IGNORECASE)
_WEIGHT_RE = re.compile(r"^\((.*):\s*[-+]?\d*\.?\d+\)$")
_EMPH_STRIP = "()[]{}"
_NON_CHAR_QUALIFIERS = {"clothing", "weapon", "object", "animal", "food", "artwork", "medium", "style",
                        "cosplay", "meme", "hair", "fashion", "pattern", "symbol", "company",
                        "vehicle", "plant", "flower", "instrument", "game", "phrase", "emoji", "cable",
                        "armor", "hat", "sword", "gun", "shape", "architecture", "creature", "robot"}


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IGNORE_EXTS or os.path.basename(path).lower() in IGNORE_NAMES or not ext:
        return None
    return "other"


def _is_grid_dir(name):
    """Forge/WebUI 的阵列图目录（txt2img-grids / img2img-grids / grids）"""
    n = (name or "").lower()
    return n == "grids" or n.endswith("-grids")


def _is_grid_path(path):
    """路径的任意一级目录是阵列图目录"""
    parts = os.path.normpath(path).split(os.sep)
    return any(_is_grid_dir(x) for x in parts[:-1])


def _strip_emphasis(t):
    """去掉强调用的括号，但保留 tag 名字本身的括号，比如 yinlin_(wuthering_waves)"""
    t = t.strip()
    pairs = {"(": ")", "[": "]", "{": "}"}
    changed = True
    while changed and t:
        changed = False
        # 外层成对包裹：((best quality)) / [blue eyes]
        if t[0] in pairs and t[-1] == pairs[t[0]]:
            inner, depth, ok = t[1:-1], 0, True
            for ch in inner:
                if ch == t[0]:
                    depth += 1
                elif ch == pairs[t[0]]:
                    depth -= 1
                    if depth < 0:
                        ok = False
                        break
            if ok and depth == 0:
                t = inner.strip()
                changed = True
                continue
        # 跨逗号的强调：「(a」「b:1.2)」拆开后剩下不成对的半边
        for o, c in pairs.items():
            if t.startswith(o) and t.count(o) > t.count(c):
                t = t[1:].strip()
                changed = True
            if t.endswith(c) and t.count(c) > t.count(o):
                t = t[:-1].strip()
                changed = True
    return t


def split_prompt_tags(prompt):
    """把提示词拆成规范化的 tag：去权重/括号/LoRA 语法，空格统一成下划线，小写"""
    if not prompt:
        return []
    text = _LORA_TAG_RE.sub(",", prompt)
    text = text.replace("BREAK", ",").replace("\n", ",")
    out, seen = [], set()
    for raw in text.split(","):
        t = raw.strip()
        if not t:
            continue
        m = _WEIGHT_RE.match(t)
        if m:
            t = m.group(1)
        # 保留转义的括号（\( \)），其余强调括号去掉
        t = t.replace("\\(", "\0L").replace("\\)", "\0R")
        t = _strip_emphasis(t).replace("\0L", "(").replace("\0R", ")")
        t = re.sub(r":\s*[-+]?\d*\.?\d+$", "", t).strip()
        if not t or len(t) > 60:
            continue
        t = re.sub(r"\s+", "_", t.lower())
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


class CharacterDict:
    """角色 tag 词典：优先 WD14 的 selected_tags.csv（category 4 = 角色），没有就用启发式"""

    def __init__(self, search_dirs):
        self.names = set()
        for d in search_dirs:
            for p in glob.glob(os.path.join(d, "**", "selected_tags.csv"), recursive=True):
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        for row in csv.DictReader(f):
                            if str(row.get("category", "")).strip() == "4":
                                n = (row.get("name") or "").strip().lower()
                                if n:
                                    self.names.add(n)
                except OSError:
                    continue

    def is_char(self, tag):
        if self.names:
            return tag in self.names
        # 启发式：Danbooru 角色 tag 大多是「名字_(作品)」
        m = re.match(r"^[^()]+_\(([^()]+)\)$", tag)
        return bool(m) and m.group(1) not in _NON_CHAR_QUALIFIERS


def parse_file(path):
    """读一张图的生成参数，失败返回空字典（索引不因个别坏文件中断）"""
    try:
        import meta_engine as me
        size = os.path.getsize(path)
        m = me.read_metadata(path, size)
    except Exception:
        return {}
    p = m.params
    loras = []
    for lo in (p.loras or []):
        name = (lo.get("name") or "").strip()
        if name:
            w = lo.get("weight", lo.get("strength_model"))
            loras.append((os.path.splitext(os.path.basename(name.replace("\\", "/")))[0], "" if w is None else str(w)))
    for name, w in _LORA_TAG_RE.findall(m.positive_prompt or ""):
        name = name.strip()
        if name and not any(n == name for n, _w in loras):
            loras.append((name, w or ""))
    model = (p.model_name or "").strip()
    if model:
        model = os.path.splitext(os.path.basename(model.replace("\\", "/")))[0] \
            if model.lower().endswith((".safetensors", ".ckpt", ".gguf", ".sft")) \
            else os.path.basename(model.replace("\\", "/"))
    return {
        "width": m.width or 0, "height": m.height or 0,
        "prompt": m.positive_prompt or "", "negative": m.negative_prompt or "",
        "model": model, "seed": "" if p.seed is None else str(p.seed),
        "sampler": p.sampler_name or "", "source": m.source_type or "",
        "has_meta": 1 if (m.positive_prompt or model or p.seed is not None) else 0,
        "loras": loras,
    }


class OutputIndex:
    def __init__(self, char_dirs=()):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self.fts = True
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS outputs_fts USING fts5(prompt, negative)")
        except sqlite3.OperationalError:
            self.fts = False         # 极少数精简版 SQLite 没有 FTS5，退回 LIKE
        self.db.commit()
        self.chars = CharacterDict(char_dirs)
        self.scanning = False
        self.scan_state = {"done": 0, "total": 0, "phase": ""}

    # ---------------------------------------------------------- 扫描 ----
    def scan(self, sources, progress=None, cancelled=lambda: False):
        """
        sources: [(iid, [dir, ...])]
        返回 {added, updated, missing, relinked}
        """
        with self.lock:
            if self.scanning:
                return {"busy": True}
            self.scanning = True
        try:
            return self._scan(sources, progress, cancelled)
        finally:
            self.scanning = False

    def _scan(self, sources, progress, cancelled):
        found = {}          # path -> (iid, size, mtime)
        for iid, dirs in sources:
            for d in dirs:
                if not d or not os.path.isdir(d):
                    continue
                for dirpath, dirnames, filenames in os.walk(d):
                    dirnames[:] = [x for x in dirnames
                                   if not x.startswith(".") and not _is_grid_dir(x)]
                    for fn in filenames:
                        p = os.path.join(dirpath, fn)
                        if not kind_of(p):
                            continue
                        try:
                            st = os.stat(p)
                        except OSError:
                            continue
                        found.setdefault(p, (iid, st.st_size, st.st_mtime))
        with self.lock:
            rows = self.db.execute("SELECT id, path, size, mtime, missing FROM outputs").fetchall()
            # 阵列图不收录：已索引的 grids 记录直接清掉（即使在收藏夹里也不留），
            # 不参与后面的「文件消失」处理——文件还在盘上，只是我们不看了
            grid_ids = [r[0] for r in rows if _is_grid_path(r[1])]
            for oid in grid_ids:
                self.db.execute("DELETE FROM collection_items WHERE oid=?", (oid,))
                self._delete_row(oid)
            if grid_ids:
                self.db.commit()
            rows = [r for r in rows if r[0] not in set(grid_ids)]
        known = {r[1]: r for r in rows}
        new_paths = [p for p in found if p not in known]
        changed = [p for p in found if p in known and
                   (known[p][2] != found[p][1] or abs(known[p][3] - found[p][2]) > 1 or known[p][4])]
        gone = [r for p, r in known.items() if p not in found and not r[4]]

        # 被改名/挪动的文件：新路径的 (大小, 时间) 跟消失的记录一致 → 接上原记录
        gone_by_fp = {}
        for r in gone:
            gone_by_fp.setdefault((r[2], int(r[3])), []).append(r)
        relinked = 0
        still_new = []
        for p in new_paths:
            iid, size, mtime = found[p]
            cand = gone_by_fp.get((size, int(mtime)))
            if cand:
                r = cand.pop(0)
                with self.lock:
                    self.db.execute("UPDATE outputs SET path=?, name=?, iid=?, missing=0 WHERE id=?",
                                    (p, os.path.basename(p), iid, r[0]))
                gone = [g for g in gone if g[0] != r[0]]
                relinked += 1
            else:
                still_new.append(p)

        todo = still_new + changed
        total = len(todo)
        self.scan_state = {"done": 0, "total": total, "phase": "解析参数"}
        t_last = 0
        batch = 0
        for i, p in enumerate(todo, 1):
            if cancelled():
                break
            iid, size, mtime = found[p]
            self._upsert(p, iid, size, mtime)
            batch += 1
            if batch >= 50:
                with self.lock:
                    self.db.commit()
                batch = 0
            self.scan_state["done"] = i
            now = time.monotonic()
            if progress and (now - t_last > 0.3 or i == total):
                t_last = now
                progress({"done": i, "total": total})
        with self.lock:
            # 消失的文件：在收藏夹里的保留记录并标记失效，其余直接删掉
            fav = {r[0] for r in self.db.execute("SELECT DISTINCT oid FROM collection_items")}
            missing = 0
            for r in gone:
                if r[0] in fav:
                    self.db.execute("UPDATE outputs SET missing=1 WHERE id=?", (r[0],))
                    missing += 1
                else:
                    self._delete_row(r[0])
            self.db.commit()
        return {"added": len(still_new), "updated": len(changed), "missing": missing,
                "relinked": relinked, "total_files": len(found)}

    def _delete_row(self, oid):
        self.db.execute("DELETE FROM outputs WHERE id=?", (oid,))
        self.db.execute("DELETE FROM output_tags WHERE oid=?", (oid,))
        self.db.execute("DELETE FROM output_loras WHERE oid=?", (oid,))
        if self.fts:
            self.db.execute("DELETE FROM outputs_fts WHERE rowid=?", (oid,))

    def _upsert(self, path, iid, size, mtime):
        kind = kind_of(path)
        meta = parse_file(path) if kind == "image" else {}
        tags = split_prompt_tags(meta.get("prompt", ""))
        with self.lock:
            cur = self.db.execute("SELECT id FROM outputs WHERE path=?", (path,)).fetchone()
            vals = (os.path.basename(path), iid, kind, size, mtime, meta.get("width", 0),
                    meta.get("height", 0), meta.get("prompt", ""), meta.get("negative", ""),
                    meta.get("model", ""), meta.get("seed", ""), meta.get("sampler", ""),
                    meta.get("source", ""), meta.get("has_meta", 0))
            if cur:
                oid = cur[0]
                self.db.execute("""UPDATE outputs SET name=?, iid=?, kind=?, size=?, mtime=?, width=?,
                    height=?, prompt=?, negative=?, model=?, seed=?, sampler=?, source=?, has_meta=?,
                    missing=0 WHERE id=?""", vals + (oid,))
                self.db.execute("DELETE FROM output_tags WHERE oid=?", (oid,))
                self.db.execute("DELETE FROM output_loras WHERE oid=?", (oid,))
                if self.fts:
                    self.db.execute("DELETE FROM outputs_fts WHERE rowid=?", (oid,))
            else:
                oid = self.db.execute("""INSERT INTO outputs(name, iid, kind, size, mtime, width, height,
                    prompt, negative, model, seed, sampler, source, has_meta, path, added)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                      vals + (path, time.time())).lastrowid
            if tags:
                self.db.executemany("INSERT INTO output_tags(oid, tag, is_char) VALUES(?,?,?)",
                                    [(oid, t, 1 if self.chars.is_char(t) else 0) for t in tags])
            if meta.get("loras"):
                self.db.executemany("INSERT INTO output_loras(oid, name, weight) VALUES(?,?,?)",
                                    [(oid, n, w) for n, w in meta["loras"]])
            if self.fts and (meta.get("prompt") or meta.get("negative")):
                self.db.execute("INSERT INTO outputs_fts(rowid, prompt, negative) VALUES(?,?,?)",
                                (oid, meta.get("prompt", ""), meta.get("negative", "")))

    # ---------------------------------------------------------- 查询 ----
    def _where(self, f):
        where, args = ["1=1"], []
        f = f or {}
        if not f.get("include_missing"):
            where.append("o.missing=0")
        if f.get("kind"):
            where.append("o.kind=?")
            args.append(f["kind"])
        if f.get("iid"):
            where.append("o.iid=?")
            args.append(f["iid"])
        if f.get("since"):
            where.append("o.mtime>=?")
            args.append(float(f["since"]))
        if f.get("until"):
            where.append("o.mtime<?")
            args.append(float(f["until"]))
        if f.get("model"):
            where.append("o.model=?")
            args.append(f["model"])
        for t in f.get("tags") or []:
            where.append("o.id IN (SELECT oid FROM output_tags WHERE tag=?)")
            args.append(t)
        for c in f.get("chars") or []:
            where.append("o.id IN (SELECT oid FROM output_tags WHERE tag=? AND is_char=1)")
            args.append(c)
        if f.get("lora"):
            where.append("o.id IN (SELECT oid FROM output_loras WHERE name=?)")
            args.append(f["lora"])
        if f.get("collection"):
            where.append("o.id IN (SELECT oid FROM collection_items WHERE cid=?)")
            args.append(int(f["collection"]))
        q = (f.get("q") or "").strip()
        if q:
            if self.fts:
                # 每个词都加引号，避免用户输入的括号/冒号被当成 FTS 语法
                terms = " ".join('"' + w.replace('"', '""') + '"' for w in q.split())
                where.append("o.id IN (SELECT rowid FROM outputs_fts WHERE outputs_fts MATCH ?)")
                args.append(terms)
            else:
                for w in q.split():
                    where.append("(o.prompt LIKE ? OR o.name LIKE ?)")
                    args += [f"%{w}%", f"%{w}%"]
        return " AND ".join(where), args

    def query(self, f):
        where, args = self._where(f)
        order = "o.mtime DESC" if (f or {}).get("sort", "new") == "new" else "o.mtime ASC"
        with self.lock:
            rows = self.db.execute(
                f"SELECT o.id, o.kind, o.width, o.height, o.missing FROM outputs o WHERE {where} "
                f"ORDER BY {order} LIMIT 100000", args).fetchall()
        return {"ids": [r[0] for r in rows], "kinds": "".join((r[1] or "o")[0] for r in rows),
                "ratios": [round((r[2] / r[3]) if r[2] and r[3] else 1, 3) for r in rows],
                "missing": [r[0] for r in rows if r[4]]}

    def facets(self, f):
        """当前筛选条件下的角色 / LoRA / 模型 / tag 计数"""
        where, args = self._where(f)
        sub = f"SELECT o.id FROM outputs o WHERE {where}"
        with self.lock:
            chars = self.db.execute(
                f"SELECT tag, COUNT(*) c FROM output_tags WHERE is_char=1 AND oid IN ({sub}) "
                "GROUP BY tag ORDER BY c DESC LIMIT 200", args).fetchall()
            loras = self.db.execute(
                f"SELECT name, COUNT(DISTINCT oid) c FROM output_loras WHERE oid IN ({sub}) "
                "GROUP BY name ORDER BY c DESC LIMIT 200", args).fetchall()
            models = self.db.execute(
                f"SELECT model, COUNT(*) c FROM outputs o WHERE {where} AND model<>'' "
                "GROUP BY model ORDER BY c DESC LIMIT 200", args).fetchall()
            tags = self.db.execute(
                f"SELECT tag, COUNT(*) c FROM output_tags WHERE is_char=0 AND oid IN ({sub}) "
                "GROUP BY tag ORDER BY c DESC LIMIT 300", args).fetchall()
            kinds = self.db.execute(
                f"SELECT kind, COUNT(*) FROM outputs o WHERE {where} GROUP BY kind", args).fetchall()
        return {"chars": chars, "loras": loras, "models": models, "tags": tags,
                "kinds": dict(kinds)}

    def detail(self, oid):
        with self.lock:
            r = self.db.execute("""SELECT id, path, name, iid, kind, size, mtime, width, height, prompt,
                negative, model, seed, sampler, source, missing FROM outputs WHERE id=?""",
                                (oid,)).fetchone()
            if not r:
                return None
            tags = self.db.execute("SELECT tag, is_char FROM output_tags WHERE oid=?", (oid,)).fetchall()
            loras = self.db.execute("SELECT name, weight FROM output_loras WHERE oid=?", (oid,)).fetchall()
            cols = [c[0] for c in self.db.execute(
                "SELECT c.id FROM collection_items i JOIN collections c ON c.id=i.cid WHERE i.oid=?",
                (oid,)).fetchall()]
        keys = ["id", "path", "name", "iid", "kind", "size", "mtime", "width", "height", "prompt",
                "negative", "model", "seed", "sampler", "source", "missing"]
        d = dict(zip(keys, r))
        d["chars"] = [t for t, c in tags if c]
        d["tags"] = [t for t, c in tags if not c]
        d["loras"] = [{"name": n, "weight": w} for n, w in loras]
        d["collections"] = cols
        return d

    def path_of(self, oid):
        with self.lock:
            r = self.db.execute("SELECT path, mtime FROM outputs WHERE id=?", (oid,)).fetchone()
        return r if r else (None, None)

    def remove(self, oid):
        with self.lock:
            self._delete_row(oid)
            self.db.execute("DELETE FROM collection_items WHERE oid=?", (oid,))
            self.db.commit()

    def count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM outputs WHERE missing=0").fetchone()[0]

    # ---------------------------------------------------------- 收藏夹 ----
    def collections(self):
        with self.lock:
            rows = self.db.execute("""SELECT c.id, c.name, COUNT(i.oid) FROM collections c
                LEFT JOIN collection_items i ON i.cid=c.id GROUP BY c.id ORDER BY c.created""").fetchall()
        return [{"id": r[0], "name": r[1], "count": r[2]} for r in rows]

    def collection_create(self, name):
        name = (name or "").strip()[:40]
        if not name:
            raise ValueError("名字不能为空")
        with self.lock:
            try:
                cid = self.db.execute("INSERT INTO collections(name, created) VALUES(?,?)",
                                      (name, time.time())).lastrowid
            except sqlite3.IntegrityError:
                raise ValueError("已有同名收藏夹")
            self.db.commit()
        return cid

    def collection_rename(self, cid, name):
        name = (name or "").strip()[:40]
        if not name:
            raise ValueError("名字不能为空")
        with self.lock:
            try:
                self.db.execute("UPDATE collections SET name=? WHERE id=?", (name, int(cid)))
            except sqlite3.IntegrityError:
                raise ValueError("已有同名收藏夹")
            self.db.commit()

    def collection_delete(self, cid):
        with self.lock:
            self.db.execute("DELETE FROM collection_items WHERE cid=?", (int(cid),))
            self.db.execute("DELETE FROM collections WHERE id=?", (int(cid),))
            # 只因为被收藏才留着的失效记录一并清掉
            self.db.execute("""DELETE FROM outputs WHERE missing=1 AND id NOT IN
                (SELECT oid FROM collection_items)""")
            self.db.commit()

    def collection_set(self, cid, oids, on):
        with self.lock:
            for oid in oids:
                if on:
                    self.db.execute("INSERT OR IGNORE INTO collection_items(cid, oid, added) VALUES(?,?,?)",
                                    (int(cid), int(oid), time.time()))
                else:
                    self.db.execute("DELETE FROM collection_items WHERE cid=? AND oid=?", (int(cid), int(oid)))
            self.db.commit()

    def collection_paths(self, cid):
        with self.lock:
            return [r[0] for r in self.db.execute(
                """SELECT o.path FROM collection_items i JOIN outputs o ON o.id=i.oid
                   WHERE i.cid=? AND o.missing=0""", (int(cid),)).fetchall()]


# ============================================================
# 本地文件服务：给网页提供原图 / 视频（支持 Range）和缩略图缓存
# ============================================================

def thumb_path(oid, mtime):
    return os.path.join(THUMB_DIR, str(int(oid) % 256), f"{int(oid)}_{int(mtime or 0)}.webp")


class _Handler(http.server.BaseHTTPRequestHandler):
    index = None
    token = ""

    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _parse(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if (qs.get("t") or [""])[0] != self.token:
            return None, None
        parts = u.path.strip("/").split("/")
        if len(parts) != 2 or not parts[1].isdigit():
            return None, None
        return parts[0], int(parts[1])

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        route, oid = self._parse()
        if route is None:
            self.send_error(403)
            return
        path, mtime = self.index.path_of(oid)
        if route == "t":
            tp = thumb_path(oid, mtime) if path else None
            if not tp or not os.path.isfile(tp):
                self.send_response(404)
                self._cors()
                self.end_headers()
                return
            return self._send_file(tp, "image/webp", cache=True)
        if route == "f":
            if not path or not os.path.isfile(path):
                self.send_error(404)
                return
            import mimetypes
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            return self._send_file(path, ctype)
        self.send_error(404)

    def do_POST(self):
        route, oid = self._parse()
        if route != "t":
            self.send_error(403)
            return
        path, mtime = self.index.path_of(oid)
        n = int(self.headers.get("Content-Length") or 0)
        if not path or n <= 0 or n > 512 * 1024:
            self.send_error(400)
            return
        data = self.rfile.read(n)
        tp = thumb_path(oid, mtime)
        try:
            os.makedirs(os.path.dirname(tp), exist_ok=True)
            with open(tp, "wb") as f:
                f.write(data)
        except OSError:
            self.send_error(500)
            return
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _send_file(self, p, ctype, cache=False):
        try:
            size = os.path.getsize(p)
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                if a.strip():
                    start = int(a)
                    if b.strip():
                        end = min(int(b), size - 1)
                elif b.strip():
                    start = max(0, size - int(b))
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "max-age=86400" if cache else "no-cache")
            self._cors()
            self.end_headers()
            with open(p, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    buf = f.read(min(1024 * 256, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
        except (OSError, ValueError, ConnectionError):
            pass


def start_file_server(index):
    """起在 127.0.0.1 的随机端口，URL 带一次性 token，只能按索引 id 取文件"""
    token = secrets.token_urlsafe(16)
    handler = type("Handler", (_Handler,), {"index": index, "token": token})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="output-file-server").start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", token
