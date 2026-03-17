# Takealot Autolister — 开发文档

> 最后更新：2026-03-16
> 用途：对话意外断开后，Claude 可通过本文档快速恢复上下文，继续开发。

---

## 一、项目概述

给跨境卖家用的半自动上架工具（桌面 GUI）。用户输入 1688 商品链接，自动完成：

1. 抓取 1688 商品信息（标题、图片、属性、描述）
2. AI 生成英文标题 / 副标题 / 产品描述 / 属性参数
3. 生成白底产品主图（AI 图生图）
4. 翻译 1688 副图中文水印→英文
5. 弹出预览编辑对话框（用户确认 / 修改）
6. 生成 Takealot loadsheet（.xlsm）
7. 自动填写 Takealot 卖家后台表单并提交草稿

**技术栈：**
- GUI：PySide6（Qt）
- 浏览器自动化：Playwright（Edge）
- AI 文本：自定义 LLM 网关（`LLM_BASE_URL` + `LLM_API_KEY`），优先走硅基流动 DeepSeek-V3，失败回退网关
- AI 视觉：硅基流动 Qwen2.5-VL-72B（主要用于图片 OCR/理解）
- AI 图像生成：五音 NanoBanana2（优先）+ Gemini 3.1 Flash Image（回退）
- 图片翻译：易可图 API（`yiketu.py`，待权限审核）/ 有道智云（`youdao.py`，备用）
- 图片存储：阿里云 OSS
- 配置：`.env` 文件

---

## 二、目录结构

```
takealot-autolister/
├── gui_qt.py                          # ★ 实际运行的主入口（双击/命令行启动）
├── run.py                             # 无头守护模式入口（scripts/run_daemon.sh 调用）
├── start_ui.command                   # macOS 一键启动 GUI
├── start_autolister.command           # macOS 一键启动守护进程
├── stop_autolister.command            # 停止守护进程
├── requirements.txt
├── .env                               # 所有 API Key 和配置（不提交）
├── .env.example
├── DEV_NOTES.md                       # 本文档
├── src/takealot_autolister/           # 主包
│   ├── pipeline.py                    # ★ 主流程编排（核心）
│   ├── preview_dialog.py              # ★ 预览编辑对话框（最复杂）
│   ├── csv_exporter.py                # ★ xlsm loadsheet 生成
│   ├── portal.py                      # ★ Takealot 后台自动填表 + probe 类目字段
│   ├── scraper_1688.py                # 1688 抓取器（Playwright）
│   ├── llm.py                         # 文本 AI 路由层（硅基流动 + 通用网关）
│   ├── siliconflow_llm.py             # 硅基流动 API（doubao_ 前缀函数名，兼容旧代码）
│   ├── image_generator.py             # AI 白底图生成（五音优先，Gemini 回退）
│   ├── gemini_image.py                # Gemini 图像生成适配器
│   ├── wuyin_image.py                 # 五音 NanoBanana2 图片生成适配器
│   ├── image_translator.py            # 产品图特征描述提取（辅助）
│   ├── yiketu.py                      # 易可图图片翻译 API（待权限审核）
│   ├── youdao.py                      # 有道智云图片翻译 API（备用）
│   ├── oss_uploader.py                # 阿里云 OSS 上传
│   ├── rules.py                       # 上架规则校验引擎
│   ├── login_helper.py                # 1688/Takealot 登录辅助
│   ├── types.py                       # 数据类：ProductSource / ListingDraft / PipelineResult
│   ├── images.py                      # 图片分析工具
│   ├── cli.py                         # 命令行入口
│   └── gui_qt.py                      # src 版 GUI（次要，实际跑根目录那个）
├── config/
│   ├── selectors.yaml                 # Takealot 后台 CSS 选择器 + portal probe 配置
│   └── rules.yaml                     # 上架规则（禁用词、商标等）
├── input/
│   ├── links.txt                      # 批量处理链接列表
│   ├── loadsheets/raw/                # Takealot Excel 模板（按品类）
│   ├── portal_fields/                 # 品类字段缓存（probe 结果 JSON）
│   └── takealot_categories.csv        # Takealot 品类完整层级表
├── .runtime/auth/
│   ├── 1688.json                      # 1688 登录态
│   └── takealot.json                  # Takealot 登录态
└── scripts/
    ├── run_daemon.sh                  # 守护进程脚本（循环调 run.py）
    ├── run_headless.sh
    └── download_loadsheets.py
```

