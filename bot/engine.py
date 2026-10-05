"""Boucle principale du robot."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .backtest import run_backtest
from .journal import Journal
from .optimizer import OptimizerSettings, optimize
from .risk import adaptive_multiplier, compute_lot, daily_loss_exceeded
from .state import StateStore
from .strategy import Strategy

log = logging.getLogger(__name__)

SIDE_FR = {1: "🟢 ACHAT", -1: "🔴 VENTE"}

HELP = (
    "Commandes :\n"
    "/status – état, compte, positions, réglages\n"
    "/pause – stoppe les nouvelles entrées\n"
    "/resume – reprend le trading\n"
    "/optimize – lance l'auto-amélioration maintenant\n"
    "/closeall oui – ferme TOUTES les positions du robot\n"
    "/help – cette aide"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Bot:
    def __init__(self, cfg: dict, broker, notifier, journal: Journal, store: StateStore,
                 strategy: Strategy, clock=utcnow):
        self.cfg = cfg
        self.broker = broker
        self.notifier = notifier
        self.journal = journal
        self.store = store
        self.strategy = strategy
        self.clock = clock
        self.t = cfg["trading"]
        self.last_bar: dict[str, object] = {}
        self._last_error_alert = 0.0
        self._last_multiplier = 1.0

    @property
    def state(self):
        return self.store.state

    def params_for(self, symbol: str) -> dict:
        p = dict(self.strategy.default_params)
        p.update(self.state.params.get(symbol, {}))
        return p

    # ================================================================ cycle de vie
    def start(self) -> None:
        self.broker.connect()
        self.notifier.drain()
        if self.state.last_deal_ticket is None:
            deals = self.broker.closed_deals(0)
            self.state.last_deal_ticket = max((d.ticket for d in deals), default=0)
        if self.state.last_optimization is None:
            self.state.last_optimization = self.clock().isoformat()
        self.roll_day(send_summary=False)
        self.store.save()

        acct = self.broker.account()
        mode = "LIVE (ordres réels)" if self.cfg["mode"] == "live" else "ALERTES SEULEMENT (aucun ordre)"
        self.notifier.send(
            f"🤖 Robot démarré\nMode : {mode}\nStratégie : {self.strategy.name}\n"
            f"Symboles : {', '.join(self.t['symbols'])} ({self.t['timeframe']})\n"
            f"Équité : {acct.equity:.2f} {acct.currency}\n"
            f"{'⏸ EN PAUSE : ' + self.state.pause_reason if self.state.paused else ''}\n{HELP}"
        )

    def run_forever(self) -> None:
        self.start()
        try:
            while True:
                try:
                    self.tick()
                except Exception as e:  # le robot ne doit jamais mourir sur une erreur ponctuelle
                    log.exception("Erreur dans la boucle")
                    if time.time() - self._last_error_alert > 900:
                        self._last_error_alert = time.time()
                        self.notifier.send(f"⚠️ Erreur : {e!r}\nLe robot continue de tourner.")
                time.sleep(self.t["poll_seconds"])
        except KeyboardInterrupt:
            log.info("Arrêt demandé")
        finally:
            self.notifier.send("🛑 Robot arrêté. Les positions ouvertes gardent leurs SL/TP chez le broker.")
            self.broker.shutdown()

    def tick(self) -> None:
        self.broker.ensure_connected()
        self.handle_commands()
        self.roll_day()
        self.report_closed_trades()
        self.check_daily_loss()
        for symbol in self.t["symbols"]:
            try:
                self.process_symbol(symbol)
            except Exception as e:
                log.exception("Erreur sur %s", symbol)
                self.notifier.send(f"⚠️ {symbol} : {e!r}")
        self.maybe_optimize()

    # ================================================================ trading
    def process_symbol(self, symbol: str) -> None:
        tf = self.t["timeframe"]
        bar_time = self.broker.rates(symbol, tf, 2).index[-1]
        prev = self.last_bar.get(symbol)
        self.last_bar[symbol] = bar_time
        # au démarrage on attend la prochaine bougie (évite de rejouer un vieux signal)
        if prev is None or bar_time <= prev or self.state.paused:
            return
        df = self.broker.rates(symbol, tf, self.t["history_bars"])
        self.evaluate(symbol, df)

    def risk_multiplier(self) -> float:
        a = self.cfg["risk"]["adaptive"]
        if not a["enabled"]:
            return 1.0
        mult = adaptive_multiplier(
            self.journal.recent_profits(a["lookback_trades"]),
            a["lookback_trades"], a["min_profit_factor"], a["reduced_multiplier"],
        )
        if mult != self._last_multiplier:
            self.notifier.send(
                f"🛡 Risque réduit à x{mult} : les {a['lookback_trades']} derniers trades sont sous le seuil."
                if mult < 1 else "✅ Performances revenues : risque normal rétabli."
            )
            self._last_multiplier = mult
        return mult

    def evaluate(self, symbol: str, df) -> None:
        params = self.params_for(symbol)
        sig = self.strategy.last_signal(df, params)
        if sig is None:
            return
        side_txt = SIDE_FR[sig.side]

        positions = self.broker.positions()
        if any(p.symbol == symbol for p in positions):
            log.info("%s : signal ignoré, position déjà ouverte", symbol)
            return
        if len(positions) >= self.cfg["risk"]["max_open_positions"]:
            self.notifier.send(f"ℹ️ {side_txt} {symbol} ignoré : maximum de positions ouvertes atteint.")
            return

        spec = self.broker.spec(symbol)
        if spec.spread_points > self.t["max_spread_points"]:
            self.notifier.send(f"ℹ️ {side_txt} {symbol} ignoré : spread trop large ({spec.spread_points} pts).")
            return
        min_dist = spec.stops_level * spec.point
        if sig.sl_dist < min_dist or sig.tp_dist < min_dist:
            self.notifier.send(f"ℹ️ {side_txt} {symbol} ignoré : SL/TP trop proches pour le broker.")
            return

        entry = spec.ask if sig.side == 1 else spec.bid
        sl = round(entry - sig.side * sig.sl_dist, spec.digits)
        tp = round(entry + sig.side * sig.tp_dist, spec.digits)

        acct = self.broker.account()
        mult = self.risk_multiplier()
        risk_pct = self.cfg["risk"]["risk_per_trade_pct"] * mult
        loss_1lot = self.broker.loss_per_lot(symbol, sig.side, entry, sl)
        lot = compute_lot(acct.equity * risk_pct / 100, loss_1lot, spec.vol_min, spec.vol_max, spec.vol_step)
        if lot <= 0:
            self.notifier.send(
                f"ℹ️ {side_txt} {symbol} ignoré : même le lot minimum ({spec.vol_min}) dépasserait "
                f"le risque autorisé de {risk_pct:.2f} %."
            )
            return

        details = (
            f"Lot : {lot}\nEntrée : {entry:.{spec.digits}f}\n"
            f"SL : {sl:.{spec.digits}f} ({sig.sl_dist / spec.point:.0f} pts)\n"
            f"TP : {tp:.{spec.digits}f} ({sig.tp_dist / spec.point:.0f} pts)\n"
            f"Risque : {lot * loss_1lot:.2f} {acct.currency} ({risk_pct:.2f} %)\n"
            f"Raison : {sig.reason}"
        )
        now = self.clock().isoformat(timespec="seconds")

        if self.cfg["mode"] != "live":
            self.journal.record_signal(now, symbol, sig.side, lot, entry, sl, tp, False, 0, sig.reason, params)
            self.notifier.send(f"📣 SIGNAL {side_txt} {symbol} (non exécuté – mode alertes)\n{details}")
            return

        res = self.broker.market_order(symbol, sig.side, lot, sl, tp, f"robot {self.strategy.name}")
        if not res.ok:
            self.notifier.send(f"❌ Ordre {side_txt} {symbol} REFUSÉ : {res.message}")
            return
        self.journal.record_signal(now, symbol, sig.side, lot, res.price or entry, sl, tp, True,
                                   res.ticket, sig.reason, params)
        self.notifier.send(f"{side_txt} {symbol} — POSITION OUVERTE (#{res.ticket})\n{details}")

    def report_closed_trades(self) -> None:
        deals = self.broker.closed_deals(self.state.last_deal_ticket or 0)
        if not deals:
            return
        now = self.clock().isoformat(timespec="seconds")
        currency = self.broker.account().currency
        for d in deals:
            self.journal.record_close(now, d)
            icon = "✅" if d.profit >= 0 else "❌"
            self.notifier.send(
                f"{icon} Position fermée {d.symbol} ({d.reason})\n"
                f"Résultat : {d.profit:+.2f} {currency} | lot {d.volume} @ {d.price}"
            )
            self.state.last_deal_ticket = d.ticket
        self.store.save()

    # ================================================================ protections
    def roll_day(self, send_summary: bool = True) -> None:
        today = self.clock().date().isoformat()
        if self.state.day == today:
            return
        if send_summary and self.state.day:
            n, wins, total = self.journal.day_summary(self.state.day)
            self.notifier.send(f"📊 Bilan du {self.state.day} : {n} trade(s), {wins} gagnant(s), "
                               f"résultat {total:+.2f}")
        self.state.day = today
        self.state.day_start_equity = self.broker.account().equity
        if self.state.paused and self.state.pause_reason == "perte journalière max atteinte":
            self.state.paused, self.state.pause_reason = False, ""
            self.notifier.send("▶️ Nouveau jour : le trading reprend.")
        self.store.save()

    def check_daily_loss(self) -> None:
        if self.state.paused:
            return
        equity = self.broker.account().equity
        limit = self.cfg["risk"]["max_daily_loss_pct"]
        if daily_loss_exceeded(self.state.day_start_equity, equity, limit):
            self.pause("perte journalière max atteinte")
            self.notifier.send(
                f"🛑 Perte journalière de {limit} % atteinte. Plus aucune nouvelle position "
                "jusqu'à demain. Les positions ouvertes gardent leurs SL/TP."
            )

    def pause(self, reason: str) -> None:
        self.state.paused, self.state.pause_reason = True, reason
        self.store.save()

    # ================================================================ commandes Telegram
    def handle_commands(self) -> None:
        for text in self.notifier.poll_commands():
            parts = text.split()
            cmd = parts[0].lower().split("@")[0]
            args = [a.lower() for a in parts[1:]]
            if cmd == "/status":
                self.notifier.send(self.status_text())
            elif cmd == "/pause":
                self.pause("pause manuelle")
                self.notifier.send("⏸ Robot en pause (aucune nouvelle entrée).")
            elif cmd == "/resume":
                self.state.paused, self.state.pause_reason = False, ""
                self.store.save()
                self.notifier.send("▶️ Robot relancé.")
            elif cmd == "/optimize":
                self.notifier.send("🧠 Auto-amélioration lancée, ça peut prendre quelques minutes…")
                self.run_optimization()
            elif cmd == "/closeall":
                if args[:1] != ["oui"]:
                    self.notifier.send("Confirme avec : /closeall oui")
                else:
                    self.close_all()
            else:
                self.notifier.send(HELP)

    def close_all(self) -> None:
        positions = self.broker.positions()
        if not positions:
            self.notifier.send("Aucune position ouverte.")
            return
        for p in positions:
            res = self.broker.close_position(p)
            status = "fermée" if res.ok else f"ÉCHEC ({res.message})"
            self.notifier.send(f"{p.symbol} #{p.ticket} : {status}")

    def status_text(self) -> str:
        acct = self.broker.account()
        positions = self.broker.positions()
        lines = [
            f"Mode : {self.cfg['mode']} | {'⏸ PAUSE (' + self.state.pause_reason + ')' if self.state.paused else '▶️ actif'}",
            f"Équité : {acct.equity:.2f} {acct.currency} (solde {acct.balance:.2f})",
            f"Positions ouvertes : {len(positions)}",
        ]
        lines += [f"  {SIDE_FR[p.side]} {p.symbol} {p.volume} lot @ {p.price_open} → {p.profit:+.2f}"
                  for p in positions]
        lines.append(f"Dernière auto-amélioration : {self.state.last_optimization}")
        for s in self.t["symbols"]:
            lines.append(f"{s} : {self.params_for(s)}")
        return "\n".join(lines)

    # ================================================================ auto-amélioration
    def maybe_optimize(self) -> None:
        o = self.cfg["optimizer"]
        if not o["enabled"] or not self.state.last_optimization:
            return
        last = datetime.fromisoformat(self.state.last_optimization)
        if self.clock() - last >= timedelta(days=o["every_days"]):
            self.run_optimization()

    def run_optimization(self) -> list:
        o = self.cfg["optimizer"]
        settings = OptimizerSettings(
            trials=o["trials"], local_steps=o["local_steps"], in_sample_ratio=o["in_sample_ratio"],
            min_trades_oos=o["min_trades_oos"], min_profit_factor_oos=o["min_profit_factor_oos"],
            improvement_margin=o["improvement_margin"],
        )
        rng = np.random.default_rng()
        reports, texts = [], []
        for symbol in self.t["symbols"]:
            try:
                df = self.broker.rates(symbol, self.t["timeframe"], o["history_bars"])
                spec = self.broker.spec(symbol)
                spread = float(np.median(df["spread"])) * spec.point if "spread" in df else spec.spread_points * spec.point
                current = self.params_for(symbol)
                rep = optimize(symbol, df, self.strategy, current, settings, spread, rng)
            except Exception as e:
                log.exception("Optimisation %s", symbol)
                texts.append(f"{symbol} : optimisation impossible ({e!r})")
                continue
            if rep.adopted:
                self.state.params[symbol] = rep.best_params
                self.store.log_params_change(symbol, current, rep.best_params, rep.reason)
            reports.append(rep)
            texts.append(rep.summary())
        self.state.last_optimization = self.clock().isoformat()
        self.store.save()
        self.notifier.send("🧠 AUTO-AMÉLIORATION\n\n" + "\n\n".join(texts))
        return reports

    def backtest_report(self) -> str:
        lines = []
        for symbol in self.t["symbols"]:
            df = self.broker.rates(symbol, self.t["timeframe"], self.cfg["optimizer"]["history_bars"])
            spec = self.broker.spec(symbol)
            spread = float(np.median(df["spread"])) * spec.point if "spread" in df else 0.0
            res = run_backtest(df, self.strategy, self.params_for(symbol), spread)
            lines.append(f"{symbol} ({len(df)} bougies {self.t['timeframe']}, {df.index[0]:%Y-%m-%d} → "
                         f"{df.index[-1]:%Y-%m-%d}) :\n  {res.summary()}")
        return "\n".join(lines)


def build_bot(cfg: dict, broker, notifier, strategy: Strategy) -> Bot:
    data = Path(cfg["data_dir"])
    return Bot(cfg, broker, notifier, Journal(data / "journal.db"), StateStore(data), strategy)
