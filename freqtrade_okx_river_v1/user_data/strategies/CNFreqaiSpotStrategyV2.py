import logging
from functools import reduce
from typing import Any

import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy


logger = logging.getLogger(__name__)


class CNFreqaiSpotStrategyV2(IStrategy):
    """
    更保守的中文 FreqAI 现货策略 v2。

    调整目标：
    - 少开仓，尽量只做更明确的上涨段
    - 更早止损，避免把小错拖成大错
    - 继续保留 FreqAI 预测，但不只靠一个预测值下单
    """

    INTERFACE_VERSION = 3

    minimal_roi = {
        "0": 0.03,
        "720": 0.015,
        "2160": 0.006,
        "4320": 0.0,
    }
    stoploss = -0.03
    use_custom_stoploss = True
    process_only_new_candles = True
    startup_candle_count: int = 240
    use_exit_signal = False
    can_short = False

    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    plot_config = {
        "main_plot": {
            "ema_20": {"color": "green"},
            "ema_50": {"color": "blue"},
            "ema_200": {"color": "orange"},
        },
        "subplots": {
            "预测收益": {"&-future_return": {"color": "green"}},
            "预测开关": {"do_predict": {"color": "brown"}},
            "RSI": {"rsi": {"color": "purple"}},
            "ADX": {"adx": {"color": "red"}},
        },
    }

    def feature_engineering_expand_all(
        self, dataframe: DataFrame, period: int, metadata: dict, **kwargs: Any
    ) -> DataFrame:
        dataframe["%-rsi-period"] = ta.RSI(dataframe, timeperiod=period)
        dataframe["%-mfi-period"] = ta.MFI(dataframe, timeperiod=period)
        dataframe["%-adx-period"] = ta.ADX(dataframe, timeperiod=period)
        dataframe["%-sma-period"] = ta.SMA(dataframe, timeperiod=period)
        dataframe["%-ema-period"] = ta.EMA(dataframe, timeperiod=period)
        dataframe["%-roc-period"] = ta.ROC(dataframe, timeperiod=period)

        bollinger = qtpylib.bollinger_bands(
            qtpylib.typical_price(dataframe), window=period, stds=2.0
        )
        dataframe["bb_lowerband-period"] = bollinger["lower"]
        dataframe["bb_middleband-period"] = bollinger["mid"]
        dataframe["bb_upperband-period"] = bollinger["upper"]
        dataframe["%-bb_width-period"] = (
            dataframe["bb_upperband-period"] - dataframe["bb_lowerband-period"]
        ) / dataframe["bb_middleband-period"]

        dataframe["%-relative_volume-period"] = (
            dataframe["volume"] / dataframe["volume"].rolling(period).mean()
        )

        return dataframe

    def feature_engineering_expand_basic(
        self, dataframe: DataFrame, metadata: dict, **kwargs: Any
    ) -> DataFrame:
        dataframe["%-pct-change"] = dataframe["close"].pct_change()
        dataframe["%-raw_volume"] = dataframe["volume"]
        dataframe["%-raw_price"] = dataframe["close"]
        return self._populate_base_indicators(dataframe)

    def feature_engineering_standard(
        self, dataframe: DataFrame, metadata: dict, **kwargs: Any
    ) -> DataFrame:
        dataframe["%-day_of_week"] = dataframe["date"].dt.dayofweek
        dataframe["%-hour_of_day"] = dataframe["date"].dt.hour
        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict, **kwargs: Any) -> DataFrame:
        label_period = self.freqai_info["feature_parameters"]["label_period_candles"]
        dataframe["&-future_return"] = (
            dataframe["close"].shift(-label_period).rolling(label_period).mean() / dataframe["close"] - 1
        )
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self.freqai.start(dataframe, metadata, self)
        return self._populate_base_indicators(dataframe)

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        enter_long_conditions = [
            df["volume"] > 0,
            df["do_predict"] == 1,
            df["&-future_return"] > 0.009,
            df["close"] > df["ema_200"],
            df["ema_50"] > df["ema_200"],
            df["ema_20"] > df["ema_50"],
            df["ema_50_slope"] > 0.0005,
            df["rsi"] > 51,
            df["rsi"] < 70,
            df["adx"] > 18,
            df["mfi"] > 45,
            df["mfi"] < 76,
            df["relative_volume_24"] > 0.75,
            df["atr_pct"] < 0.05,
            df["close"] > df["bb_middleband"],
        ]

        if enter_long_conditions:
            df.loc[
                reduce(lambda x, y: x & y, enter_long_conditions),
                ["enter_long", "enter_tag"],
            ] = (1, "freqai_trend_long_v2")

        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        return df

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs: Any,
    ) -> float:
        if current_profit > 0.04:
            return -0.008
        if current_profit > 0.02:
            return -0.01
        if current_profit > 0.012:
            return -0.014
        if current_profit > 0.006:
            return -0.018
        return -0.028

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time,
        current_rate: float,
        current_profit: float,
        **kwargs: Any,
    ) -> str | None:
        if not self.dp:
            return None

        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return None

        last_candle = dataframe.iloc[-1]

        if current_profit > 0.03 and last_candle["rsi"] > 74:
            return "take_profit_rsi"

        if current_profit > 0.012 and last_candle["close"] < last_candle["ema_20"]:
            return "protect_profit"

        if (
            current_profit > 0.008
            and last_candle.get("do_predict", 0) == 1
            and last_candle["&-future_return"] < 0
            and last_candle["rsi"] < 50
        ):
            return "protect_profit_freqai"

        if current_profit < -0.015 and last_candle["close"] < last_candle["ema_50"]:
            return "cut_loss_trend"

        if (
            current_profit < -0.008
            and last_candle.get("do_predict", 0) == 1
            and last_candle["&-future_return"] < -0.006
            and last_candle["rsi"] < 48
        ):
            return "freqai_reversal"

        return None

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> bool:
        if not self.dp:
            return True

        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df.empty:
            return True

        last_candle = df.iloc[-1]

        if rate > (last_candle["close"] * 1.003):
            return False

        if last_candle["atr_pct"] > 0.05:
            return False

        return True

    @staticmethod
    def _populate_base_indicators(dataframe: DataFrame) -> DataFrame:
        dataframe["ema_20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema_200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["mfi"] = ta.MFI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["volume_mean_24"] = dataframe["volume"].rolling(24).mean()
        dataframe["relative_volume_24"] = dataframe["volume"] / dataframe["volume_mean_24"]
        dataframe["ema_20_slope"] = dataframe["ema_20"] / dataframe["ema_20"].shift(3) - 1
        dataframe["ema_50_slope"] = dataframe["ema_50"] / dataframe["ema_50"].shift(3) - 1

        bollinger = qtpylib.bollinger_bands(
            qtpylib.typical_price(dataframe), window=20, stds=2.0
        )
        dataframe["bb_lowerband"] = bollinger["lower"]
        dataframe["bb_middleband"] = bollinger["mid"]
        dataframe["bb_upperband"] = bollinger["upper"]
        return dataframe
