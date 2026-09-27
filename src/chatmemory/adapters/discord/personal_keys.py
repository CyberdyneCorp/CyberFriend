"""What the Discord surface says about a CyberWealth connected-app key.

Every reply names the key by its last four characters at most. None of them
repeats the key, and none is produced by a model: a message carrying a key is
answered here, before anything could reach a prompt, a trace or memory.
"""

from __future__ import annotations

from chatmemory.app.language import Language
from chatmemory.app.personal_keys import ConnectOutcome, ConnectResult

EN = Language.ENGLISH
PT = Language.PORTUGUESE

_TEXTS: dict[str, dict[Language, str]] = {
    "connected": {
        EN: (
            "Connected your CyberWealth key ending `{last4}`. I use it only here, in our "
            "direct messages, when you ask about your own finances, and I never show it "
            "back. You can delete your message now. `/forget` or `/privacy` removes the key."
        ),
        PT: (
            "Conectei sua chave do CyberWealth terminando em `{last4}`. Eu só uso ela aqui, "
            "nas nossas mensagens diretas, quando você pergunta sobre as suas finanças, e "
            "nunca mostro ela de volta. Você já pode apagar sua mensagem. `/forget` ou "
            "`/privacy` removem a chave."
        ),
    },
    "malformed": {
        EN: (
            "That doesn't look like a complete CyberWealth key (`cwk_live_…`), so I saved "
            "nothing. Copy the whole key from CyberWealth → Settings → Connected apps and "
            "send it here."
        ),
        PT: (
            "Isso não parece uma chave completa do CyberWealth (`cwk_live_…`), então não "
            "salvei nada. Copie a chave inteira em CyberWealth → Configurações → Apps "
            "conectados e mande aqui."
        ),
    },
    "refused": {
        EN: "You've opted out, so I don't store anything you send, this key included. "
        "Nothing was saved.",
        PT: "Você optou por sair, então não guardo nada do que você manda, nem esta chave. "
        "Nada foi salvo.",
    },
    "not_direct": {
        EN: "I only take keys in a direct message with me. Nothing was saved; send it to "
        "me in a DM.",
        PT: "Só aceito chaves numa mensagem direta comigo. Nada foi salvo; mande numa DM.",
    },
    "unavailable": {
        EN: "I can't store keys on this deployment, so nothing was saved. You can delete "
        "your message.",
        PT: "Não consigo guardar chaves nesta instalação, então nada foi salvo. Você pode "
        "apagar sua mensagem.",
    },
    "channel_warning": {
        EN: (
            "Your message in {channel} looks like it contains a CyberWealth key. I didn't "
            "save or archive it, but everyone in that channel can read it: delete the "
            "message, revoke the key in CyberWealth → Settings → Connected apps, and send "
            "a new one only here, in a direct message."
        ),
        PT: (
            "Sua mensagem em {channel} parece ter uma chave do CyberWealth. Não salvei nem "
            "arquivei, mas todo mundo no canal pode ler: apague a mensagem, revogue a chave "
            "em CyberWealth → Configurações → Apps conectados e mande uma nova só aqui, numa "
            "mensagem direta."
        ),
    },
    "forgotten": {
        EN: "Your CyberWealth key was deleted too.",
        PT: "Sua chave do CyberWealth também foi apagada.",
    },
}

_RESULT_TEXT = {
    ConnectResult.MALFORMED: "malformed",
    ConnectResult.REFUSED: "refused",
    ConnectResult.NOT_DIRECT: "not_direct",
}


def key_text(key: str, language: Language, **values: object) -> str:
    """One reply, in Portuguese when that is the language, else English."""
    texts = _TEXTS[key]
    return texts.get(language, texts[EN]).format(**values)


def connect_reply(outcome: ConnectOutcome | None, language: Language) -> str:
    """The reply to a key sent in a DM or with `/connect`. None: keys are off here."""
    if outcome is None:
        return key_text("unavailable", language)
    if outcome.result is ConnectResult.CONNECTED and outcome.held is not None:
        return key_text("connected", language, last4=outcome.held.last4)
    return key_text(_RESULT_TEXT.get(outcome.result, "malformed"), language)


def channel_warning(channel: str, language: Language) -> str:
    return key_text("channel_warning", language, channel=channel)
