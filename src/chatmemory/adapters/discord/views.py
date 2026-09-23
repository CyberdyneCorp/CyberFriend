"""Buttons only the person they were shown to can press.

Shared by the two prompts that ask somebody to decide: approving a change to
another system (`bot._ApprovalView`) and confirming a position alert
(`alerts.AlertConfirmView`). The rule is the same for both, and the reason is
the one `bot`'s module docstring gives for using buttons at all: a press
arrives as an interaction carrying the account that pressed it, so whose
decision it was is something Discord says, not something anybody typed.

A prompt in a direct message can still be forwarded, and one in a channel is
in front of everyone there, so every press is checked against the one account
the prompt belongs to, and anybody else is told so privately and changes
nothing.
"""

from __future__ import annotations

import discord
import structlog

log = structlog.get_logger()


class RequesterOnlyView(discord.ui.View):
    """A view whose items only `requester_id` may use."""

    def __init__(self, requester_id: int, refusal: str, timeout: float | None) -> None:
        super().__init__(timeout=timeout)
        self._requester_id = requester_id
        self._refusal = refusal

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        """Refuse a press from anyone but the requester, and tell them privately.

        Refusing here means a second person's press never reaches a button's
        callback at all.
        """
        if interaction.user.id == self._requester_id:
            return True
        log.warning(
            "confirmation.button.not_requester",
            requester_id=self._requester_id,
            clicked_by=interaction.user.id,
        )
        try:
            await interaction.response.send_message(self._refusal, ephemeral=True)
        except discord.HTTPException:
            # Saying so is a courtesy; refusing is the requirement.
            log.info("confirmation.button.refusal_not_shown")
        return False

    def disable_buttons(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
