"""The legal pages: privacy policy and terms of use, served as static HTML.

Same delivery as the landing (see `server.py`): plain documents, no Streamlit
boot, one per language via `?lang=es`. The copy lives here rather than in the
locale JSON because it is page-length prose that changes as a unit — a stamped
`LAST_UPDATED` date and two parallel translations are easier to keep honest in
one file than sprayed across catalogs.

The one line that matters most legally — this is not investment advice — also
lives in the landing footer (landing.footer_disclaimer) and is restated here
in full.
"""

from __future__ import annotations

from stocks.web.i18n import DEFAULT_LANG, LANGUAGES
from stocks.web.markup import esc

PATH_PRIVACY = "/legal/privacy"
PATH_TERMS = "/legal/terms"

LAST_UPDATED = "2026-08-31"

_CONTACT = "https://github.com/ignasi-sant/stocks/issues"

# ------------------------------------------------------------------- copy
# {doc: {lang: (title, [(heading, [paragraphs...]), ...])}}

_PRIVACY = {
    "en": (
        "Privacy policy",
        [
            ("What this is", [
                "TopStocks is an open-source, non-commercial portfolio tracker "
                "operated by an individual. This page explains what data the "
                "hosted app stores and why. Questions and requests: open an "
                f"issue at {_CONTACT}.",
            ]),
            ("Data we store", [
                "Account identity: when you sign in with Google we receive your "
                "email address, display name and profile picture. The email "
                "keys your data folder; nothing else is read from your Google "
                "account.",
                "Your data: the watchlist, imported transactions, preferences "
                "and assistant conversations you create in the app. They are "
                "stored in a private object-storage bucket and are never "
                "visible to other users.",
                "Server logs: page views and errors, tagged with a pseudonymous "
                "account slug, kept for about 30 days for debugging. Periodic "
                "backups of the storage bucket are kept for about 30 days.",
            ]),
            ("Cookies", [
                "One first-party cookie (ts_app) remembers that your browser "
                "has used the app, so the address bar goes to the app instead "
                "of the marketing page. Signing in sets the session cookies "
                "the login needs. There are no advertising or cross-site "
                "tracking cookies and no third-party analytics.",
            ]),
            ("Third parties", [
                "Market data is fetched from public market-data providers; "
                "those requests carry ticker symbols, never your identity.",
                "If you use the AI assistant, the text of your messages (plus "
                "the portfolio context needed to answer) is sent to the "
                "configured model provider for that reply. If you bring your "
                "own API key it is stored encrypted.",
                "If you link Telegram, your chat id is stored so notifications "
                "you enabled can be sent to you.",
                "Your data is never sold and never used for advertising.",
            ]),
            ("Your rights and deletion", [
                "You can delete your account and all stored data yourself "
                "from Profile → Preferences → Delete account. Deletion removes "
                "your data folder and its cloud copies; log lines and backup "
                "snapshots expire on their own schedule (about 30 days).",
            ]),
        ],
    ),
    "es": (
        "Política de privacidad",
        [
            ("Qué es esto", [
                "TopStocks es un rastreador de carteras de código abierto y "
                "sin ánimo comercial, operado por un particular. Esta página "
                "explica qué datos almacena la app y por qué. Preguntas y "
                f"solicitudes: abre una incidencia en {_CONTACT}.",
            ]),
            ("Datos que almacenamos", [
                "Identidad de la cuenta: al iniciar sesión con Google recibimos "
                "tu correo, tu nombre y tu foto de perfil. El correo identifica "
                "tu carpeta de datos; no se lee nada más de tu cuenta de Google.",
                "Tus datos: la watchlist, las transacciones importadas, las "
                "preferencias y las conversaciones con el asistente que crees "
                "en la app. Se guardan en un bucket privado de almacenamiento "
                "de objetos y ningún otro usuario puede verlos.",
                "Registros del servidor: páginas vistas y errores, etiquetados "
                "con un identificador seudónimo de cuenta, conservados unos "
                "30 días para depuración. Las copias de seguridad periódicas "
                "del bucket se conservan unos 30 días.",
            ]),
            ("Cookies", [
                "Una cookie propia (ts_app) recuerda que tu navegador ya usó "
                "la app, para llevarte a ella en vez de a la página de "
                "presentación. Iniciar sesión crea las cookies de sesión que "
                "el login necesita. No hay cookies publicitarias ni de "
                "seguimiento entre sitios, ni analítica de terceros.",
            ]),
            ("Terceros", [
                "Los datos de mercado se obtienen de proveedores públicos; "
                "esas peticiones llevan símbolos de cotización, nunca tu "
                "identidad.",
                "Si usas el asistente de IA, el texto de tus mensajes (más el "
                "contexto de cartera necesario para responder) se envía al "
                "proveedor de modelo configurado para esa respuesta. Si "
                "aportas tu propia clave de API, se guarda cifrada.",
                "Si vinculas Telegram, se guarda tu chat id para poder "
                "enviarte las notificaciones que actives.",
                "Tus datos nunca se venden ni se usan para publicidad.",
            ]),
            ("Tus derechos y la eliminación", [
                "Puedes borrar tu cuenta con todos los datos "
                "desde Perfil → Preferencias → Eliminar cuenta. El borrado "
                "elimina tu carpeta de datos y sus copias en la nube; los "
                "registros y las instantáneas de respaldo caducan por sí "
                "solos (unos 30 días).",
            ]),
        ],
    ),
}

