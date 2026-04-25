"""
sender.py — Envoi du digest par Gmail SMTP et Bot Telegram
Utilise uniquement des bibliothèques gratuites (smtplib natif + python-telegram-bot).

Configuration requise :
    GMAIL_ADDRESS   : ton adresse Gmail
    GMAIL_APP_PASSWORD : mot de passe d'application Gmail (pas ton mot de passe principal)
                         → Compte Google > Sécurité > Validation en 2 étapes > Mots de passe des apps
    TELEGRAM_BOT_TOKEN : token obtenu via @BotFather sur Telegram
    TELEGRAM_CHAT_ID   : ton chat ID (obtenu via @userinfobot sur Telegram)

Usage:
    python sender.py                  # envoie le digest du jour
    python sender.py --date 2026-04-21
    python sender.py --channel email  # email uniquement
    python sender.py --channel telegram
    python sender.py --test           # envoie un message de test sur les deux canaux
"""

import smtplib
import logging
import argparse
import asyncio
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from config import cfg

EMAIL_SUBJECT_TEMPLATE = "📰 NewsDigest — {date_display}"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sender")

# ---------------------------------------------------------------------------
# Envoi Gmail
# ---------------------------------------------------------------------------

def send_email(html: str, digest_date: str) -> bool:
    """
    Envoie le digest HTML via Gmail SMTP (TLS port 587).
    Retourne True si l'envoi a réussi.

    Prérequis Gmail :
    1. Activer la validation en 2 étapes sur ton compte Google
    2. Créer un "Mot de passe d'application" (pas ton vrai mot de passe)
       → myaccount.google.com > Sécurité > Mots de passe des applis
    3. Coller ce mot de passe dans GMAIL_APP_PASSWORD dans le fichier .env
    """
    if not cfg.gmail_configured:
        log.warning("Gmail non configuré — envoi email ignoré")
        return False

    from datetime import datetime
    try:
        dt = datetime.strptime(digest_date, "%Y-%m-%d")
        date_display = dt.strftime("%d/%m/%Y")
    except ValueError:
        date_display = digest_date

    subject = EMAIL_SUBJECT_TEMPLATE.format(date_display=date_display)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg.GMAIL_ADDRESS
    msg["To"]      = cfg.GMAIL_ADDRESS
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        log.info(f"Envoi email → {cfg.GMAIL_ADDRESS}")
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(cfg.GMAIL_ADDRESS, cfg.GMAIL_APP_PASSWORD)
            server.sendmail(cfg.GMAIL_ADDRESS, cfg.GMAIL_ADDRESS, msg.as_string())
        log.info("  Email envoyé avec succès")
        return True

    except smtplib.SMTPAuthenticationError:
        log.error(
            "  Erreur d'authentification Gmail.\n"
            "  Vérifie que tu utilises un mot de passe d'application,\n"
            "  pas ton mot de passe Gmail principal."
        )
        return False

    except smtplib.SMTPException as e:
        log.error(f"  Erreur SMTP : {e}")
        return False

    except Exception as e:
        log.error(f"  Erreur inattendue email : {e}")
        return False


# ---------------------------------------------------------------------------
# Envoi Telegram
# ---------------------------------------------------------------------------

