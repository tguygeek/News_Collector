"""
extractor.py — Extraction du contenu complet des articles avec newspaper3k
Appelé par collector.py après la collecte RSS/GNews pour enrichir le résumé brut.

Ce module est optionnel — si newspaper3k échoue sur un article,
le résumé RSS (court) est conservé comme fallback. Le pipeline ne plante jamais.

Usage autonome (test) :
    python extractor.py https://example.com/article
"""

import logging
import time
from dataclasses import dataclass

log = logging.getLogger("extractor")

# Timeout réseau pour chaque article (secondes)
FETCH_TIMEOUT = 10

# Longueur minimale du texte extrait pour qu'il soit jugé valide (caractères)
MIN_TEXT_LENGTH = 200

# Longueur maximale stockée en base (caractères) — évite les textes géants
MAX_TEXT_LENGTH = 8000


# ---------------------------------------------------------------------------
# Résultat d'extraction
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    url: str
    full_text: str        # texte complet extrait
    top_image: str        # URL de l'image principale (optionnel)
    authors: list[str]    # auteurs détectés
    success: bool         # True si extraction réussie
    source: str           # "newspaper" | "rss_fallback" | "error"


# ---------------------------------------------------------------------------
# Extraction principale
# ---------------------------------------------------------------------------

def extract(url: str, rss_summary: str = "") -> ExtractionResult:
    """
    Tente d'extraire le contenu complet d'un article via newspaper3k.
    En cas d'échec, retourne le résumé RSS comme fallback.

    Args:
        url         : URL de l'article
        rss_summary : résumé court déjà extrait depuis le flux RSS (fallback)

    Returns:
        ExtractionResult avec le texte le plus riche disponible
    """
    try:
        from newspaper import Article as NewspaperArticle, Config as NpConfig

        # Configuration newspaper3k
        np_config = NpConfig()
        np_config.request_timeout = FETCH_TIMEOUT
        np_config.fetch_images = False       # pas besoin des images pour les résumés
        np_config.memoize_articles = False   # on ne met pas en cache
        np_config.browser_user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )

        article = NewspaperArticle(url, config=np_config)
        article.download()
        article.parse()

        text = (article.text or "").strip()

        # Validation : le texte doit être suffisamment long pour être utile
        if len(text) < MIN_TEXT_LENGTH:
            log.debug(f"Texte trop court ({len(text)} chars) — fallback RSS : {url[:60]}")
            return _fallback(url, rss_summary, "rss_fallback")

        # Troncature si le texte est très long
        if len(text) > MAX_TEXT_LENGTH:
            text = text[:MAX_TEXT_LENGTH] + "…"

        return ExtractionResult(
            url=url,
            full_text=text,
            top_image=article.top_image or "",
            authors=article.authors or [],
            success=True,
            source="newspaper",
        )

    except Exception as e:
        log.debug(f"Extraction échouée ({type(e).__name__}) — fallback RSS : {url[:60]}")
        return _fallback(url, rss_summary, "error")


def _fallback(url: str, rss_summary: str, source: str) -> ExtractionResult:
    """Retourne le résumé RSS comme contenu de secours."""
    return ExtractionResult(
        url=url,
        full_text=rss_summary,
        top_image="",
        authors=[],
        success=False,
        source=source,
    )


# ---------------------------------------------------------------------------
# Extraction en lot — pour le collector
# ---------------------------------------------------------------------------

def enrich_articles(
    articles: list[dict],
    delay: float = 0.5,
) -> list[dict]:
    """
    Enrichit une liste d'articles avec leur contenu full-text.
    Chaque article est un dict avec au minimum : url, summary.
    Retourne la même liste avec 'summary' enrichi si extraction réussie.

    Args:
        articles : liste de dicts article
        delay    : pause entre chaque requête (politesse réseau)
    """
    total = len(articles)
    success_count = 0

    for i, article in enumerate(articles, 1):
        url = article.get("url", "")
        rss_summary = article.get("summary", "")

        if not url:
            continue

        log.debug(f"  Extraction {i}/{total} : {url[:70]}")
        result = extract(url, rss_summary)

        if result.success:
            article["summary"] = result.full_text
            article["top_image"] = result.top_image
            article["authors"] = ", ".join(result.authors)
            success_count += 1
        # Si échec : summary reste le résumé RSS original — rien de perdu

        time.sleep(delay)

    log.info(f"Extraction full-text : {success_count}/{total} articles enrichis")
    return articles


# ---------------------------------------------------------------------------
# Point d'entrée — test sur une URL
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage : python extractor.py <URL>")
        print("Exemple : python extractor.py https://techcrunch.com/2026/04/24/some-article/")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s — %(message)s")

    url = sys.argv[1]
    print(f"\nExtraction de : {url}\n")

    result = extract(url)

    print(f"Succès      : {result.success}")
    print(f"Source      : {result.source}")
    print(f"Auteurs     : {result.authors}")
    print(f"Image       : {result.top_image[:80] if result.top_image else 'aucune'}")
    print(f"Longueur    : {len(result.full_text)} caractères")
    print(f"\n--- Début du texte ---")
    print(result.full_text[:600])
    print("---")