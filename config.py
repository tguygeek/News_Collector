"""
config.py — Chargement centralisé de la configuration depuis .env
Toutes les clés API et paramètres sont lus ici.
Chaque module importe uniquement ce dont il a besoin depuis config.

Usage dans les autres modules :
    from config import cfg
    print(cfg.GROQ_API_KEY)
    print(cfg.DIGEST_SEND_TIME)
"""

import os
import sys
import logging
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Chargement du .env — doit arriver AVANT tout os.getenv()
# ---------------------------------------------------------------------------
# Cherche le .env dans le dossier du fichier config.py,
# puis dans le dossier de travail courant (cwd), par ordre de priorité.
_candidates = [
    Path(__file__).resolve().parent / ".env",  # dossier du projet
    Path.cwd() / ".env",                        # dossier courant
]
_loaded = False
for _env_path in _candidates:
    if _env_path.exists():
        load_dotenv(dotenv_path=_env_path, override=True)
        _loaded = True
        break

if not _loaded:
    import warnings as _warnings
    _warnings.warn(
        "Fichier .env introuvable. "
        "Copie .env.example en .env et remplis tes clés.",
        stacklevel=2,
    )

log = logging.getLogger("config")

# ---------------------------------------------------------------------------
# Helpers de lecture  (appelés APRÈS load_dotenv — toujours safe)
# ---------------------------------------------------------------------------

def _require(key: str) -> str:
    """Lit une variable obligatoire. Lève une erreur claire si absente."""
    val = os.getenv(key, "").strip()
    if not val:
        raise EnvironmentError(
            f"\n[config] Variable manquante : {key}\n"
            f"  → Ajoute-la dans ton fichier .env\n"
            f"  → Exemple : {key}=ta_valeur_ici"
        )
    return val


def _optional(key: str, default: str = "") -> str:
    """Lit une variable optionnelle avec valeur par défaut."""
    return os.getenv(key, default).strip() or default


def _optional_int(key: str, default: int) -> int:
    """Lit une variable optionnelle comme entier."""
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning(f"[config] {key} invalide ('{raw}') — valeur par défaut : {default}")
        return default


def _is_placeholder(val: str) -> bool:
    """Détecte si une clé n'a pas encore été renseignée."""
    placeholders = ("VOTRE_", "YOUR_", "TODO", "CHANGEME")
    return not val or any(val.startswith(p) for p in placeholders)


# ---------------------------------------------------------------------------
# Classe de configuration — __init__ explicite, lecture lazy garantie
# ---------------------------------------------------------------------------
# Pourquoi pas @dataclass avec default_factory ?
# Les lambdas dans default_factory sont évaluées à l'instanciation de Config(),
# soit au moment de `cfg = Config()` en bas de ce module.  Si load_dotenv()
# n'a pas encore été appelé à cet instant (import précoce par un autre module),
# os.getenv() renvoie systématiquement des chaînes vides.
# Avec __init__ explicite placé APRÈS le bloc load_dotenv ci-dessus, les appels
# os.getenv() se produisent toujours APRÈS le chargement du .env. ✓

