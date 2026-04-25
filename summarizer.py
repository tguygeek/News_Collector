"""
summarizer.py — Résumé IA des articles via Groq API (Llama 3, gratuit)
Sélectionne les N meilleurs articles par catégorie et génère un résumé
en 3-5 phrases. Met à jour la colonne ai_summary dans SQLite.

Usage:
    python summarizer.py              # résume les articles du jour
    python summarizer.py --test       # résume 2 articles pour tester
    python summarizer.py --date 2025-04-21  # résume un jour précis
"""

import sqlite3
import logging
import argparse
import time
from datetime import datetime, date
from pathlib import Path

from groq import Groq, RateLimitError, APIError

from config import cfg

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "data" / "newsdigest.db"

# Prompts système par langue
SYSTEM_PROMPTS = {
    "fr": (
        "Tu es un journaliste expert chargé de résumer des articles d'actualité. "
        "Écris un résumé factuel, neutre et clair en 3 à 5 phrases en français. "
        "Va droit au but. N'invente rien. Si l'information est insuffisante, dis-le."
    ),
    "en": (
        "You are an expert journalist summarizing news articles. "
        "Write a factual, neutral and clear summary in 3 to 5 sentences in English. "
        "Be concise and accurate. Do not invent information. "
        "If the content is insufficient, say so briefly."
    ),
    "unknown": (
        "Summarize this news article in 3 to 5 clear, factual sentences. "
        "Be concise. Do not add information not present in the text."
    ),
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("summarizer")

# ---------------------------------------------------------------------------
# Connexion base de données
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Base de données introuvable : {DB_PATH}\n"
            "Lance d'abord collector.py pour initialiser la base."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Sélection des articles à résumer
# ---------------------------------------------------------------------------

def select_articles_to_summarize(
    conn: sqlite3.Connection,
    target_date: str,
    top_n: int | None = None,
) -> list[sqlite3.Row]:
    """
    Sélectionne les top N articles par catégorie collectés à la date cible
    qui n'ont pas encore de résumé IA.
    Récupère full_text en priorité — résumé RSS en fallback.
    """
    if top_n is None:
        top_n = cfg.TOP_N_PER_CATEGORY

    # Ajout de full_text dans la sélection
    # COALESCE : utilise full_text s'il existe, sinon summary
    rows = conn.execute(
        """
        SELECT
            id, title, language, category, source, url,
            COALESCE(
                CASE WHEN full_text IS NOT NULL AND LENGTH(full_text) > 200
                     THEN full_text ELSE NULL END,
                summary
            ) AS best_content,
            summary,
            full_text,
            LENGTH(COALESCE(full_text, summary, '')) AS content_length
        FROM articles
        WHERE
            DATE(collected_at) = ?
            AND ai_summary IS NULL
            AND (
                (full_text IS NOT NULL AND LENGTH(full_text) > 200)
                OR (summary IS NOT NULL AND LENGTH(summary) > 50)
            )
        ORDER BY category, content_length DESC, published_at DESC
        """,
        (target_date,),
    ).fetchall()

    # Garder top_n par catégorie
    seen: dict[str, int] = {}
    selected = []
    for row in rows:
        cat = row["category"]
        seen[cat] = seen.get(cat, 0)
        if seen[cat] < top_n:
            selected.append(row)
            seen[cat] += 1

    # Stats sur la qualité du contenu
    full_text_count = sum(
        1 for r in selected
        if r["full_text"] and len(r["full_text"]) > 200
    )
    log.info(
        f"Articles éligibles : {len(rows)} — "
        f"sélectionnés : {len(selected)} "
        f"({full_text_count} avec full-text, "
        f"{len(selected) - full_text_count} avec résumé RSS)"
    )
    return selected


# ---------------------------------------------------------------------------
# Résumé IA via Groq
# ---------------------------------------------------------------------------

def build_prompt(title: str, summary: str, language: str) -> str:
    """Construit le prompt utilisateur à envoyer à Groq."""
    if language == "fr":
        return (
            f"Titre de l'article : {title}\n\n"
            f"Contenu disponible :\n{summary}\n\n"
            "Rédige un résumé de cet article en 3 à 5 phrases."
        )
    else:
        return (
            f"Article title: {title}\n\n"
            f"Available content:\n{summary}\n\n"
            "Write a summary of this article in 3 to 5 sentences."
        )


def summarize_article(
    client: Groq,
    article_id: int,
    title: str,
    summary: str,
    language: str,
) -> str | None:
    """
    Appelle Groq pour résumer un article.
    Retourne le texte du résumé ou None en cas d'erreur.
    """
    lang_key = language if language in SYSTEM_PROMPTS else "unknown"
    system_prompt = SYSTEM_PROMPTS[lang_key]
    user_prompt = build_prompt(title, summary, language)

    try:
        response = client.chat.completions.create(
            model=cfg.GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=300,
            temperature=0.3,  # faible pour des résumés factuels et stables
        )
        ai_text = response.choices[0].message.content.strip()
        tokens_used = response.usage.total_tokens if response.usage else "?"
        log.info(f"  [#{article_id}] OK — {tokens_used} tokens")
        return ai_text

    except RateLimitError:
        log.warning(f"  [#{article_id}] Rate limit atteint — pause 60s")
        time.sleep(60)
        return None

    except APIError as e:
        log.error(f"  [#{article_id}] Erreur API Groq : {e}")
        return None

    except Exception as e:
        log.error(f"  [#{article_id}] Erreur inattendue : {e}")
        return None


def save_ai_summary(
    conn: sqlite3.Connection,
    article_id: int,
    ai_summary: str,
    digest_date: str,
) -> None:
    """Met à jour l'article avec son résumé IA et la date du digest."""
    conn.execute(
        "UPDATE articles SET ai_summary = ?, digest_date = ? WHERE id = ?",
        (ai_summary, digest_date, article_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Résumé global du digest
# ---------------------------------------------------------------------------

DIGEST_SUMMARY_PROMPT_FR = """
Tu es un éditorialiste. Voici les titres des principales actualités du jour,
classées par thématique. Rédige en 4 à 6 phrases une synthèse générale
de l'actualité du jour, en français, dans un style journalistique sobre.
Ne liste pas les titres — fais une vraie synthèse narrative.

Titres du jour :
{headlines}
"""

DIGEST_SUMMARY_PROMPT_EN = """
You are an editor. Here are today's main headlines by category.
Write a 4 to 6 sentence general news summary in English,
in a clear journalistic style. Do not list the titles — write
a genuine narrative synthesis.

Today's headlines:
{headlines}
"""


def generate_digest_summary(
    client: Groq,
    conn: sqlite3.Connection,
    digest_date: str,
    language: str = "fr",
) -> str:
    """
    Génère un résumé global de toute l'actualité du jour
    à partir des titres des articles sélectionnés.
    """
    rows = conn.execute(
        """
        SELECT title, category FROM articles
        WHERE digest_date = ?
        ORDER BY category
        """,
        (digest_date,),
    ).fetchall()

    if not rows:
        return "Aucun article disponible pour générer un résumé global."

    # Formatage des titres par catégorie
    by_cat: dict[str, list[str]] = {}
    for row in rows:
        by_cat.setdefault(row["category"], []).append(row["title"])

    headlines = "\n".join(
        f"[{cat.upper()}] " + " | ".join(titles)
        for cat, titles in by_cat.items()
    )

    prompt_template = DIGEST_SUMMARY_PROMPT_FR if language == "fr" else DIGEST_SUMMARY_PROMPT_EN
    user_prompt = prompt_template.format(headlines=headlines)

    log.info("Génération du résumé global du digest...")
    try:
        response = client.chat.completions.create(
            model=cfg.GROQ_MODEL,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=400,
            temperature=0.4,
        )
        result = response.choices[0].message.content.strip()
        log.info("  Résumé global généré.")
        return result

    except Exception as e:
        log.error(f"  Erreur résumé global : {e}")
        return "Résumé global indisponible (erreur API)."


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------

def print_summary_stats(conn: sqlite3.Connection, digest_date: str) -> None:
    """Affiche les articles résumés pour une date donnée."""
    rows = conn.execute(
        """
        SELECT category, title, language,
               SUBSTR(ai_summary, 1, 80) as preview
        FROM articles
        WHERE digest_date = ?
        ORDER BY category, language
        """,
        (digest_date,),
    ).fetchall()

    print(f"\n{'─'*62}")
    print(f"Articles résumés pour le {digest_date}")
    print(f"{'─'*62}")
    current_cat = None
    for r in rows:
        if r["category"] != current_cat:
            current_cat = r["category"]
            print(f"\n  [{current_cat.upper()}]")
        lang_tag = f"[{r['language']}]"
        print(f"  {lang_tag:<5} {r['title'][:55]}")
        if r["preview"]:
            print(f"         → {r['preview']}...")
    print(f"\n  Total : {len(rows)} articles résumés\n")


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def run(target_date: str | None = None, test_mode: bool = False) -> None:
    if not cfg.groq_configured:
        log.error(
            "Clé Groq non configurée.\n"
            "1. Crée un compte sur https://console.groq.com\n"
            "2. Génère une clé API gratuite\n"
            "3. Ajoute GROQ_API_KEY=ta_clé dans le fichier .env"
        )
        return

    digest_date = target_date or date.today().isoformat()
    log.info("=" * 50)
    log.info(f"Résumé IA — {digest_date}")
    log.info("=" * 50)

    conn = get_conn()
    client = Groq(api_key=cfg.GROQ_API_KEY)

    top_n = 1 if test_mode else cfg.TOP_N_PER_CATEGORY
    articles = select_articles_to_summarize(conn, digest_date, top_n=top_n)

    if not articles:
        log.warning(
            f"Aucun article à résumer pour le {digest_date}. "
            "Lance d'abord collector.py."
        )
        conn.close()
        return

    # Résumé article par article
    success = 0
    for article in articles:
        # Utilise le full_text si disponible, sinon le résumé RSS
        content = article["best_content"] or article["summary"] or ""
        content_source = (
            "full-text" if article["full_text"] and len(article["full_text"]) > 200
            else "résumé RSS"
        )
        log.info(
            f"Résumé [{article['category']}] "
            f"({article['language']}) [{content_source}] : {article['title'][:55]}..."
        )
        ai_summary = summarize_article(
            client,
            article["id"],
            article["title"],
            content,
            article["language"],
        )
        if ai_summary:
            save_ai_summary(conn, article["id"], ai_summary, digest_date)
            success += 1

        time.sleep(cfg.DELAY_BETWEEN_CALLS)

    log.info(f"\n{success}/{len(articles)} articles résumés avec succès")

    # Résumé global (uniquement si assez d'articles)
    if success >= 3 and not test_mode:
        digest_summary = generate_digest_summary(client, conn, digest_date, language="fr")
        # Stockage du résumé global dans une table dédiée
        conn.execute("""
            CREATE TABLE IF NOT EXISTS digests (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                digest_date  TEXT UNIQUE NOT NULL,
                summary      TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            """
            INSERT INTO digests (digest_date, summary)
            VALUES (?, ?)
            ON CONFLICT(digest_date) DO UPDATE SET summary = excluded.summary
            """,
            (digest_date, digest_summary),
        )
        conn.commit()
        log.info("Résumé global sauvegardé dans la table 'digests'")

    print_summary_stats(conn, digest_date)
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Résumé IA des articles NewsDigest")
    parser.add_argument("--test", action="store_true", help="Mode test (1 article/catégorie)")
    parser.add_argument("--date", type=str, help="Date cible YYYY-MM-DD (défaut: aujourd'hui)")
    args = parser.parse_args()
    run(target_date=args.date, test_mode=args.test)