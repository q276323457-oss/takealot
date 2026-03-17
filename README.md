# Takealot 自动上架脚本（MVP）

这是一个**不依赖 OpenClaw**的独立脚本，功能包括：
- 读取 1688 链接
- 用 Playwright 抓取商品信息
- 按 Takealot 规则生成上架草稿
- 生成白底图
- 可选：自动进入 Seller Portal 填表并保存草稿/发布

## 0）双击一键启动（推荐）

你可以直接在 Finder 双击：
- `start_autolister.command`：一键启动（后台守护，每5分钟一轮）
- `stop_autolister.command`：一键停止

路径：
- `/Users/wangfugui/Desktop/重要文件/takealot-autolister/start_autolister.command`
- `/Users/wangfugui/Desktop/重要文件/takealot-autolister/stop_autolister.command`

## 1）安装与初始化（手动方式）

```bash
cd '/Users/wangfugui/Desktop/重要文件/takealot-autolister'
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium msedge
cp .env.example .env
```

然后编辑这两个文件：
- `.env`（填 LLM key、登录状态文件路径）
- `config/selectors.yaml`（填你当前 Takealot 页面真实选择器）

## 2）准备 1688 链接

把链接放到 `input/links.txt`，一行一个；
也可以运行时用 `--link 'https://detail.1688.com/offer/xxxx.html'` 传入。

## 3）运行方式

### A. 后台单次运行（推荐先用这个）

```bash
cd '/Users/wangfugui/Desktop/重要文件/takealot-autolister'
./scripts/run_headless.sh
```

### B. 后台守护运行（每5分钟跑1次）

```bash
cd '/Users/wangfugui/Desktop/重要文件/takealot-autolister'
nohup ./scripts/run_daemon.sh > logs/daemon.out 2>&1 &
```

停止守护：

```bash
pkill -f run_daemon.sh
```

### C. 首次登录（推荐用 UI 登录按钮）

推荐在桌面 UI 里点“登录1688 / 登录Takealot”，会保存状态到：
- `.runtime/auth/1688.json`
- `.runtime/auth/takealot.json`

如果要纯命令行登录：

```bash
cd '/Users/wangfugui/Desktop/重要文件/takealot-autolister'
source .venv/bin/activate
PYTHONPATH=src python -m takealot_autolister.login_helper --url https://detail.1688.com --mode 1688 --state-path .runtime/auth/1688.json
PYTHONPATH=src python -m takealot_autolister.login_helper --url https://sellers.takealot.com --mode takealot --state-path .runtime/auth/takealot.json
```

登录完成后，后台模式会自动复用状态文件。

你也可以用有界面模式跑一次，手动过登录/验证码：

```bash
cd '/Users/wangfugui/Desktop/重要文件/takealot-autolister'
source .venv/bin/activate
PYTHONPATH=src python run.py --headed --limit 1 --automate-portal --portal-mode draft
```

登录成功后，后续再切回 `--headless` 后台跑。

## 4）输出结果位置

每次运行会在 `output/runs/<时间戳>/` 生成：
- `source.json`：1688抓取数据
- `draft.json`：生成的上架草稿
- `listing_package.md`：可读版上架包
- `validation.json`：规则校验结果
- `images_white/`：白底图
- `portal_result.json`：自动填单结果（启用 portal 自动化时）

## 5）登录拦截时的表现（正常）

- 如果 1688 未登录，会返回：`need_login_1688`
- 如果 Takealot 卖家后台未登录，会返回：`need_login`

这不是报错，是保护机制。先补齐对应登录状态文件再跑即可。

## 6）常用命令

只做上架包，不进卖家后台：

```bash
source .venv/bin/activate
PYTHONPATH=src python run.py --headless --no-llm --limit 1
```

自动填卖家后台并保存草稿：

```bash
source .venv/bin/activate
PYTHONPATH=src python run.py --headless --automate-portal --portal-mode draft --limit 1
```

## 7）注意事项

- `config/selectors.yaml` 必须和你当前页面 DOM 对应，否则无法准确填单。
- 大模型超时会自动降级到模板生成，不会中断主流程。
- 想要更强抠图效果，可安装 `rembg` 并加 `--remove-bg`。

## 8）给别人发软件 + OSS 自动更新

