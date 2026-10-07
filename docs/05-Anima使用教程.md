# 05 · Anima 使用教程

> [← 返回教程目录](README.md)

Anima 是 CircleStone Labs 出的**二次元文生图模型**（2B 参数，基于 NVIDIA Cosmos-Predict2）。
特点：认识大量动漫角色和画师；**标签和自然语言都能写**；8GB 显存就能跑。

和 SDXL 大模型最大的区别：**它不是一个文件就能用的，要三个文件配齐**——主模型 + 文本编码器 + VAE。很多人「出不了图 / 出一片噪点」都是因为只下了主模型。

> 授权：模型本身仅限非商用；**生成的图片可以商用**（包括出售、接稿）。以官方页面为准。

---

## 一、选哪个版本

| 版本 | 特点 | 步数 | CFG |
|---|---|---|---|
| **Turbo**（v1.1 / v1.0） | **最快**，8~12 步出图，出图稳，但多样性低一点。**作者推荐新手先用它** | 8~12 | **1** |
| **Aesthetic**（v1.1 / v1.0 / v1.0b） | **画质最好**，只用高质量图精调过，不用写质量词 | 30~50 | 4~5（3 也行） |
| Base（v1.0） | 原始底模，最灵活，主要给训练 LoRA 用 | 30~50 | 4~5 |
| preview / preview2 / preview3 | 早期预览版，已过时 | — | — |

**建议**：显卡一般 → Turbo；想要好看 → Aesthetic；要练 LoRA → Base。每个版本都是 4.18GB，可以都下来对比。

---

## 二、需要下载的 3 个文件

