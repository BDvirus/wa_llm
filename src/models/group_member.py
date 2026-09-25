from typing import ClassVar

from sqlmodel import Field, SQLModel


class GroupMember(SQLModel, table=True):
    """Who is in which group, synced from gowa by gather_groups.

    One row per identity: a participant can be known by a phone JID, a LID,
    or both, so each gets its own row and a lookup is `identity IN (...)`.
    `identity` comes first in the primary key because that is the lookup.
    `participant` is one stable key per person, so member counts don't double.
    """

    __tablename__: ClassVar[str] = "group_member"

    identity: str = Field(primary_key=True, max_length=255)
    group_jid: str = Field(
        primary_key=True,
        max_length=255,
        foreign_key="group.group_jid",
        ondelete="CASCADE",
    )
    participant: str = Field(max_length=255)
