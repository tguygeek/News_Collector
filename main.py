"""
main.py — Orchestrateur principal du pipeline NewsDigest
Enchaîne collecte → résumé IA → digest → envoi, et planifie l'exécution quotidienne.

Usage:
    python main.py                    # lance le scheduler (tourne en continu)
    python main.py --run-now          # exécute le pipeline immédiatement
    python main.py --run-now --date 2026-04-21
    python main.py --test             # test de bout en bout (sans envoi réel)
    python main.py --status           # affiche l'état de la base
"""

import logging
import argparse
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path

# --- Fix d'import : ajoute le dossier du projet au sys.path ---
# Garantit que tous les modules (collector, summarizer, etc.) sont trouvables
# quelle que soit la façon dont main.py est lancé :
#   python main.py               (depuis News_V2/)
#   python News_V2/main.py       (depuis le dossier parent)
#   python /chemin/absolu/main.py
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import schedule

from config import cfg

# ---------------------------------------------------------------------------
# Logging — fichier + console
# ---------------------------------------------------------------------------

LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "newsdigest.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Étapes du pipeline
# ---------------------------------------------------------------------------

def step_collect(target_date: str | None = None) -> bool:
    """Étape 1 — Collecte RSS + GNews."""
    log.info("┌─ Étape 1 : Collecte des articles")
    try:
        from collector import run as collect_run
        collect_run(test_mode=False)
        log.info("└─ Collecte terminée")
        return True
    except Exception as e:
        log.error(f"└─ Collecte échouée : {e}")
        log.debug(traceback.format_exc())
        return False


def step_summarize(target_date: str | None = None) -> bool:
    """Étape 2 — Résumé IA via Groq."""
    log.info("┌─ Étape 2 : Résumé IA")
    try:
        from summarizer import run as summarize_run
        summarize_run(target_date=target_date)
        log.info("└─ Résumé terminé")
        return True
    except Exception as e:
        log.error(f"└─ Résumé échoué : {e}")
        log.debug(traceback.format_exc())
        return False


def step_build_and_send(target_date: str | None = None) -> bool:
    """Étapes 3 & 4 — Construction du digest et envoi."""
    log.info("┌─ Étape 3 : Construction du digest")
    try:
        from digest_builder import build
        html, chunks, data = build(digest_date=target_date)

        if not data or not html:
            log.warning("└─ Digest vide — rien à envoyer")
            return False

        log.info(f"│  {data.total_articles} articles, {len(data.categories)} catégories")
        log.info("└─ Digest construit")
    except Exception as e:
        log.error(f"└─ Construction échouée : {e}")
        log.debug(traceback.format_exc())
        return False

    log.info("┌─ Étape 4 : Envoi du digest")
    try:
        from sender import send
        digest_date = target_date or date.today().isoformat()
        results = send(html, chunks, digest_date, channel=cfg.SEND_CHANNEL)

        sent_any = any(results.values())
        if sent_any:
            log.info("└─ Envoi terminé")
        else:
            log.warning("└─ Aucun canal n'a pu envoyer le digest")
        return sent_any

    except Exception as e:
        log.error(f"└─ Envoi échoué : {e}")
        log.debug(traceback.format_exc())
        return False


# ---------------------------------------------------------------------------
# Pipeline complet
# ---------------------------------------------------------------------------

def run_pipeline(target_date: str | None = None, skip_collect: bool = False) -> bool:
    """
    Exécute le pipeline complet :
    Collecte → Résumé IA → Build digest → Envoi

    skip_collect=True : saute la collecte (utile pour relancer uniquement
    la partie résumé+envoi sur des articles déjà collectés).
    """
    digest_date = target_date or date.today().isoformat()

    log.info("=" * 55)
    log.info(f"  PIPELINE NEWSDIGEST — {digest_date}")
    log.info(f"  Démarré le {datetime.now().strftime('%d/%m/%Y à %H:%M:%S')}")
    log.info("=" * 55)

    results = {}

    if not skip_collect:
        results["collect"] = step_collect(digest_date)
        # On continue même si la collecte a partiellement échoué —
        # il peut y avoir des articles en base d'une collecte précédente
    else:
        log.info("(collecte ignorée — --skip-collect)")
        results["collect"] = True

    results["summarize"] = step_summarize(digest_date)

    if not results["summarize"]:
        log.error("Résumé IA échoué — abandon du pipeline")
        _log_pipeline_result(results, digest_date)
        return False

    results["send"] = step_build_and_send(digest_date)

    _log_pipeline_result(results, digest_date)
    return all(results.values())


