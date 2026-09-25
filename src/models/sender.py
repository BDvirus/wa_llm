from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from pydantic import field_validator
from sqlmodel import Column, DateTime, Field, Relationship, SQLModel

from whatsapp.jid import normalize_jid

if TYPE_CHECKING:
    from .group import Group
    from .message import Message
    from .reaction import Reaction


class BaseSender(SQLModel):
    jid: str = Field(primary_key=True, max_length=255)
    push_name: Optional[str] = Field(default=None, max_length=255)
    # When this user last asked the bot "what did I miss" in a private chat.
    last_catchup_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    @field_validator("jid", mode="before")
    @classmethod
    def normalize(cls, value: str) -> str:
        return normalize_jid(value)


class Sender(BaseSender, table=True):
    messages: List["Message"] = Relationship(back_populates="sender")
    groups_owned: List["Group"] = Relationship(back_populates="owner")
    # Reactions relationship - one sender can have many reactions
    reactions: List["Reaction"] = Relationship(
        back_populates="sender", sa_relationship_kwargs={"lazy": "selectin"}
    )


Sender.model_rebuild()
