"""Fixed UI strings. Written by people and never generated at runtime (spec section 11).

Every key has English plus Yoruba and Nigerian Pidgin drafts. A language's strings are shown
only once a native speaker has reviewed them and the language is listed in REVIEWED; until
then its learners see English UI, while their QA answers and lessons are still generated in
their language. Print a review sheet for a reviewer with:

    uv run python -m app.core.i18n --review yo
"""
import argparse
import string
import sys

from app.models import Language

# Languages whose drafts a native speaker has checked. Add a language here, with the reviewer
# and date in the comment, only after its review sheet has been signed off.
REVIEWED: frozenset[Language] = frozenset({Language.EN})

# Endonyms, so each learner can find their own language whatever the current UI language.
LANGUAGE_NAMES: dict[Language, str] = {
    Language.EN: "English",
    Language.YO: "Yorùbá",
    Language.PCM: "Naijá (Pidgin)",
}

STRINGS: dict[str, dict[Language, str]] = {
    # Onboarding (spec section 7.1)
    "onboarding.intro": {
        Language.EN: "👋 Welcome to Ajo, a study circle for JAMB Biology.\n"
                     "Every day you get a short lesson and two questions.\n"
                     "Your circle keeps each other going, and you can ask me Biology questions any time.",
        Language.YO: "👋 Káàbọ̀ sí Ajo, ẹgbẹ́ ìkẹ́kọ̀ọ́ fún JAMB Biology.\n"
                     "Lójoojúmọ́ o máa gba ẹ̀kọ́ kúkúrú àti ìbéèrè méjì.\n"
                     "Ẹgbẹ́ rẹ máa ń ti ara wọn lẹ́yìn, o sì lè bi mí ní ìbéèrè Biology nígbàkígbà.",
        Language.PCM: "👋 Welcome to Ajo, study circle for JAMB Biology.\n"
                      "Every day you go get short lesson and two questions.\n"
                      "Your circle go dey push each other, and you fit ask me Biology question anytime.",
    },
    "onboarding.consent": {
        Language.EN: "Before we start, here is how Ajo uses your data:\n"
                     "• We store your first name, language, goal, answers and questions. Your account ID is "
                     "stored only in scrambled (hashed) form, and your chat ID only so we can message you.\n"
                     "• Your circle sees only your first name and whether you checked in. Your scores stay "
                     "private unless you turn on /share_scores.\n"
                     "• Answers come from an AI service that never receives your name or ID.\n"
                     "• Send /delete_me at any time to delete your data.",
        Language.YO: "Kí a tó bẹ̀rẹ̀, báyìí ni Ajo ṣe ń lo ìwífún rẹ:\n"
                     "• A máa ń tọ́jú orúkọ àkọ́kọ́ rẹ, èdè rẹ, àfojúsùn rẹ, ìdáhùn àti ìbéèrè rẹ. A máa ń "
                     "tọ́jú ID àkáǹtì rẹ ní ọ̀nà tí a ti dà rú (hash) nìkan, àti ID ìfọ̀rọ̀wérọ̀ rẹ kí a lè "
                     "fi ránṣẹ́ sí ọ.\n"
                     "• Ẹgbẹ́ rẹ máa rí orúkọ àkọ́kọ́ rẹ nìkan àti bóyá o ti wọlé. Máàkì rẹ jẹ́ àṣírí àyàfi "
                     "tí o bá tan /share_scores.\n"
                     "• Ìdáhùn máa ń wá láti iṣẹ́ AI tí kì í gba orúkọ tàbí ID rẹ rárá.\n"
                     "• Fi /delete_me ránṣẹ́ nígbàkígbà láti pa ìwífún rẹ rẹ́.",
        Language.PCM: "Before we start, na so Ajo dey use your data:\n"
                      "• We dey keep your first name, language, goal, answers and questions. Your account ID "
                      "we dey keep only as scrambled (hashed) code, and your chat ID only so we fit message you.\n"
                      "• Your circle go see only your first name and whether you don check in. Your score na "
                      "your own secret unless you on /share_scores.\n"
                      "• Answers dey come from AI service wey no dey see your name or ID.\n"
                      "• Send /delete_me anytime to delete your data.",
    },
    "onboarding.agree": {Language.EN: "I agree", Language.YO: "Mo gbà", Language.PCM: "I gree"},
    "onboarding.consent_required": {
        Language.EN: "Tap \"I agree\" when you're ready. Ajo stores nothing about you until you do.",
        Language.YO: "Tẹ \"Mo gbà\" nígbà tí o bá ṣetán. Ajo kò ní tọ́jú nǹkan kan nípa rẹ títí o fi ṣe bẹ́ẹ̀.",
        Language.PCM: "Tap \"I gree\" when you ready. Ajo no go keep anything about you until you do am.",
    },
    "onboarding.choose_language": {
        Language.EN: "Which language do you prefer?",
        Language.YO: "Èdè wo ni o fẹ́ràn?",
        Language.PCM: "Which language you like?",
    },
    "onboarding.ask_name": {
        Language.EN: "What is your first name? Your circle will see it.",
        Language.YO: "Kí ni orúkọ àkọ́kọ́ rẹ? Ẹgbẹ́ rẹ máa rí i.",
        Language.PCM: "Wetin be your first name? Your circle go see am.",
    },
    "onboarding.invalid_name": {
        Language.EN: "Please send only your first name, in letters, from {min} to {max} characters.",
        Language.YO: "Jọ̀wọ́ fi orúkọ àkọ́kọ́ rẹ nìkan ránṣẹ́, ní lẹ́tà, láti ẹyọ {min} sí {max}.",
        Language.PCM: "Abeg send only your first name, for letters, from {min} reach {max} characters.",
    },
    "onboarding.choose_goal": {
        Language.EN: "What are you studying for?",
        Language.YO: "Kí ni o ń kàwé fún?",
        Language.PCM: "Wetin you dey study for?",
    },
    "onboarding.no_goals": {
        Language.EN: "Ajo's library isn't ready yet. Please try again later.",
        Language.YO: "Ibi ìkàwé Ajo kò tíì ṣetán. Jọ̀wọ́ gbìyànjú lẹ́ẹ̀kan sí i ní àsìkò mìíràn.",
        Language.PCM: "Ajo library never ready. Abeg try again later.",
    },
    "onboarding.joined": {
        Language.EN: "You're in, {name}! Lessons start at a medium level and adapt as you go. "
                     "Every lesson is open to you, with no test to pass first.",
        Language.YO: "O ti wọlé, {name}! Ẹ̀kọ́ máa bẹ̀rẹ̀ ní ìpele àárín, yóò sì máa yí padà bí o ṣe ń "
                     "tẹ̀síwájú. Gbogbo ẹ̀kọ́ ló ṣí sílẹ̀ fún ọ, kò sí ìdánwò kankan láti kọ́kọ́ ṣe.",
        Language.PCM: "You don enter, {name}! Lessons go start for medium level and e go adjust as you dey go. "
                      "Every lesson dey open for you, no test to pass first.",
    },
    "onboarding.already_done": {
        Language.EN: "Welcome back, {name}! You're already in your circle.",
        Language.YO: "Káàbọ̀ padà, {name}! O ti wà nínú ẹgbẹ́ rẹ tẹ́lẹ̀.",
        Language.PCM: "Welcome back, {name}! You don already dey your circle.",
    },
    "onboarding.start_first": {
        Language.EN: "Send /start to join Ajo.",
        Language.YO: "Fi /start ránṣẹ́ láti dara pọ̀ mọ́ Ajo.",
        Language.PCM: "Send /start to join Ajo.",
    },
    "circle.welcome": {
        Language.EN: "🔵 Welcome to {circle}\nMembers: {names}",
        Language.YO: "🔵 Káàbọ̀ sí {circle}\nỌmọ ẹgbẹ́: {names}",
        Language.PCM: "🔵 Welcome to {circle}\nMembers: {names}",
    },
    "circle.welcome_alone": {
        Language.EN: "🔵 Welcome to {circle}\nYou're the first member. Others will join soon.",
        Language.YO: "🔵 Káàbọ̀ sí {circle}\nIwọ ni ọmọ ẹgbẹ́ àkọ́kọ́. Àwọn mìíràn máa darapọ̀ láìpẹ́.",
        Language.PCM: "🔵 Welcome to {circle}\nNa you be the first member. Others go join soon.",
    },
    "circle.member_left": {
        Language.EN: "A member left the circle.",
        Language.YO: "Ọmọ ẹgbẹ́ kan ti kúrò nínú ẹgbẹ́.",
        Language.PCM: "One member don comot from the circle.",
    },
    "help": {
        Language.EN: "Here's what you can do:\n"
                     "• /lesson: today's lesson and questions\n"
                     "• Type any Biology question, or use /ask followed by your question\n"
                     "• /language: change your language\n"
                     "• /share_scores: choose whether your circle sees your scores\n"
                     "• /delete_me: delete your data",
        Language.YO: "Ohun tí o lè ṣe nìyí:\n"
                     "• /lesson: ẹ̀kọ́ àti ìbéèrè òní\n"
                     "• Kọ ìbéèrè Biology èyíkéyìí, tàbí lo /ask pẹ̀lú ìbéèrè rẹ\n"
                     "• /language: yí èdè rẹ padà\n"
                     "• /share_scores: yàn bóyá ẹgbẹ́ rẹ máa rí máàkì rẹ\n"
                     "• /delete_me: pa ìwífún rẹ rẹ́",
        Language.PCM: "Na wetin you fit do be dis:\n"
                      "• /lesson: today lesson and questions\n"
                      "• Type any Biology question, or use /ask with your question\n"
                      "• /language: change your language\n"
                      "• /share_scores: choose whether your circle go see your score\n"
                      "• /delete_me: delete your data",
    },
    # Account commands
    "language.changed": {
        Language.EN: "Done. Your language is now {language_name}.",
        Language.YO: "Ó ti parí. Èdè rẹ báyìí ni {language_name}.",
        Language.PCM: "E don set. Your language na {language_name} now.",
    },
    "language.unreviewed": {
        Language.EN: "Menus stay in English until a native speaker has checked the {language_name} translation. "
                     "Answers will still come in {language_name}.",
        Language.YO: "Àwọn àkọlé máa wà ní èdè Gẹ̀ẹ́sì títí ẹni tí èdè {language_name} jẹ́ èdè abínibí rẹ̀ yóò fi "
                     "ṣàyẹ̀wò ìtúmọ̀ rẹ̀. Ìdáhùn ṣì máa wá ní {language_name}.",
        Language.PCM: "Menu go still dey for English until person wey sabi {language_name} well-well check the "
                      "translation. Answers go still come for {language_name}.",
    },
    "scores.status_private": {
        Language.EN: "Your scores are private: your circle sees only that you checked in.",
        Language.YO: "Máàkì rẹ jẹ́ àṣírí: ẹgbẹ́ rẹ máa rí pé o wọlé nìkan.",
        Language.PCM: "Your score na secret: your circle go only see say you check in.",
    },
    "scores.status_shared": {
        Language.EN: "Your circle can see your scores.",
        Language.YO: "Ẹgbẹ́ rẹ lè rí máàkì rẹ.",
        Language.PCM: "Your circle fit see your score.",
    },
    "scores.share": {Language.EN: "Share my scores", Language.YO: "Pín máàkì mi", Language.PCM: "Share my score"},
    "scores.keep_private": {
        Language.EN: "Keep them private",
        Language.YO: "Jẹ́ kí ó jẹ́ àṣírí",
        Language.PCM: "Make e be secret",
    },
    "delete.confirm": {
        Language.EN: "This deletes your name, answers, questions and chat ID from Ajo and removes you from "
                     "your circle. Your circle will see \"A member left the circle.\" This can't be undone.\n"
                     "Delete everything?",
        Language.YO: "Èyí máa pa orúkọ rẹ, ìdáhùn rẹ, ìbéèrè rẹ àti ID ìfọ̀rọ̀wérọ̀ rẹ rẹ́ kúrò nínú Ajo, yóò "
                     "sì yọ ọ́ kúrò nínú ẹgbẹ́ rẹ. Ẹgbẹ́ rẹ máa rí \"Ọmọ ẹgbẹ́ kan ti kúrò nínú ẹgbẹ́.\" A kò lè "
                     "yí èyí padà.\nṢé kí a pa gbogbo rẹ̀ rẹ́?",
        Language.PCM: "Dis one go delete your name, answers, questions and chat ID from Ajo and remove you from "
                      "your circle. Your circle go see \"One member don comot from the circle.\" You no fit "
                      "undo am.\nMake we delete everything?",
    },
    "delete.yes": {
        Language.EN: "Yes, delete my data",
        Language.YO: "Bẹ́ẹ̀ni, pa ìwífún mi rẹ́",
        Language.PCM: "Yes, delete my data",
    },
    "delete.cancel": {Language.EN: "Cancel", Language.YO: "Fagilé", Language.PCM: "Cancel"},
    "delete.done": {
        Language.EN: "Your data has been deleted. Send /start if you ever want to come back.",
        Language.YO: "A ti pa ìwífún rẹ rẹ́. Fi /start ránṣẹ́ tí o bá fẹ́ padà wá.",
        Language.PCM: "We don delete your data. Send /start if you wan come back.",
    },
    "delete.cancelled": {
        Language.EN: "Okay, nothing was deleted.",
        Language.YO: "Ó dáa, a kò pa nǹkan kan rẹ́.",
        Language.PCM: "Okay, we no delete anything.",
    },
    "delete.nothing_stored": {
        Language.EN: "Ajo has no data stored for you.",
        Language.YO: "Ajo kò ní ìwífún kankan nípa rẹ.",
        Language.PCM: "Ajo no get any data for you.",
    },
    # General
    "ask.usage": {
        Language.EN: "Type your question after /ask, for example: /ask What does the kidney do?",
        Language.YO: "Kọ ìbéèrè rẹ lẹ́yìn /ask, fún àpẹẹrẹ: /ask Kí ni kídìnrín ń ṣe?",
        Language.PCM: "Type your question after /ask, like: /ask Wetin kidney dey do?",
    },
    "command.unknown": {
        Language.EN: "I don't know that command.",
        Language.YO: "Mi ò mọ àṣẹ yẹn.",
        Language.PCM: "I no sabi that command.",
    },
    "command.coming_soon": {
        Language.EN: "That feature isn't ready yet.",
        Language.YO: "Ẹ̀yà yẹn kò tíì ṣetán.",
        Language.PCM: "That one never ready.",
    },
    "message.text_only": {
        Language.EN: "For now I can only read text messages.",
        Language.YO: "Fún báyìí, ọ̀rọ̀ tí a kọ sílẹ̀ nìkan ni mo lè kà.",
        Language.PCM: "For now na only text message I fit read.",
    },
    "button.expired": {
        Language.EN: "That button has expired.",
        Language.YO: "Bọ́tìnnì yẹn ti kọjá àsìkò.",
        Language.PCM: "That button don expire.",
    },
    "error.generic": {
        Language.EN: "Sorry, something went wrong. Please try again.",
        Language.YO: "Má bínú, nǹkan kan ṣẹlẹ̀. Jọ̀wọ́ gbìyànjú lẹ́ẹ̀kan sí i.",
        Language.PCM: "Sorry, something spoil. Abeg try again.",
    },
    # Question answering (spec section 7.4)
    "qa.fallback": {
        Language.EN: "I couldn't find a reliable answer to that in the Ajo library.\n"
                     "Do you want to post it to your circle?",
        Language.YO: "Mi ò rí ìdáhùn tí ó ṣeé gbẹ́kẹ̀lé sí ìyẹn nínú ibi ìkàwé Ajo.\n"
                     "Ṣé o fẹ́ fi ránṣẹ́ sí ẹgbẹ́ rẹ?",
        Language.PCM: "I no fit find correct answer for that one for Ajo library.\n"
                      "You wan post am give your circle?",
    },
    "qa.source_line": {Language.EN: "Source: {citation}", Language.YO: "Orísun: {citation}",
                       Language.PCM: "Source: {citation}"},
    "qa.show_source": {Language.EN: "📖 Show source", Language.YO: "📖 Fi orísun hàn",
                       Language.PCM: "📖 Show source"},
    "qa.show_source_n": {Language.EN: "📖 Source {number}", Language.YO: "📖 Orísun {number}",
                         Language.PCM: "📖 Source {number}"},
    "qa.ask_circle": {Language.EN: "Ask my circle", Language.YO: "Bi ẹgbẹ́ mi", Language.PCM: "Ask my circle"},
    "qa.no_thanks": {Language.EN: "No thanks", Language.YO: "Rárá o ṣé", Language.PCM: "No, thank you"},
    "qa.posted_to_circle": {
        Language.EN: "Posted. Your circle will see your question in their next update.",
        Language.YO: "A ti fi ránṣẹ́. Ẹgbẹ́ rẹ máa rí ìbéèrè rẹ nínú ìròyìn wọn tó kàn.",
        Language.PCM: "E don post. Your circle go see your question for their next update.",
    },
    "qa.already_posted": {
        Language.EN: "You already posted this question to your circle.",
        Language.YO: "O ti fi ìbéèrè yìí ránṣẹ́ sí ẹgbẹ́ rẹ tẹ́lẹ̀.",
        Language.PCM: "You don already post this question give your circle.",
    },
    "qa.not_postable": {
        Language.EN: "That question can't be posted to your circle.",
        Language.YO: "A kò lè fi ìbéèrè yẹn ránṣẹ́ sí ẹgbẹ́ rẹ.",
        Language.PCM: "We no fit post that question give your circle.",
    },
    "qa.not_in_circle": {
        Language.EN: "You're not in a circle yet, so there's no one to ask.",
        Language.YO: "O kò tíì wà nínú ẹgbẹ́ kankan, nítorí náà kò sí ẹni tí o lè bi.",
        Language.PCM: "You never dey any circle, so nobody dey to ask.",
    },
    "qa.dismissed": {
        Language.EN: "Okay. Ask me anything else about Biology.",
        Language.YO: "Ó dáa. Bi mí ní ohunkóhun mìíràn nípa Biology.",
        Language.PCM: "Okay. Ask me any other thing about Biology.",
    },
    "qa.source_unavailable": {
        Language.EN: "That source is no longer available.",
        Language.YO: "Orísun yẹn kò sí mọ́.",
        Language.PCM: "That source no dey again.",
    },
    # Daily lessons (spec section 7.2)
    "lesson.header": {Language.EN: "📘 Day {day}: {topic}", Language.YO: "📘 Ọjọ́ {day}: {topic}",
                      Language.PCM: "📘 Day {day}: {topic}"},
    "lesson.example": {Language.EN: "Example: {example}", Language.YO: "Àpẹẹrẹ: {example}",
                       Language.PCM: "Example: {example}"},
    "lesson.answer_questions": {
        Language.EN: "✏️ Answer questions",
        Language.YO: "✏️ Dáhùn àwọn ìbéèrè",
        Language.PCM: "✏️ Answer the questions",
    },
    "lesson.english_only": {
        Language.EN: "A translation of this lesson isn't available right now, so here it is in English.",
        Language.YO: "Ìtúmọ̀ ẹ̀kọ́ yìí kò sí nísinsìnyí, nítorí náà èyí ni ní èdè Gẹ̀ẹ́sì.",
        Language.PCM: "Translation for this lesson no dey now, so na English be this.",
    },
    "lesson.unavailable": {
        Language.EN: "Today's lesson isn't ready yet. Please try again later.",
        Language.YO: "Ẹ̀kọ́ òní kò tíì ṣetán. Jọ̀wọ́ gbìyànjú lẹ́ẹ̀kan sí i ní àsìkò mìíràn.",
        Language.PCM: "Today lesson never ready. Abeg try again later.",
    },
    "lesson.not_in_circle": {
        Language.EN: "Lessons come through your circle, and you're not in one yet.",
        Language.YO: "Inú ẹgbẹ́ rẹ ni ẹ̀kọ́ ti ń wá, o kò sì tíì wà nínú ẹgbẹ́ kankan.",
        Language.PCM: "Lesson dey come through your circle, and you never dey any circle.",
    },
    # Answering questions (spec section 7.3)
    "question.header": {Language.EN: "Question {number} of {total}", Language.YO: "Ìbéèrè {number} nínú {total}",
                        Language.PCM: "Question {number} of {total}"},
    "question.easier": {
        Language.EN: "Here's an easier question on the same idea:",
        Language.YO: "Èyí ni ìbéèrè tí ó rọrùn jù lórí èrò kan náà:",
        Language.PCM: "Try this easier question on the same idea:",
    },
    "question.correct": {Language.EN: "✅ Correct!", Language.YO: "✅ O tọ̀nà!", Language.PCM: "✅ Correct!"},
    "question.wrong": {
        Language.EN: "❌ Not quite. The answer is {option}: {answer}",
        Language.YO: "❌ Kò tọ̀nà. Ìdáhùn náà ni {option}: {answer}",
        Language.PCM: "❌ E no correct. The answer na {option}: {answer}",
    },
    "question.done": {
        Language.EN: "🎉 You've answered today's questions. Your circle will see that you answered, but not your "
                     "score unless you turn on /share_scores.",
        Language.YO: "🎉 O ti dáhùn àwọn ìbéèrè òní. Ẹgbẹ́ rẹ máa rí pé o dáhùn, ṣùgbọ́n wọn kò ní rí máàkì rẹ "
                     "àyàfi tí o bá tan /share_scores.",
        Language.PCM: "🎉 You don answer today questions. Your circle go see say you answer, but dem no go see your "
                      "score unless you on /share_scores.",
    },
    "question.already_answered": {
        Language.EN: "You already answered that question.",
        Language.YO: "O ti dáhùn ìbéèrè yẹn tẹ́lẹ̀.",
        Language.PCM: "You don already answer that question.",
    },
    "question.all_answered": {
        Language.EN: "You've already answered all of this lesson's questions.",
        Language.YO: "O ti dáhùn gbogbo ìbéèrè ẹ̀kọ́ yìí tẹ́lẹ̀.",
        Language.PCM: "You don already answer all the questions for this lesson.",
    },
    # The batched circle feed (spec section 7.5): first names and check-in status; scores only after opt-in.
    "feed.header": {Language.EN: "🔵 Circle update", Language.YO: "🔵 Ìròyìn ẹgbẹ́", Language.PCM: "🔵 Circle update"},
    "feed.answered": {
        Language.EN: "{name} ✅ answered today's questions",
        Language.YO: "{name} ✅ ti dáhùn àwọn ìbéèrè òní",
        Language.PCM: "{name} ✅ don answer today questions",
    },
    "feed.answered_score": {
        Language.EN: "{name} ✅ answered today's questions ({correct}/{total} correct)",
        Language.YO: "{name} ✅ ti dáhùn àwọn ìbéèrè òní ({correct}/{total} ló tọ̀nà)",
        Language.PCM: "{name} ✅ don answer today questions ({correct}/{total} correct)",
    },
    "feed.checked_in": {
        Language.EN: "{name} ✅ checked in today",
        Language.YO: "{name} ✅ ti wọlé lónìí",
        Language.PCM: "{name} ✅ don check in today",
    },
    "feed.checked_in_yesterday": {
        Language.EN: "{name} ☑️ checked in yesterday",
        Language.YO: "{name} ☑️ wọlé lánàá",
        Language.PCM: "{name} ☑️ check in yesterday",
    },
    "feed.quiet_since": {
        Language.EN: "{name} ⏳ no check-in since {weekday}",
        Language.YO: "{name} ⏳ kò tíì wọlé láti {weekday}",
        Language.PCM: "{name} ⏳ never check in since {weekday}",
    },
    "feed.quiet_long": {
        Language.EN: "{name} ⏳ no check-in for over a week",
        Language.YO: "{name} ⏳ kò tíì wọlé fún ohun tó ju ọ̀sẹ̀ kan lọ",
        Language.PCM: "{name} ⏳ never check in for more than one week",
    },
    "feed.not_started": {
        Language.EN: "{name} ⏳ no check-in yet",
        Language.YO: "{name} ⏳ kò tíì wọlé rí",
        Language.PCM: "{name} ⏳ never check in at all",
    },
    "feed.question": {
        Language.EN: "❓ {name} asked the circle: {question}",
        Language.YO: "❓ {name} bi ẹgbẹ́ ní ìbéèrè: {question}",
        Language.PCM: "❓ {name} ask the circle: {question}",
    },
    "feed.more": {
        Language.EN: "…and {count} more circle post(s) in your next update.",
        Language.YO: "…àti {count} ìfiránṣẹ́ ẹgbẹ́ mìíràn nínú ìròyìn rẹ tó kàn.",
        Language.PCM: "…and {count} more circle post(s) for your next update.",
    },
    "feed.reply": {
        Language.EN: "💬 {name} replied to a circle question: I think this part of the book explains it.",
        Language.YO: "💬 {name} dáhùn ìbéèrè ẹgbẹ́ kan: Mo rò pé apá ìwé yìí ló ṣàlàyé rẹ̀.",
        Language.PCM: "💬 {name} reply one circle question: I think say this part of the book explain am.",
    },
    # Inactivity and peer nudges (spec section 7.6): the nudge always comes from a person.
    "nudge.prompt": {
        Language.EN: "💤 {name} has gone quiet. A nudge from you could help.",
        Language.YO: "💤 {name} ti dákẹ́ fún ọjọ́ díẹ̀. Ìránlétí láti ọ̀dọ̀ rẹ lè ṣèrànwọ́.",
        Language.PCM: "💤 {name} don quiet small. Nudge from you fit help.",
    },
    "nudge.button": {Language.EN: "👋 Send {name} a nudge", Language.YO: "👋 Rán {name} létí",
                     Language.PCM: "👋 Nudge {name}"},
    "nudge.message": {
        Language.EN: "👋 {name} from your circle is checking on you.\nToday's lesson is short. Want it now?",
        Language.YO: "👋 {name} láti inú ẹgbẹ́ rẹ ń béèrè nípa rẹ.\nẸ̀kọ́ òní kò gùn. Ṣé o fẹ́ ẹ báyìí?",
        Language.PCM: "👋 {name} from your circle dey check on you.\nToday lesson short. You want am now?",
    },
    "nudge.yes": {Language.EN: "Yes, send it", Language.YO: "Bẹ́ẹ̀ni, fi ránṣẹ́", Language.PCM: "Yes, send am"},
    "nudge.later": {Language.EN: "Later", Language.YO: "Nígbà míì", Language.PCM: "Later"},
    "nudge.later_reply": {
        Language.EN: "No problem. Send /lesson whenever you're ready.",
        Language.YO: "Kò burú. Fi /lesson ránṣẹ́ nígbàkígbà tí o bá ṣetán.",
        Language.PCM: "No wahala. Send /lesson anytime wey you ready.",
    },
    "nudge.sent": {
        Language.EN: "👋 Your nudge is on its way to {name}.",
        Language.YO: "👋 Ìránlétí rẹ ti ń lọ sọ́dọ̀ {name}.",
        Language.PCM: "👋 Your nudge don dey go meet {name}.",
    },
    "nudge.already_sent": {
        Language.EN: "{name} has already been nudged today, so yours wasn't sent. Thanks for looking out for your "
                     "circle.",
        Language.YO: "Ẹnìkan ti rán {name} létí lónìí, nítorí náà a kò fi tìrẹ ránṣẹ́. O ṣé fún bí o ṣe ń "
                     "bójú tó ẹgbẹ́ rẹ.",
        Language.PCM: "Person don already nudge {name} today, so we no send your own. Thank you as you dey look out "
                      "for your circle.",
    },
    "nudge.not_quiet": {
        Language.EN: "{name} has checked in recently, so no nudge is needed.",
        Language.YO: "{name} ti wọlé láìpẹ́, nítorí náà kò sí ìdí fún ìránlétí.",
        Language.PCM: "{name} don check in recently, so no need for nudge.",
    },
    "weekday.0": {Language.EN: "Monday", Language.YO: "Ọjọ́ Ajé", Language.PCM: "Monday"},
    "weekday.1": {Language.EN: "Tuesday", Language.YO: "Ọjọ́ Ìṣẹ́gun", Language.PCM: "Tuesday"},
    "weekday.2": {Language.EN: "Wednesday", Language.YO: "Ọjọ́rú", Language.PCM: "Wednesday"},
    "weekday.3": {Language.EN: "Thursday", Language.YO: "Ọjọ́bọ̀", Language.PCM: "Thursday"},
    "weekday.4": {Language.EN: "Friday", Language.YO: "Ọjọ́ Ẹtì", Language.PCM: "Friday"},
    "weekday.5": {Language.EN: "Saturday", Language.YO: "Ọjọ́ Àbámẹ́ta", Language.PCM: "Saturday"},
    "weekday.6": {Language.EN: "Sunday", Language.YO: "Ọjọ́ Àìkú", Language.PCM: "Sunday"},
    # Demo admin commands (spec section 9.3), for Telegram user IDs in ADMIN_IDS only
    "admin.forbidden": {
        Language.EN: "That command is only for demo admins.",
        Language.YO: "Àwọn alábòójútó àfihàn nìkan ló lè lo àṣẹ yẹn.",
        Language.PCM: "Na only demo admin fit use that command.",
    },
    "admin.no_goal": {
        Language.EN: "There is no goal to seed yet. Build the library first: "
                     "uv run python -m ingest.ingest --sources ingest/sources.yaml",
        Language.YO: "Kò sí àfojúsùn kankan láti gbìn síbẹ̀. Kọ́kọ́ kọ́ ilé-ìkàwé náà: "
                     "uv run python -m ingest.ingest --sources ingest/sources.yaml",
        Language.PCM: "No goal dey to seed yet. Build the library first: "
                      "uv run python -m ingest.ingest --sources ingest/sources.yaml",
    },
    "admin.seed_usage": {
        Language.EN: "Send /seed_demo, or /seed_demo followed by a whole-number seed such as 42.",
        Language.YO: "Fi /seed_demo ránṣẹ́, tàbí /seed_demo pẹ̀lú nọ́ńbà odidi bíi 42.",
        Language.PCM: "Send /seed_demo, or /seed_demo with whole number seed like 42.",
    },
    "admin.seeded": {
        Language.EN: "🌱 Demo ready: {circle} with {names}. Seed {seed}.\nSend /simulate_day to play the next day.",
        Language.YO: "🌱 Àfihàn ti ṣetán: {circle} pẹ̀lú {names}. Irúgbìn {seed}.\n"
                     "Fi /simulate_day ránṣẹ́ láti ṣe ọjọ́ tó kàn.",
        Language.PCM: "🌱 Demo don ready: {circle} with {names}. Seed {seed}.\nSend /simulate_day to play the next day.",
    },
    "admin.join_first": {
        Language.EN: "You're not in the circle yet. Send /start to join it as a learner.",
        Language.YO: "O kò tíì sí nínú ẹgbẹ́ náà. Fi /start ránṣẹ́ láti dara pọ̀ gẹ́gẹ́ bí akẹ́kọ̀ọ́.",
        Language.PCM: "You never dey the circle. Send /start to join am as learner.",
    },
    "admin.not_seeded": {
        Language.EN: "There is no seeded demo circle yet. Send /seed_demo first.",
        Language.YO: "Kò tíì sí ẹgbẹ́ àfihàn kankan. Kọ́kọ́ fi /seed_demo ránṣẹ́.",
        Language.PCM: "No demo circle dey yet. Send /seed_demo first.",
    },
    "admin.day_done": {
        Language.EN: "⏩ {date}: {lesson}\nChecked in: {names}\nQueued: {lessons} lesson post(s), {updates} circle update(s)",
        Language.YO: "⏩ {date}: {lesson}\nÀwọn tó wọlé: {names}\nNí ìlà: {lessons} ẹ̀kọ́, {updates} ìròyìn ẹgbẹ́",
        Language.PCM: "⏩ {date}: {lesson}\nWho check in: {names}\nFor queue: {lessons} lesson post(s), "
                      "{updates} circle update(s)",
    },
    "admin.day_quiet": {
        Language.EN: "Gone quiet: {names} (the circle was invited to nudge)",
        Language.YO: "Àwọn tó dákẹ́: {names} (a ti pe ẹgbẹ́ láti rán wọn létí)",
        Language.PCM: "Who don quiet: {names} (we don invite the circle to nudge)",
    },
    "admin.day_lesson": {
        Language.EN: "Day {day}, {topic}",
        Language.YO: "Ọjọ́ {day}, {topic}",
        Language.PCM: "Day {day}, {topic}",
    },
    "admin.no_lesson": {
        Language.EN: "no lesson (generation failed and the topic has no pre-generated lesson)",
        Language.YO: "kò sí ẹ̀kọ́ (ṣíṣe ẹ̀kọ́ kùnà, kò sì sí ẹ̀kọ́ tí a ti pèsè sílẹ̀ fún kókó náà)",
        Language.PCM: "no lesson (generation fail and no pre-generated lesson dey for the topic)",
    },
    "admin.already_ran": {
        Language.EN: "The daily jobs for {date} had already run, so nothing was repeated.",
        Language.YO: "Iṣẹ́ ojoojúmọ́ fún {date} ti ṣẹlẹ̀ tẹ́lẹ̀, nítorí náà a kò tún un ṣe.",
        Language.PCM: "Daily jobs for {date} don already run, so nothing repeat.",
    },
    "admin.nobody": {Language.EN: "nobody", Language.YO: "kò sí ẹnìkan", Language.PCM: "nobody"},
    "admin.never": {Language.EN: "never", Language.YO: "kò tíì sí rí", Language.PCM: "never"},
    "admin.payout_pending": {
        Language.EN: "Weekly payouts are not available yet, so none was sent.",
        Language.YO: "Ẹ̀bùn ọ̀sẹ̀ kò tíì sí, nítorí náà a kò fi ìkankan ránṣẹ́.",
        Language.PCM: "Weekly payout never ready, so we no send any.",
    },
    "admin.status": {
        Language.EN: "🧪 Demo status\nVirtual time: {time} (clock {offset})\nCircle: {circle}, seed {seed}\n"
                     "Days run: {days}\nToday's lesson: {lesson}\nMembers:\n{members}",
        Language.YO: "🧪 Ipò àfihàn\nÀkókò àfihàn: {time} (aago {offset})\nẸgbẹ́: {circle}, irúgbìn {seed}\n"
                     "Ọjọ́ tí a ti ṣe: {days}\nẸ̀kọ́ òní: {lesson}\nỌmọ ẹgbẹ́:\n{members}",
        Language.PCM: "🧪 Demo status\nVirtual time: {time} (clock {offset})\nCircle: {circle}, seed {seed}\n"
                      "Days wey don run: {days}\nToday lesson: {lesson}\nMembers:\n{members}",
    },
    "admin.status_member": {
        Language.EN: "• {name}{persona}: last check-in {last}, streak {streak}",
        Language.YO: "• {name}{persona}: ìgbà tó wọlé kẹ́yìn {last}, ọjọ́ léraléra {streak}",
        Language.PCM: "• {name}{persona}: last check-in {last}, streak {streak}",
    },
    "admin.persona_marker": {Language.EN: " (persona)", Language.YO: " (ẹ̀dá àfihàn)", Language.PCM: " (persona)"},
    "admin.reset_confirm": {
        Language.EN: "Reset the demo? This deletes the circle's lessons, answers, feed, nudges and personas and sets "
                     "the clock back to real time. Learners, their questions and the library are kept.",
        Language.YO: "Ṣé kí a tún àfihàn bẹ̀rẹ̀? Èyí máa pa ẹ̀kọ́, ìdáhùn, ìròyìn, ìránlétí àti àwọn ẹ̀dá àfihàn "
                     "ẹgbẹ́ náà rẹ́, yóò sì dá aago padà sí àkókò gidi. A máa pa àwọn akẹ́kọ̀ọ́, ìbéèrè wọn àti "
                     "ilé-ìkàwé mọ́.",
        Language.PCM: "You wan reset the demo? E go delete the circle lessons, answers, feed, nudges and personas, "
                      "and carry the clock back to real time. Learners, their questions and the library go stay.",
    },
    "admin.reset_yes": {Language.EN: "Yes, reset", Language.YO: "Bẹ́ẹ̀ni, tún un bẹ̀rẹ̀", Language.PCM: "Yes, reset am"},
    "admin.reset_done": {
        Language.EN: "♻️ Demo reset. The clock is back to real time. Send /seed_demo to add the personas again.",
        Language.YO: "♻️ A ti tún àfihàn bẹ̀rẹ̀. Aago ti padà sí àkókò gidi. Fi /seed_demo ránṣẹ́ láti fi àwọn "
                     "ẹ̀dá àfihàn kún un padà.",
        Language.PCM: "♻️ Demo don reset. Clock don go back to real time. Send /seed_demo to add the personas again.",
    },
    "admin.reset_cancelled": {
        Language.EN: "Reset cancelled. Nothing was changed.",
        Language.YO: "A ti fagilé àtúnbẹ̀rẹ̀. Kò sí ohun tó yí padà.",
        Language.PCM: "Reset don cancel. Nothing change.",
    },
}


