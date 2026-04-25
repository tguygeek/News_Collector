"""
collector.py — Collecte d'articles depuis RSS et GNews API
Stockage local SQLite avec déduplication automatique.

Usage:
    python collector.py              # collecte toutes les sources
    python collector.py --test       # collecte 1 seul flux RSS pour tester
"""

import sqlite3
import hashlib
import logging
import argparse
import time
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field

import feedparser
import requests
from langdetect import detect, LangDetectException

from config import cfg
from extractor import extract as extract_fulltext

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "data" / "newsdigest.db"
MAX_TEXT_LENGTH = 8000  # cohérent avec extractor.py
GNEWS_LANGUAGES = ["fr", "en"]

# Flux RSS par thématique — FR et EN
RSS_SOURCES = {
    "tech_ia": [
        "https://feeds.feedburner.com/TechCrunch",
        "https://www.lemonde.fr/pixels/rss_full.xml",
        "https://www.wired.com/feed/rss",
        "https://intelligence-artificielle.developpez.com/index/rss",
    ],
    "economie": [
        "https://www.lesechos.fr/rss/rss_la_une.xml",
        "https://feeds.bloomberg.com/markets/news.rss",
        "https://www.latribune.fr/rss/une.html",
    ],
    "politique": [
        "https://www.lemonde.fr/politique/rss_full.xml",
        "https://feeds.bbci.co.uk/news/politics/rss.xml",
        "https://rss.politico.com/politics-news.xml",
    ],
    "sport": [
        "https://www.lequipe.fr/rss/actu_rss.xml",
        "https://feeds.bbci.co.uk/sport/rss.xml",
        "https://www.eurosport.fr/rss.xml",
    ],
    "science": [
        "https://www.sciencesetavenir.fr/rss.xml",
        "https://feeds.feedburner.com/sciencedaily/top-news",
        "https://www.nature.com/nature.rss",
    ],
    "general": [
        "https://www.lemonde.fr/rss/une.xml",
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
        "https://www.rfi.fr/fr/rss",
    ],
}

# Mots-clés GNews par thématique
GNEWS_TOPICS = {
    "tech_ia": "intelligence artificielle OR AI technology",
    "economie": "économie finance bourse",
    "politique": "politique gouvernement élection",
    "sport": "sport football championnat",
    "science": "science recherche découverte",
    "general": "actualité",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("collector")

# ---------------------------------------------------------------------------
# Modèle de données
# ---------------------------------------------------------------------------

@dataclass
class Article:
    title: str
    url: str
    source: str
    category: str
    published_at: str
    summary: str = ""
    full_text: str = ""   # contenu complet extrait par newspaper3k
    language: str = "unknown"
    url_hash: str = field(init=False)

    def __post_init__(self):
        self.url_hash = hashlib.md5(self.url.strip().encode()).hexdigest()
        if not self.language or self.language == "unknown":
            self.language = _detect_language(self.title + " " + self.summary)


def _detect_language(text: str) -> str:
    """Détecte la langue d'un texte. Retourne 'unknown' si impossible."""
    try:
        if len(text.strip()) < 20:
            return "unknown"
        return detect(text)
    except LangDetectException:
        return "unknown"


# ---------------------------------------------------------------------------
# Base de données SQLite
# ---------------------------------------------------------------------------

def init_db() -> sqlite3.Connection:
    """Crée la base et les tables si elles n'existent pas."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash     TEXT UNIQUE NOT NULL,
            title        TEXT NOT NULL,
            url          TEXT NOT NULL,
            source       TEXT,
            category     TEXT,
            language     TEXT,
            summary      TEXT,
            full_text    TEXT,
            published_at TEXT,
            collected_at TEXT DEFAULT (datetime('now')),
            ai_summary   TEXT,
            digest_date  TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_category ON articles(category);
        CREATE INDEX IF NOT EXISTS idx_language ON articles(language);
        CREATE INDEX IF NOT EXISTS idx_collected ON articles(collected_at);
        CREATE INDEX IF NOT EXISTS idx_digest ON articles(digest_date);
    """)

    # Migration : ajoute full_text si la base existait avant cette version
    existing_cols = [
        row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()
    ]
    if "full_text" not in existing_cols:
        conn.execute("ALTER TABLE articles ADD COLUMN full_text TEXT")
        log.info("Migration DB : colonne full_text ajoutée")

    conn.commit()
    return conn