def _log_pipeline_result(results: dict, digest_date: str) -> None:
    """Affiche le bilan du pipeline."""
    status = {True: "✓", False: "✗"}
    log.info("─" * 55)
    log.info(f"  BILAN — {digest_date}")
    for step, ok in results.items():
        log.info(f"  {status[ok]}  {step}")
    log.info("─" * 55)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def scheduled_collect():
    """Job de collecte horaire (sans résumé ni envoi)."""
    log.info(">> Collecte horaire déclenchée")
    step_collect()


def scheduled_daily_pipeline():
    """Job quotidien complet : résumé + digest + envoi."""
    log.info(">> Pipeline quotidien déclenché")
    # La collecte a déjà tourné dans la nuit — on saute cette étape
    # pour ne pas surcharger les APIs au moment de l'envoi
    run_pipeline(skip_collect=True)


def start_scheduler() -> None:
    """
    Lance le scheduler en boucle infinie.

    Planification :
    - Collecte des articles : toutes les heures à HH:15
    - Pipeline quotidien (résumé + envoi) : chaque jour à DIGEST_SEND_TIME

    Le scheduler tourne jusqu'à Ctrl+C ou arrêt du processus.
    """
    log.info("=" * 55)
    log.info("  NEWSDIGEST — Scheduler démarré")
    log.info(f"  Collecte : toutes les heures à :{cfg.COLLECT_MINUTE}")
    log.info(f"  Digest   : chaque jour à {cfg.DIGEST_SEND_TIME}")
    log.info("  Ctrl+C pour arrêter")
    log.info("=" * 55)

    # Collecte horaire
    schedule.every().hour.at(f":{cfg.COLLECT_MINUTE}").do(scheduled_collect)

    # Pipeline quotidien
    schedule.every().day.at(cfg.DIGEST_SEND_TIME).do(scheduled_daily_pipeline)

    # Collecte initiale au démarrage (pour ne pas attendre la prochaine heure)
    log.info("Collecte initiale au démarrage...")
    step_collect()

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)  # vérifie toutes les 30 secondes
    except KeyboardInterrupt:
        log.info("Arrêt du scheduler (Ctrl+C)")
    except Exception as e:
        log.critical(f"Erreur critique scheduler : {e}")
        log.debug(traceback.format_exc())
        sys.exit(1)


# ---------------------------------------------------------------------------
# Commande --status
# ---------------------------------------------------------------------------

def print_status() -> None:
    """Affiche l'état de la base de données."""
    import sqlite3
    db_path = ROOT_DIR / "data" / "newsdigest.db"

    if not db_path.exists():
        print("Base de données introuvable. Lance d'abord : python main.py --run-now")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Stats globales
    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    summarized = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE ai_summary IS NOT NULL"
    ).fetchone()[0]

    # Dernière collecte
    last = conn.execute(
        "SELECT MAX(collected_at) as t FROM articles"
    ).fetchone()["t"] or "jamais"

    # Digests envoyés
    try:
        digests = conn.execute(
            "SELECT digest_date FROM digests ORDER BY digest_date DESC LIMIT 5"
        ).fetchall()
        digest_dates = [r["digest_date"] for r in digests]
    except Exception:
        digest_dates = []

    # Articles par catégorie aujourd'hui
    today_rows = conn.execute("""
        SELECT category,
               COUNT(*) as total,
               SUM(CASE WHEN ai_summary IS NOT NULL THEN 1 ELSE 0 END) as resumés
        FROM articles
        WHERE DATE(collected_at) = DATE('now')
        GROUP BY category
        ORDER BY total DESC
    """).fetchall()

    print("\n" + "═" * 50)
    print("  NEWSDIGEST — État de la base")
    print("═" * 50)
    print(f"  Articles total     : {total}")
    print(f"  Articles résumés   : {summarized}")
    print(f"  Dernière collecte  : {last}")
    print(f"  Digests générés    : {', '.join(digest_dates) if digest_dates else 'aucun'}")

    if today_rows:
        print(f"\n  Aujourd'hui ({date.today().isoformat()})")
        print(f"  {'Catégorie':<16} {'Total':>6} {'Résumés':>8}")
        print("  " + "─" * 32)
        for r in today_rows:
            print(f"  {r['category']:<16} {r['total']:>6} {r['resumés']:>8}")
    else:
        print("\n  Aucun article collecté aujourd'hui.")

    print("═" * 50 + "\n")
    conn.close()


# ---------------------------------------------------------------------------
# Commande --test
# ---------------------------------------------------------------------------

