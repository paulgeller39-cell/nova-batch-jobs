from AlgorithmImports import *

class NovaBtcEmaCross(QCAlgorithm):
    """Nova's first Lean algorithm: 10/50 EMA cross on BTCUSD (Coinbase, daily).

    Long-only spot, cash account — deliberately the same shape as Nova's
    bot-backtest harness so results can be compared once the cost models
    are matched. BACKTEST ONLY: this repo contains no brokerage
    credentials and the engine config is hard-set to backtesting.
    """

    def Initialize(self):
        # NOTE (calibration): these dates assume the free sample data shipped
        # in the QuantConnect/Lean repo covers BTCUSD/Coinbase for 2021.
        # The workflow's "coverage" step prints the actual Data/crypto files;
        # pin these dates to that coverage before trusting any result.
        self.SetStartDate(2021, 1, 1)
        self.SetEndDate(2021, 12, 31)
        self.SetCash(100000)
        # Coinbase brokerage model = its fee/fill models; cash (spot) account.
        self.SetBrokerageModel(BrokerageName.Coinbase, AccountType.Cash)
        self.btc = self.AddCrypto("BTCUSD", Resolution.Daily, Market.Coinbase).Symbol
        self.fast = self.EMA(self.btc, 10, Resolution.Daily)
        self.slow = self.EMA(self.btc, 50, Resolution.Daily)
        self.SetWarmUp(60, Resolution.Daily)

    def OnData(self, data):
        if self.IsWarmingUp:
            return
        if not (self.fast.IsReady and self.slow.IsReady):
            return
        if self.btc not in data:
            return
        invested = self.Portfolio[self.btc].Invested
        if self.fast.Current.Value > self.slow.Current.Value and not invested:
            self.SetHoldings(self.btc, 0.95)
        elif self.fast.Current.Value < self.slow.Current.Value and invested:
            self.Liquidate(self.btc)
