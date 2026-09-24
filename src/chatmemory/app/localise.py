"""The fixed replies the answer paths can return, in Portuguese.

An answer is written in the language it was asked in. A model answer follows
that rule through its prompt; these do not pass through a model, so a
Portuguese question got "I couldn't find anything about that in the messages
you can see." The table is applied once, at the end of `AskService.ask`, to an
answer that *is* one of these texts -- a model answer is never rewritten.

`NOTHING_FOUND` stays a single wording per language: two wordings for "no
answer" would make the reply an oracle for content the requester cannot read.
"""

from __future__ import annotations

from dataclasses import replace

from chatmemory.app.catchup import CHANNEL_UNAVAILABLE, NAME_THE_CHANNEL, NO_SUMMARY
from chatmemory.app.language import Language
from chatmemory.app.reasoning.contract import NOTHING_FOUND
from chatmemory.app.reasoning.loop import NOTHING_EXTERNAL
from chatmemory.app.reasoning.service import (
    MARKET_UNAVAILABLE,
    MCP_CHANGE_REFUSAL,
    NO_ADVICE,
    WALLET_ADDRESS_MISSING,
    WALLET_UNAVAILABLE,
)
from chatmemory.ports.answers import Answer

PORTUGUESE: dict[str, str] = {
    MCP_CHANGE_REFUSAL: (
        "Não consigo adicionar, remover ou mudar servidores MCP pelo chat. Conectar "
        "um servidor amplia o que eu alcanço, então isso é feito por um operador no "
        "console de administração, onde a mudança é autenticada e registrada."
    ),
    NAME_THE_CHANNEL: (
        "Me diga qual canal resumir, e escolha-o na lista de canais do Discord para "
        "ele chegar como link — como `o que perdi no #general`."
    ),
    CHANNEL_UNAVAILABLE: (
        "Não consigo resumir esse canal aqui. Só resumo canais que eu indexo, que "
        "você pode ler e que todos que veem esta conversa podem ler — e não vou "
        "dizer qual dessas condições falha, nem se o canal existe."
    ),
    NO_SUMMARY: (
        "Houve atividade nesse período, mas não consegui montar um resumo em que eu "
        "confie. Me pergunte algo específico sobre ele e eu olho de novo."
    ),
    NOTHING_FOUND: "Não encontrei nada sobre isso nas mensagens que você pode ver.",
    NOTHING_EXTERNAL: "Não consegui uma resposta das fontes externas que alcanço agora.",
    WALLET_ADDRESS_MISSING: (
        "Qual carteira? Me passe o endereço (começa com `0x`) e eu consulto "
        "Ethereum, Base e Arbitrum.\n"
        "Só consigo consultar um endereço que você digitar aqui - não um que "
        "eu encontrei num canal."
    ),
    WALLET_UNAVAILABLE: (
        "Não consegui ler esse endereço agora - os endpoints da blockchain não "
        "responderam. Tente de novo em instantes."
    ),
    MARKET_UNAVAILABLE: (
        "Não consegui um valor atual dos dados de mercado agora, e não vou dar "
        "um valor antigo nem um preço citado num canal no lugar. Tente de novo "
        "em instantes."
    ),
}

PREFIXES_PORTUGUESE: dict[str, str] = {
    NO_ADVICE: (
        "Não dou recomendações de compra, venda ou manter. Aqui está o valor "
        "atual, para você decidir:"
    ),
}


def localised(answer: Answer, language: Language) -> Answer:
    """The answer, with a fixed English reply given in `language`."""
    if language is not Language.PORTUGUESE:
        return answer
    whole = PORTUGUESE.get(answer.text)
    if whole is not None:
        return replace(answer, text=whole)
    for english, portuguese in PREFIXES_PORTUGUESE.items():
        if answer.text.startswith(english):
            return replace(answer, text=portuguese + answer.text[len(english):])
    return answer
