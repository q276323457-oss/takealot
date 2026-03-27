from functools import reduce
from typing import Any

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.strategy import IStrategy, informative


class OKXRiverTrendStrategyV2(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "5m"
    can_short = True
    process_only_new_candles = True
    startup_candle_count = 700
    use_exit_signal = True

    minimal_roi = {
        "0": 0.03,
        "90": 0.018,
        "240": 0.008,
        "480": 0.0,
    }
    stoploss = -0.018
    trailing_stop = True
    trailing_stop_positive = 0.008
    trailing_stop_positive_offset = 0.015
    trailing_only_offset_is_reached = True

    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    plot_config = {
        "main_plot": {
            "ema9": {"color": "green"},
            "ema21": {"color": "blue"},
            "ema50": {"color": "orange"},
        },
        "subplots": {
            "ADX": {"adx": {"color": "red"}},
            "RSI": {"rsi": {"color": "purple"}},
            "量能": {"relative_volume": {"color": "brown"}},
        },
    }

    @informative("15m")
    def populate_indicators_15m(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    @informative("30m")
    def populate_indicators_30m(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema100"] = ta.EMA(dataframe, timeperiod=100)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["volume_mean_36"] = dataframe["volume"].rolling(36).mean()
        dataframe["relative_volume"] = dataframe["volume"] / dataframe["volume_mean_36"]
        dataframe["hh_12"] = dataframe["high"].rolling(12).max().shift(1)
        dataframe["ll_12"] = dataframe["low"].rolling(12).min().shift(1)
        dataframe["hh_18"] = dataframe["high"].rolling(18).max().shift(1)
        dataframe["ll_18"] = dataframe["low"].rolling(18).min().shift(1)
        dataframe["price_change_3"] = dataframe["close"] / dataframe["close"].shift(3) - 1
        return dataframe

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        long_conditions = [
            df["volume"] > 0,
            df["close_30m"] > df["ema100_30m"] * 1.006,
            df["ema20_30m"] > df["ema50_30m"],
            df["ema50_30m"] > df["ema100_30m"],
            df["adx_30m"] > 18,
            df["rsi_30m"] > 58,
            df["close_15m"] > df["ema20_15m"],
            df["ema20_15m"] > df["ema50_15m"],
            df["adx_15m"] > 16,
            df["rsi_15m"] > 55,
            df["rsi_15m"] < 70,
            df["ema9"] > df["ema21"],
            df["ema21"] > df["ema50"],
            df["rsi"] > 53,
            df["rsi"] < 66,
            df["adx"] > 14,
            df["relative_volume"] > 0.9,
            df["atr_pct"] < 0.016,
            df["price_change_3"] > 0.0025,
            df["close"] > df["hh_18"],
        ]

        short_conditions = [
            df["volume"] > 0,
            df["close_30m"] < df["ema100_30m"] * 0.998,
            df["ema20_30m"] < df["ema50_30m"],
            df["ema50_30m"] < df["ema100_30m"],
            df["adx_30m"] > 18,
            df["rsi_30m"] < 48,
            df["close_15m"] < df["ema20_15m"],
            df["ema20_15m"] < df["ema50_15m"],
            df["adx_15m"] > 16,
            df["rsi_15m"] < 50,
            df["rsi_15m"] > 28,
            df["ema9"] < df["ema21"],
            df["ema21"] < df["ema50"],
            df["rsi"] < 50,
            df["rsi"] > 32,
            df["adx"] > 14,
            df["relative_volume"] > 0.8,
            df["atr_pct"] < 0.018,
            df["price_change_3"] < -0.0015,
            df["close"] < df["ll_12"],
        ]

        if long_conditions:
            df.loc[
                reduce(lambda x, y: x & y, long_conditions),
                ["enter_long", "enter_tag"],
            ] = (1, "river_v2_long")

        if short_conditions:
            df.loc[
                reduce(lambda x, y: x & y, short_conditions),
                ["enter_short", "enter_tag"],
            ] = (1, "river_v2_short")

        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        exit_long_conditions = [
            df["volume"] > 0,
            (
                (df["close"] < df["ema21"])
                | (df["rsi"] < 45)
                | (df["close_15m"] < df["ema20_15m"])
                | (df["rsi_15m"] < 46)
            ),
        ]

        exit_short_conditions = [
            df["volume"] > 0,
            (
                (df["close"] > df["ema21"])
                | (df["rsi"] > 55)
                | (df["close_15m"] > df["ema20_15m"])
                | (df["rsi_15m"] > 54)
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

        if side == "long" and rate > last_candle["close"] * 1.002:
            return False
        if side == "short" and rate < last_candle["close"] * 0.998:
            return False

        return True
