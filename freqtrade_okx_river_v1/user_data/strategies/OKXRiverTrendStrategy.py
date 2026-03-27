from functools import reduce
from typing import Any

import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from freqtrade.strategy import IStrategy, informative


class OKXRiverTrendStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = True
    process_only_new_candles = True
    startup_candle_count = 450
    use_exit_signal = True

    minimal_roi = {
        "0": 0.05,
        "360": 0.025,
        "1080": 0.01,
        "2160": 0.0,
    }
    stoploss = -0.03
    trailing_stop = True
    trailing_stop_positive = 0.012
    trailing_stop_positive_offset = 0.025
    trailing_only_offset_is_reached = True

    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    plot_config = {
        "main_plot": {
            "ema20": {"color": "green"},
            "ema50": {"color": "blue"},
            "ema100": {"color": "orange"},
        },
        "subplots": {
            "ADX": {"adx": {"color": "red"}},
            "RSI": {"rsi": {"color": "purple"}},
            "量能": {"relative_volume": {"color": "brown"}},
        },
    }

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema100"] = ta.EMA(dataframe, timeperiod=100)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema100"] = ta.EMA(dataframe, timeperiod=100)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["volume_mean_24"] = dataframe["volume"].rolling(24).mean()
        dataframe["relative_volume"] = dataframe["volume"] / dataframe["volume_mean_24"]
        dataframe["hh_12"] = dataframe["high"].rolling(12).max().shift(1)
        dataframe["ll_12"] = dataframe["low"].rolling(12).min().shift(1)

        bollinger = qtpylib.bollinger_bands(
            qtpylib.typical_price(dataframe), window=20, stds=2.0
        )
        dataframe["bb_mid"] = bollinger["mid"]
        return dataframe

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        long_conditions = [
            df["volume"] > 0,
            df["close"] > df["ema100"],
            df["ema20"] > df["ema50"],
            df["ema50"] > df["ema100"],
            df["close_4h"] > df["ema100_4h"],
            df["ema50_4h"] > df["ema100_4h"],
            df["adx"] > 18,
            df["adx_4h"] > 20,
            df["rsi"] > 52,
            df["rsi"] < 72,
            df["rsi_4h"] > 52,
            df["relative_volume"] > 0.8,
            df["atr_pct"] < 0.09,
            df["close"] > df["bb_mid"],
            qtpylib.crossed_above(df["close"], df["hh_12"]),
        ]

        short_conditions = [
            df["volume"] > 0,
            df["close"] < df["ema100"],
            df["ema20"] < df["ema50"],
            df["ema50"] < df["ema100"],
            df["close_4h"] < df["ema100_4h"],
            df["ema50_4h"] < df["ema100_4h"],
            df["adx"] > 18,
            df["adx_4h"] > 20,
            df["rsi"] < 48,
            df["rsi"] > 28,
            df["rsi_4h"] < 48,
            df["relative_volume"] > 0.8,
            df["atr_pct"] < 0.09,
            df["close"] < df["bb_mid"],
            qtpylib.crossed_below(df["close"], df["ll_12"]),
        ]

        if long_conditions:
            df.loc[
                reduce(lambda x, y: x & y, long_conditions),
                ["enter_long", "enter_tag"],
            ] = (1, "river_trend_long")

        if short_conditions:
            df.loc[
                reduce(lambda x, y: x & y, short_conditions),
                ["enter_short", "enter_tag"],
            ] = (1, "river_trend_short")

        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        exit_long_conditions = [
            df["volume"] > 0,
            (
                qtpylib.crossed_below(df["close"], df["ema20"])
                | (df["rsi"] < 45)
                | (df["close_4h"] < df["ema50_4h"])
                | (df["rsi_4h"] < 46)
            ),
        ]

        exit_short_conditions = [
            df["volume"] > 0,
            (
                qtpylib.crossed_above(df["close"], df["ema20"])
                | (df["rsi"] > 55)
                | (df["close_4h"] > df["ema50_4h"])
                | (df["rsi_4h"] > 54)
            ),
        ]

        if exit_long_conditions:
            df.loc[
                reduce(lambda x, y: x & y, exit_long_conditions),
                "exit_long",
            ] = 1

        if exit_short_conditions:
            df.loc[
                reduce(lambda x, y: x & y, exit_short_conditions),
                "exit_short",
            ] = 1

        return df

    def leverage(
        self,
        pair: str,
        current_time,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> float:
        return min(2.0, max_leverage)

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

        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return True

        last_candle = dataframe.iloc[-1]

        if side == "long" and rate > last_candle["close"] * 1.004:
            return False
        if side == "short" and rate < last_candle["close"] * 0.996:
            return False

        return True