> **注意**：`gui_qt.py`（根目录）才是实际运行的 GUI，`src/takealot_autolister/gui_qt.py` 是次要版本。
> `start_ui.command` 调用的是根目录的 `gui_qt.py`。

---

## 三、.env 配置

```ini
# 硅基流动（主力 AI）
SILICONFLOW_API_KEY=sk-xxx
SILICONFLOW_MODEL=deepseek-ai/DeepSeek-V3
SILICONFLOW_VL_MODEL=Qwen/Qwen2.5-VL-72B-Instruct
SILICONFLOW_IMAGE_MODEL=Qwen/Qwen-Image-Edit-2509

# LLM 网关（OpenAI 兼容，文本主力）
LLM_BASE_URL=https://api.viviai.cc/v1
LLM_API_KEY=sk-xxx
LLM_MODEL=gpt-5.3-codex-medium

# Google Gemini（图像生成，通过 yansd666 代理）
GEMINI_API_KEY=xxx
GEMINI_IMAGE_BASE_URL=https://yansd666.com
GEMINI_IMAGE_MODEL=gemini-2.5-flash-image-preview

# 易可图图片翻译（审核中）
YIKETU_APP_KEY=8634859649
YIKETU_APP_SECRET=xxx

# 阿里云 OSS
OSS_ACCESS_KEY_ID=xxx
OSS_ACCESS_KEY_SECRET=xxx
OSS_BUCKET=takealot8
OSS_ENDPOINT=oss-cn-hongkong.aliyuncs.com
OSS_BASE_URL=https://takealot8.oss-cn-hongkong.aliyuncs.com

# 浏览器
BROWSER_USER_DATA_DIR="/Users/xxx/Library/Application Support/Microsoft Edge"
BROWSER_CHANNEL=msedge
BROWSER_PROFILE_DIRECTORY=Default

# 认证状态
STORAGE_STATE_1688=/path/to/.runtime/auth/1688.json
STORAGE_STATE_TAKEALOT=/path/to/.runtime/auth/takealot.json

# 流程默认值
DEFAULT_PORTAL_MODE=draft
DEFAULT_HEADLESS=true
```

---

## 四、核心流程（pipeline.py）

```
[1/5] scraper_1688.py            → 抓取商品（标题/图片/属性/描述）→ source.json
[2/5] find_probe_category_path   → 仅做 Takealot 类目匹配（不自动打开后台）；如命中缓存则加载该类目的字段定义 → input/portal_fields/*.json
[3/5] llm.py                     → 可选：生成初始草稿（GUI 默认走 fallback 模板）→ draft.json
[4/5] preview_dialog.py          → 弹出预览对话框（用户在这里决定所有字段/文案/图片）
[5/5] csv_exporter.py            → 按预览结果生成 xlsm；如启用自动上架则由 portal.py 填写 Takealot 后台
```

当前设计原则（2026-03-16）：
- **预览 = 单一事实来源**：标题、副标题、卖点、颜色、材质、保修、类目等关键字段都以预览中的最终值为准；
- **导出阶段不再调用 AI**：`csv_exporter` 只做规则推断 + portal 字段透传 + 数据清洗，不做任何 LLM 猜测；
- **自动 probe 只在预览中手动触发**：pipeline 不再全自动调用 `probe_category_fields`，而是由用户在预览顶部选择类目后点击“保存并重探测字段”触发。

