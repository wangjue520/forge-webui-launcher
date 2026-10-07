/* imgcontainer.js —— 浏览器端的图片容器读取器。
 *
 * 为什么要在前端再写一遍（Python 侧已经有 container_reader.py）：
 *
 * 网页拖进来一个文件时，浏览器手里已经有完整字节了，但**拿不到磁盘路径**
 * （安全限制）。原来的做法是把 drop 事件绕到 Python 侧去取
 * pywebviewFullPath，再把路径推回前端 —— 那条桥要序列化整个事件对象，
 * 实测是整个流程里最慢的一环，比解析本身慢几千倍。
 *
 * 换个思路：既然字节就在手上，那就地把元数据抠出来，只把这几 KB 文本送过去。
 * 图片本体一个字节都不过桥，路径也不需要了。
 *
 * 抠出来的结构跟 Python 侧 container_reader 产出的完全一致
 * （键名对齐 PIL 的 img.info），所以后端的解析调度和各家格式的门禁
 * 全部原样复用，不存在"前端一套后端一套"。
 */
(function () {
  "use strict";

  const PNG_SIG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

  // 单个文本块的上限。正常的 parameters / workflow 再大也就几百 KB，
  // 超过基本是坏文件，跳过即可，别让它拖住整个解析。
  const MAX_TEXT = 32 * 1024 * 1024;

  const td = (bytes, enc) => new TextDecoder(enc || "utf-8", { fatal: false }).decode(bytes);

  function startsWith(buf, sig, off) {
    off = off || 0;
    for (let i = 0; i < sig.length; i++) if (buf[off + i] !== sig[i]) return false;
    return true;
  }

  /* ============================ PNG ============================ */

  function readPNG(bytes) {
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const info = {};
    let width = 0, height = 0, pos = 8;

    while (pos + 8 <= bytes.length) {
      const len = dv.getUint32(pos);
      const type = td(bytes.subarray(pos + 4, pos + 8), "latin1");
      const dataStart = pos + 8;
      if (len > bytes.length) break;
      const data = bytes.subarray(dataStart, dataStart + len);

      if (type === "IHDR" && len >= 8) {
        width = dv.getUint32(dataStart);
        height = dv.getUint32(dataStart + 4);
      } else if (type === "tEXt" || type === "iTXt" || type === "zTXt") {
        if (len <= MAX_TEXT) absorbText(info, type, data);
      } else if (type === "eXIf") {
        const parsed = parseTIFF(data);
        if (parsed) mergeExif(info, parsed, data);
      } else if (type === "IDAT" || type === "IEND") {
        // 文本块按规范都在 IDAT 之前；真有写在后面的，继续扫也无妨，
        // 但 IEND 一定是结束
        if (type === "IEND") break;
      }
      pos = dataStart + len + 4;
    }
    return { info: info, width: width, height: height, format: "PNG" };
  }

  function absorbText(info, type, data) {
    try {
      const nul = data.indexOf(0);
      if (nul < 0) return;
      const key = td(data.subarray(0, nul), "latin1");

      if (type === "tEXt") {
        // 规范说是 latin-1，但 ComfyUI 之类的工具照样往里写 UTF-8，
        // 所以先按 UTF-8 试，不行再退回 latin-1，免得中文提示词变乱码
        info[key] = bestText(data.subarray(nul + 1));
      } else if (type === "iTXt") {
        // keyword\0 compFlag(1) compMethod(1) lang\0 translated\0 text
        const compFlag = data[nul + 1];
        let p = nul + 3;
        p = data.indexOf(0, p) + 1;      // lang
        p = data.indexOf(0, p) + 1;      // translated
        const body = data.subarray(p);
        info[key] = compFlag ? td(inflate(body)) : td(body);
      } else if (type === "zTXt") {
        info[key] = bestText(inflate(data.subarray(nul + 2)));
      }
    } catch (e) { /* 单个块坏了不影响别的 */ }
  }

  function bestText(bytes) {
    const s = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
    // TextDecoder 非 fatal 模式会把非法字节变成 U+FFFD，
    // 出现替换字符就说明不是 UTF-8，退回 latin-1
    return s.indexOf("\uFFFD") >= 0 ? td(bytes, "latin1") : s;
  }

  // zlib 解压。浏览器有现成的 DecompressionStream，但它是异步的，
  // 而这里是同步流程；好在压缩过的文本块很少见（zTXt / 压缩 iTXt），
  // 遇到了就退回让后端按路径重读一次，不值得为它把整条链路改成异步。
  function inflate(bytes) {
    if (typeof DecompressionStream === "undefined") throw new Error("no inflate");
    throw new Error("deferred");   // 交给后端处理
  }

  /* ============================ JPEG ============================ */

  function readJPEG(bytes) {
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const info = {};
    let width = 0, height = 0, pos = 2;
    const comments = [];
    let exifTiff = null;

    while (pos < bytes.length) {
      if (bytes[pos] !== 0xff) { pos++; continue; }
      let m = pos + 1;
      while (m < bytes.length && bytes[m] === 0xff) m++;
      if (m >= bytes.length) break;
      const mk = bytes[m];
      if (mk === 0xd8 || mk === 0x01 || (mk >= 0xd0 && mk <= 0xd7)) { pos = m + 1; continue; }
      if (mk === 0xd9) break;
      if (m + 3 > bytes.length) break;
      const size = dv.getUint16(m + 1);
      const dataStart = m + 3, dataEnd = m + 1 + size;
      if (dataEnd > bytes.length) break;
      if (mk === 0xda) break;   // SOS 之后全是压缩数据

      const data = bytes.subarray(dataStart, dataEnd);
      if (mk === 0xe1) {
        if (startsWith(data, [0x45, 0x78, 0x69, 0x66, 0x00, 0x00])) {
          if (!exifTiff) exifTiff = data.subarray(6);
        } else {
          const head = td(data.subarray(0, 200), "latin1");
          if (head.indexOf("<x:xmpmeta") >= 0 || head.indexOf("ns.adobe.com/xap/") >= 0) {
            if (!info["XML:com.adobe.xmp"]) info["XML:com.adobe.xmp"] = td(data);
          }
        }
      } else if (mk === 0xfe) {
        const t = td(data).replace(/\0+$/, "").trim();
        if (t) comments.push(t);
      } else if (mk >= 0xc0 && mk <= 0xcf && mk !== 0xc4 && mk !== 0xc8 && mk !== 0xcc) {
        if (data.length >= 5) {
          height = dv.getUint16(dataStart + 1);
          width = dv.getUint16(dataStart + 3);
        }
      }
      pos = dataEnd;
    }

    if (exifTiff) {
      const parsed = parseTIFF(exifTiff);
      if (parsed) mergeExif(info, parsed, exifTiff);
    }
    if (comments.length && !info._jpeg_comment) info._jpeg_comment = comments.join("\n");
    return { info: info, width: width, height: height, format: "JPEG" };
  }

  /* ============================ WebP ============================ */

  function readWebP(bytes) {
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const info = {};
    let width = 0, height = 0, pos = 12;

    while (pos + 8 <= bytes.length) {
      const fourcc = td(bytes.subarray(pos, pos + 4), "latin1");
      const size = dv.getUint32(pos + 4, true);
      const padded = size + (size & 1);
      const data = bytes.subarray(pos + 8, pos + 8 + size);

      if (fourcc === "EXIF") {
        const body = startsWith(data, [0x45, 0x78, 0x69, 0x66, 0x00, 0x00]) ? data.subarray(6) : data;
        const parsed = parseTIFF(body);
        if (parsed) mergeExif(info, parsed, body);
      } else if (fourcc === "XMP ") {
        if (!info["XML:com.adobe.xmp"]) info["XML:com.adobe.xmp"] = td(data);
      } else if (fourcc === "VP8X" && size >= 10) {
        width = (data[4] | (data[5] << 8) | (data[6] << 16)) + 1;
        height = (data[7] | (data[8] << 8) | (data[9] << 16)) + 1;
      } else if (fourcc === "VP8 " && !width && size >= 10) {
        if (data[3] === 0x9d && data[4] === 0x01 && data[5] === 0x2a) {
          width = dv.getUint16(pos + 8 + 6, true) & 0x3fff;
          height = dv.getUint16(pos + 8 + 8, true) & 0x3fff;
        }
      } else if (fourcc === "VP8L" && !width && size >= 5 && data[0] === 0x2f) {
        const bits = dv.getUint32(pos + 9, true);
        width = (bits & 0x3fff) + 1;
        height = ((bits >> 14) & 0x3fff) + 1;
      }
      pos += 8 + padded;
    }
    return { info: info, width: width, height: height, format: "WEBP" };
  }

  /* ============================ TIFF / EXIF ============================ */

  const EXIF_STRING_TAGS = {
    0x010e: "ImageDescription", 0x010f: "Make", 0x0110: "Model",
    0x0131: "Software", 0x013b: "Artist", 0x8298: "Copyright",
  };
  const TAG_USER_COMMENT = 0x9286, TAG_EXIF_IFD = 0x8769, TAG_XMP = 0x02bc;
  const TYPE_SIZE = { 1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8, 11: 4, 12: 8 };

  function parseTIFF(tiff) {
    if (tiff.length < 8) return null;
    let little;
    if (tiff[0] === 0x49 && tiff[1] === 0x49) little = true;
    else if (tiff[0] === 0x4d && tiff[1] === 0x4d) little = false;
    else return null;

    const dv = new DataView(tiff.buffer, tiff.byteOffset, tiff.byteLength);
    const u16 = (o) => dv.getUint16(o, little);
    const u32 = (o) => dv.getUint32(o, little);
    const strings = {};
    let userComment = null, xmp = null;

    function readIFD(off, depth) {
      if (depth > 4 || off <= 0 || off + 2 > tiff.length) return;
      const n = u16(off);
      if (n > 4096) return;
      for (let i = 0; i < n; i++) {
        const e = off + 2 + i * 12;
        if (e + 12 > tiff.length) return;
        const tag = u16(e), type = u16(e + 2), num = u32(e + 4);
        const size = (TYPE_SIZE[type] || 1) * num;
        if (size > tiff.length) continue;
        if (tag === TAG_EXIF_IFD) { readIFD(u32(e + 8), depth + 1); continue; }
        const vOff = size <= 4 ? e + 8 : u32(e + 8);
        if (vOff + size > tiff.length) continue;
        const raw = tiff.subarray(vOff, vOff + size);
        if (EXIF_STRING_TAGS[tag]) {
          const t = td(raw).split("\0")[0].trim();
          if (t && !strings[EXIF_STRING_TAGS[tag]]) strings[EXIF_STRING_TAGS[tag]] = t;
        } else if (tag === TAG_USER_COMMENT && userComment === null) {
          userComment = decodeUserComment(raw, little);
        } else if (tag === TAG_XMP && xmp === null) {
          xmp = td(raw).split("\0")[0].trim() || null;
        }
      }
    }
    try { readIFD(u32(4), 0); } catch (e) { return null; }
    return { strings: strings, usercomment: userComment, xmp: xmp };
  }

  function decodeUserComment(raw, little) {
    if (!raw || !raw.length) return null;
    const prefix = td(raw.subarray(0, 8), "latin1");
    const body = raw.subarray(8);
    let text = null;

    if (prefix.indexOf("UNICODE") === 0) {
      // A1111 写 UTF-16。字节序理论上跟 TIFF 一致，实测有反着写的，
      // 所以先按 BOM / 零字节分布猜一次，再拿另一个兜底
      let order;
      if (body[0] === 0xfe && body[1] === 0xff) order = ["utf-16be", "utf-16le"];
      else if (body[0] === 0xff && body[1] === 0xfe) order = ["utf-16le", "utf-16be"];
      else if (body[0] === 0 && body[1] !== 0) order = ["utf-16be", "utf-16le"];
      else order = little ? ["utf-16le", "utf-16be"] : ["utf-16be", "utf-16le"];
      for (const enc of order) {
        try {
          const c = new TextDecoder(enc).decode(body).replace(/[\0\uFEFF]/g, "").trim();
          if (c) { text = c; break; }
        } catch (e) { /* 换下一个 */ }
      }
    } else if (prefix.indexOf("ASCII") === 0 || prefix.toLowerCase().indexOf("charset") === 0
               || /^\0{8}$/.test(prefix)) {
      text = td(body).replace(/\0+/g, "").trim();
    }
    if (!text) text = td(raw).replace(/\0+/g, "").trim();
    return text || null;
  }

  function mergeExif(info, parsed, tiffBytes) {
    for (const k in parsed.strings) if (!(k in info)) info[k] = parsed.strings[k];
    if (parsed.usercomment && !info._usercomment) info._usercomment = parsed.usercomment;
    if (parsed.xmp && !info["XML:com.adobe.xmp"]) info["XML:com.adobe.xmp"] = parsed.xmp;
    // EXIF 原始字节不往后端送 —— 字符串字段已经解出来并入 info 了，
    // 后端那边走的正是"info 自有键优先"这条路，不需要原始 TIFF
  }

  /* ============================ 对外入口 ============================ */

  /**
   * 从 ArrayBuffer 抠出容器数据。
   * 返回 {info, width, height, format}，认不出来返回 null，
   * 遇到需要解压的文本块返回 {deferred: true} 让调用方回退到后端。
   */
  function readContainer(arrayBuffer) {
    const bytes = new Uint8Array(arrayBuffer);
    try {
      if (startsWith(bytes, PNG_SIG)) return readPNG(bytes);
      if (bytes[0] === 0xff && bytes[1] === 0xd8) return readJPEG(bytes);
      if (td(bytes.subarray(0, 4), "latin1") === "RIFF"
          && td(bytes.subarray(8, 12), "latin1") === "WEBP") return readWebP(bytes);
    } catch (e) {
      if (e && e.message === "deferred") return { deferred: true };
      return null;
    }
    return null;
  }

  /* ============================ 视频 ============================ */
  /* Forge Neo / H3 用 ffmpeg -metadata description=<A1111 参数> 存视频：
   * mp4 在 moov/udta/meta/ilst/desc，mkv 在 Tags/SimpleTag。ComfyUI 视频写 prompt/workflow。
   * 视频可能几百 MB，而 moov 常在文件末尾 —— 用 File.slice 跳着读，只读需要的几 KB。
   * 和 Python 侧 meta_engine/container_reader.py 的视频部分是同一套逻辑、同样的输出键。 */

  const VIDEO_EXT_RE = /\.(mp4|m4v|mov|mkv|webm)$/i;
  function isVideoFile(file) {
    return !!file && (/^video\//.test(file.type || "") || VIDEO_EXT_RE.test(file.name || ""));
  }
  async function sliceBytes(file, start, end) {
    return new Uint8Array(await file.slice(start, end).arrayBuffer());
  }
  const u32 = (b, o) => ((b[o] << 24) >>> 0) + (b[o + 1] << 16) + (b[o + 2] << 8) + b[o + 3];
  const u16 = (b, o) => (b[o] << 8) | b[o + 1];

  function* boxes(b, start, end) {
    let pos = start;
    while (pos + 8 <= end) {
      let size = u32(b, pos), hdr = 8;
      const typ = td(b.subarray(pos + 4, pos + 8), "latin1");
      if (size === 1) { size = u32(b, pos + 8) * 4294967296 + u32(b, pos + 12); hdr = 16; }
      else if (size === 0) size = end - pos;
      if (size < hdr || pos + size > end) return;
      yield [typ, pos + hdr, pos + size];
      pos += size;
    }
  }
  function dataText(b, s, e) {
    for (const [t, ds, de] of boxes(b, s, e)) if (t === "data" && de - ds >= 8) return bestText(b.subarray(ds + 8, de));
    return null;
  }
  function metaItems(b, s, e, out) {
    let handler = "", keys = [], ilst = null;
    for (const [t, cs, ce] of boxes(b, s + 4, e)) {
      if (t === "hdlr" && ce - cs >= 12) handler = td(b.subarray(cs + 8, cs + 12), "latin1");
      else if (t === "keys") {
        const n = u32(b, cs + 4); let p = cs + 8;
        for (let i = 0; i < n && p + 8 <= ce; i++) { const ks = u32(b, p); keys.push(td(b.subarray(p + 8, p + ks))); p += ks; }
      } else if (t === "ilst") ilst = [cs, ce];
    }
    if (!ilst) return;
    for (const [t, cs, ce] of boxes(b, ilst[0], ilst[1])) {
      let name = t;
      if (handler === "mdta" && keys.length) {
        const idx = u32(b, cs - 4);
        if (idx >= 1 && idx <= keys.length) name = keys[idx - 1].startsWith("com.") ? keys[idx - 1].split(".").pop() : keys[idx - 1];
      }
      const v = dataText(b, cs, ce);
      if (v && !(name in out)) out[name] = v;
    }
  }
  function parseMoov(b) {
    const texts = {};
    let width = 0, height = 0, duration = null;
    const hdr = u32(b, 0) === 1 ? 16 : 8;
    for (const [t, s, e] of boxes(b, hdr, b.length)) {
      if (t === "mvhd" && e - s >= 20) {
        const v1 = b[s] === 1;
        const ts = u32(b, s + (v1 ? 20 : 12));
        const dur = v1 ? u32(b, s + 24) * 4294967296 + u32(b, s + 28) : u32(b, s + 16);
        if (ts) duration = dur / ts;
      } else if (t === "trak") {
        for (const [t2, s2, e2] of boxes(b, s, e)) {
          if (t2 === "tkhd" && e2 - s2 >= 84) {
            const off = s2 + (b[s2] === 1 ? 88 : 76);
            if (off + 8 <= e2) {
              const w = u32(b, off) >>> 16, h = u32(b, off + 4) >>> 16;
              if (w * h > width * height) { width = w; height = h; }
            }
          }
        }
      } else if (t === "udta") {
        for (const [t2, s2, e2] of boxes(b, s, e)) {
          if (t2 === "meta") metaItems(b, s2, e2, texts);
          else if (t2.charCodeAt(0) === 0xa9 && e2 - s2 >= 4) {
            const n = u16(b, s2);
            if (n > 0 && n <= e2 - s2 - 4 && !(t2 in texts)) texts[t2] = bestText(b.subarray(s2 + 4, s2 + 4 + n));
          }
        }
      } else if (t === "meta") metaItems(b, s, e, texts);
    }
    return { info: videoInfo(texts), width: width, height: height, format: videoFormat("MP4", duration) };
  }
  async function readMP4(file) {
    let pos = 0;
    const size = file.size;
    while (pos + 8 <= size) {
      const h = await sliceBytes(file, pos, pos + 16);
      if (h.length < 8) break;
      let bs = u32(h, 0);
      const typ = td(h.subarray(4, 8), "latin1");
      if (bs === 1 && h.length >= 16) bs = u32(h, 8) * 4294967296 + u32(h, 12);
      else if (bs === 0) bs = size - pos;
      if (bs < 8) break;
      if (typ === "moov") {
        if (bs > 64 * 1024 * 1024) return null;
        return parseMoov(await sliceBytes(file, pos, pos + bs));
      }
      pos += bs;
    }
    return null;
  }

  // ---- Matroska / WebM ----
  function vint(b, o, isId) {
    const first = b[o];
    if (first === undefined) return null;
    let mask = 0x80, len = 1;
    while (len <= 8 && !(first & mask)) { mask >>= 1; len++; }
    if (len > (isId ? 4 : 8)) return null;
    let val = isId ? first : (first & (mask - 1));
    for (let i = 1; i < len; i++) val = val * 256 + b[o + i];
    const unknown = !isId && val === Math.pow(2, 7 * len) - 1;
    return { val: val, len: len, unknown: unknown };
  }
  // 读 [start, end) 里的子元素头（每个元素头最多 12 字节）
  async function ebmlChildren(file, start, end, cb) {
    let pos = start;
    end = Math.min(end, file.size);
    while (pos < end) {
      const h = await sliceBytes(file, pos, Math.min(pos + 12, file.size));
      const id = vint(h, 0, true); if (!id) return;
      const sz = vint(h, id.len, false); if (!sz) return;
      const ds = pos + id.len + sz.len;
      const stop = await cb(id.val, ds, sz.unknown ? null : sz.val);
      if (stop === true || sz.unknown) return;
      pos = ds + sz.val;
    }
  }
  const beUint = (b) => { let v = 0; for (let i = 0; i < b.length && i < 8; i++) v = v * 256 + b[i]; return v; };
  async function readMKV(file) {
    const texts = {};
    let width = 0, height = 0, duration = null, scale = 1000000, seg = null;
    await ebmlChildren(file, 0, file.size, async (id, s, n) => {
      if (id === 0x18538067) { seg = [s, n === null ? file.size : s + n]; return true; }
    });
    if (!seg) return null;
    const todo = [];
    async function visit(id, s, n) {
      const end = s + (n || 0);
      if (id === 0x114D9B74) {
        await ebmlChildren(file, s, end, async (sid, ss, sn) => {
          if (sid !== 0x4DBB) return;
          let target = null, tpos = null;
          await ebmlChildren(file, ss, ss + (sn || 0), async (cid, cs, cn) => {
            const v = await sliceBytes(file, cs, cs + (cn || 0));
            if (cid === 0x53AB) target = beUint(v);
            else if (cid === 0x53AC) tpos = beUint(v);
          });
          if ((target === 0x1254C367 || target === 0x1654AE6B || target === 0x1549A966) && tpos !== null) todo.push(seg[0] + tpos);
        });
      } else if (id === 0x1549A966) {
        await ebmlChildren(file, s, end, async (cid, cs, cn) => {
          const v = await sliceBytes(file, cs, cs + (cn || 0));
          if (cid === 0x2AD7B1) scale = beUint(v) || scale;
          else if (cid === 0x4489 && (cn === 4 || cn === 8)) {
            const dv = new DataView(v.buffer, v.byteOffset, v.byteLength);
            duration = cn === 4 ? dv.getFloat32(0) : dv.getFloat64(0);
          }
        });
      } else if (id === 0x1654AE6B) {
        await ebmlChildren(file, s, end, async (tid, ts, tn) => {
          if (tid !== 0xAE) return;
          await ebmlChildren(file, ts, ts + (tn || 0), async (vid, vs, vn) => {
            if (vid !== 0xE0) return;
            let w = 0, h = 0;
            await ebmlChildren(file, vs, vs + (vn || 0), async (pid, ps, pn) => {
              const v = await sliceBytes(file, ps, ps + (pn || 0));
              if (pid === 0xB0) w = beUint(v); else if (pid === 0xBA) h = beUint(v);
            });
            if (w * h > width * height) { width = w; height = h; }
          });
        });
      } else if (id === 0x1254C367) {
        await ebmlChildren(file, s, end, async (tid, ts, tn) => {
          if (tid !== 0x7373) return;
          await ebmlChildren(file, ts, ts + (tn || 0), async (sid, ss, sn) => {
            if (sid !== 0x67C8) return;
            let name = null, val = null;
            await ebmlChildren(file, ss, ss + (sn || 0), async (cid, cs, cn) => {
              if ((cid === 0x45A3 || cid === 0x4487) && cn !== null && cn <= MAX_TEXT) {
                const t = td(await sliceBytes(file, cs, cs + cn)).replace(/\0+$/, "");
                if (cid === 0x45A3) name = t; else val = t;
              }
            });
            if (name && val && !(name.toLowerCase() in texts)) texts[name.toLowerCase()] = val;
          });
        });
      }
    }
    const seen = new Set();
    await ebmlChildren(file, seg[0], seg[1], async (id, s, n) => {
      seen.add(s);
      if (id === 0x1F43B675) return true;    // Cluster：后面全是画面，剩下的交给 SeekHead
      await visit(id, s, n);
    });
    for (const pos of todo) {
      if (seen.has(pos)) continue;
      await ebmlChildren(file, pos, file.size, async (id, s, n) => { await visit(id, s, n); return true; });
    }
    if (duration !== null) duration = duration * scale / 1e9;
    const fmt = /\.webm$/i.test(file.name || "") ? "WEBM" : "MKV";
    return { info: videoInfo(texts), width: width, height: height, format: videoFormat(fmt, duration) };
  }

  function videoFormat(name, duration) {
    return duration ? `${name} 视频 ${duration.toFixed(1)} 秒` : `${name} 视频`;
  }
  const TEXT_KEYS = ["desc", "des", "ldes", "description", "cmt", "comment", "parameters", "inf"];
  function videoInfo(texts) {
    const info = {}, low = {};
    for (const k in texts) low[k.toLowerCase().replace(/^©/, "")] = texts[k];
    for (const k of ["prompt", "workflow"]) {
      if (typeof low[k] === "string" && low[k].trim().startsWith("{")) info[k] = low[k];
    }
    for (const k of ["comment", "cmt", "description", "des", "desc"]) {
      const v = low[k];
      if (typeof v === "string" && v.trim().startsWith("{") && !("prompt" in info)) {
        try {
          const obj = JSON.parse(v);
          if (obj && typeof obj === "object" && ("prompt" in obj || "workflow" in obj)) {
            for (const kk of ["prompt", "workflow"]) {
              if (kk in obj) info[kk] = typeof obj[kk] === "string" ? obj[kk] : JSON.stringify(obj[kk]);
            }
          }
        } catch (e) { /* 不是 JSON */ }
      }
    }
    if ("prompt" in info || "workflow" in info) return info;
    for (const k of TEXT_KEYS) {
      if (typeof low[k] === "string" && low[k].trim()) { info.parameters = low[k]; break; }
    }
    return info;
  }

  /** 视频文件（File/Blob）→ 容器结构；认不出返回 null */
  async function readVideo(file) {
    try {
      const head = await sliceBytes(file, 0, 16);
      if (td(head.subarray(4, 8), "latin1") === "ftyp") return await readMP4(file);
      if (head[0] === 0x1a && head[1] === 0x45 && head[2] === 0xdf && head[3] === 0xa3) return await readMKV(file);
    } catch (e) { /* 坏文件 */ }
    return null;
  }

  window.ImgContainer = { readContainer: readContainer, readVideo: readVideo, isVideoFile: isVideoFile };
})();