async def _send_telegram_async(chunks: list[str]) -> bool:
    """Envoie les messages Telegram de façon asynchrone."""
    try:
        from telegram import Bot
        from telegram.error import TelegramError

        bot = Bot(token=cfg.TELEGRAM_BOT_TOKEN)

        for i, chunk in enumerate(chunks, 1):
            log.info(f"  Telegram message {i}/{len(chunks)} → chat {cfg.TELEGRAM_CHAT_ID}")
            await bot.send_message(
                chat_id=cfg.TELEGRAM_CHAT_ID,
                text=chunk,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            if i < len(chunks):
                await asyncio.sleep(0.5)  # petite pause entre messages

        log.info("  Telegram : tous les messages envoyés")
        return True

    except Exception as e:
        log.error(f"  Erreur Telegram : {e}")
        return False


def send_telegram(chunks: list[str]) -> bool:
    """
    Envoie le digest via Bot Telegram.
    Retourne True si l'envoi a réussi.

    Prérequis Telegram :
    1. Ouvre Telegram, cherche @BotFather
    2. Tape /newbot, suis les instructions → copie le token dans TELEGRAM_BOT_TOKEN
    3. Cherche @userinfobot, tape /start → copie ton ID dans TELEGRAM_CHAT_ID
    4. Démarre une conversation avec ton bot (tape /start dans le chat du bot)
    """
    if not cfg.telegram_configured:
        log.warning("Telegram non configuré — envoi Telegram ignoré")
        return False

    if not chunks:
        log.warning("Aucun contenu Telegram à envoyer")
        return False

    log.info(f"Envoi Telegram — {len(chunks)} message(s)")
    return asyncio.run(_send_telegram_async(chunks))


# ---------------------------------------------------------------------------
# Message de test
# ---------------------------------------------------------------------------

def send_test(channel: str = "both") -> None:
    """Envoie un message de test pour vérifier la configuration."""
    test_html = """\
    <html><body style="font-family:Arial;padding:20px;max-width:600px">
    <h2 style="color:#1a1a1a">NewsDigest — Test de configuration</h2>
    <p style="color:#555">Si tu reçois cet email, ta configuration Gmail fonctionne correctement.</p>
    <p style="color:#999;font-size:12px">NewsDigest &bull; Groq Llama 3</p>
    </body></html>
    """
    test_telegram = ["🟢 *NewsDigest — Test de configuration*\n\nSi tu reçois ce message, ton bot Telegram est bien configuré\\!"]

    if channel in ("both", "email"):
        ok = send_email(test_html, date.today().isoformat())
        print(f"Email  : {'OK' if ok else 'ECHEC'}")

    if channel in ("both", "telegram"):
        ok = send_telegram(test_telegram)
        print(f"Telegram : {'OK' if ok else 'ECHEC'}")


# ---------------------------------------------------------------------------
# Envoi principal
# ---------------------------------------------------------------------------

def send(
    html: str,
    telegram_chunks: list[str],
    digest_date: str,
    channel: str = "both",
) -> dict[str, bool]:
    """
    Envoie le digest sur les canaux demandés.
    channel: 'both' | 'email' | 'telegram'
    Retourne un dict avec le statut de chaque canal.
    """
    results = {"email": False, "telegram": False}

    log.info("=" * 50)
    log.info(f"Envoi du digest — {digest_date}")
    log.info("=" * 50)

    if channel in ("both", "email"):
        results["email"] = send_email(html, digest_date)

    if channel in ("both", "telegram"):
        results["telegram"] = send_telegram(telegram_chunks)

    # Rapport final
    print("\n" + "─" * 40)
    print(f"  Email    : {'✓ Envoyé' if results['email'] else '✗ Non envoyé'}")
    print(f"  Telegram : {'✓ Envoyé' if results['telegram'] else '✗ Non envoyé'}")
    print("─" * 40 + "\n")

    return results


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Envoi du digest NewsDigest")
    parser.add_argument("--date", type=str, help="Date YYYY-MM-DD (défaut: aujourd'hui)")
    parser.add_argument(
        "--channel",
        choices=["both", "email", "telegram"],
        default="both",
        help="Canal d'envoi (défaut: both)",
    )
    parser.add_argument("--test", action="store_true", help="Envoie un message de test")
    args = parser.parse_args()

    if args.test:
        send_test(args.channel)
    else:
        # Import ici pour éviter une dépendance circulaire si appelé depuis main.py
        from News_V2.digest_builder import build
        target_date = args.date or date.today().isoformat()
        html, chunks, data = build(digest_date=target_date)
        if html:
            send(html, chunks, target_date, channel=args.channel)
        else:
            log.error("Digest vide — rien à envoyer. Lance collector.py et summarizer.py d'abord.")
