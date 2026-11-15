"""
RadioLog — core/i18n.py
=======================
Sistema de internacionalización minimalista.

Uso:
    from core.i18n import init_i18n, t

    # Al arrancar (después de cargar Config):
    init_i18n(config)           # o init_i18n_lang("en")

    # En cualquier parte de la UI:
    label.setText(t("logbook.title"))

Reglas:
  - Si la clave no existe en el idioma activo, se intenta español.
  - Si tampoco existe en español, se devuelve la clave tal cual.
  - Nunca lanza excepciones.
  - El cambio de idioma requiere reiniciar la app (sin traducción en caliente).

Valores válidos de ui.language:  "Español" | "English"
Códigos internos:                 "es"      | "en"
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Mapa nombre-display → código ISO ─────────────────────────────────────────
_LANG_MAP: dict[str, str] = {
    "Español":  "es",
    "español":  "es",
    "es":       "es",
    "Spanish":  "es",
    "English":  "en",
    "english":  "en",
    "en":       "en",
}

# ── Estado global del módulo ──────────────────────────────────────────────────
_strings:     dict[str, str] = {}   # idioma activo
_strings_es:  dict[str, str] = {}   # español como fallback
_active_lang: str = "es"


# ─────────────────────────────────────────────────────────────────────────────
# API pública
# ─────────────────────────────────────────────────────────────────────────────

def init_i18n(config) -> None:
    """
    Inicializa el sistema de idioma leyendo ui.language de la Config.
    Llamar una sola vez en main.py, después de instanciar Config.
    """
    raw = str(config.get("ui.language", "Español") or "Español")
    lang_code = _LANG_MAP.get(raw, "es")
    init_i18n_lang(lang_code)


def init_i18n_lang(lang_code: str) -> None:
    """
    Inicializa directamente con un código ISO ('es' o 'en').
    Útil para tests o para llamar sin un objeto Config.
    """
    global _strings, _strings_es, _active_lang

    _strings_es = _load_file("es")
    if lang_code == "es":
        _strings = _strings_es
    else:
        _strings = _load_file(lang_code)

    _active_lang = lang_code
    log.debug("i18n: idioma activo = %s  (%d claves)", lang_code, len(_strings))


def t(key: str, **kwargs) -> str:
    """
    Devuelve la traducción de `key` en el idioma activo.

    Orden de búsqueda:
      1. Idioma activo (_strings)
      2. Español como fallback (_strings_es)
      3. La propia clave como último recurso

    Acepta kwargs para interpolación básica:
        t("msg.hello", name="EA5YC")  →  "Hola, EA5YC"
        (requiere que el JSON tenga "msg.hello": "Hola, {name}")
    """
    text = _strings.get(key) or _strings_es.get(key) or key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, ValueError):
            pass   # devuelve el texto sin interpolar antes de romper
    return text


def active_lang() -> str:
    """Devuelve el código ISO del idioma activo ('es' o 'en')."""
    return _active_lang


def lang_display_name() -> str:
    """Devuelve el nombre de display del idioma activo ('Español', 'English'…)."""
    return _strings.get("_meta.name", _active_lang)


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────

def _lang_dir() -> Path:
    """
    Localiza la carpeta lang/ relativa al ejecutable.
    Funciona tanto en desarrollo como en bundle PyInstaller (sys._MEIPASS).
    """
    import sys
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        # PyInstaller bundle: los datos se extraen en sys._MEIPASS
        return Path(sys._MEIPASS) / "lang"
    # Desarrollo normal: lang/ está en la raíz del proyecto
    return Path(__file__).parent.parent / "lang"


def _load_file(lang_code: str) -> dict[str, str]:
    """Carga y devuelve el JSON de un idioma. Nunca lanza excepciones."""
    path = _lang_dir() / f"{lang_code}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Aplanar _meta como claves "_meta.xxx" para que t() también las encuentre
        flat: dict[str, str] = {}
        for k, v in data.items():
            if k == "_meta" and isinstance(v, dict):
                for mk, mv in v.items():
                    flat[f"_meta.{mk}"] = str(mv)
            elif isinstance(v, str):
                flat[k] = v
        return flat
    except FileNotFoundError:
        log.warning("i18n: archivo no encontrado: %s", path)
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        log.error("i18n: error cargando %s — %s", path, exc)
        return {}
