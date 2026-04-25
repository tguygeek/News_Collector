"""
digest_builder.py — Assemblage du digest quotidien
Construit deux versions du digest : HTML (pour email) et texte (pour Telegram).
Récupère les articles résumés depuis SQLite et les organise par catégorie.

Usage:
    python digest_builder.py              # digest du jour
    python digest_builder.py --date 2026-04-21
    python digest_builder.py --preview    # affiche le digest texte dans le terminal
"""

import sqlite3
import logging
import argparse
from datetime import date
from pathlib import Path
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "data" / "newsdigest.db"

# Ordre d'affichage des catégories dans le digest
CATEGORY_ORDER = ["tech_ia", "economie", "politique", "sport", "science", "general"]

CATEGORY_LABELS = {
    "tech_ia":   {"fr": "Technologie & IA",  "emoji": "💡"},
    "economie":  {"fr": "Économie",           "emoji": "📈"},
    "politique": {"fr": "Politique",          "emoji": "🏛"},
    "sport":     {"fr": "Sport",              "emoji": "⚽"},
    "science":   {"fr": "Science",            "emoji": "🔬"},
    "general":   {"fr": "Actualité générale", "emoji": "🌍"},
}

# Couleurs par catégorie pour l'email HTML
CATEGORY_COLORS = {
    "tech_ia":   "#534AB7",
    "economie":  "#0F6E56",
    "politique": "#993C1D",
    "sport":     "#185FA5",
    "science":   "#3B6D11",
    "general":   "#5F5E5A",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("digest_builder")

# ---------------------------------------------------------------------------
# Modèles de données
# ---------------------------------------------------------------------------

@dataclass
class ArticleDigest:
    id: int
    title: str
    url: str
    source: str
    language: str
    ai_summary: str
    category: str


@dataclass
class CategoryBlock:
    category: str
    label: str
    emoji: str
    color: str
    articles: list[ArticleDigest]


@dataclass
class DigestData:
    digest_date: str
    global_summary: str
    categories: list[CategoryBlock]
    total_articles: int


# ---------------------------------------------------------------------------
# Récupération des données
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Base introuvable : {DB_PATH}\n"
            "Lance collector.py puis summarizer.py d'abord."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_digest_data(conn: sqlite3.Connection, digest_date: str) -> DigestData | None:
    """Charge tous les articles résumés pour une date et le résumé global."""

    # Création de la table digests si elle n'existe pas encore
    # (normalement créée par summarizer.py, mais on se protège si digest_builder
    # est lancé seul ou avant summarizer)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS digests (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            digest_date  TEXT UNIQUE NOT NULL,
            summary      TEXT,
            created_at   TEXT
        )
    """)
    conn.commit()

    # Résumé global du jour
    row = conn.execute(
        "SELECT summary FROM digests WHERE digest_date = ?",
        (digest_date,),
    ).fetchone()
    global_summary = row["summary"] if row else ""

    # Articles résumés du jour, ordonnés par catégorie
    articles = conn.execute(
        """
        SELECT id, title, url, source, language, ai_summary, category
        FROM articles
        WHERE digest_date = ? AND ai_summary IS NOT NULL
        ORDER BY category, language
        """,
        (digest_date,),
    ).fetchall()

    if not articles:
        log.warning(f"Aucun article résumé trouvé pour le {digest_date}.")
        return None

    # Groupement par catégorie
    by_category: dict[str, list[ArticleDigest]] = {}
    for row in articles:
        cat = row["category"]
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(ArticleDigest(
            id=row["id"],
            title=row["title"],
            url=row["url"],
            source=row["source"] or "",
            language=row["language"] or "?",
            ai_summary=row["ai_summary"],
            category=cat,
        ))

    # Construction des blocs dans l'ordre défini
    categories = []
    for cat in CATEGORY_ORDER:
        if cat not in by_category:
            continue
        meta = CATEGORY_LABELS.get(cat, {"fr": cat, "emoji": "•"})
        categories.append(CategoryBlock(
            category=cat,
            label=meta["fr"],
            emoji=meta["emoji"],
            color=CATEGORY_COLORS.get(cat, "#444441"),
            articles=by_category[cat],
        ))

    # Catégories non listées dans CATEGORY_ORDER (au cas où)
    for cat, arts in by_category.items():
        if cat not in CATEGORY_ORDER:
            meta = CATEGORY_LABELS.get(cat, {"fr": cat.capitalize(), "emoji": "•"})
            categories.append(CategoryBlock(
                category=cat,
                label=meta["fr"],
                emoji=meta["emoji"],
                color=CATEGORY_COLORS.get(cat, "#444441"),
                articles=arts,
            ))

    total = sum(len(b.articles) for b in categories)
    log.info(f"Digest chargé : {total} articles, {len(categories)} catégories")

    return DigestData(
        digest_date=digest_date,
        global_summary=global_summary,
        categories=categories,
        total_articles=total,
    )


# ---------------------------------------------------------------------------
# Rendu HTML (pour email Gmail)
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NewsDigest — {date_display}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
         background: #f5f5f5; margin: 0; padding: 20px; color: #1a1a1a; }}
  .container {{ max-width: 680px; margin: 0 auto; background: #ffffff;
               border-radius: 12px; overflow: hidden; }}
  .header {{ background: #1a1a1a; padding: 32px 36px; }}
  .header h1 {{ color: #ffffff; margin: 0; font-size: 22px; font-weight: 500; }}
  .header p  {{ color: #9a9a9a; margin: 6px 0 0; font-size: 13px; }}
  .global-summary {{ background: #f9f9f9; padding: 24px 36px;
                     border-bottom: 1px solid #ebebeb; }}
  .global-summary p {{ margin: 0; font-size: 15px; line-height: 1.7;
                       color: #333; font-style: italic; }}
  .category {{ padding: 24px 36px; border-bottom: 1px solid #ebebeb; }}
  .category-title {{ display: flex; align-items: center; gap: 10px;
                     margin: 0 0 18px; }}
  .category-dot {{ width: 10px; height: 10px; border-radius: 50%;
                   flex-shrink: 0; }}
  .category-title h2 {{ margin: 0; font-size: 15px; font-weight: 600;
                        color: #1a1a1a; }}
  .article {{ margin-bottom: 20px; padding-bottom: 20px;
              border-bottom: 1px solid #f0f0f0; }}
  .article:last-child {{ margin-bottom: 0; padding-bottom: 0; border-bottom: none; }}
  .article-meta {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }}
  .article-source {{ font-size: 11px; font-weight: 500; color: #888;
                     text-transform: uppercase; letter-spacing: 0.05em; }}
  .article-lang {{ font-size: 10px; background: #f0f0f0; color: #666;
                   padding: 2px 6px; border-radius: 10px; }}
  .article a {{ font-size: 15px; font-weight: 500; color: #1a1a1a;
               text-decoration: none; line-height: 1.4; display: block;
               margin-bottom: 8px; }}
  .article a:hover {{ color: #444; }}
  .article-summary {{ font-size: 13px; color: #555; line-height: 1.65; margin: 0; }}
  .footer {{ padding: 20px 36px; background: #f9f9f9;
             border-top: 1px solid #ebebeb; }}
  .footer p {{ margin: 0; font-size: 12px; color: #aaa; text-align: center; }}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <h1>NewsDigest</h1>
    <p>{date_display} &mdash; {total_articles} articles résumés</p>
  </div>

  {global_summary_block}

  {categories_html}

  <div class="footer">
    <p>Généré automatiquement &bull; Sources : RSS &amp; GNews &bull; IA : Groq Llama 3</p>
  </div>

</div>
</body>
</html>
"""

