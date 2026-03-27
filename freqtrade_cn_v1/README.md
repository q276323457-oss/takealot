# Freqtrade + FreqAI 中文版 v1

这是一套独立于当前项目的 `Freqtrade + FreqAI` 中文落地包。

目标不是“自动暴富”，而是先给你一套能跑、能看懂、能慢慢迭代的中文骨架：

- 中文说明
- 中文配置模板
- 中文启动脚本
- 中文示例策略
- 默认先跑模拟盘，不直接上真钱

## 先说清楚

- 这不是稳赚机器人。
- FreqAI 能自动重训模型，但不代表它会自动赚钱。
- 本包默认是 `现货 + 模拟盘 + 小范围交易对`，目的是先让你跑通。
- Freqtrade 的 WebUI 仍然是英文界面，本包只把“使用流程”和“配置理解”做成中文版。

## 目录结构

```text
freqtrade_cn_v1/
├── docker-compose.yml
├── .gitignore
├── README.md
├── scripts/
│   ├── 00_prepare.sh
│   ├── 01_download_data.sh
│   ├── 02_backtest.sh
│   ├── 03_dry_run.sh
│   ├── 04_stop.sh
│   └── 05_logs.sh
└── user_data/
    ├── config.base.json
    ├── config.private.example.json
    └── strategies/
        ├── CNFreqaiSpotStrategy.py
        └── CNFreqaiSpotStrategyV2.py
```

## 当前默认策略

- 默认按钮和脚本现在会跑 `CNFreqaiSpotStrategyV2`
- `V2` 比 `V1` 更保守，目标是少乱买、少深套、先把大亏损压下去
- `V1` 还保留在目录里，只是不给新手默认跑了

## v1 默认方案

- 交易所：`Binance`（你也可以后面改成别的）
- 模式：`spot` 现货
- 交易对：`BTC/USDT`、`ETH/USDT`
- 周期：`4h`
- 模型：`XGBoostRegressor`
- 风控：轻仓、不开空、不加杠杆
- 启动方式：Docker

## 你只需要按这个顺序做

## 中文按钮界面

如果你不想手动敲命令，可以直接双击这个文件启动中文按钮界面：

```text
/Users/wangfugui/Desktop/重要文件/takealot-autolister/start_freqtrade_cn_ui.command
```

界面里已经做成按钮的操作：

- 保存 API / WebUI 配置
- 初始化环境
- 下载数据
- 回测
- 启动模拟盘
- 停止模拟盘
- 打开 WebUI
- 打开回测结果目录

### 1）进入目录

```bash
cd '/Users/wangfugui/Desktop/重要文件/takealot-autolister/freqtrade_cn_v1'
```

### 2）初始化目录和私密配置

```bash
bash scripts/00_prepare.sh
```

这一步会：

- 拉取官方 `stable_freqaitorch` 镜像
- 创建日志/数据目录
- 如果还没有私密配置，就自动复制一份模板

### 3）编辑私密配置

打开这个文件：

```text
freqtrade_cn_v1/user_data/config.private.json
```

至少改这几项：

- `exchange.key`
- `exchange.secret`
- `api_server.username`
- `api_server.password`
- `api_server.jwt_secret_key`
- `api_server.ws_token`

如果你用的是 OKX 一类需要第三个口令的交易所，再填 `exchange.password`。

### 4）下载历史数据

```bash
bash scripts/01_download_data.sh
```

默认下载：

- `BTC/USDT`
- `ETH/USDT`
- `4h`

默认时间范围从 `2023-01-01` 开始。

如果你前面下载过一段短数据，后面又想补更长历史，建议直接用“重下模式”：

```bash
bash scripts/01_download_data.sh 20240101- fresh
```

这个模式会先清掉本地旧行情，再重新下载，避免不同时间段拼接后出现训练空值。

### 5）先回测

```bash
bash scripts/02_backtest.sh
```

默认回测时间范围从 `2024-01-01` 开始。

### 6）确认没问题后再开模拟盘

```bash
bash scripts/03_dry_run.sh
```

启动后可以访问：

```text
http://127.0.0.1:8080
```

登录账号密码就是你在 `config.private.json` 里设置的 `api_server.username/password`。

### 7）查看日志

```bash
bash scripts/05_logs.sh
```

### 8）停止

```bash
bash scripts/04_stop.sh
```

## 什么时候才能切真钱

先满足这几个条件再说：

1. 模拟盘至少稳定跑满 `30` 天。
2. 你能看懂日志里每一类报错。
3. 你知道怎么停止机器人。
4. 你知道自己每笔最多亏多少。
5. 你能接受它连续亏损。

## 真钱前必须改的项目

在 [config.base.json](/Users/wangfugui/Desktop/重要文件/takealot-autolister/freqtrade_cn_v1/user_data/config.base.json) 里：

- 把 `dry_run` 从 `true` 改成 `false`
- 把 `stake_amount` 保持很小
- 第一次真钱建议总资金不要超过你能完全承受损失的范围

## 这个 v1 做了什么取舍

- 不做合约
- 不做杠杆
- 不做做空
- 不做一堆山寨币
- 不追求高频
- 先把“能跑起来”放在“花哨”前面

## 常见问题

### WebUI 为什么还是英文？

因为官方前端不是现成中文，本包先做的是“中文使用版”，不是“完整汉化前端版”。

### 可以换成 OKX 吗？

可以，但你要自己把 `exchange.name` 和对应 API 参数改掉，并重新下载数据。

### 这个策略一定赚钱吗？

不会。它只是一个偏保守的 FreqAI 示例骨架，用来让你建立第一套可控流程。

### 我不会看 K 线怎么办？

你现在不需要会画线。你先学会三件事：

- 能启动
- 能看日志
- 能关停

先把机器人当成“自动执行器”，不要把它当成财神。

## 下一步建议

当这套 v1 跑通后，下一步再做其中一个：

1. 中文界面汉化版
2. OKX 版配置
3. Telegram 中文提醒
4. 更保守的 BTC-only 版本
5. 你自己的交易所 API 接入版