def is_reviewed(language: Language | str) -> bool:
    return Language(language) in REVIEWED


def t(key: str, language: Language | str = Language.EN, **values: object) -> str:
    variants = STRINGS[key]
    language = Language(language)
    template = variants[language] if language in REVIEWED and language in variants else variants[Language.EN]
    return template.format(**values) if values else template


def placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def review_sheet(language: Language) -> str:
    """A Markdown table of English source strings beside their drafts, for a native-speaker reviewer."""
    rows = [f"# Ajo UI strings: {LANGUAGE_NAMES[language]} review", "",
            "Check each draft for meaning, tone and spelling (including tone marks). Keep `{placeholders}` and "
            "/commands unchanged. Biology terms may stay in English.", "",
            "| Key | English | Draft | OK / correction |", "| --- | --- | --- | --- |"]
    for key, variants in STRINGS.items():
        cells = [variants[Language.EN], variants.get(language, "(missing)")]
        rows.append(f"| `{key}` | " + " | ".join(cell.replace("|", "\\|").replace("\n", "<br>") for cell in cells)
                    + " |  |")
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a native-speaker review sheet for the fixed UI strings.")
    parser.add_argument("--review", required=True, choices=[item.value for item in Language if item != Language.EN])
    args = parser.parse_args(argv)
    sys.stdout.write(review_sheet(Language(args.review)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
