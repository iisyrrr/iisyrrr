# Robot de trading MT5 autonome

Robot Python qui tourne 24h/24 sur un VPS Windows, trade via **MetaTrader 5**,
t'envoie une **alerte Telegram** à chaque prise ou clôture de position, et
**ré-optimise lui-même ses réglages** chaque semaine, avec des garde-fous.

## Ce que fait le robot

| Fonction | Détail |
|---|---|
| Trading autonome | Analyse chaque bougie clôturée, ouvre les positions avec SL/TP posés directement chez le broker |
| Alertes Telegram | Ouverture (lot, entrée, SL, TP, risque en €, raison), clôture (gain/perte, SL ou TP), bilan quotidien, erreurs |
| Contrôle à distance | `/status`, `/pause`, `/resume`, `/optimize`, `/closeall oui` depuis Telegram (seul ton compte est accepté) |
| Gestion du risque | Lot calculé pour risquer X % de l'équité, positions max, filtre de spread, **arrêt automatique si perte journalière max** |
| Risque adaptatif | Si les 20 derniers trades réels sont mauvais, le risque est divisé par 2 automatiquement, puis rétabli quand ça repart |
| Auto-amélioration | Ré-optimisation walk-forward chaque semaine. Les nouveaux réglages ne sont adoptés **que** s'ils battent les actuels sur des données jamais vues |
| Journal | Tous les signaux et trades dans `data/journal.db` (SQLite), historique des changements de réglages dans `data/params_history.jsonl` |

## Comment il « s'améliore tout seul »

Un robot qui se modifie sans contrôle finit presque toujours par **sur-apprendre** :
il trouve des réglages parfaits sur le passé, puis il perd en réel. Ici :

1. Chaque semaine, il récupère ~20 000 bougies récentes.
2. Il cherche les meilleurs paramètres sur les **70 % les plus anciens**.
3. Il fait passer un examen aux meilleurs réglages trouvés sur les **30 % les plus récents**, qu'il n'a jamais vus.
4. Il n'adopte les nouveaux réglages que si, sur cet examen :
   - ils ont au moins 30 trades,
   - ils ont un profit factor ≥ 1.15,
   - ils font au moins 10 % mieux que les réglages actuels (score SQN).
5. Sinon il garde ses réglages. Dans tous les cas, il t'envoie un rapport sur Telegram.

Les paramètres ne sortent jamais des bornes définies dans la stratégie, donc
le robot ne peut pas « inventer » un comportement que tu n'as pas prévu.

## Installation sur le VPS Windows

### 1. MetaTrader 5
1. Installe MT5 (celui de ton broker) sur le VPS et connecte-toi à ton compte.
2. Active le bouton **Algo Trading** dans la barre d'outils.
3. *Outils → Options → Graphiques → Max bars in chart* : mets **Unlimited** (nécessaire pour l'auto-amélioration).
4. Ouvre la fenêtre « Market Watch » et vérifie le **nom exact** de tes symboles (ex : `EURUSD`, `EURUSD.r`, `XAUUSDm`…).

### 2. Python
1. Installe [Python 3.11+](https://www.python.org/downloads/windows/) en cochant **« Add Python to PATH »**.
2. Dans un terminal (PowerShell), dans le dossier du robot :
   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

### 3. Bot Telegram (2 minutes)
1. Sur Telegram, écris à **@BotFather**, envoie `/newbot` et suis les étapes. Il te donne un **token**.
2. Écris n'importe quel message à ton nouveau bot (sinon il ne peut pas t'écrire).
3. Écris à **@userinfobot** : il te donne ton **chat_id** (un nombre).

### 4. Configuration
```powershell
copy config.example.yaml config.yaml
notepad config.yaml
```
Remplis `mt5` (login, mot de passe, serveur), `telegram` (token, chat_id) et
`trading.symbols`. `config.yaml` n'est jamais envoyé sur GitHub.

### 5. Vérifications
```powershell
python run_bot.py --test-telegram   # tu dois recevoir un message
python run_bot.py --backtest        # performance des réglages actuels sur l'historique
python run_bot.py --optimize        # lance une première auto-amélioration
```

### 6. Lancement
```powershell
python run_bot.py
```

## Démarrage prudent (fortement conseillé)

1. **Compte DÉMO + `mode: alert_only`** : le robot envoie les signaux sans trader. Compare avec ton analyse pendant quelques jours.
2. **Compte DÉMO + `mode: live`** : il trade réellement sur la démo. Laisse-le tourner 2 à 4 semaines.
3. **Compte RÉEL + `mode: live`** avec un petit `risk_per_trade_pct` (0.25 %), puis augmente progressivement.

## Faire tourner le robot 24h/24 (redémarrage automatique)

Le terminal MT5 **et** le robot doivent se relancer seuls après un redémarrage du VPS.

1. **Connexion automatique de Windows** : `Win + R` → `netplwiz` → décoche « Les utilisateurs doivent entrer un nom d'utilisateur… ».
2. **MT5 au démarrage** : `Win + R` → `shell:startup` → mets-y un raccourci vers `terminal64.exe`.
3. **Le robot au démarrage** : dans *Planificateur de tâches* → *Créer une tâche* :
   - Déclencheur : « À l'ouverture de session », avec un délai de 1 minute (le temps que MT5 démarre)
   - Action : programme `C:\chemin\robot\.venv\Scripts\python.exe`, arguments `run_bot.py`, « Commencer dans » `C:\chemin\robot`
   - Paramètres : « Si la tâche échoue, redémarrer toutes les 1 minute »

Si le robot s'arrête, les positions ouvertes restent protégées par leur SL/TP chez le broker.

## Commandes Telegram

| Commande | Effet |
|---|---|
| `/status` | Mode, équité, positions ouvertes, réglages actuels |
| `/pause` | Plus aucune nouvelle entrée (les positions restent ouvertes) |
| `/resume` | Reprend le trading |
| `/optimize` | Lance l'auto-amélioration maintenant |
| `/closeall oui` | Ferme toutes les positions du robot |

Les commandes envoyées pendant que le robot est éteint sont ignorées au redémarrage (sécurité).

## Brancher ta stratégie

Ta stratégie va dans `bot/strategy.py` : une classe avec
- `default_params` : tes réglages de départ,
- `param_space` : les bornes dans lesquelles l'auto-amélioration a le droit de chercher,
- `compute(df, params)` : renvoie pour chaque bougie `signal` (1 / -1 / 0), `sl_dist` et `tp_dist`.

La même fonction sert au trading réel, au backtest et à l'optimisation : ce
qui est testé est exactement ce qui est tradé. La stratégie actuelle
(`ema_cross_rsi_atr`) n'est qu'un exemple à remplacer.

## Structure

```
run_bot.py          point d'entrée
config.example.yaml modèle de configuration
bot/
  engine.py         boucle principale, alertes, commandes, protections
  broker.py         connexion MetaTrader 5
  strategy.py       stratégies (ta stratégie ici)
  indicators.py     EMA, RSI, ATR…
  backtest.py       backtest prudent (spread, gaps, SL prioritaire)
  optimizer.py      auto-amélioration walk-forward
  risk.py           taille de lot, perte journalière, risque adaptatif
  notifier.py       Telegram
  journal.py        journal SQLite
  state.py          état persistant (survit aux redémarrages)
tests/              tests automatiques (`pytest`)
```

## Avertissement

Aucun robot ne garantit des gains. Les performances passées (backtest compris)
ne préjugent pas des performances futures. Teste toujours sur un compte démo
avant de passer en réel, et ne risque que de l'argent que tu peux perdre.