`preview_callback` 是后台线程与主线程的桥梁：
- 后台线程发 `preview_request` 信号 → 主线程打开 `PreviewDialog`
- 主线程用 `threading.Event` 阻塞后台线程等待用户操作
- 用户确认 → `event.set()` → 后台线程继续
- **`_on_preview_request` 已加 try/except/finally**：对话框构建异常时会弹错误提示，`event.set()` 在 `finally` 里保证必定被调用（不会永久阻塞）

---

## 五、preview_dialog.py 架构

### 5.1 后台 Worker 类

| 类名 | 功能 | 信号 |
|------|------|------|
| `_ImageWorker` | AI 生成白底主图 | `finished(list[bytes])`, `error(str)` |
| `_TranslateWorker` | 副图中文→英文翻译 | `finished(list[bytes])`, `partial(list[bytes])`, `error(str)` |
| `_ListingWorker` | AI 生成标题/描述/属性 | `finished(dict)`, `error(str)` |

**重要**：Worker 必须赋值给 `self._xxx_worker`，不能是局部变量，否则被 GC 回收。

### 5.2 PreviewData 数据结构

```python
@dataclass
class PreviewData:
    title: str
    subtitle: str
    source_image_urls: list[str]
    portal_fields: list[dict]      # probe 探测到的字段 [{label, type, required, options}]
    field_values: dict[str, str]   # 已填字段值（portal label → value）
    category_path: list[str]
    product_info: dict             # 原始抓取数据
    run_dir: str = ""              # run 目录路径（自动保存用）
```

### 5.3 自动保存（autosave）

- **实时保存**：标题/副标题/所有字段变动后 1.5 秒自动写盘（防抖 QTimer）
- **取消时**：`reject()` 重写，确保关闭时也保存
- **窗口 ✕**：`closeEvent()` 触发保存
- **确认提交后**：`_on_confirm()` 删除 `preview_autosave.json`（不恢复已提交的数据）
- 文件位置：`run_dir/preview_autosave.json`

### 5.4 下拉框填值规则

所有写入 QComboBox 的地方（AI 生成、预填、restore）统一使用：
```python
idx = widget.findText(v_str, Qt.MatchFlag.MatchFixedString)
if idx < 0:  # case-insensitive fallback
    for i in range(widget.count()):
        if widget.itemText(i).lower() == v_str.lower():
            idx = i; break
if idx >= 0:
    widget.setCurrentIndex(idx)
# 不在选项里时不写入（保持默认）
```
禁止直接用 `widget.setCurrentText(v_str)`（会写入非法值导致 Takealot 驳回）。

### 5.5 重新预览（_preview_run）

根目录 `gui_qt.py` 的"重新预览"按钮：从历史记录选择 run 目录 → 读取 `draft.json` + `source.json` → 恢复所有字段 → 打开预览对话框 → 确认后生成新 xlsm + 上传 OSS。

---

## 六、csv_exporter.py（xlsm 生成）

### 6.1 xlsm 阶段完全不再调用 AI

导出阶段不再调用 `ai_fill_missing_fields` 或任何 LLM，所有列值来源仅有：

1. `build_row_values()`：基于 1688 抓取 + 规则推断的基础字段（SKU、基础尺寸、布尔值等）；
2. `_apply_portal_field_overrides()`：把预览里 portal 字段（保存在 `draft.attributes` 中）映射到 loadsheet 相应列；
3. 少量硬编码默认值（例如保修类型/保修期）。

如果某个必填字段仍然为空，优先通过“在预览中补字段”来解决，而不是在 xlsm 里做隐式补值。

### 6.2 portal 字段映射（_apply_portal_field_overrides）

把 `draft.attributes` 里的 portal 字段标签映射到 xlsm 列：

**归一化规则**：
```python
norm = re.sub(r"[/]+", " ", attr_key.lower())     # / → 空格（不能删掉！）
norm = re.sub(r"[&'\"()]+", "", norm).strip()
norm = re.sub(r"[\s\-]+", "_", norm)
# 例："Main Material/Fabric" → "main_material_fabric" ✓
# 旧写法直接删 / 会得到 "main_materialfabric" → 找不到列
```