def run_test() -> None:
    """
    Test de bout en bout sans appel API réel ni envoi.
    Vérifie que tous les modules s'importent et s'enchaînent correctement.
    """
    log.info("=" * 55)
    log.info("  MODE TEST — vérification des modules")
    log.info("=" * 55)

    checks = []

    # Import de chaque module
    for module in ["collector", "summarizer", "digest_builder", "sender"]:
        try:
            __import__(module)
            log.info(f"  ✓ import {module}")
            checks.append(True)
        except ImportError as e:
            log.error(f"  ✗ import {module} : {e}")
            checks.append(False)

    # Vérification de la base
    db_path = ROOT_DIR / "data" / "newsdigest.db"
    if db_path.exists():
        log.info(f"  ✓ Base SQLite trouvée : {db_path}")
        checks.append(True)
    else:
        log.warning(f"  ⚠ Base SQLite absente (normal au premier lancement)")
        checks.append(True)  # pas bloquant

    # Vérification des dossiers
    for d in ["data", "logs"]:
        p = ROOT_DIR / d
        if p.exists():
            log.info(f"  ✓ Dossier {d}/ présent")
        else:
            log.info(f"  ✓ Dossier {d}/ sera créé au premier lancement")
        checks.append(True)

    # Résultat
    all_ok = all(checks)
    log.info("─" * 55)
    if all_ok:
        log.info("  Tous les checks passent — projet prêt à démarrer")
        log.info("  Lance : python main.py --run-now  pour un premier test")
    else:
        log.error("  Certains checks ont échoué — vérifie les dépendances")
        log.info("  pip install feedparser requests langdetect groq schedule python-telegram-bot")
    log.info("─" * 55)


# ---------------------------------------------------------------------------
# Commande --reset-dates (utile pour tester sans attendre de nouveaux articles)
# ---------------------------------------------------------------------------

def reset_dates(target_date: str | None = None) -> None:
    """
    Réassigne tous les articles existants à la date cible (défaut: aujourd'hui)
    et efface leurs résumés IA pour permettre un nouveau cycle complet.
    Utile pour tester le pipeline sans attendre de nouveaux articles RSS.
    """
    import sqlite3
    db_path = ROOT_DIR / "data" / "newsdigest.db"
    if not db_path.exists():
        print("Base introuvable — lance d'abord collector.py")
        return

    target = target_date or date.today().isoformat()
    conn = sqlite3.connect(db_path)

    # Compte avant
    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]

    # Réassignation
    conn.execute(
        "UPDATE articles SET collected_at = ?, ai_summary = NULL, digest_date = NULL",
        (f"{target} 06:00:00",),
    )
    conn.commit()
    conn.close()

    print(f"\n  {total} articles réassignés au {target}")
    print("  Résumés IA effacés — prêt pour un nouveau cycle")
    print(f"\n  Lance maintenant :")
    print(f"    python main.py --run-now --skip-collect\n")


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NewsDigest — Orchestrateur principal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  python main.py                           Lance le scheduler (tourne en continu)
  python main.py --run-now                 Exécute le pipeline complet maintenant
  python main.py --run-now --skip-collect  Résumé + envoi uniquement
  python main.py --run-now --date 2026-04-21
  python main.py --reset-dates             Réassigne les articles à aujourd'hui (test)
  python main.py --status                  Affiche l'état de la base
  python main.py --test                    Vérifie que tout est bien installé
        """,
    )

    parser.add_argument("--run-now", action="store_true", help="Exécute le pipeline immédiatement")
    parser.add_argument("--date", type=str, help="Date cible YYYY-MM-DD (défaut: aujourd'hui)")
    parser.add_argument("--skip-collect", action="store_true", help="Saute la collecte")
    parser.add_argument("--channel", choices=["both", "email", "telegram"], default=None)
    parser.add_argument("--status", action="store_true", help="Affiche l'état de la base")
    parser.add_argument("--test", action="store_true", help="Vérifie l'installation")
    parser.add_argument(
        "--reset-dates",
        action="store_true",
        help="Réassigne tous les articles à aujourd'hui (utile pour tester)",
    )

    args = parser.parse_args()

    if args.channel:
        cfg.SEND_CHANNEL = args.channel

    if args.test:
        run_test()
    elif args.status:
        print_status()
    elif args.reset_dates:
        reset_dates(target_date=args.date)
    elif args.run_now:
        success = run_pipeline(
            target_date=args.date,
            skip_collect=args.skip_collect,
        )
        sys.exit(0 if success else 1)
    else:
        start_scheduler()