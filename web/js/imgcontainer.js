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

  window.ImgContainer = { readContainer: readContainer };
})();
