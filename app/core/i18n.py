"""Fixed UI strings. Written by people and never generated at runtime.

Only English exists so far. Yoruba and Pidgin strings are added in milestone 3 after
native-speaker review; until then every language falls back to English.
"""
from app.models import Language

STRINGS: dict[str, dict[str, str]] = {
    "qa.fallback": {
        Language.EN: "I couldn't find a reliable answer to that in the Ajo library.\n"
                     "Do you want to post it to your circle?",
    },
    "qa.source_line": {Language.EN: "Source: {citation}"},
    "qa.show_source": {Language.EN: "📖 Show source"},
    "qa.show_source_n": {Language.EN: "📖 Source {number}"},
    "qa.ask_circle": {Language.EN: "Ask my circle"},
    "qa.no_thanks": {Language.EN: "No thanks"},
    "qa.posted_to_circle": {Language.EN: "Posted. Your circle will see your question in their next update."},
    "qa.already_posted": {Language.EN: "You already posted this question to your circle."},
    "qa.not_postable": {Language.EN: "That question can't be posted to your circle."},
    "qa.not_in_circle": {Language.EN: "You're not in a circle yet, so there's no one to ask."},
    "qa.dismissed": {Language.EN: "Okay. Ask me anything else about Biology."},
    "qa.source_unavailable": {Language.EN: "That source is no longer available."},
}


def t(key: str, language: Language | str = Language.EN, **values: object) -> str:
    variants = STRINGS[key]
    template = variants.get(Language(language)) or variants[Language.EN]
    return template.format(**values) if values else template
