#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import webbrowser
from pathlib import Path
from zipfile import ZipFile

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


ROOT = Path(__file__).resolve().parent
PRIVATE_CONFIG = ROOT / "user_data" / "config.private.json"
PRIVATE_EXAMPLE = ROOT / "user_data" / "config.private.example.json"
README_PATH = ROOT / "README.md"
BACKTEST_DIR = ROOT / "user_data" / "backtest_results"
DATA_DIR = ROOT / "user_data" / "data"
LAST_RESULT_PATH = BACKTEST_DIR / ".last_result.json"


def open_external(target: str) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform.startswith("darwin"):
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except Exception as exc:
        raise RuntimeError(f"无法打开：{target}\n{exc}") from exc


class CommandWorker(QObject):
    log_line = Signal(str, str)
    finished = Signal(int, str)

    def __init__(self, title: str, command: str) -> None:
        super().__init__()
        self.title = title
        self.command = command
        self.process: subprocess.Popen[str] | None = None

    def run(self) -> None:
        try:
            self.log_line.emit(f"[开始] {self.title}", "info")
            self.log_line.emit(f"$ {self.command}", "info")
            self.process = subprocess.Popen(
                ["/bin/bash", "-lc", self.command],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.log_line.emit(line.rstrip("\n"), "plain")
            rc = self.process.wait()
            self.finished.emit(rc, self.title)
        except Exception as exc:
            self.log_line.emit(str(exc), "err")
            self.finished.emit(1, self.title)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Freqtrade + FreqAI 中文按钮界面")
        self.resize(1080, 760)

        self._thread: QThread | None = None
        self._worker: CommandWorker | None = None

        self._build_ui()
        self._ensure_private_config()
        self._load_private_config()
        self._refresh_status()
        self._refresh_backtest_summary()

        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start()

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("Freqtrade + FreqAI 中文按钮界面")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(title)

        subtitle = QLabel("推荐顺序：1 保存配置  2 初始化环境  3 下载数据  4 先回测  5 启动模拟盘  6 打开 WebUI")
        subtitle.setStyleSheet("color: #555;")
        layout.addWidget(subtitle)

        status_frame = QFrame()
        status_frame.setFrameShape(QFrame.Shape.StyledPanel)
        status_layout = QGridLayout(status_frame)
        status_layout.addWidget(QLabel("配置文件"), 0, 0)
        self.config_status = QLabel("未知")
        status_layout.addWidget(self.config_status, 0, 1)
        status_layout.addWidget(QLabel("Docker"), 0, 2)
        self.docker_status = QLabel("未知")
        status_layout.addWidget(self.docker_status, 0, 3)
        status_layout.addWidget(QLabel("模拟盘"), 0, 4)
        self.bot_status = QLabel("未知")
        status_layout.addWidget(self.bot_status, 0, 5)
        layout.addWidget(status_frame)

        config_frame = QFrame()
        config_frame.setFrameShape(QFrame.Shape.StyledPanel)
        config_layout = QGridLayout(config_frame)
        config_layout.addWidget(self._section("交易所与界面配置"), 0, 0, 1, 6)

        config_layout.addWidget(QLabel("API Key"), 1, 0)
        self.api_key = QLineEdit()
        config_layout.addWidget(self.api_key, 1, 1, 1, 2)

        config_layout.addWidget(QLabel("API Secret"), 2, 0)
        self.api_secret = QLineEdit()
        self.api_secret.setEchoMode(QLineEdit.EchoMode.Password)
        config_layout.addWidget(self.api_secret, 2, 1, 1, 2)

        config_layout.addWidget(QLabel("交易所口令"), 3, 0)
        self.api_password = QLineEdit()
        self.api_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_password.setPlaceholderText("Binance 可留空，OKX 等部分交易所需要")
        config_layout.addWidget(self.api_password, 3, 1, 1, 2)

        config_layout.addWidget(QLabel("WebUI 用户名"), 1, 3)
        self.webui_user = QLineEdit()
        config_layout.addWidget(self.webui_user, 1, 4)

        config_layout.addWidget(QLabel("WebUI 密码"), 2, 3)
        self.webui_pass = QLineEdit()
        self.webui_pass.setEchoMode(QLineEdit.EchoMode.Password)
        config_layout.addWidget(self.webui_pass, 2, 4)

        btn_gen_secret = QPushButton("生成随机密钥")
        btn_gen_secret.clicked.connect(self._generate_secrets)
        config_layout.addWidget(btn_gen_secret, 3, 3)

        btn_save_config = QPushButton("保存配置")
        btn_save_config.clicked.connect(self._save_private_config)
        config_layout.addWidget(btn_save_config, 3, 4)

        btn_open_config = QPushButton("打开配置文件")
        btn_open_config.clicked.connect(lambda: self._open_path(PRIVATE_CONFIG))
        config_layout.addWidget(btn_open_config, 3, 5)

        layout.addWidget(config_frame)

        action_frame = QFrame()
        action_frame.setFrameShape(QFrame.Shape.StyledPanel)
        action_layout = QGridLayout(action_frame)
        action_layout.addWidget(self._section("按钮操作"), 0, 0, 1, 6)

        action_layout.addWidget(QLabel("下载数据范围"), 1, 0)
        self.download_range = QLineEdit("20230101-")
        self.download_range.setPlaceholderText("例如 20230101- 或 20230101-20250630")
        action_layout.addWidget(self.download_range, 1, 1, 1, 2)
        self.download_fresh = QCheckBox("重下全部数据")
        self.download_fresh.setChecked(True)
        action_layout.addWidget(self.download_fresh, 1, 3)

        action_layout.addWidget(QLabel("回测范围"), 2, 0)
        self.backtest_range = QLineEdit("20250101-20250630")
        action_layout.addWidget(self.backtest_range, 2, 1, 1, 2)

        btn_prepare = QPushButton("1 初始化环境")
        btn_prepare.clicked.connect(lambda: self._run_command("初始化环境", "bash scripts/00_prepare.sh"))
        action_layout.addWidget(btn_prepare, 1, 4)

        btn_download = QPushButton("2 下载数据")
        btn_download.clicked.connect(self._download_data)
        action_layout.addWidget(btn_download, 1, 5)

        btn_backtest = QPushButton("3 开始回测")
        btn_backtest.clicked.connect(self._backtest)
        action_layout.addWidget(btn_backtest, 2, 4)

        btn_dry_run = QPushButton("4 启动模拟盘")
        btn_dry_run.clicked.connect(lambda: self._run_command("启动模拟盘", "bash scripts/03_dry_run.sh"))
        action_layout.addWidget(btn_dry_run, 2, 5)

        btn_stop = QPushButton("停止当前任务")
        btn_stop.clicked.connect(self._stop_running_command)
        action_layout.addWidget(btn_stop, 3, 4)

        btn_stop_bot = QPushButton("停止模拟盘")
        btn_stop_bot.clicked.connect(lambda: self._run_command("停止模拟盘", "bash scripts/04_stop.sh"))
        action_layout.addWidget(btn_stop_bot, 3, 5)

        layout.addWidget(action_frame)

        summary_frame = QFrame()
        summary_frame.setFrameShape(QFrame.Shape.StyledPanel)
        summary_layout = QGridLayout(summary_frame)
        summary_layout.addWidget(self._section("傻瓜结论区"), 0, 0, 1, 4)

        summary_layout.addWidget(QLabel("结论"), 1, 0)
        self.summary_grade = QLabel("暂无回测结果")
        self.summary_grade.setStyleSheet("font-weight: 700; color: #666;")
        summary_layout.addWidget(self.summary_grade, 1, 1)

        summary_layout.addWidget(QLabel("一句话"), 1, 2)
        self.summary_one_line = QLabel("先点“开始回测”，跑完后这里会自动告诉你结果。")
        self.summary_one_line.setWordWrap(True)
        summary_layout.addWidget(self.summary_one_line, 1, 3)

        summary_layout.addWidget(QLabel("关键数字"), 2, 0)
        self.summary_stats = QLabel("暂无")
        self.summary_stats.setWordWrap(True)
        summary_layout.addWidget(self.summary_stats, 2, 1, 1, 3)

        summary_layout.addWidget(QLabel("建议动作"), 3, 0)
        self.summary_action = QLabel("先做一次回测。")
        self.summary_action.setWordWrap(True)
        summary_layout.addWidget(self.summary_action, 3, 1, 1, 3)

        layout.addWidget(summary_frame)

        tools_frame = QFrame()
        tools_frame.setFrameShape(QFrame.Shape.StyledPanel)
        tools_layout = QHBoxLayout(tools_frame)
        tools_layout.addWidget(self._section("常用入口"))
        btn_webui = QPushButton("打开 WebUI")
        btn_webui.clicked.connect(lambda: webbrowser.open("http://127.0.0.1:8080"))
        tools_layout.addWidget(btn_webui)

        btn_readme = QPushButton("打开中文说明")
        btn_readme.clicked.connect(lambda: self._open_path(README_PATH))
        tools_layout.addWidget(btn_readme)

        btn_data_dir = QPushButton("打开数据目录")
        btn_data_dir.clicked.connect(lambda: self._open_path(DATA_DIR))
        tools_layout.addWidget(btn_data_dir)

        btn_result_dir = QPushButton("打开回测结果")
        btn_result_dir.clicked.connect(lambda: self._open_path(BACKTEST_DIR))
        tools_layout.addWidget(btn_result_dir)

        btn_logs = QPushButton("读取容器日志")
        btn_logs.clicked.connect(
            lambda: self._run_command(
                "读取容器日志",
                "docker compose logs --tail 200 freqtrade",
            )
        )
        tools_layout.addWidget(btn_logs)
        tools_layout.addStretch(1)
        layout.addWidget(tools_frame)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setStyleSheet(
            "QPlainTextEdit { background: #1e1e1e; color: #d4d4d4; border-radius: 6px; }"
        )
        layout.addWidget(self.log_box, 1)

    def _section(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-weight: 700; font-size: 13px;")
        return label

    def _ensure_private_config(self) -> None:
        if not PRIVATE_CONFIG.exists() and PRIVATE_EXAMPLE.exists():
            PRIVATE_CONFIG.write_text(PRIVATE_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    def _load_private_config(self) -> None:
        try:
            with PRIVATE_CONFIG.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

        exchange = data.get("exchange", {})
        api_server = data.get("api_server", {})

        self.api_key.setText(str(exchange.get("key", "")))
        self.api_secret.setText(str(exchange.get("secret", "")))
        self.api_password.setText(str(exchange.get("password", "")))
        self.webui_user.setText(str(api_server.get("username", "admin")))
        self.webui_pass.setText(str(api_server.get("password", "")))

    def _save_private_config(self) -> None:
        try:
            if PRIVATE_CONFIG.exists():
                with PRIVATE_CONFIG.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                with PRIVATE_EXAMPLE.open("r", encoding="utf-8") as f:
                    data = json.load(f)

            data.setdefault("exchange", {})
            data.setdefault("api_server", {})
            data.setdefault("telegram", {})

            data["exchange"]["key"] = self.api_key.text().strip()
            data["exchange"]["secret"] = self.api_secret.text().strip()
            data["exchange"]["password"] = self.api_password.text().strip()
            data["api_server"]["enabled"] = True
            data["api_server"]["listen_ip_address"] = "0.0.0.0"
            data["api_server"]["listen_port"] = 8080
            data["api_server"]["verbosity"] = "error"
            data["api_server"]["username"] = self.webui_user.text().strip() or "admin"
            data["api_server"]["password"] = self.webui_pass.text().strip() or "admin123456"
            data["api_server"]["jwt_secret_key"] = data["api_server"].get("jwt_secret_key") or secrets.token_urlsafe(32)
            data["api_server"]["ws_token"] = data["api_server"].get("ws_token") or secrets.token_urlsafe(32)

            PRIVATE_CONFIG.write_text(
                json.dumps(data, ensure_ascii=False, indent=4) + "\n",
                encoding="utf-8",
            )
            self._append_log("配置已保存到 user_data/config.private.json", "ok")
            self._refresh_status()
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    def _generate_secrets(self) -> None:
        if not self.webui_pass.text().strip():
            self.webui_pass.setText(secrets.token_urlsafe(16))
        self._append_log("已生成 WebUI 密码；JWT 和 WS 密钥会在保存时自动生成。", "info")

    def _download_data(self) -> None:
        timerange = self.download_range.text().strip() or "20230101-"
        mode = " fresh" if self.download_fresh.isChecked() else ""
        self._run_command("下载数据", f"bash scripts/01_download_data.sh {timerange}{mode}")

    def _backtest(self) -> None:
        timerange = self.backtest_range.text().strip() or "20250101-20250630"
        self._run_command("开始回测", f"bash scripts/02_backtest.sh {timerange}")

    def _run_command(self, title: str, command: str) -> None:
        if self._thread and self._thread.isRunning():
            QMessageBox.information(self, "请稍等", "当前还有任务在运行，请先等待完成或点击“停止当前任务”。")
            return

        self._thread = QThread(self)
        self._worker = CommandWorker(title, command)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log_line.connect(self._append_log)
        self._worker.finished.connect(self._on_command_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _stop_running_command(self) -> None:
        if self._worker:
            self._worker.stop()
            self._append_log("已请求停止当前任务。", "warn")

    def _on_command_finished(self, code: int, title: str) -> None:
        if code == 0:
            self._append_log(f"[完成] {title}", "ok")
        else:
            self._append_log(f"[失败] {title}，退出码 {code}", "err")
        self._worker = None
        self._thread = None
        self._refresh_status()
        if title == "开始回测" and code == 0:
            self._refresh_backtest_summary()

    def _append_log(self, text: str, level: str = "plain") -> None:
        color = {
            "info": "#8ecae6",
            "ok": "#90be6d",
            "warn": "#f9c74f",
            "err": "#f94144",
            "plain": "#d4d4d4",
        }.get(level, "#d4d4d4")
        self.log_box.appendHtml(f'<span style="color:{color}">{text}</span>')

    def _refresh_status(self) -> None:
        self.config_status.setText("已就绪" if PRIVATE_CONFIG.exists() else "缺失")
        self.config_status.setStyleSheet("color: #2a9d8f;" if PRIVATE_CONFIG.exists() else "color: #e76f51;")

        docker_ok = self._check_command("docker --version")
        self.docker_status.setText("已安装" if docker_ok else "未安装")
        self.docker_status.setStyleSheet("color: #2a9d8f;" if docker_ok else "color: #e76f51;")

        running = self._check_command("docker compose ps --status running --services | rg '^freqtrade$'")
        self.bot_status.setText("运行中" if running else "未运行")
        self.bot_status.setStyleSheet("color: #2a9d8f;" if running else "color: #777;")

    def _refresh_backtest_summary(self) -> None:
        try:
            summary = self._load_latest_backtest_summary()
        except Exception as exc:
            self.summary_grade.setText("读取失败")
            self.summary_grade.setStyleSheet("font-weight: 700; color: #e76f51;")
            self.summary_one_line.setText(str(exc))
            self.summary_stats.setText("暂无")
            self.summary_action.setText("先重新点一次“开始回测”。")
            return

        if not summary:
            self.summary_grade.setText("暂无回测结果")
            self.summary_grade.setStyleSheet("font-weight: 700; color: #666;")
            self.summary_one_line.setText("先点“开始回测”，跑完后这里会自动告诉你结果。")
            self.summary_stats.setText("暂无")
            self.summary_action.setText("先做一次回测。")
            return

        grade, grade_color, one_line, action = self._humanize_backtest(summary)
        stats = (
            f"总收益 {summary['profit_pct']:.2f}% | "
            f"盈利 {summary['profit_abs']:.3f} USDT | "
            f"交易 {summary['trades']} 次 | "
            f"胜率 {summary['winrate_pct']:.1f}% | "
            f"最大回撤 {summary['drawdown_pct']:.2f}% | "
            f"时间 {summary['backtest_start']} -> {summary['backtest_end']}"
        )

        self.summary_grade.setText(grade)
        self.summary_grade.setStyleSheet(f"font-weight: 700; color: {grade_color};")
        self.summary_one_line.setText(one_line)
        self.summary_stats.setText(stats)
        self.summary_action.setText(action)

    def _load_latest_backtest_summary(self) -> dict | None:
        if not LAST_RESULT_PATH.exists():
            return None

        with LAST_RESULT_PATH.open("r", encoding="utf-8") as f:
            last_result = json.load(f)

        zip_name = last_result.get("latest_backtest")
        if not zip_name:
            return None

        zip_path = BACKTEST_DIR / str(zip_name)
        if not zip_path.exists():
            return None

        with ZipFile(zip_path) as zf:
            result_json_name = next(
                name for name in zf.namelist()
                if name.endswith(".json") and "_config" not in name
            )
            data = json.loads(zf.read(result_json_name))

        comparison = data.get("strategy_comparison") or []
        if not comparison:
            return None

        best = comparison[0]
        strategy_name = best.get("key")
        strategy_data = (data.get("strategy") or {}).get(strategy_name, {})

        return {
            "strategy_name": strategy_name,
            "profit_pct": float(best.get("profit_total_pct", 0.0)),
            "profit_abs": float(best.get("profit_total_abs", 0.0)),
            "trades": int(best.get("trades", 0)),
            "winrate_pct": float(best.get("winrate", 0.0)) * 100,
            "drawdown_pct": float(best.get("max_drawdown_account", 0.0)) * 100,
            "profit_factor": float(best.get("profit_factor", 0.0)),
            "backtest_start": str(strategy_data.get("backtest_start", "-")),
            "backtest_end": str(strategy_data.get("backtest_end", "-")),
        }

    def _humanize_backtest(self, summary: dict) -> tuple[str, str, str, str]:
        profit_pct = summary["profit_pct"]
        drawdown_pct = summary["drawdown_pct"]
        trades = summary["trades"]
        profit_factor = summary["profit_factor"]

        if trades < 10:
            return (
                "样本太少",
                "#f4a261",
                "这次交易次数太少，结果参考价值有限。",
                "先扩大回测时间范围，至少让交易次数更高，再决定要不要继续。",
            )

        if profit_pct <= 0:
            return (
                "不建议用",
                "#e76f51",
                "这次回测总体亏钱，先不要上真钱。",
                "先继续改策略或换参数，不要急着实盘。",
            )

        if profit_pct < 2 or profit_factor < 1.2:
            return (
                "能跑但偏弱",
                "#e9c46a",
                "这套机器人现在是测试骨架，能工作，但赚钱能力偏弱。",
                "可以继续观察模拟盘，也可以让我帮你继续优化策略。",
            )

        if drawdown_pct > 10:
            return (
                "收益还行但风险偏高",
                "#f4a261",
                "看起来能赚，但回撤偏大，真钱阶段容易扛不住。",
                "先把风控收紧，再考虑模拟盘长期运行。",
            )

        return (
            "结果不错",
            "#2a9d8f",
            "这次回测表现还可以，可以继续跑模拟盘验证稳定性。",
            "先别直接上真钱，至少再跑 30 天模拟盘。",
        )

    def _check_command(self, command: str) -> bool:
        try:
            result = subprocess.run(
                ["/bin/bash", "-lc", command],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=8,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _open_path(self, path: Path) -> None:
        try:
            if not path.exists():
                raise RuntimeError(f"路径不存在：{path}")
            open_external(str(path))
        except Exception as exc:
            QMessageBox.warning(self, "打开失败", str(exc))


def main() -> None:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
