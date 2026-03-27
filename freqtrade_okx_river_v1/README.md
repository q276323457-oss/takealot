# OKX RIVER 趋势合约 中文版 v2

这是一套单独给 `OKX 的 RIVER/USDT 永续` 做的中文按钮版。

这版不是“自动学习自己进化”的 AI 版，而是先用更容易落地的 `趋势合约版`：

- 只做 `RIVER/USDT:USDT`
- 默认 `5m` 执行，`15m + 30m` 判断趋势
- 会做多，也会做空
- 默认低杠杆、隔离保证金、先模拟盘

## 关于高频版

我已经试过更激进的 `v3`，目的是把交易频率拉到接近你要的水平。

结果很差，属于典型过度交易，所以现在默认还是 `v2`。

## 为什么这版先不用 FreqAI

`RIVER/USDT` 在 OKX 的永续上线时间很短，历史样本不长。

所以 v1 先不用自学习模型，先做一套你能看懂、能回测、能跑模拟盘的趋势机器人。

## 目录结构

```text
freqtrade_okx_river_v1/
├── docker-compose.yml
├── README.md
├── gui_qt.py
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
        └── OKXRiverTrendStrategy.py
```

## 默认方案

- 交易所：`OKX`
- 合约：`RIVER/USDT:USDT`
- 模式：`futures`
- 保证金：`isolated`
- 默认杠杆：策略内限制到 `2x`
- 周期：`5m + 15m + 30m`
- 目标：只做趋势，不抄底摸顶

## 中文按钮界面

直接双击这个文件启动：

```text
/Users/wangfugui/Desktop/重要文件/takealot-autolister/start_freqtrade_okx_river_ui.command
```

## 使用顺序

1. 保存 OKX API / WebUI 配置
2. 初始化环境
3. 下载 RIVER 历史数据
4. 先回测
5. 结论区显示通过后，再开合约模拟盘

## API 什么时候要填

- 只下载数据、只回测：可以先不填
- 要启动 OKX 合约机器人：建议填完整
- OKX 需要三项：`key`、`secret`、`password(passphrase)`

## 默认脚本

```bash
cd '/Users/wangfugui/Desktop/重要文件/takealot-autolister/freqtrade_okx_river_v1'
bash scripts/00_prepare.sh
bash scripts/01_download_data.sh
bash scripts/02_backtest.sh
bash scripts/03_dry_run.sh
```

## 风险提醒

- RIVER 波动很大，这版只是趋势骨架，不是印钞机
- 回测结果如果还是亏，就继续改，不要硬上真钱
- 合约先模拟盘，不要一开始就高杠杆