| 文件 | 作用 | 大小 | 下载地址 |
|---|---|---|---|
| 主模型，如 `anima-aesthetic-v1.1.safetensors` | 画图本体 | 4.18GB | C 站 [Anima Official](https://civitai.com/models/2458426)（用启动器下载）/ HuggingFace |
| `qwen_3_06b_base.safetensors` | 文本编码器（理解提示词） | 1.19GB | HuggingFace |
| `qwen_image_vae.safetensors` | VAE（生成最终图片） | 254MB | HuggingFace |

### 下载主模型（用启动器）

1. 浏览器打开 C 站 https://civitai.com/models/2458426 ，点上面的版本标签选 Turbo 或 Aesthetic
2. 复制地址栏网址 → 启动器「模型下载」页粘贴 →「获取信息」→「下载并放入对应文件夹」
3. Forge Neo 会自动放进 `models\Stable-diffusion\`

### 下载文本编码器和 VAE（浏览器）

这两个只在 HuggingFace 上，启动器的「模型下载」页不支持，用浏览器直接下：

**国内（hf-mirror 镜像，不用梯子）**：
- 文本编码器：https://hf-mirror.com/circlestone-labs/Anima/resolve/main/split_files/text_encoders/qwen_3_06b_base.safetensors
- VAE：https://hf-mirror.com/circlestone-labs/Anima/resolve/main/split_files/vae/qwen_image_vae.safetensors

**有梯子（官方）**：把上面网址里的 `hf-mirror.com` 换成 `huggingface.co`。

主模型也可以在这里下：https://hf-mirror.com/circlestone-labs/Anima/tree/main/split_files/diffusion_models

> 点链接就直接开始下载。浏览器下载很慢的话，把链接复制到 IDM / 迅雷 / Motrix 等下载工具里。

---

## 三、放到正确的文件夹

**这一步最容易错**。打开你的 Forge 安装目录（「一键启动」页的「根目录」），按下表放：

### Forge Neo

| 文件 | 放到 |
|---|---|
| 主模型 `anima-xxx.safetensors` | `models\Stable-diffusion\` |
| `qwen_3_06b_base.safetensors` | `models\text_encoder\`（没有这个文件夹就新建一个，名字一个字母都不能错） |
| `qwen_image_vae.safetensors` | `models\VAE\` |

```
D:\AI\forge\
└─ models\
   ├─ Stable-diffusion\
   │   └─ anima-aesthetic-v1.1.safetensors
   ├─ text_encoder\
   │   └─ qwen_3_06b_base.safetensors
   └─ VAE\
       └─ qwen_image_vae.safetensors
```

**开了[共享模型库](06-模型管理与共享模型库.md#六共享模型库)的更省事**：「模型管理」页左侧会有「扩散模型 (UNet / DiT)」「文本编码器」「VAE」分类，选好分类把文件拖进去就行（主模型放「扩散模型」，Forge Neo 和 ComfyUI 都能直接用，不用区分）。

### ComfyUI

| 文件 | 放到 |
|---|---|
| 主模型 | `models\diffusion_models\`（⚠ 不是 checkpoints！用启动器从 C 站下载的会被放进 `checkpoints`，要手动挪过去） |
| `qwen_3_06b_base.safetensors` | `models\text_encoders\` |
| `qwen_image_vae.safetensors` | `models\vae\` |

> Forge Classic（常规版）**不支持** Anima，请用 Neo 或 ComfyUI。老的 Neo 整合包不认识 Anima 的话，「环境部署」页对它再点一次「开始部署」更新一下。

---

## 四、在 Forge Neo 里出图

1. 启动器「一键启动」，等网页打开
2. 左上角 **UI 预设** 选 **`anima`**（如果你的版本没有这个选项，选 `xl` 再手动调参数也行）
3. **Checkpoint** 选 `anima-xxx`（看不到就点 🔄 刷新）
4. **VAE / Text Encoder** 下拉框（Checkpoint 右边那个）：**同时选上** `qwen_3_06b_base.safetensors` 和 `qwen_image_vae.safetensors`（这个框可以多选，两个都要选！看不到就点旁边 🔄）
5. 填提示词（见下面）
6. 设置参数：

| 参数 | Turbo 版 | Aesthetic / Base 版 |
|---|---|---|
| Sampling method | `Euler a` 或 `er_sde`（有就选） | `er_sde`（画风中性、线条清晰）或 `Euler a` |
| Steps | 8~12 | 30~50（先用 30） |
| CFG Scale | **1** | **4~5** |
| 尺寸 | 832×1216（竖图）、1024×1024、1216×832（横图） | 同左 |

尺寸范围：512×512 ~ 1536×1536 之间都行，约 100 万像素（1024×1024 附近）效果最稳。

7. 点 **Generate**

---

## 五、提示词怎么写

### 标签顺序

```
[质量 / 年份 / 安全标签]  [1girl / 1boy 等人数]  [角色名]  [作品名]  [@画师]  [其他描述标签]
```

### 三条规则

1. **全小写，空格代替下划线**：写 `long hair`，不写 `long_hair`（只有 `score_7` 这种分数标签用下划线）
2. **画师前面必须加 @**：`@画师名`，不加 @ 不生效
3. **可以混写自然语言**：标签后面跟一两句英文描述；纯自然语言的话至少写两句

### 可以直接抄的模板

**Base / Turbo 版正向：**
```
masterpiece, best quality, score_7, safe, 1girl, solo, long hair, silver hair, blue eyes, school uniform, smile, cherry blossoms, outdoors. She is standing under a cherry tree and waving at the viewer.
```

**Aesthetic 版正向**（不用写质量词，模型本身已经很好看）：
```
safe, 1girl, solo, long hair, silver hair, blue eyes, school uniform, smile, cherry blossoms, outdoors
```

**负向（Base / Aesthetic 版）：**
```
worst quality, low quality, score_1, score_2, score_3, artist name, blurry, jpeg artifacts, chromatic aberration
```

> Turbo 版 CFG = 1 时**负向提示词不起作用**，可以留空。

### 常用标签参考

| 类别 | 标签 |
|---|---|
| 质量 | `masterpiece`、`best quality`、`good quality`；或分数 `score_9` ~ `score_1`（越大越好） |
| 年份 / 新旧 | `year 2025`，或 `newest`、`recent`、`mid`、`early`、`old` |
| 安全分级 | `safe`、`sensitive`、`nsfw`、`explicit` |
| 画面 | `highres`、`absurdres`、`anime screenshot` |

角色名、作品名、画师名用 Danbooru 上的写法（把下划线换成空格），比如角色 `hatsune miku`、作品 `vocaloid`。

---

## 六、在 ComfyUI 里用

1. ComfyUI 菜单 → **Workflow → Browse Templates（模板）**，搜 **Anima**，有官方模板就直接用（打开后把三个加载节点里的文件名对上就能跑）
2. 没有模板就自己连：
   - **Load Diffusion Model** → 选 `anima-xxx`
   - **Load CLIP** → 选 `qwen_3_06b_base`，type 选列表里的 `anima`（不确定就以模板里的设置为准）
   - **Load VAE** → 选 `qwen_image_vae`
   - 接 CLIP Text Encode（正 / 负）→ KSampler（sampler `er_sde`，steps / cfg 按上表）→ VAE Decode → Save Image
3. 最省事：在 C 站 / HuggingFace 找一张别人用 ComfyUI 出的 Anima 原图，**直接拖进 ComfyUI 网页**，整个工作流就出来了

---

## 七、常见问题

| 问题 | 原因 / 处理 |
|---|---|
| 出图是一片噪点 / 彩色雪花 | VAE 没选或选错了。VAE / Text Encoder 下拉框里选上 `qwen_image_vae` |
| 报错 / 出的图跟提示词完全无关 | 文本编码器没选。下拉框里选上 `qwen_3_06b_base` |
| 下拉框里找不到 qwen 那两个文件 | 文件夹放错了（看第三节），或者没点 🔄 刷新 |
| 画面发灰、糊 | Turbo 版 CFG 没设成 1；或者 Aesthetic / Base 版步数太少（< 25） |
| 画师风格不生效 | 画师名前面没加 `@` |
| 用了 SDXL / Illustrious 的 LoRA 没效果 | Anima 只能用 **Anima 的 LoRA**（C 站 Base Model 选 Anima） |
| Forge Classic 加载报错 | Classic 不支持，换 Neo |
| 爆显存（6GB 卡） | 「高级选项」显存策略选低显存；文本编码器精度选 FP8 或放 CPU |

---

上一篇：[← 04 · 要下哪些模型](04-要下哪些模型.md)　｜　下一篇：[06 · 模型管理与共享模型库 →](06-模型管理与共享模型库.md)