class Config:
    """Configuration centralisée — lue une fois à l'import de config.py."""

    def __init__(self) -> None:
        # --- Groq ---
        self.GROQ_API_KEY: str = _optional("GROQ_API_KEY")
        self.GROQ_MODEL: str   = _optional("GROQ_MODEL", "llama-3.1-8b-instant")

        # --- GNews ---
        self.GNEWS_API_KEY: str    = _optional("GNEWS_API_KEY")
        self.GNEWS_MAX_ARTICLES: int = _optional_int("GNEWS_MAX_ARTICLES", 10)

        # --- Gmail ---
        self.GMAIL_ADDRESS: str      = _optional("GMAIL_ADDRESS")
        self.GMAIL_APP_PASSWORD: str = _optional("GMAIL_APP_PASSWORD")

        # --- Telegram ---
        self.TELEGRAM_BOT_TOKEN: str = _optional("TELEGRAM_BOT_TOKEN")
        self.TELEGRAM_CHAT_ID: str   = _optional("TELEGRAM_CHAT_ID")

        # --- Pipeline ---
        self.DIGEST_SEND_TIME: str = _optional("DIGEST_SEND_TIME", "07:00")
        self.COLLECT_MINUTE: str   = _optional("COLLECT_MINUTE", "15")
        self.SEND_CHANNEL: str     = _optional("SEND_CHANNEL", "both")

        # --- Résumé IA ---
        self.TOP_N_PER_CATEGORY: int   = _optional_int("TOP_N_PER_CATEGORY", 3)
        self.DELAY_BETWEEN_CALLS: int  = _optional_int("DELAY_BETWEEN_CALLS", 2)

    # --- Propriétés de statut ---

    @property
    def groq_configured(self) -> bool:
        return bool(self.GROQ_API_KEY) and not _is_placeholder(self.GROQ_API_KEY)

    @property
    def gnews_configured(self) -> bool:
        return bool(self.GNEWS_API_KEY) and not _is_placeholder(self.GNEWS_API_KEY)

    @property
    def gmail_configured(self) -> bool:
        return (
            bool(self.GMAIL_ADDRESS)
            and not _is_placeholder(self.GMAIL_ADDRESS)
            and bool(self.GMAIL_APP_PASSWORD)
            and not _is_placeholder(self.GMAIL_APP_PASSWORD)
        )

    @property
    def telegram_configured(self) -> bool:
        return (
            bool(self.TELEGRAM_BOT_TOKEN)
            and not _is_placeholder(self.TELEGRAM_BOT_TOKEN)
            and bool(self.TELEGRAM_CHAT_ID)
            and not _is_placeholder(self.TELEGRAM_CHAT_ID)
        )

    def print_status(self) -> None:
        """Affiche l'état de configuration de chaque service."""
        ok, nok = "✓", "✗"
        print("\n" + "─" * 45)
        print("  Configuration NewsDigest")
        print("─" * 45)
        print(f"  {ok if self.groq_configured     else nok}  Groq API       (résumé IA)")
        print(f"  {ok if self.gnews_configured    else nok}  GNews API      (collecte articles)")
        print(f"  {ok if self.gmail_configured    else nok}  Gmail SMTP     (envoi email)")
        print(f"  {ok if self.telegram_configured else nok}  Telegram Bot   (envoi Telegram)")
        print("─" * 45)
        print(f"  Pipeline  : digest à {self.DIGEST_SEND_TIME}, collecte à :{self.COLLECT_MINUTE}")
        print(f"  Canal     : {self.SEND_CHANNEL}")
        print(f"  Top N/cat : {self.TOP_N_PER_CATEGORY} articles résumés par catégorie")
        print("─" * 45 + "\n")

    def validate_for_run(self) -> list[str]:
        """
        Vérifie que la config est suffisante pour lancer le pipeline.
        Retourne une liste d'avertissements (vide = tout est OK).
        """
        warns: list[str] = []
        if not self.groq_configured:
            warns.append("Groq non configuré — résumés IA désactivés")
        if not self.gnews_configured:
            warns.append("GNews non configuré — collecte GNews désactivée")
        if not self.gmail_configured and not self.telegram_configured:
            warns.append("Aucun canal d'envoi configuré — le digest ne sera pas envoyé")
        return warns


# ---------------------------------------------------------------------------
# Instance globale — importée par tous les modules
# ---------------------------------------------------------------------------
# À ce stade, load_dotenv() a déjà été appelé plus haut dans ce fichier.
# __init__ lit os.getenv() maintenant → valeurs garanties correctes.

cfg = Config()


# ---------------------------------------------------------------------------
# Point d'entrée direct — affiche l'état de la config
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _env_found = any(p.exists() for p in _candidates)
    if not _env_found:
        print("\n  Fichier .env introuvable.")
        print("  Copie .env.example en .env et remplis les valeurs.\n")
        sys.exit(1)

    cfg.print_status()

    run_warnings = cfg.validate_for_run()
    if run_warnings:
        print("  Avertissements :")
        for w in run_warnings:
            print(f"    ⚠  {w}")
        print()