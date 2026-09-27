"""`/account create|link`: a CyberdyneAuth account, asked for in a DM.

Only in a direct message. Asked in a server channel, the command answers
privately that it continues in a DM and sends the rest there. The DM shows the
exact name, email and language that will be sent (the email from the person's
facts, or typed into a form, which does not save it as a fact), what the
invitation means, and that the CyberdyneAuth account outlives `/privacy`'s
Delete everything. Nothing is sent until [Confirm].

After Confirm the reply is the same for every outcome the provider can have,
because the provider answers the same way for all of them. [Link my account]
and `/account link` DM a single-use sign-in link.

When the link is made on the web, `LinkAnnouncer` (the bot's sweep) DMs
"Linked to a***@example.com, not you? [Unlink]". [Unlink] is a persistent
button: it works after a restart, and it unlinks whoever presses it, since
only they can see their own DM.

Only the person the prompt was for can press anything on it
(`RequesterOnlyView`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import discord
import structlog
from discord import app_commands

from chatmemory.adapters.discord.views import RequesterOnlyView
from chatmemory.app.accounts import (
    AccountService,
    ConsentDraft,
    LinkAnnouncements,
    ProvisioningOutcome,
    ProvisioningResult,
)
from chatmemory.app.language import Language
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.accounts import LinkCodeVerdict, LinkNotice

log = structlog.get_logger()

PLATFORM = "discord"

CONSENT_SECONDS = 600.0
"""How long [Confirm] and [Enter email] stay live."""

LINK_BUTTON_SECONDS = 24 * 3600.0
"""How long [Link my account] stays live: accepting the invitation takes a
while. `/account link` does the same after that."""

EN, PT = Language.ENGLISH, Language.PORTUGUESE

_TEXT: dict[str, dict[Language, str]] = {
    "not_yours": {
        EN: "Only the person who asked can use these buttons.",
        PT: "Só quem pediu pode usar estes botões.",
    },
    "sent_to_dm": {
        EN: "I'll continue in a direct message: check your DMs.",
        PT: "Vou continuar por mensagem direta: confira suas DMs.",
    },
    "dm_closed": {
        EN: (
            "I couldn't send you a direct message. Allow direct messages from "
            "this server's members and try again."
        ),
        PT: (
            "Não consegui te mandar mensagem direta. Permita mensagens diretas "
            "de membros deste servidor e tente de novo."
        ),
    },
    "limited": {
        EN: "You asked for an account recently. You can ask again {when}.",
        PT: "Você pediu uma conta há pouco tempo. Pode pedir de novo {when}.",
    },
    "ask_email": {
        EN: (
            "To create your CyberdyneAuth account I need the email address it "
            "should use, and I don't have one saved for you. Press **Enter email** "
            "to type it. I'll show you exactly what will be sent before anything is."
        ),
        PT: (
            "Para criar sua conta CyberdyneAuth preciso do email que ela deve usar, "
            "e não tenho nenhum salvo para você. Aperte **Informar email** para "
            "digitá-lo. Vou mostrar exatamente o que será enviado antes de enviar."
        ),
    },
    "email_button": {EN: "Enter email", PT: "Informar email"},
    "email_title": {EN: "Your email address", PT: "Seu endereço de email"},
    "email_label": {EN: "Email", PT: "Email"},
    "email_invalid": {
        EN: "That doesn't look like an email address, so nothing was sent.",
        PT: "Isso não parece um endereço de email, então nada foi enviado.",
    },
    "consent": {
        EN: (
            "**Create my CyberdyneAuth account**\n"
            "I'll send CyberdyneAuth exactly this, and nothing else about you:\n"
            "• Name: {name}\n"
            "• Email: {email}\n"
            "• Language: {locale_name}\n\n"
            "CyberdyneAuth will email an invitation to that address. The account "
            "works only once the invitation is accepted, so nobody gets an account "
            "through an address they don't control.\n\n"
            "The CyberdyneAuth account, with this name and email, is **not** deleted "
            "by `/privacy` → Delete everything, because CyberdyneAuth can't delete "
            "accounts on request yet. To have it deleted, ask a server admin to "
            "request its deletion from the CyberdyneAuth team."
        ),
        PT: (
            "**Criar minha conta CyberdyneAuth**\n"
            "Vou enviar à CyberdyneAuth exatamente isto, e nada mais sobre você:\n"
            "• Nome: {name}\n"
            "• Email: {email}\n"
            "• Idioma: {locale_name}\n\n"
            "A CyberdyneAuth vai mandar um convite para esse endereço. A conta só "
            "funciona depois que o convite for aceito, então ninguém ganha uma conta "
            "com um endereço que não controla.\n\n"
            "A conta CyberdyneAuth, com este nome e email, **não** é apagada pelo "
            "`/privacy` → Apagar tudo, porque a CyberdyneAuth ainda não apaga contas "
            "a pedido. Para apagá-la, peça a um admin do servidor que solicite a "
            "exclusão à equipe da CyberdyneAuth."
        ),
    },
    "no_name": {EN: "(none)", PT: "(nenhum)"},
    "language_name": {EN: "English", PT: "português"},
    "confirm_button": {EN: "Confirm", PT: "Confirmar"},
    "cancel_button": {EN: "Cancel", PT: "Cancelar"},
    "cancelled": {EN: "Cancelled. Nothing was sent.", PT: "Cancelado. Nada foi enviado."},
    "requested": {
        EN: (
            "If this address doesn't have a CyberdyneAuth account yet, it will "
            "receive an invitation. Once you've accepted it, press **Link my "
            "account** (or use `/account link`) and I'll DM you a sign-in link."
        ),
        PT: (
            "Se este endereço ainda não tiver uma conta CyberdyneAuth, ele vai "
            "receber um convite. Depois de aceitá-lo, aperte **Vincular minha "
            "conta** (ou use `/account link`) e eu te mando um link de acesso por DM."
        ),
    },
    "try_later": {
        EN: (
            "CyberdyneAuth couldn't take the request right now. It didn't count "
            "toward your limit; try again later."
        ),
        PT: (
            "A CyberdyneAuth não conseguiu receber o pedido agora. Ele não contou "
            "no seu limite; tente de novo mais tarde."
        ),
    },
    "link_button": {EN: "Link my account", PT: "Vincular minha conta"},
    "link": {
        EN: (
            "Here's your sign-in link. It works once, for 15 minutes, in the browser "
            "you open it in. Sign in with the email you gave me, and don't share it.\n"
            "{url}"
        ),
        PT: (
            "Aqui está seu link de acesso. Ele funciona uma vez, por 15 minutos, no "
            "navegador em que você abrir. Entre com o email que você me deu, e não "
            "o compartilhe.\n{url}"
        ),
    },
    "no_consent": {
        EN: "There's no account request to link yet. Use `/account create` first.",
        PT: "Ainda não há pedido de conta para vincular. Use `/account create` antes.",
    },
    "linked": {
        EN: (
            "Your Discord account is now linked to the CyberdyneAuth account "
            "{email}. You can see your data at {url}\nNot you? Press **Unlink**."
        ),
        PT: (
            "Sua conta do Discord agora está vinculada à conta CyberdyneAuth "
            "{email}. Veja seus dados em {url}\nNão foi você? Aperte **Desvincular**."
        ),
    },
    "unlink_button": {EN: "Unlink", PT: "Desvincular"},
    "unlinked": {
        EN: "Unlinked. That CyberdyneAuth account no longer reaches your data.",
        PT: "Desvinculado. Aquela conta CyberdyneAuth não acessa mais seus dados.",
    },
    "nothing_linked": {
        EN: "No CyberdyneAuth account is linked to you.",
        PT: "Nenhuma conta CyberdyneAuth está vinculada a você.",
    },
    "link_limited": {
        EN: "You've asked for too many sign-in links today. You can ask again {when}.",
        PT: "Você pediu links de acesso demais hoje. Pode pedir de novo {when}.",
    },
}


def _lang(language: Language) -> Language:
    return PT if language is PT else EN


def text(key: str, language: Language, **values: object) -> str:
    return _TEXT[key][_lang(language)].format(**values)


def when(at: datetime) -> str:
    """A Discord timestamp: shown in each reader's own zone and language."""
    return f"<t:{int(at.timestamp())}:f>"


