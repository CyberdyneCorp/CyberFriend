"""A fake Discord guild that models the parts of the permission system that matter.

Deliberately models per-member overwrites as well as roles, because the gap
between "everyone with the right role can read it" and "everyone who can read
the destination can read it" is exactly where a containment bug lives.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakePermissions:
    view_channel: bool
    read_message_history: bool


@dataclass(frozen=True)
class FakeMember:
    id: int
    roles: frozenset[str] = frozenset()
    bot: bool = False


@dataclass
class FakeChannel:
    """A channel whose readability is decided by roles, then member overwrites."""

    id: int
    allowed_roles: frozenset[str] = frozenset()
    # Member-level overwrites, applied after roles. These are the cases a
    # role-subset comparison gets wrong.
    member_allow: frozenset[int] = frozenset()
    member_deny: frozenset[int] = frozenset()
    # Readable by everyone unless restricted.
    public: bool = False
    # A channel visible to a role that may not read back through its history.
    no_history_roles: frozenset[str] = frozenset()

    def permissions_for(self, member: FakeMember) -> FakePermissions:
        if member.id in self._deny():
            return FakePermissions(False, False)
        view = (
            self.public
            or bool(member.roles & self.allowed_roles)
            or member.id in self.member_allow
        )
        history = view and not (member.roles & self.no_history_roles)
        return FakePermissions(view, history)

    def _deny(self) -> frozenset[int]:
        return self.member_deny


@dataclass
class FakeGuild:
    id: int = 1
    members: list[FakeMember] = field(default_factory=list)
    text_channels: list[FakeChannel] = field(default_factory=list)

    def get_member(self, user_id: int) -> FakeMember | None:
        return next((m for m in self.members if m.id == user_id), None)