桌面 UI 已内置“检查更新”按钮，默认会读取 OSS 上的更新清单。

### 需要配置

在 `.env` 中确保有：

```env
APP_VERSION=1.0.0
OSS_BASE_URL=https://your-bucket.oss-xxx.aliyuncs.com
AUTO_UPDATE_MANIFEST_KEY=takealot/updates/update_manifest.json
# 或直接指定：
# AUTO_UPDATE_MANIFEST_URL=https://your-bucket.oss-xxx.aliyuncs.com/takealot/updates/update_manifest.json
```

### 发布新版本清单（manifest）

```bash
cd '/Users/wangfugui/Desktop/重要文件/takealot-autolister'
source .venv/bin/activate
python scripts/publish_update_manifest.py \
  --version 1.0.1 \
  --mac-url "https://your-bucket.../takealot/updates/TakealotAutoLister-mac-1.0.1.dmg" \
  --win-url "https://your-bucket.../takealot/updates/TakealotAutoLister-win-1.0.1.exe" \
  --notes "修复 xlsm 生成与生图稳定性"
```

发布后，客户端点击“⬇️ 检查更新”即可拉取最新版本信息并打开下载链接。

## 9）卡密授权（绑定机器码）

已内置授权机制：
- 客户端显示“机器码”
- 用户输入“卡密（授权码）”激活
- 卡密绑定机器码，不匹配无法激活

### 初始化密钥（作者只做一次）

```bash
cd '/Users/wangfugui/Desktop/重要文件/takealot-autolister'
source .venv/bin/activate
python scripts/init_license_keys.py
```

会生成：
- 私钥：`.runtime/license_private.pem`（仅作者保管，不可外发）
- 公钥：`config/license_public.pem`（随软件发布）

### 给用户生成卡密

让用户先发机器码给你，然后执行：

```bash
cd '/Users/wangfugui/Desktop/重要文件/takealot-autolister'
source .venv/bin/activate
python scripts/gen_license_token.py \
  --machine "用户机器码" \
  --card-id "CARD-20260317-001" \
  --days 365
```

把输出的授权码发给用户，用户在软件里点“输入卡密激活”即可。

## 10）打包脚本（mac / windows）

### macOS 打包

```bash
cd '/Users/wangfugui/Desktop/重要文件/takealot-autolister'
bash scripts/build_mac.sh
```

产物：`dist/西安众创南非Takealot自建链接AI工具.app`

### Windows 打包（在 Windows 机器执行）

```powershell
cd D:\path\to\takealot-autolister
powershell -ExecutionPolicy Bypass -File .\scripts\build_win.ps1
```

产物：`dist\西安众创南非Takealot自建链接AI工具\`

## 11）Mac 上一键云端打 Windows 包（推荐）

你可以不用 Windows 电脑，直接用 GitHub Actions 自动构建 Win 包。

### A. 推送代码后手动点一下运行

1. 把项目推送到 GitHub 仓库（`git push`）。
2. 打开仓库网页 → `Actions` → `Build Windows Package`。
3. 点 `Run workflow`，输入版本号（例如 `1.0.1`）并确认。
4. 等待完成后，在该次任务的 `Artifacts` 下载 `TakealotAutoLister-win-版本.zip`。

### B. 发版自动构建（打 tag）

```bash
git tag v1.0.1
git push origin v1.0.1
```

系统会自动：
- 构建 Windows 包
- 在 `Actions Artifacts` 里保存 zip
- 自动创建 GitHub Release 并挂上 zip 附件

工作流文件位置：
- `.github/workflows/build-win.yml`

## 12）小白一键用法（不用记命令）

项目根目录里有两个可双击文件：

- `start_setup_github_cloud_build.command`
- `start_release_win_cloud.command`

### 第一次（只做一次）

双击 `start_setup_github_cloud_build.command`，按提示输入你的 GitHub 仓库地址。  
脚本会自动完成：
- 初始化 git
- 提交代码
- 推送到 `main`

### 以后每次发版

双击 `start_release_win_cloud.command`，输入版本号（如 `1.0.1`）。  
脚本会自动：
- 提交改动
- 打 `v1.0.1` 标签
- 推送到 GitHub
- 自动触发 Windows 云打包