GLOBAL_SUMMARY_BLOCK = """\
  <div class="global-summary">
    <p>{text}</p>
  </div>
"""

CATEGORY_BLOCK = """\
  <div class="category">
    <div class="category-title">
      <div class="category-dot" style="background:{color}"></div>
      <h2>{emoji} {label}</h2>
    </div>
    {articles_html}
  </div>
"""

ARTICLE_BLOCK = """\
    <div class="article">
      <div class="article-meta">
        <span class="article-source">{source}</span>
        <span class="article-lang">{language}</span>
      </div>
      <a href="{url}" target="_blank">{title}</a>
      <p class="article-summary">{ai_summary}</p>
    </div>
"""


def render_html(data: DigestData) -> str:
    """Génère le HTML complet du digest pour l'email."""
    from datetime import datetime
    try:
        dt = datetime.strptime(data.digest_date, "%Y-%m-%d")
        date_display = dt.strftime("%A %d %B %Y").capitalize()
    except ValueError:
        date_display = data.digest_date

    global_block = ""
    if data.global_summary:
        global_block = GLOBAL_SUMMARY_BLOCK.format(text=data.global_summary)

    categories_html = ""
    for block in data.categories:
        arts_html = ""
        for art in block.articles:
            arts_html += ARTICLE_BLOCK.format(
                source=art.source[:40],
                language=art.language.upper(),
                url=art.url,
                title=art.title,
                ai_summary=art.ai_summary,
            )
        categories_html += CATEGORY_BLOCK.format(
            color=block.color,
            emoji=block.emoji,
            label=block.label,
            articles_html=arts_html,
        )

    return HTML_TEMPLATE.format(
        date_display=date_display,
        total_articles=data.total_articles,
        global_summary_block=global_block,
        categories_html=categories_html,
    )