def consent_text(draft: ConsentDraft) -> str:
    """The consent, with the exact values that will be sent."""
    language = draft.language
    escape = discord.utils.escape_markdown
    return text(
        "consent",
        language,
        name=escape(draft.name) if draft.name else text("no_name", language),
        email=escape(draft.email or ""),
        locale_name=text("language_name", language),
    )


def result_text(result: ProvisioningResult, language: Language) -> str:
    if result.outcome is ProvisioningOutcome.REQUESTED:
        return text("requested", language)
    if result.outcome is ProvisioningOutcome.LIMITED and result.retry_at is not None:
        return text("limited", language, when=when(result.retry_at))
    return text("try_later", language)


def _person(user: discord.abc.User) -> PersonRef:
    return PersonRef(PLATFORM, user.id)


def _in_dm(interaction: discord.Interaction) -> bool:
    return interaction.guild_id is None


# --- sending --------------------------------------------------------------------

async def _send_in_dm(
    interaction: discord.Interaction, content: str, view: discord.ui.View | None
) -> bool:
    """Send in the person's DM: the reply itself in a DM, a new message otherwise."""
    extra: dict[str, Any] = {"view": view} if view is not None else {}
    allowed = discord.AllowedMentions.none()
    if _in_dm(interaction):
        await interaction.followup.send(content, allowed_mentions=allowed, **extra)
        return True
    try:
        channel = await interaction.user.create_dm()
        await channel.send(content, allowed_mentions=allowed, **extra)
    except discord.HTTPException:
        log.info("accounts.dm_refused", user_id=interaction.user.id)
        return False
    return True