**下拉值验证**：写入前检查 `_probe_fields` 里的 options，值不在列表里的跳过（打印日志）。

### 6.3 包装尺寸写入位置

**正确**：`Attribute.merchant_packaged_dimensions.{width/length/height}`（Packaged 列）
**错误坑**：不要写到 `Attribute.product_dimensions.{width/length/height}`（Assembled 列）

```python
"Attribute.merchant_packaged_dimensions.width":  str(attrs.get("packaged_width")  or attrs.get("width_cm")  or ""),
"Attribute.merchant_packaged_dimensions.length": str(attrs.get("packaged_length") or attrs.get("length_cm") or ""),
"Attribute.merchant_packaged_dimensions.height": str(attrs.get("packaged_height") or attrs.get("height_cm") or ""),
```
用 `or` 跳过空字符串（`attrs.get("key", "10")` 在 key 存在但值为 `""` 时不会用默认值）。

### 6.4 保修默认值

```python
"Attribute.warranty.type":         "Limited",   # 始终用 Limited，不调用 AI
"Attribute.warranty.period.value": "6",         # 始终 6 个月
```

### 6.5 数值字段统一去掉单位

导出前，对以下字段统一做一次“提取数字”处理，只保留纯数字字符串，丢弃 `cm` / `°` 等单位：

- `Attribute.merchant_packaged_dimensions.{width,length,height}`
- `Attribute.product_dimensions.{width,length,height}`
- `Attribute.view_angle.value`
- `Attribute.input_voltage.value`
- `Attribute.output_voltage.value`
- `Attribute.rated_voltage.value`

实现：`_parse_cm_number("9 cm") → "9"`, `_parse_cm_number("90°") → "90"`。

---

## 七、portal.py（Takealot 后台）

### 7.1 品类完整路径（只做路径，不做字段）

`find_probe_category_path()` + `_find_full_portal_path()` 用于把 1688 类目映射到 Takealot 完整路径，例如：

```python
["Consumer Electronics", "Computer Components", "Storage Devices", "Portable HDD"]
```

`_find_full_portal_path()` 的查找优先级：
1. `selectors.yaml` 里的 `portal.category_keyword_paths`（手动配置，精确匹配）
2. `input/takealot_categories.csv` 按 `lowest` 字段精确匹配
3. 按 `main` 字段匹配
4. 找不到则原样返回

pipeline 只负责这一步，不再自动调用 `probe_category_fields()`。

### 7.2 手动字段 probe + 缓存复用

- 缓存文件：`input/portal_fields/<category_key_normalized>.json`
- 结构：`{"category_key": ..., "category_path": [...], "fields": [{label, type, required, options, hint, section}], ...}`

工作流：

1. 用户在预览顶部手动调整 Takealot 类目；
2. 点击“保存并重探测字段”：
   - 由 `_CategoryProbeWorker` 调用 `probe_category_fields()`；
   - 打开后台页面，按类目路径自动点击，选 None 变体，抓取字段定义；
   - 结果写入 `input/portal_fields/*.json`，并更新当前预览的字段列表；
   - 同时写入 `input/category_overrides.yaml` 记忆 1688 → Takealot 类目映射。
3. 下次 pipeline 遇到同一个 Takealot 类目时，只加载缓存字段定义，不再自动打开浏览器。

---

## 八、llm.py（AI 文本生成）

### 8.1 下拉字段约束（重要！）

prompt 里已加强约束，防止 AI 生成 Takealot 不接受的值：
```
CRITICAL: For dropdown/select fields that have an 'options' list: you MUST copy
the value EXACTLY as it appears in 'options'. Do NOT invent, modify, translate,
combine, or approximate values. If no option is a good match, leave empty ("").
```

如果 AI 仍然写出非法值，`_on_text_gen_done` 里的 `findText()` 逻辑会拦截（不写入控件），`_apply_portal_field_overrides` 里也会验证（不写入 xlsm）。三层防护。

### 8.2 标题/副标题生成规则（去品牌 + 去规格）

