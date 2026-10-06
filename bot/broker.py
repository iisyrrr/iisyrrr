"""Connexion à MetaTrader 5 (Windows uniquement).

Le terminal MT5 doit être lancé et connecté au compte sur le VPS, avec le
bouton « Algo Trading » activé. Le module Python pilote ce terminal.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class SymbolSpec:
    name: str
    point: float
    digits: int
    vol_min: float
    vol_max: float
    vol_step: float
    stops_level: int  # distance mini SL/TP en points imposée par le broker
    spread_points: int
    bid: float
    ask: float
    tick_time: float = 0.0  # horodatage du dernier prix reçu (figé quand le marché est fermé)


@dataclass
class Account:
    balance: float
    equity: float
    currency: str


@dataclass
class Position:
    ticket: int
    symbol: str
    side: int
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float


@dataclass
class OrderResult:
    ok: bool
    ticket: int = 0
    price: float = 0.0
    message: str = ""


@dataclass
class ClosedDeal:
    ticket: int
    position_id: int
    symbol: str
    volume: float
    price: float
    profit: float  # profit + commission + swap
    reason: str


class MT5Broker:
    def __init__(self, mt5_cfg: dict, magic: int, deviation: int):
        try:
            import MetaTrader5 as mt5
        except ImportError as e:  # pragma: no cover - dépend de Windows
            raise RuntimeError(
                "Le module MetaTrader5 est introuvable. Il ne fonctionne que sous "
                "Windows : `pip install MetaTrader5` sur le VPS Windows."
            ) from e
        self.mt5 = mt5
        self.cfg = mt5_cfg
        self.magic = magic
        self.deviation = deviation

    # ------------------------------------------------------------- connexion
    def connect(self) -> None:
        kwargs = {}
        if self.cfg.get("path"):
            kwargs["path"] = self.cfg["path"]
        if self.cfg.get("login"):
            kwargs.update(
                login=int(self.cfg["login"]),
                password=str(self.cfg["password"]),
                server=str(self.cfg["server"]),
            )
        if not self.mt5.initialize(**kwargs):
            raise RuntimeError(f"Connexion MT5 impossible : {self.mt5.last_error()}")
        info = self.mt5.terminal_info()
        if info is not None and not info.trade_allowed:
            log.warning("« Algo Trading » est désactivé dans le terminal MT5 : aucun ordre ne passera.")
        log.info("Connecté à MT5 (%s)", self.mt5.account_info().server)

    def ensure_connected(self) -> None:
        if self.mt5.terminal_info() is None:
            log.warning("Connexion MT5 perdue, reconnexion…")
            self.mt5.shutdown()
            self.connect()

    def shutdown(self) -> None:
        self.mt5.shutdown()

    # ------------------------------------------------------------- données
    def rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Renvoie les `count` dernières bougies CLÔTURÉES."""
        mt5 = self.mt5
        mt5.symbol_select(symbol, True)
        tf = getattr(mt5, f"TIMEFRAME_{timeframe}")
        arr = mt5.copy_rates_from_pos(symbol, tf, 0, count + 1)
        if arr is None or len(arr) < 2:
            raise RuntimeError(f"Pas de données pour {symbol} : {mt5.last_error()}")
        df = pd.DataFrame(arr)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df.set_index("time").iloc[:-1]  # retire la bougie en cours

    def spec(self, symbol: str) -> SymbolSpec:
        info = self.mt5.symbol_info(symbol)
        tick = self.mt5.symbol_info_tick(symbol)
        if info is None or tick is None:
            raise RuntimeError(f"Symbole {symbol} introuvable chez le broker")
        return SymbolSpec(
            name=symbol,
            point=info.point,
            digits=info.digits,
            vol_min=info.volume_min,
            vol_max=info.volume_max,
            vol_step=info.volume_step,
            stops_level=info.trade_stops_level,
            spread_points=int(round((tick.ask - tick.bid) / info.point)),
            bid=tick.bid,
            ask=tick.ask,
            tick_time=float(getattr(tick, "time_msc", 0) or getattr(tick, "time", 0)),
        )

    def account(self) -> Account:
        a = self.mt5.account_info()
        return Account(balance=a.balance, equity=a.equity, currency=a.currency)

    def positions(self) -> list[Position]:
        out = []
        for p in self.mt5.positions_get() or []:
            if p.magic != self.magic:
                continue
            side = 1 if p.type == self.mt5.POSITION_TYPE_BUY else -1
            out.append(Position(p.ticket, p.symbol, side, p.volume, p.price_open, p.sl, p.tp, p.profit))
        return out

    def loss_per_lot(self, symbol: str, side: int, entry: float, sl: float) -> float:
        order_type = self.mt5.ORDER_TYPE_BUY if side == 1 else self.mt5.ORDER_TYPE_SELL
        profit = self.mt5.order_calc_profit(order_type, symbol, 1.0, entry, sl)
        if profit is None:
            raise RuntimeError(f"Calcul du risque impossible pour {symbol} : {self.mt5.last_error()}")
        return abs(profit)

    # ------------------------------------------------------------- ordres
    def _filling(self, symbol: str) -> int:
        mode = self.mt5.symbol_info(symbol).filling_mode
        if mode & 1:
            return self.mt5.ORDER_FILLING_FOK
        if mode & 2:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    def _send(self, request: dict) -> OrderResult:
        res = self.mt5.order_send(request)
        if res is None:
            return OrderResult(False, message=str(self.mt5.last_error()))
        if res.retcode not in (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_PLACED):
            return OrderResult(False, message=f"retcode {res.retcode} : {res.comment}")
        return OrderResult(True, ticket=res.order, price=res.price, message=res.comment)

    def market_order(self, symbol: str, side: int, volume: float, sl: float, tp: float, comment: str) -> OrderResult:
        tick = self.mt5.symbol_info_tick(symbol)
        return self._send(
            {
                "action": self.mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": float(volume),
                "type": self.mt5.ORDER_TYPE_BUY if side == 1 else self.mt5.ORDER_TYPE_SELL,
                "price": tick.ask if side == 1 else tick.bid,
                "sl": float(sl),
                "tp": float(tp),
                "deviation": self.deviation,
                "magic": self.magic,
                "comment": comment[:31],
                "type_time": self.mt5.ORDER_TIME_GTC,
                "type_filling": self._filling(symbol),
            }
        )

    def close_position(self, pos: Position) -> OrderResult:
        tick = self.mt5.symbol_info_tick(pos.symbol)
        return self._send(
            {
                "action": self.mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": float(pos.volume),
                "type": self.mt5.ORDER_TYPE_SELL if pos.side == 1 else self.mt5.ORDER_TYPE_BUY,
                "position": pos.ticket,
                "price": tick.bid if pos.side == 1 else tick.ask,
                "deviation": self.deviation,
                "magic": self.magic,
                "comment": "fermeture robot",
                "type_time": self.mt5.ORDER_TIME_GTC,
                "type_filling": self._filling(pos.symbol),
            }
        )

    def closed_deals(self, after_ticket: int) -> list[ClosedDeal]:
        """Sorties de position du robot (ticket > after_ticket) des 7 derniers jours."""
        mt5 = self.mt5
        now = datetime.now(timezone.utc)
        deals = mt5.history_deals_get(now - timedelta(days=7), now + timedelta(days=2)) or []
        reasons = {mt5.DEAL_REASON_SL: "stop-loss", mt5.DEAL_REASON_TP: "take-profit"}
        out = []
        for d in deals:
            if d.magic != self.magic or d.ticket <= after_ticket:
                continue
            if d.entry not in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY):
                continue
            out.append(
                ClosedDeal(
                    ticket=d.ticket,
                    position_id=d.position_id,
                    symbol=d.symbol,
                    volume=d.volume,
                    price=d.price,
                    profit=d.profit + d.commission + d.swap,
                    reason=reasons.get(d.reason, "manuel/robot"),
                )
            )
        return sorted(out, key=lambda d: d.ticket)
