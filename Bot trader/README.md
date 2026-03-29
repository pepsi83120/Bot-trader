# 📦 INSTALLATION — Bot MT5 Telegram

## Structure des fichiers

```
ton-dossier/
├── bot_mt5.py              ← Bot Python (corrigé)
├── TelegramBridge.mq5      ← EA MetaTrader 5 (corrigé)
├── .env                    ← Tes tokens secrets (à créer)
├── .env.example            ← Modèle de configuration
└── requirements.txt        ← Dépendances Python
```

---

## ÉTAPE 1 — Installer les dépendances Python

```bash
pip install python-telegram-bot yfinance pandas python-dotenv
```

---

## ÉTAPE 2 — Créer le fichier .env

Copie `.env.example` en `.env` et remplis tes vraies valeurs :

```
TELEGRAM_TOKEN=ton_token_ici
ADMIN_CHAT_ID=ton_chat_id_ici
```

**Comment obtenir ton Chat ID ?**
Envoie un message à @userinfobot sur Telegram → il te donne ton ID.

---

## ÉTAPE 3 — Installer l'EA dans MetaTrader 5

1. Ouvre MetaTrader 5
2. Menu **Fichier → Ouvrir le dossier des données**
3. Va dans `MQL5 → Experts`
4. Copie `TelegramBridge.mq5` dans ce dossier
5. Dans MT5 : **Outils → Compilateur MetaEditor** (F4) → Compile l'EA
6. Retourne sur un graphique (ex: XAUUSD M1)
7. Glisse l'EA `TelegramBridge` sur ce graphique
8. Coche **"Autoriser le trading automatique"**
9. Coche **"Autoriser les modifications de fichiers extérieurs"**
10. Valide

✅ L'EA doit afficher un smiley vert dans le coin du graphique.

---

## ÉTAPE 4 — Lancer le bot Python

```bash
python bot_mt5.py
```

Tu devrais recevoir un message Telegram de confirmation.

---

## ÉTAPE 5 — Tester

Dans Telegram, envoie :
- `/solde` → doit afficher ton solde MT5
- `/positions` → doit afficher tes positions ouvertes
- `/buy XAUUSD 0.01 1900 1960` → doit placer un ordre

---

## ❗ Problèmes fréquents

| Erreur | Cause | Solution |
|--------|-------|----------|
| Timeout EA n'a pas répondu | L'EA ne tourne pas | Vérifie le smiley vert sur le graphique |
| Commande inconnue | Mauvais EA (ancienne version) | Recompile et recharge le nouvel EA |
| Symbole non disponible | Symbole pas dans la liste MT5 | Ajoute-le via "Observation du marché" |
| ORDER_FILLING_IOC rejeté | Broker n'accepte pas IOC | Change en `ORDER_FILLING_FOK` dans le MQ5 |

---

## ✅ Corrections apportées vs version originale

### Bot Python (bot_mt5.py)
- ✅ Token/ID déplacés dans `.env` (sécurité)
- ✅ `send_command` rendue async (ne bloque plus l'event loop)
- ✅ `asyncio.Lock()` sur les fichiers (pas de race condition)
- ✅ `@functools.wraps` sur les décorateurs (handlers PTB corrects)
- ✅ TTL de 10 min sur les signaux (pas d'ordres fantômes)
- ✅ Nettoyage automatique des signaux expirés
- ✅ Persistance users + stats (survit aux redémarrages)
- ✅ Back-off exponentiel sur erreurs réseau yfinance
- ✅ Cache 5 min sur les données yfinance
- ✅ Protection ZeroDivisionError dans le calcul RSI
- ✅ Validation robuste dans tous les parsings

### EA MQ5 (TelegramBridge.mq5)
- ✅ Lecture fichier corrigée (FILE_ANSI, nettoyage BOM/espaces)
- ✅ `StringToUpper(action)` → accepte buy/BUY/Buy indifféremment
- ✅ `StringTrimLeft/Right` sur toutes les valeurs parsées
- ✅ Validation complète avant chaque ordre (lot min/max, prix)
- ✅ `OrderCheck` avant `OrderSend`
- ✅ `ORDER_FILLING_IOC` (compatible la plupart des brokers)
- ✅ `CLOSEALL` itère à l'envers (évite les bugs d'index)
- ✅ Format réponse standardisé `OK|...` / `ERROR|...`