_TERMS = {
    "en": (
        "Terms of use",
        [
            ("Not investment advice", [
                "TopStocks is an informational tool. Nothing in the app — "
                "prices, indicators, screeners, analyses or AI assistant "
                "replies — is investment advice, tax advice or a "
                "recommendation to buy or sell any security. Investment "
                "decisions and their consequences are yours alone.",
            ]),
            ("Data accuracy", [
                "Market data comes from third-party sources and may be "
                "delayed, incomplete or wrong. Past performance does not "
                "predict future results. Verify anything that matters against "
                "your broker or an official source before acting on it.",
            ]),
            ("The service", [
                "The app is open source (MIT licence) and provided free, "
                "as-is and as-available, without warranty of any kind. To the "
                "extent permitted by law, the operator is not liable for any "
                "loss arising from use of the app, including investment "
                "losses and data loss. The service may change or stop at any "
                "time.",
            ]),
            ("Your account", [
                "Use the app lawfully and don't abuse it (no scraping the "
                "authenticated app, probing other accounts, or overloading "
                "the service). Accounts that abuse the service can be "
                "removed. You can delete your account and data at any time "
                "from the Profile page.",
            ]),
            ("Changes", [
                "These terms may be updated; the date below reflects the "
                "latest revision. Continued use after a change means you "
                "accept the updated terms.",
            ]),
        ],
    ),
    "es": (
        "Condiciones de uso",
        [
            ("No es asesoramiento de inversión", [
                "TopStocks es una herramienta informativa. Nada en la app — "
                "precios, indicadores, cribadores, análisis o respuestas del "
                "asistente de IA — constituye asesoramiento de inversión ni "
                "fiscal, ni una recomendación de compra o venta de ningún "
                "valor. Las decisiones de inversión y sus consecuencias son "
                "únicamente tuyas.",
            ]),
            ("Exactitud de los datos", [
                "Los datos de mercado provienen de terceros y pueden llegar "
                "con retraso, incompletos o con errores. Las rentabilidades "
                "pasadas no predicen resultados futuros. Verifica cualquier "
                "dato importante con tu bróker o una fuente oficial antes de "
                "actuar.",
            ]),
            ("El servicio", [
                "La app es de código abierto (licencia MIT) y se ofrece "
                "gratis, tal cual y según disponibilidad, sin garantía de "
                "ningún tipo. En la medida en que la ley lo permita, el "
                "operador no responde de pérdidas derivadas del uso de la "
                "app, incluidas pérdidas de inversión y de datos. El servicio "
                "puede cambiar o cesar en cualquier momento.",
            ]),
            ("Tu cuenta", [
                "Usa la app de forma legal y sin abusos (nada de extraer "
                "datos de la app autenticada, sondear otras cuentas o "
                "sobrecargar el servicio). Las cuentas que abusen del "
                "servicio pueden ser eliminadas. Puedes borrar tu cuenta y "
                "tus datos en cualquier momento desde la página de Perfil.",
            ]),
            ("Cambios", [
                "Estas condiciones pueden actualizarse; la fecha de abajo "
                "refleja la última revisión. Seguir usando la app tras un "
                "cambio implica aceptar las condiciones actualizadas.",
            ]),
        ],
    ),
}

