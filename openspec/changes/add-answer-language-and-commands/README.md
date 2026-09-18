# add-answer-language-and-commands

Answer in the language the question was asked in, and say what the assistant
can actually do.

From a production reply: asked "Oque voce pode fazer? Quais as suas
funcionalidades e comandos ?" -- a whole sentence of Portuguese -- it answered
in English, listed one command out of six, and named two capabilities by their
internal server identifiers.

A saved preferred language still wins, because that is a choice somebody made
rather than one inferred for them.

- `proposal.md` -- why, what changes, and what is deliberately not attempted
- `specs/answer-language/spec.md` -- which language, and what decides it
- `specs/self-description/spec.md` -- what the assistant says about itself
- `tasks.md` -- progress