`llm._build_prompt()` 和 `generate_listing_with_instructions()` 中对 title/subtitle 的约束已经更新：

- 标题：
  - 只体现产品类型 + 主要用途/卖点；
  - 禁止包含品牌名、型号、以及详细数字规格（容量、GB、Hz、寸、瓦等）。
- 副标题：
  - 一句话补充 1–2 个卖点，可以适当提到关键规格；
  - 避免重复品牌/型号。

GUI 默认不在 Step 3 一次性自动生成最终文案，而是在预览中由用户点击“AI 生成”按需产生，便于反复调整。

### 8.3 pipeline.py 的 _log 去重

`pipeline._log()` 有 `log_callback` 时只走回调不 `print()`，避免 `_StdoutCapture` 双重输出导致日志重复。

---

## 九、图像生成（image_generator.py + wuyin_image.py）

```
generate(count=N) / refine(user_instruction, count=N)
    ↓
_call_generate(prompt, count, reference_urls)
    ├── 优先：五音 NanoBanana2（wuyin_image.py）
    │   ├── 每张图一个任务，服务端并行；使用多组变体 prompt（主图/场景/特写/多角度/生活方式）
    │   └── 自动把 1688 原图 URL 中的 \"...jpg_.webp\"、\"...png_.webp\" 修正为 \"...jpg\" / \"...png\"
    └── 回退：Gemini（gemini_image.py）
        └── 同样使用多变体 prompt，逐张生成
```

**变体图 prompt**（`_VARIANT_INSTRUCTIONS`）：
- [0] 主图：纯白底正面居中
- [1] 副图1：使用场景（生活化）
- [2] 副图2：功能 / 卖点特写
- [3] 副图3：多角度 / 配件组合
- [4] 副图4：生活方式场景 / 氛围图

---

## 十、图片翻译（_TranslateWorker）

优先级（preview_dialog.py `_translate_thread` 里）：
1. 易可图 API（`yiketu.py`）→ 真正替换图中文字，效果最好
2. 有道智云（`youdao.py`）→ OCR + 渲染翻译
3. VL 读中文 + DeepSeek 翻译 + PIL 叠加遮罩（回退，效果差）

**易可图状态**：API 已接入，`appKey=8634859649`，已充值，"图片翻译"功能权限申请中（未审核通过）。

---

## 十一、已修复的 Bug 清单（2026-03-11 ~ 16）