async def _say_privately(interaction: discord.Interaction, content: str) -> None:
    await interaction.followup.send(
        content, ephemeral=not _in_dm(interaction), allowed_mentions=discord.AllowedMentions.none()
    )


async def send_link(
    interaction: discord.Interaction, accounts: AccountService, language: Language
) -> None:
    """Issue a link code and DM it, or say why not."""
    issued = await accounts.link_code(_person(interaction.user))
    if issued.verdict is LinkCodeVerdict.NO_CONSENT:
        await _say_privately(interaction, text("no_consent", language))
        return
    if issued.url is None:
        assert issued.retry_at is not None
        limited = text("link_limited", language, when=when(issued.retry_at))
        await _say_privately(interaction, limited)
        return
    sent = await _send_in_dm(interaction, text("link", language, url=issued.url), None)
    if not _in_dm(interaction):
        await _say_privately(interaction, text("sent_to_dm" if sent else "dm_closed", language))


# --- the views ----------------------------------------------------------------------


class LinkView(RequesterOnlyView):
    """[Link my account], under the reply to Confirm."""

    def __init__(
        self,
        requester_id: int,
        language: Language,
        accounts: AccountService,
        timeout: float = LINK_BUTTON_SECONDS,
    ) -> None:
        super().__init__(requester_id, text("not_yours", language), timeout=timeout)
        self._requester_id = requester_id
        self._language = language
        self._accounts = accounts
        self.link_button.label = text("link_button", language)

    @discord.ui.button(label="Link my account", style=discord.ButtonStyle.primary)
    async def link_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await interaction.response.defer(ephemeral=not _in_dm(interaction), thinking=True)
        await send_link(interaction, self._accounts, self._language)


UNLINK_ID = "cyberfriend:account:unlink"
"""The persistent [Unlink] button's id, the same on every link notice."""

PersonLanguage = Callable[[PersonRef], Awaitable[Language]]


class UnlinkView(discord.ui.View):
    """[Unlink] under a link notice. Persistent: no timeout, a fixed id, and
    registered at startup, so a notice sent before a restart still works.

    It acts on whoever presses it. The notice is in their DM, which nobody
    else can see, and unlinking only ever takes access away.
    """

    def __init__(
        self, accounts: AccountService, language_of: PersonLanguage, language: Language = EN
    ) -> None:
        super().__init__(timeout=None)
        self._accounts = accounts
        self._language_of = language_of
        self.unlink_button.label = text("unlink_button", language)

    @discord.ui.button(
        label="Unlink", style=discord.ButtonStyle.danger, custom_id=UNLINK_ID
    )
    async def unlink_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await interaction.response.defer()
        person = _person(interaction.user)
        language = await self._language_of(person)
        unlinked = await self._accounts.unlink(person)
        try:
            await interaction.edit_original_response(view=None)
        except discord.HTTPException:
            log.info("accounts.unlink.button_not_removed")
        reply = text("unlinked" if unlinked else "nothing_linked", language)
        await interaction.followup.send(reply)


class LinkAnnouncer:
    """DMs each person whose account was linked on the web, with [Unlink]."""

    def __init__(
        self,
        announcements: LinkAnnouncements,
        accounts: AccountService,
        fetch_user: Callable[[int], Awaitable[Any]],
        language_of: PersonLanguage,
    ) -> None:
        self._announcements = announcements
        self._accounts = accounts
        self._fetch_user = fetch_user
        self._language_of = language_of

    async def announce(self) -> int:
        return await self._announcements.announce(self._tell)

    async def _tell(self, notice: LinkNotice) -> bool:
        """True once there is nothing more to do: sent, or the DM is shut."""
        try:
            user = await self._fetch_user(notice.person.platform_user_id)
            language = await self._language_of(notice.person)
            body = text(
                "linked",
                language,
                email=discord.utils.escape_markdown(notice.email_hint),
                url=self._accounts.user_area_url,
            )
            await user.send(
                body,
                view=UnlinkView(self._accounts, self._language_of, language),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.NotFound):
            log.info("accounts.link_notice_undeliverable", person=str(notice.person))
            return True
        except discord.HTTPException:
            log.warning("accounts.link_notice_failed", person=str(notice.person))
            return False
        return True


