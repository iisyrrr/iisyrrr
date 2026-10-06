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
python run_bot.py --news            # teste la veille news (agenda, infos filtrées, briefing)
```

### 6. Lancement
```powershell
python run_bot.py
```

## Démarrage prudent (fortement conseillé)

1. **Compte DÉMO + `mode: alert_only`** : le robot envoie les signaux sans trader. Compare avec ton analyse pendant quelques jours.
2. **Compte DÉMO + `mode: live`** : il trade réellement sur la démo. Laisse-le tourner 2 à 4 semaines.
3. **Compte RÉEL + `mode: live`** avec un petit `risk_per_trade_pct` (0.25 %), puis augmente progressivement.

## Veille news : calendrier, médias, réseaux sociaux

Le robot surveille en permanence l'actualité qui peut faire bouger tes actifs,
la **filtre** pour ne garder que l'information fiable, et s'en sert pour te
prévenir et pour se protéger.

### Ce qu'il collecte (34 sources gratuites, vérifiées en conditions réelles)

| Type | Sources par défaut | Fréquence |
|---|---|---|
| Calendrier économique | ForexFactory : NFP, CPI, banques centrales…, avec prévision et précédent | 1 h (limite du site) |
| Officiel (tier 1) | Fed (politique monétaire, discours), BLS (emploi, inflation), BCE, Eurostat, Destatis, Bank of England, Bank of Japan, ministère japonais des Finances (interventions), BNS, Bank of Canada, RBA | 5 min |
| Grands médias (tier 1) | Reuters, Bloomberg, WSJ, CNBC, Financial Times, AP (via son compte Bluesky vérifié) | 3 à 10 min |
| Médias forex (tier 2) | FinancialJuice (le plus rapide : quelques secondes après une annonce), FXStreet, investingLive (ex-ForexLive), Investing.com | 1 à 3 min |
| Déclarations politiques | Posts de Trump (archive de Truth Social), filtrés sur droits de douane, Fed, Chine, pétrole, dollar… | 3 min |
| Réseaux sociaux (tier 3) | StockTwits ; en option X (payant) et Bluesky | 15 min |

Une source ne sert que si elle concerne un actif que tu trades. Par exemple, la Bank of Japan n'est prise en compte que si tu trades du JPY.

### Comment il filtre (pour n'avoir que de l'info de qualité)

1. **Fraîcheur** : rien de plus vieux que 24 h. Le score d'une info baisse de moitié toutes les 3 h. Les dates absurdes (1899) ou futures (agendas déguisés en news) sont rejetées.
2. **Anti-spam** : les posts sociaux promotionnels sont jetés, ainsi que les comptes à moins de 1 000 abonnés ou sans engagement. Exemples de posts promotionnels : « free signals », « TP1 / SL », « VIP », listes de tickers…
3. **Bavardage** : un post social sans vrai mot de marché (taux, inflation, intervention…) est jeté, sauf s'il fait énormément réagir.
4. **Pertinence** : seules les infos qui touchent tes actifs sont gardées. Pour EURUSD, c'est l'EUR et l'USD (BCE, Lagarde, Fed, Warsh, NFP…).
5. **Dédoublonnage** : la même info reprise par plusieurs sites compte pour une seule info. Deux titres avec des chiffres différents (« -10,6 % » et « -5 % ») ne sont jamais fusionnés.
6. **Fiabilité combinée** : chaque source a un poids :

   | Source | Poids |
   |---|---|
   | Officielle | 1,0 |
   | Grande agence | 0,9 |
   | Média spécialisé | 0,75 |
   | Réseau social | 0,15 |

   Quand plusieurs groupes **indépendants** rapportent la même info, leurs poids se combinent. Par exemple, FinancialJuice seul vaut 0,75, et FinancialJuice + FXStreet 0,94. Reuters sur Bluesky et Reuters sur son site comptent pour un seul groupe : il ne se « confirme » pas lui-même.
7. **Nature de l'info** : les analyses, prévisions et récapitulatifs (« Price Forecast », « What are the main events today »…) sont fortement déclassés. Ils ne déclenchent jamais d'alerte. Les formulations de rumeur (« selon des sources », « reportedly ») sont pénalisées tant qu'une source de premier plan ne confirme pas.
8. **Rumeurs** : une info venue seulement des réseaux sociaux reste marquée **RUMEUR**. Elle n'a aucun effet sur les alertes ni sur le trading, même si l'IA la juge crédible : l'IA peut déclasser une info, jamais la promouvoir.
9. **Analyse IA (Claude)**, si une clé API est configurée. Chaque info est jugée sur :
   - sa pertinence et son impact (fort, moyen, faible, aucun) ;
   - sa crédibilité, et s'il s'agit d'un fait ou d'une rumeur ;
   - son effet probable sur chaque devise (ex : USD⬇ EUR⬆) ;
   - un résumé d'une ligne en français.

   Elle reconnaît aussi une même info écrite différemment par deux médias. Chaque info n'est analysée qu'une fois.

### Ce que tu reçois sur Telegram

- **🚨 Alertes** : seulement les infos à fort impact et crédibles, une seule fois chacune, avec un maximum de 6 par heure. Sans IA, une info doit avoir une fiabilité combinée d'au moins 0,85 : une source officielle, une grande agence, ou deux médias indépendants.
- **⏰ Rappels** : 15 minutes avant chaque annonce à fort impact sur tes devises.
- **☀️ Briefing du matin** (7 h 30, heure de Paris) : le thème du jour, les points clés, un biais par actif avec sa raison, les risques, et l'agenda de la journée.
- **Commandes** : `/news` (infos fiables, puis réseaux sociaux « non confirmé » à part), `/calendar`, `/brief`.

### Comment le robot s'en sert pour trader

- **Fenêtre de sécurité** : aucune nouvelle position de 30 min avant à 30 min après une annonce à fort impact sur une devise du symbole. Pour les annonces majeures (NFP, CPI, banques centrales), c'est de 45 min avant à 60 min après. Les conférences de presse (FOMC, BCE) sont couvertes jusqu'à 30 min après leur fin.
- **Info urgente imprévue** (intervention, décision d'urgence, choc géopolitique) : si l'IA la juge à fort impact et qu'une source de premier plan la rapporte, aucune entrée sur les actifs concernés pendant 15 min.
- **Spread anormal** : aucune entrée si le spread dépasse 2,5 fois sa valeur habituelle. Ce garde-fou attrape les news surprises, même sans aucune source.
- **Sécurité en cas de panne** : si le calendrier économique est indisponible, périmé (plus de 26 h) ou ne couvre pas la semaine en cours, le robot **n'ouvre aucune position** et te prévient (une fois par heure). Il ne fait jamais comme s'il n'y avait « aucune annonce ». Au redémarrage, le calendrier est chargé avant la première décision de trading. C'est désactivable avec `news.blackout.fail_closed: false`.
- **Robustesse** : une source, l'IA ou Telegram en panne n'arrête jamais le reste. Une alerte non livrée est renvoyée, et un même événement n'est jamais alerté deux fois, même s'il arrive par plusieurs médias. Les rappels ne sont pas répétés après un redémarrage. `/brief` est préparé en tâche de fond, sans jamais bloquer le trading.
- **Secrets protégés** : le jeton Telegram et les clés API sont masqués (`***`) dans les journaux et les messages d'erreur.
- **Symboles non reconnus** : si un symbole n'est associé à aucune devise (ex : un indice exotique), le robot le signale au démarrage et dans chaque alerte. Indique alors ses actifs dans `news.symbol_assets`. `GOLD`, `SILVER`, `US2000`… sont reconnus automatiquement.
- **Sentiment des news** : chaque alerte de trade affiche le sentiment (de -1 à +1), la prochaine annonce et les 2 infos clés. Si le trade va contre les news, c'est signalé (`warn`) ou bloqué (`block`).
- **Il apprend** : le sentiment est enregistré avec chaque trade. Le rapport hebdomadaire compare les résultats des trades pris dans le sens des news et contre elles. En mode `sentiment_filter: auto`, le robot bloque seul les trades contre les news dès que ses propres résultats montrent qu'ils perdent (au moins 30 trades sur les 90 derniers jours). Comme il ne regarde que les 90 derniers jours, il peut changer d'avis quand le marché change.

### Activer l'IA

1. Crée une clé sur [console.anthropic.com](https://console.anthropic.com).
2. Mets-la dans `config.yaml` (`news.ai.api_key`) ou dans la variable d'environnement `ANTHROPIC_API_KEY`.
3. Teste : `python run_bot.py --news`. Ça affiche les sources, l'agenda, les infos filtrées et le briefing, et ça l'envoie sur Telegram.

Le modèle utilisé est `claude-opus-5-5`, avec un effort réduit pour le classement des news. L'API est payante à l'usage : surveille ta consommation sur la console Anthropic les premiers jours.

### Sources optionnelles

- **X (Twitter)** : la meilleure source temps réel, mais payante à la lecture. Compte environ 45 à 75 $/mois pour 10 à 15 comptes bien choisis. Exemples dans `config.example.yaml`.
- **Bluesky** : autres comptes vérifiés (Reuters, Fed, BCE…), gratuits.
- **Finnhub** : clé gratuite, flux de news supplémentaire.
- **Reddit** : déconseillé. La création d'applis est fermée et l'accès public sera coupé en mars 2027.

Teste toutes les sources depuis ton VPS avec `python run_bot.py --news` : certains sites traitent différemment les adresses IP de serveurs.

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
| `/news` | Infos fiables du moment (réseaux sociaux à part, « non confirmé ») |
| `/calendar` | Agenda économique à venir pour tes devises |
| `/brief` | Le briefing complet tout de suite |

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
  news/             veille : sources, filtre qualité, calendrier, analyse IA, alertes
  state.py          état persistant (survit aux redémarrages)
tests/              tests automatiques (`pytest`)
```

## Avertissement

Aucun robot ne garantit des gains. Les performances passées (backtest compris)
ne préjugent pas des performances futures. Teste toujours sur un compte démo
avant de passer en réel, et ne risque que de l'argent que tu peux perdre.