def save_article(conn: sqlite3.Connection, article: Article) -> bool:
    """
    Insère un article. Retourne True si inséré, False si doublon.
    La contrainte UNIQUE sur url_hash gère la déduplication.
    """
    try:
        conn.execute(
            """
            INSERT INTO articles
                (url_hash, title, url, source, category, language,
                 summary, full_text, published_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article.url_hash,
                article.title[:500],
                article.url[:1000],
                article.source[:200],
                article.category,
                article.language,
                article.summary[:2000],
                article.full_text[:MAX_TEXT_LENGTH] if article.full_text else None,
                article.published_at,
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # doublon — silencieux et attendu


# ---------------------------------------------------------------------------
# Collecte RSS
# ---------------------------------------------------------------------------

def collect_rss(conn: sqlite3.Connection, category: str, urls: list[str]) -> int:
    """Collecte tous les flux RSS d'une catégorie. Retourne le nb d'articles nouveaux."""
    total_new = 0

    for url in urls:
        log.info(f"  RSS [{category}] → {url}")
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "NewsDigest/1.0"})

            if feed.bozo and not feed.entries:
                log.warning(f"    Flux invalide ou inaccessible : {feed.bozo_exception}")
                continue

            source_name = feed.feed.get("title", url)
            new_count = 0

            for entry in feed.entries[:20]:  # max 20 articles par flux
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()

                if not title or not link:
                    continue

                # Date de publication
                pub = entry.get("published", entry.get("updated", ""))
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
                    except Exception:
                        pass

                # Résumé brut (avant traitement IA)
                raw_summary = entry.get("summary", entry.get("description", ""))
                # Nettoyage HTML basique
                import re
                raw_summary = re.sub(r"<[^>]+>", " ", raw_summary)
                raw_summary = re.sub(r"\s+", " ", raw_summary).strip()

                # Extraction full-text via newspaper3k
                # Si échoue → raw_summary RSS utilisé comme fallback (pipeline intact)
                extraction = extract_fulltext(link, raw_summary)
                full_text = extraction.full_text if extraction.success else ""

                article = Article(
                    title=title,
                    url=link,
                    source=source_name,
                    category=category,
                    published_at=pub,
                    summary=raw_summary[:1000],
                    full_text=full_text,
                )

                if save_article(conn, article):
                    new_count += 1

            log.info(f"    {new_count} nouveaux articles")
            total_new += new_count
            time.sleep(0.5)  # politesse envers les serveurs

        except Exception as e:
            log.error(f"    Erreur sur {url} : {e}")

    return total_new


# ---------------------------------------------------------------------------
# Collecte GNews API
# ---------------------------------------------------------------------------

def collect_gnews(conn: sqlite3.Connection, category: str, query: str) -> int:
    """Collecte des articles via GNews API pour une thématique."""
    if not cfg.gnews_configured:
        log.warning("  GNews : clé API non configurée, collecte ignorée")
        return 0

    total_new = 0

    for lang in GNEWS_LANGUAGES:
        log.info(f"  GNews [{category}] lang={lang} → \"{query}\"")
        try:
            response = requests.get(
                "https://gnews.io/api/v4/search",
                params={
                    "q": query,
                    "lang": lang,
                    "max": cfg.GNEWS_MAX_ARTICLES,
                    "apikey": cfg.GNEWS_API_KEY,
                },
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()

            new_count = 0
            for item in data.get("articles", []):
                title = item.get("title", "").strip()
                url = item.get("url", "").strip()

                if not title or not url:
                    continue

                rss_desc = item.get("description", "")[:1000]

                # Extraction full-text via newspaper3k
                extraction = extract_fulltext(url, rss_desc)
                full_text = extraction.full_text if extraction.success else ""

                article = Article(
                    title=title,
                    url=url,
                    source=item.get("source", {}).get("name", "GNews"),
                    category=category,
                    published_at=item.get("publishedAt", ""),
                    summary=rss_desc,
                    full_text=full_text,
                    language=lang,
                )

                if save_article(conn, article):
                    new_count += 1

            log.info(f"    {new_count} nouveaux articles")
            total_new += new_count
            time.sleep(1)  # respect du rate limit GNews

        except requests.RequestException as e:
            log.error(f"    Erreur GNews ({lang}) : {e}")

    return total_new


# ---------------------------------------------------------------------------
# Rapport de collecte
# ---------------------------------------------------------------------------

def print_stats(conn: sqlite3.Connection) -> None:
    """Affiche un résumé de la base après collecte."""
    rows = conn.execute("""
        SELECT
            category,
            COUNT(*) as total,
            SUM(CASE WHEN collected_at >= datetime('now', '-1 hour') THEN 1 ELSE 0 END) as last_hour,
            SUM(CASE WHEN language='fr' THEN 1 ELSE 0 END) as fr,
            SUM(CASE WHEN language='en' THEN 1 ELSE 0 END) as en
        FROM articles
        GROUP BY category
        ORDER BY total DESC
    """).fetchall()

    print("\n" + "─" * 62)
    print(f"{'Catégorie':<15} {'Total':>7} {'Dernière h':>11} {'FR':>5} {'EN':>5}")
    print("─" * 62)
    for r in rows:
        print(f"{r['category']:<15} {r['total']:>7} {r['last_hour']:>11} {r['fr']:>5} {r['en']:>5}")

    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    print("─" * 62)
    print(f"Total en base : {total} articles\n")


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def run(test_mode: bool = False) -> None:
    """Lance la collecte complète (ou 1 flux en mode test)."""
    log.info("=" * 50)
    log.info("Démarrage de la collecte")
    log.info("=" * 50)

    conn = init_db()
    total_new = 0

    if test_mode:
        # En mode test : un seul flux RSS pour vérifier que tout fonctionne
        log.info("[MODE TEST] 1 flux RSS uniquement")
        total_new += collect_rss(conn, "general", [RSS_SOURCES["general"][0]])
    else:
        # Collecte RSS complète
        log.info("--- Collecte RSS ---")
        for category, urls in RSS_SOURCES.items():
            total_new += collect_rss(conn, category, urls)

        # Collecte GNews
        log.info("--- Collecte GNews ---")
        for category, query in GNEWS_TOPICS.items():
            total_new += collect_gnews(conn, category, query)

    log.info(f"\nCollecte terminée — {total_new} nouveaux articles")
    print_stats(conn)
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collecteur d'articles NewsDigest")
    parser.add_argument("--test", action="store_true", help="Mode test (1 flux RSS)")
    args = parser.parse_args()
    run(test_mode=args.test)