_UI = {
    "en": {"updated": "Last updated", "home": "Back to TopStocks",
           "privacy": "Privacy policy", "terms": "Terms of use"},
    "es": {"updated": "Última actualización", "home": "Volver a TopStocks",
           "privacy": "Política de privacidad", "terms": "Condiciones de uso"},
}

# Matches the landing's dark surface (seo.THEME_COLOR family) without pulling
# in its whole stylesheet: legal pages should cost one small response.
_CSS = """
:root { color-scheme: dark; }
body { margin: 0; background: #18161C; color: #E8E5EF;
       font: 16px/1.65 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 44rem; margin: 0 auto; padding: 3rem 1.25rem 4rem; }
h1 { font-size: 1.7rem; line-height: 1.25; margin: 0 0 0.4rem; }
h2 { font-size: 1.1rem; margin: 2rem 0 0.5rem; }
p { margin: 0.6rem 0; color: #C9C4D4; }
a { color: #9AD1FF; }
nav { display: flex; gap: 1rem; flex-wrap: wrap; margin-top: 2.5rem;
      padding-top: 1rem; border-top: 1px solid #2C2933; font-size: 0.9rem; }
.updated { color: #8E88A0; font-size: 0.85rem; margin-bottom: 1.5rem; }
a:focus-visible { outline: 2px solid #9AD1FF; outline-offset: 2px; }
"""


def _linkify(text: str) -> str:
    """Escape a paragraph, then re-link the one URL the copy may contain."""
    out = esc(text)
    if _CONTACT in text:
        url = esc(_CONTACT)
        out = out.replace(url, f'<a href="{url}">{url}</a>')
    return out


def document(doc: str, lang: str) -> str:
    """The full HTML document for `doc` ("privacy" | "terms") in `lang`."""
    lang = lang if lang in LANGUAGES else DEFAULT_LANG
    title, sections = (_PRIVACY if doc == "privacy" else _TERMS)[lang]
    ui = _UI[lang]

    body = "".join(
        f"<h2>{esc(heading)}</h2>"
        + "".join(f"<p>{_linkify(p)}</p>" for p in paragraphs)
        for heading, paragraphs in sections
    )
    other = PATH_TERMS if doc == "privacy" else PATH_PRIVACY
    other_label = ui["terms"] if doc == "privacy" else ui["privacy"]
    lang_q = "?lang=es" if lang == "es" else ""

    return (
        "<!doctype html>"
        f'<html lang="{lang}"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{esc(title)} — TopStocks</title>"
        f"<style>{_CSS}</style>"
        "</head><body><main>"
        f"<h1>{esc(title)}</h1>"
        f'<p class="updated">{esc(ui["updated"])}: {LAST_UPDATED}</p>'
        f"{body}"
        "<nav>"
        f'<a href="/">{esc(ui["home"])}</a>'
        f'<a href="{other}{lang_q}">{esc(other_label)}</a>'
        "</nav>"
        "</main></body></html>"
    )
