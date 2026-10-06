"""Boucle principale du robot."""
from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .backtest import run_backtest
from .journal import Journal
from .news.calendar import format_event
from .optimizer import OptimizerSettings, optimize
from .risk import adaptive_multiplier, compute_lot, daily_loss_exceeded
from .secrets import mask
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
    "/news – les infos importantes du moment\n"
    "/calendar – l'agenda économique à venir\n"
    "/brief – le briefing complet maintenant\n"
    "/help – cette aide"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Bot:
    def __init__(self, cfg: dict, broker, notifier, journal: Journal, store: StateStore,
                 strategy: Strategy, clock=utcnow, news=None):
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
        self.news = news  # service de veille (optionnel)
        # True si la veille a été demandée (avec fail_closed) mais n'a pas pu démarrer :
        # dans ce cas on n'ouvre aucune position sans protection (voir build_bot)
        self.news_required = False
        self._last_sentiment_mode: str | None = None
        self._throttle: dict[str, float] = {}
        self._spreads: dict[str, deque] = {}
        self._news_builder = None  # pour retenter le démarrage de la veille si elle a échoué
        self._news_retry_at = 0.0

    def maybe_retry_news(self) -> None:
        """Veille en panne au démarrage : on retente toutes les heures plutôt que de bloquer
        les entrées jusqu'au prochain redémarrage manuel."""
        if self.news is not None or self._news_builder is None or time.time() < self._news_retry_at:
            return
        self._news_retry_at = time.time() + 3600
        try:
            news = self._news_builder()
            if news is None:
                return
            news.start()
        except Exception:
            log.exception("Nouvel échec du démarrage de la veille news")
            return
        self.news = news
        self.news_required = False
        self.notifier.send("✅ Veille news redémarrée : les entrées sont de nouveau autorisées.")

    def _notify_throttled(self, key: str, text: str, every_seconds: float = 3600) -> None:
        """Envoie au plus un message de ce type par heure (évite d'inonder Telegram)."""
        now = time.time()
        if now - self._throttle.get(key, 0) >= every_seconds:
            self._throttle[key] = now
            self.notifier.send(text)

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
            f"{self.news.status_line() if self.news else 'Veille news : désactivée'}\n"
            f"{'⏸ EN PAUSE : ' + self.state.pause_reason if self.state.paused else ''}\n{HELP}"
        )

    def run_forever(self) -> None:
        if self.news:
            self.news.start()  # charge le calendrier AVANT le message de démarrage et le premier trade
        self.start()
        try:
            while True:
                try:
                    self.tick()
                except Exception as e:  # le robot ne doit jamais mourir sur une erreur ponctuelle
                    log.exception("Erreur dans la boucle")
                    if time.time() - self._last_error_alert > 900:
                        self._last_error_alert = time.time()
                        self.notifier.send(f"⚠️ Erreur : {mask(repr(e))}\nLe robot continue de tourner.")
                time.sleep(self.t["poll_seconds"])
        except KeyboardInterrupt:
            log.info("Arrêt demandé")
        finally:
            if self.news:
                self.news.stop()
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
                self._notify_throttled(f"symbol_error:{symbol}:{e.__class__.__name__}",
                                       f"⚠️ {symbol} : {mask(repr(e))}")
        self.maybe_retry_news()
        self.maybe_optimize()

    # ================================================================ trading
    def record_spread(self, symbol: str, points: float) -> None:
        self._spreads.setdefault(symbol, deque(maxlen=720)).append(points)  # ~2 h à 1 mesure / 10 s

    def typical_spread(self, symbol: str) -> float | None:
        samples = self._spreads.get(symbol)
        if not samples or len(samples) < 30:
            return None  # pas encore assez de mesures : garde-fou inactif
        return float(np.median(samples))

    def process_symbol(self, symbol: str) -> None:
        tf = self.t["timeframe"]
        if self.t.get("spread_spike_factor"):
            try:
                self.record_spread(symbol, self.broker.spec(symbol).spread_points)
            except Exception:
                log.debug("Spread de %s indisponible", symbol)
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
        factor = self.t.get("spread_spike_factor", 0)
        typical = self.typical_spread(symbol) if factor else None
        if typical is not None:
            # spread réel mesuré en continu (pas le minimum des bougies) + écart minimal de 3 points
            if spec.spread_points > factor * typical and spec.spread_points - typical >= 3:
                self.notifier.send(
                    f"ℹ️ {side_txt} {symbol} ignoré : spread anormal ({spec.spread_points} pts contre "
                    f"{typical:.0f} habituellement), signe d'une news ou d'un marché illiquide."
                )
                return
        min_dist = spec.stops_level * spec.point
        if sig.sl_dist < min_dist or sig.tp_dist < min_dist:
            self.notifier.send(f"ℹ️ {side_txt} {symbol} ignoré : SL/TP trop proches pour le broker.")
            return

        news_lines: list[str] = []
        news_sentiment = None
        if self.news is None and self.news_required:
            self._notify_throttled("news_down", f"🛑 {side_txt} {symbol} ignoré : la veille news est en panne "
                                                "(protection autour des annonces impossible).")
            return
        if self.news is not None:
            ctx = self.news.context(symbol)
            if not ctx.calendar_ok and self.cfg["news"]["blackout"]["fail_closed"]:
                self._notify_throttled(
                    "calendar_down",
                    f"🛑 {side_txt} {symbol} ignoré : calendrier économique indisponible ou périmé, la protection "
                    "autour des annonces n'est pas garantie. (Désactivable : news.blackout.fail_closed)")
                return
            if not ctx.recognized:
                news_lines.append("⚠️ Symbole non reconnu par la veille : aucune protection news "
                                  "(renseigne news.symbol_assets)")
            if ctx.blackout_event:
                ev = ctx.blackout_event
                self.notifier.send(
                    f"🗓 {side_txt} {symbol} ignoré : annonce « {ev.title} » ({ev.currency}) à "
                    f"{ev.time.astimezone(self.news.tz):%H:%M}, fenêtre de sécurité news."
                )
                return
            news_sentiment = ctx.sentiment
            mode = self.sentiment_mode()
            if ctx.sentiment is not None and mode != "off":
                news_lines.append(f"Sentiment news : {ctx.sentiment:+.2f} ({ctx.sentiment_items} info(s) analysée(s))")
                if sig.side * ctx.sentiment <= -self.cfg["news"]["sentiment_threshold"]:
                    if mode == "block":
                        self.notifier.send(
                            f"🧭 {side_txt} {symbol} ignoré : contraire au sentiment des news ({ctx.sentiment:+.2f})."
                        )
                        return
                    news_lines.append("⚠️ Ce trade va CONTRE le sentiment des news")
            if ctx.next_event:
                news_lines.append(f"Prochaine annonce forte : {format_event(ctx.next_event, self.news.tz)}")
            for item in ctx.top_items[:2]:
                text = item.analysis.get("summary_fr") if item.analysis else item.title
                news_lines.append(f"• {text} ({item.source})")

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
        if news_lines:
            details += "\n\n" + "\n".join(news_lines)
        now = self.clock().isoformat(timespec="seconds")

        if self.cfg["mode"] != "live":
            self.journal.record_signal(now, symbol, sig.side, lot, entry, sl, tp, False, 0, sig.reason, params,
                                       news_sentiment)
            self.notifier.send(f"📣 SIGNAL {side_txt} {symbol} (non exécuté – mode alertes)\n{details}")
            return

        res = self.broker.market_order(symbol, sig.side, lot, sl, tp, f"robot {self.strategy.name}")
        if not res.ok:
            self.notifier.send(f"❌ Ordre {side_txt} {symbol} REFUSÉ : {res.message}")
            return
        self.journal.record_signal(now, symbol, sig.side, lot, res.price or entry, sl, tp, True,
                                   res.ticket, sig.reason, params, news_sentiment)
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
            try:
                self.handle_command(text)
            except Exception as e:  # une commande en erreur ne doit pas faire perdre les suivantes
                log.exception("Commande %s en erreur", text)
                self.notifier.send(f"⚠️ Commande {text.split()[0] if text.split() else text} en erreur : {mask(repr(e))}")

    def handle_command(self, text: str) -> None:
        parts = text.split()
        if not parts:
            return
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
        elif cmd in ("/news", "/calendar", "/brief"):
            if self.news is None:
                self.notifier.send("🛑 Veille news en panne : entrées bloquées (nouvel essai chaque heure)."
                                   if self.news_required else
                                   "La veille news est désactivée (news.enabled dans config.yaml).")
            elif cmd == "/news":
                self.notifier.send(self.news.news_text())
            elif cmd == "/calendar":
                self.notifier.send(self.news.calendar_text())
            else:
                self.news.request_brief()  # l'IA peut être lente : jamais dans la boucle de trading
                self.notifier.send("☀️ Briefing en préparation, il arrive dans un instant…")
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
        if self.news_required and self.news is None:
            lines.append("🛑 Veille news en panne : aucune nouvelle position (nouvel essai chaque heure)")
        if self.news:
            lines.append(self.news.status_line())
            lines.append(f"Filtre sentiment : {self.sentiment_mode()}")
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
        if self.news:
            texts.append(self.news_alignment_text())
        self.notifier.send("🧠 AUTO-AMÉLIORATION\n\n" + "\n\n".join(texts))
        return reports

    # ================================================================ news : apprentissage
    def sentiment_mode(self) -> str:
        """off | warn | block. En mode « auto », le robot décide lui-même à partir de ses
        propres trades : s'il perd de l'argent quand il trade contre le sentiment des news
        (et nettement plus que dans le sens des news), il se met à bloquer ces trades."""
        n = self.cfg["news"]
        mode = n["sentiment_filter"]
        if mode != "auto":
            return mode
        since = (self.clock() - timedelta(days=n["auto_lookback_days"])).isoformat(timespec="seconds")
        stats = self.journal.alignment_stats(n["sentiment_threshold"], since)
        against_n, against_pf = stats["against"]
        aligned_n, aligned_pf = stats["aligned"]
        min_n = n["auto_min_trades"]
        decided = "warn"
        if against_n >= min_n and against_pf < 1.0:
            # on compare au sens des news seulement si cet échantillon est suffisant
            if aligned_n < min_n or against_pf < aligned_pf - 0.2:
                decided = "block"
        if self._last_sentiment_mode is not None and decided != self._last_sentiment_mode:
            aligned_txt = f"{aligned_pf:.2f} sur {aligned_n} trades" if aligned_n else "pas encore de trades"
            self.notifier.send(
                "🧭 Le robot BLOQUE désormais les trades contraires au sentiment des news "
                f"(PF contre : {against_pf:.2f} sur {against_n} trades, PF dans le sens : {aligned_txt}). "
                f"Il réexaminera la question sur les {n['auto_lookback_days']} derniers jours."
                if decided == "block" else "🧭 Les trades contraires au sentiment des news sont de nouveau autorisés."
            )
        self._last_sentiment_mode = decided
        return decided

    def news_alignment_text(self) -> str:
        n = self.cfg["news"]
        days = n["auto_lookback_days"]
        since = (self.clock() - timedelta(days=days)).isoformat(timespec="seconds")
        stats = self.journal.alignment_stats(n["sentiment_threshold"], since)

        def fmt(key, label):
            count, pf = stats[key]
            return f"{label} : {count} trade(s)" + (f", PF {pf:.2f}" if count else "")

        return (f"📰 Trades vs sentiment des news ({days} derniers jours)\n"
                + fmt("aligned", "Dans le sens des news") + "\n"
                + fmt("against", "Contre les news") + "\n" + fmt("neutral", "Sentiment neutre/inconnu")
                + f"\nFiltre actuel : {self.sentiment_mode()}")

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


def build_bot(cfg: dict, broker, notifier, strategy: Strategy, with_news: bool = True) -> Bot:
    from .news.factory import build_news_service

    data = Path(cfg["data_dir"])
    n = cfg["news"]
    # la panne de la veille ne bloque les entrées que si la protection calendrier est réellement demandée
    protection = bool(n["enabled"] and n["blackout"]["enabled"] and n["blackout"]["fail_closed"]
                      and n["calendar"]["enabled"])
    news, failed = None, False
    if with_news:
        try:
            news = build_news_service(cfg, notifier)
        except Exception as e:  # la veille ne doit jamais empêcher le robot de démarrer
            log.exception("Veille news en panne")
            failed = True
            notifier.send(f"⚠️ Veille news en panne : {mask(repr(e))}\n"
                          + ("Aucune nouvelle position tant qu'elle ne fonctionne pas (nouvel essai chaque heure)."
                             if protection else "Le robot trade sans protection news."))
    bot = Bot(cfg, broker, notifier, Journal(data / "journal.db"), StateStore(data), strategy, news=news)
    if failed:
        bot.news_required = protection
        bot._news_builder = lambda: build_news_service(cfg, notifier)
        bot._news_retry_at = time.time() + 3600
    return bot