class ConsentView(RequesterOnlyView):
    """[Confirm] [Cancel] under the consent text. One press decides."""

    def __init__(
        self,
        requester_id: int,
        draft: ConsentDraft,
        accounts: AccountService,
        timeout: float = CONSENT_SECONDS,
    ) -> None:
        super().__init__(requester_id, text("not_yours", draft.language), timeout=timeout)
        self._requester_id = requester_id
        self._draft = draft
        self._accounts = accounts
        self.confirm_button.label = text("confirm_button", draft.language)
        self.cancel_button.label = text("cancel_button", draft.language)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await self._close(interaction)
        language = self._draft.language
        result = await self._accounts.confirm(_person(interaction.user), self._draft)
        view = (
            LinkView(self._requester_id, language, self._accounts)
            if result.outcome is ProvisioningOutcome.REQUESTED
            else discord.utils.MISSING
        )
        await interaction.followup.send(
            result_text(result, language),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await self._close(interaction)
        await interaction.followup.send(text("cancelled", self._draft.language))

    async def _close(self, interaction: discord.Interaction) -> None:
        """Stop the view and take its buttons away, so it is pressed once."""
        self.stop()
        await interaction.response.defer()
        try:
            await interaction.edit_original_response(view=None)
        except discord.HTTPException:
            log.info("accounts.consent.buttons_not_removed")


class EmailModal(discord.ui.Modal):
    """The address, typed. Checked like an email fact, and not saved as one."""

    def __init__(self, requester_id: int, draft: ConsentDraft, accounts: AccountService) -> None:
        super().__init__(title=text("email_title", draft.language), timeout=CONSENT_SECONDS)
        self._requester_id = requester_id
        self._draft = draft
        self._accounts = accounts
        self.email: discord.ui.TextInput[EmailModal] = discord.ui.TextInput(
            label=text("email_label", draft.language), max_length=254
        )
        self.add_item(self.email)

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        draft = self._draft.with_email(self.email.value)
        if draft is None:
            await interaction.response.send_message(
                text("email_invalid", self._draft.language), ephemeral=True
            )
            return
        # Deferred, then a followup: the consent is a message of its own, which
        # its buttons are stored against.
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(
            consent_text(draft),
            view=ConsentView(self._requester_id, draft, self._accounts),
            allowed_mentions=discord.AllowedMentions.none(),
        )


class EmailPromptView(RequesterOnlyView):
    """[Enter email], when no email fact is saved."""

    def __init__(
        self,
        requester_id: int,
        draft: ConsentDraft,
        accounts: AccountService,
        timeout: float = CONSENT_SECONDS,
    ) -> None:
        super().__init__(requester_id, text("not_yours", draft.language), timeout=timeout)
        self._requester_id = requester_id
        self._draft = draft
        self._accounts = accounts
        self.email_button.label = text("email_button", draft.language)

    @discord.ui.button(label="Enter email", style=discord.ButtonStyle.primary)
    async def email_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await interaction.response.send_modal(
            EmailModal(self._requester_id, self._draft, self._accounts)
        )


def consent_message(
    requester_id: int, draft: ConsentDraft, accounts: AccountService
) -> tuple[str, discord.ui.View]:
    """The consent with [Confirm] [Cancel], or the request for an email first."""
    if draft.email is None:
        return text("ask_email", draft.language), EmailPromptView(requester_id, draft, accounts)
    return consent_text(draft), ConsentView(requester_id, draft, accounts)


# --- the command --------------------------------------------------------------------

LanguageOf = Callable[[discord.Interaction], Awaitable[Language]]


def account_group(accounts: AccountService, language_of: LanguageOf) -> app_commands.Group:
    """`/account create|link`, keyed on the interaction's user like `/privacy`."""
    group = app_commands.Group(name="account", description="Your CyberdyneAuth account")

    @group.command(name="create", description="Create my CyberdyneAuth account")
    async def create(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=not _in_dm(interaction), thinking=True)
        language = await language_of(interaction)
        person = _person(interaction.user)
        wait = await accounts.next_allowed(person)
        if wait is not None:
            await _say_privately(interaction, text("limited", language, when=when(wait)))
            return
        draft = await accounts.draft(person, interaction.user.display_name, language)
        content, view = consent_message(interaction.user.id, draft, accounts)
        sent = await _send_in_dm(interaction, content, view)
        if not _in_dm(interaction):
            await _say_privately(interaction, text("sent_to_dm" if sent else "dm_closed", language))

    @group.command(name="link", description="DM me a link to link my CyberdyneAuth account")
    async def link(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=not _in_dm(interaction), thinking=True)
        language = await language_of(interaction)
        await send_link(interaction, accounts, language)

    return group