# ---------------------------------------------------------------------------
# Rendu texte (pour Telegram — Markdown V2 simplifié)
# ---------------------------------------------------------------------------

def render_telegram(data: DigestData) -> str:
    """
    Génère le message Telegram.
    Telegram limite à 4096 caractères par message — on découpe si nécessaire.
    Retourne une liste de messages à envoyer en séquence.
    """
    from datetime import datetime
    try:
        dt = datetime.strptime(data.digest_date, "%Y-%m-%d")
        date_display = dt.strftime("%d/%m/%Y")
    except ValueError:
        date_display = data.digest_date

    lines = []
    lines.append(f"📰 *NewsDigest — {date_display}*")
    lines.append(f"_{data.total_articles} articles résumés_\n")

    if data.global_summary:
        lines.append(f"_{data.global_summary}_\n")
        lines.append("─" * 30)

    for block in data.categories:
        lines.append(f"\n{block.emoji} *{block.label}*")
        for art in block.articles:
            lang_tag = f"[{art.language.upper()}]"
            lines.append(f"\n*{art.title}* {lang_tag}")
            lines.append(f"{art.ai_summary}")
            lines.append(f"🔗 {art.url}")

    lines.append("\n─" * 15)
    lines.append("_Généré par NewsDigest • Groq Llama 3_")

    full_text = "\n".join(lines)

    # Découpage en messages de max 4000 chars (marge de sécurité)
    chunks = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 4000:
            chunks.append(current.strip())
            current = line
        else:
            current += "\n" + line
    if current.strip():
        chunks.append(current.strip())

    return chunks


# ---------------------------------------------------------------------------
# Sauvegarde du HTML généré
# ---------------------------------------------------------------------------

def save_html(html: str, digest_date: str) -> Path:
    """Sauvegarde le HTML dans le dossier data/digests/."""
    output_dir = DB_PATH.parent / "digests"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"digest_{digest_date}.html"
    path.write_text(html, encoding="utf-8")
    log.info(f"HTML sauvegardé : {path}")
    return path


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def build(digest_date: str | None = None, preview: bool = False) -> tuple[str, list[str], DigestData | None]:
    """
    Construit le digest. Retourne (html, telegram_chunks, data).
    html et telegram_chunks sont vides si aucun article n'est disponible.
    """
    target_date = digest_date or date.today().isoformat()
    log.info("=" * 50)
    log.info(f"Construction du digest — {target_date}")
    log.info("=" * 50)

    conn = get_conn()
    data = load_digest_data(conn, target_date)
    conn.close()

    if not data:
        return "", [], None

    html = render_html(data)
    telegram_chunks = render_telegram(data)
    save_html(html, target_date)

    if preview:
        print("\n" + "=" * 60)
        print("APERÇU TELEGRAM")
        print("=" * 60)
        for i, chunk in enumerate(telegram_chunks, 1):
            print(f"\n--- Message {i}/{len(telegram_chunks)} ---")
            print(chunk)

    log.info(f"Digest prêt — HTML: {len(html)} chars, Telegram: {len(telegram_chunks)} message(s)")
    return html, telegram_chunks, data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Constructeur de digest NewsDigest")
    parser.add_argument("--date", type=str, help="Date YYYY-MM-DD (défaut: aujourd'hui)")
    parser.add_argument("--preview", action="store_true", help="Affiche le digest Telegram dans le terminal")
    args = parser.parse_args()
    build(digest_date=args.date, preview=args.preview)