| # | 问题描述 | 根本原因 | 修复位置 |
|---|---------|---------|---------|
| 1 | 预览对话框不弹出，后台永久阻塞 | `preview_dialog.__init__` 里 `Path` 未导入，构造异常被吞掉 | `preview_dialog.py` 加 `from pathlib import Path`；`gui_qt._on_preview_request` 加 try/except/finally |
| 2 | 日志每条显示两遍 | `pipeline._log()` 既 print 又调 callback，stdout 被 `_StdoutCapture` 二次捕获 | `pipeline.py`：有 callback 时不再 print |
| 3 | autosave 不生效（取消后字段丢失） | Qt `reject()` 调 `hide()` 而非 `close()`，不触发 `closeEvent` | `preview_dialog.py` 重写 `reject()` 方法 |
| 4 | autosave 不实时（只有关闭时保存） | 只在 close/reject 时调用一次 autosave | 加防抖 QTimer（1.5s），所有字段变动连接到 `_schedule_autosave()` |
| 5 | xlsm 写入错误的 Assembled 尺寸列 | 用了 `product_dimensions` 而非 `merchant_packaged_dimensions` | `csv_exporter.build_row_values()` 修正列名 |
| 6 | xlsm 用 AI 填写了不相关的 192 列 | `ai_fill_missing_fields()` 扫描所有列 | 完全禁用该调用；`_apply_portal_field_overrides` 只写 probe 字段 |
| 7 | AI 生成无效下拉值（Albania/Ethnic/Lilac/Khaki 等） | `setCurrentText()` 允许任意文本；LLM prompt 无强制约束 | `llm.py` 加 CRITICAL 约束；`preview_dialog.py` 所有 QComboBox 改用 `findText()` |
| 8 | portal 字段 `/` 归一化错误导致找不到 xlsm 列 | `Main Material/Fabric` → `/` 被删掉 → `main_materialfabric`，找不到列 | `csv_exporter._apply_portal_field_overrides()`：`/` 改为替换成空格 |
| 9 | xlsm 写入不在下拉列表的值（平台驳回） | `_apply_portal_field_overrides` 直接写入不验证 | 写入前从 `_probe_fields` 取 options，不在列表里的跳过 |
| 10 | 品类路径只有叶节点导致 portal probe 超时 | pipeline 只传 `['Speakers']`，后台需要完整路径 | `portal._find_full_portal_path()` + `probe_category_fields()` 自动扩展 |
| 11 | Worker 被 GC 回收线程无法启动 | Worker 是局部变量，函数返回后被回收 | 所有 Worker 赋值给 `self._xxx_worker` |
| 12 | 标题带品牌+硬规格（GB/寸/瓦）不符合需求 | 文案 prompt 鼓励 `[Brand][Model][Spec]` | 调整 `llm._build_prompt` 及 `generate_listing_with_instructions`，禁止品牌和详细规格出现在标题里 |
| 13 | Main Material/Fabric 写入如 \"ABS plastic\" 这类不在下拉列表的值 | 使用 1688/LLM 粗略材质字段，未与预览选择对齐 | 在 `pipeline.py` 中，以预览里的 `Main Material/Fabric` / `Main Strap Material` 覆盖 `draft.attributes['material']`，导出只看预览结果 |
| 14 | 颜色列写入整串（如 \"Red / Blue / Black / White\"） | 直接使用 1688/LLM 的颜色描述 | `csv_exporter._split_colours` 拆分颜色后只保留单一主色；次色不自动填，由预览决定 |
| 15 | 尺寸/视角/电压等数字字段带单位（\"9 cm\"、\"90°\"）导致校验失败 | 1688 文本直接写入 loadsheet | 在 `generate_loadsheet` 中通过 `_parse_cm_number` 清洗相关列，只保留数字部分 |
| 16 | 自动 probe 类目字段耗时长且命中率低 | pipeline 启动时无脑调用 `probe_category_fields` | 修改 pipeline：启动不再自动 probe；字段探测改为在预览里手动点击“保存并重探测字段”触发 |

---

## 十二、待办事项

| 优先级 | 任务 | 状态 |
|--------|------|------|
| 高 | 易可图图片翻译权限审核通过后测试 | ⏳ 等审核 |
| 中 | 验证 probe 是否正确抓取所有类目的下拉 options（部分字段 options 为空，Takealot 后端校验） | 🔍 需要手动验证 |
| 中 | 有道智云图片翻译测试（youdao.py 备用方案） | 📋 代码已写，未测试 |
| 低 | 用量监控（防单用户滥用 API 额度） | 📋 待开发 |

---

## 十三、快速恢复上下文 Checklist

新对话开始时确认：

1. **实际运行文件**：`gui_qt.py`（根目录），`start_ui.command` 启动
2. **主要开发文件**：`src/takealot_autolister/preview_dialog.py`、`csv_exporter.py`、`portal.py`、`pipeline.py`、`llm.py`
3. **AI 不填 xlsm**：`ai_fill_missing_fields` 调用已被注释禁用，只有预览对话框里的字段才写入
4. **下拉值三层防护**：llm prompt 约束 → findText 拦截 → _apply_portal_field_overrides 验证
5. **autosave**：实时（1.5s 防抖）+ reject/closeEvent 兜底；confirm 后删除文件
6. **品类路径**：已自动扩展短路径（`_find_full_portal_path`），字段 probe 只在预览中手动触发
7. **图像生成**：五音 NanoBanana2 优先，Gemini 回退；多任务并行，修正 1688 `*.webp` URL
8. **翻译**：易可图待审核，有道智云备用，PIL 回退

---

*本文档由 Claude 维护，每次重大变更后更新。*
