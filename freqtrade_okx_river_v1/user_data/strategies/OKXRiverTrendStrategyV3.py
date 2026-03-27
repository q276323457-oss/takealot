from functools import reduce
from typing import Any

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.strategy import IStrategy, informative


class OKXRiverTrendStrategyV3(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "5m"
    can_short = True
    process_only_new_candles = True
    startup_candle_count = 500
    use_exit_signal = True

    minimal_roi = {
        "0": 0.01,
        "30": 0.006,
        "90": 0.003,
        "180": 0.0,
    }
    stoploss = -0.012
    trailing_stop = True
    trailing_stop_positive = 0.003
    trailing_stop_positive_offset = 0.005
    trailing_only_offset_is_reached = True

    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    @informative("15m")
    def populate_indicators_15m(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema12"] = ta.EMA(dataframe, timeperiod=12)
        dataframe["ema36"] = ta.EMA(dataframe, timeperiod=36)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    @informative("30m")
    def populate_indicators_30m(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema12"] = ta.EMA(dataframe, timeperiod=12)
        dataframe["ema36"] = ta.EMA(dataframe, timeperiod=36)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema6"] = ta.EMA(dataframe, timeperiod=6)
        dataframe["ema12"] = ta.EMA(dataframe, timeperiod=12)
        dataframe["ema24"] = ta.EMA(dataframe, timeperiod=24)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["volume_mean_24"] = dataframe["volume"].rolling(24).mean()
        dataframe["relative_volume"] = dataframe["volume"] / dataframe["volume_mean_24"]
        dataframe["hh_3"] = dataframe["high"].rolling(3).max().shift(1)
        dataframe["ll_3"] = dataframe["low"].rolling(3).min().shift(1)
        dataframe["hh_6"] = dataframe["high"].rolling(6).max().shift(1)
        dataframe["ll_6"] = dataframe["low"].rolling(6).min().shift(1)
        dataframe["change_2"] = dataframe["close"] / dataframe["close"].shift(2) - 1
        return dataframe

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        short_conditions = [
            df["volume"] > 0,
            df["close_30m"] < df["ema36_30m"] * 1.002,
            df["ema12_30m"] <= df["ema36_30m"],
            df["adx_30m"] > 10,
            df["close_15m"] < df["ema36_15m"],
            df["ema12_15m"] <= df["ema36_15m"],
            df["rsi_15m"] < 55,
            df["ema6"] < df["ema12"],
            df["rsi"] < 56,
            df["rsi"] > 24,
            df["adx"] > 8,
            df["relative_volume"] > 0.45,
            df["atr_pct"] < 0.032,
            (
                (df["close"] < df["ll_3"])
                | ((df["change_2"] < -0.0005) & (df["close"] < df["ema6"]))
            ),
        ]

        if short_conditions:
            df.loc[
                reduce(lambda x, y: x & y, short_conditions),
                ["enter_short", "enter_tag"],
            ] = (1, "river_v3_short")

        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        exit_short_conditions = [
            df["volume"] > 0,
            (
                (df["close"] > df["ema6"])
                | (df["rsi"] > 58)
                | (df["close_15m"] > df["ema12_15m"])
                | (df["close"] > df["hh_6"])
            ),
        ]

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

        if side == "long" and rate > last_candle["close"] * 1.0015:
            return False
        if side == "short" and rate < last_candle["close"] * 0.9985:
            return False

        return True
