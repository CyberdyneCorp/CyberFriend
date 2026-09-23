## Why

After introductions shipped, the production conversation still failed in six
ways: fact replies were in English to Portuguese messages; "Qual o meu
telefone?" and "what's my phone number?" went to the corpus; "Que informações
você pode guardar sobre mim?" went to the corpus; "me chamo Leonardo Araujo"
saved no full name; "/forget" typed as text went to the corpus; and the
assistant could not use the asker's contact details even in a DM, because
they never reached the prompt anywhere.

## What Changes

- Fact replies, and the fixed no-answer and wallet/market replies, are given
  in the language the message was written in (English and Portuguese).
- "What's my phone / email / wallet / name?" shows that one fact from the store.
- "What can you remember about me?" lists what may be kept.
- "Me chamo X" is a name statement, like "my name is X".
- A slash command typed as a message is answered with how to run it.
- **Email, phone and wallets reach the prompt in a direct message**, where the
  only reader is their owner. A channel prompt still never holds them.
- A capitalised word with an accent (a name) no longer makes an English
  message Portuguese.

## Risk

Contact details in a DM prompt means a model can repeat them in a DM answer,
to their owner, and they reach the model provider and the tracing destination
as part of that prompt. The egress guard is unchanged: none of them may be an
outbound argument except the wallets it already allowed. Channel prompts are
unchanged.
