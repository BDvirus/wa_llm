"""add private chat: group members, dm toggle, catch-up marker

Revision ID: c7e1f4a2b9d3
Revises: b2c3d4e5f6g7
Create Date: 2026-09-25

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7e1f4a2b9d3"
down_revision: Union[str, None] = "b2c3d4e5f6g7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Off by default: each group is opened to private questions deliberately.
    op.add_column(
        "group",
        sa.Column(
            "dm_queries_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "sender",
        sa.Column("last_catchup_at", sa.DateTime(timezone=True), nullable=True),
    )

    # One row per identity (phone JID and LID separately); identity leads the
    # primary key because private-chat authorization looks up by identity.
    op.create_table(
        "group_member",
        sa.Column("identity", sa.String(length=255), nullable=False),
        sa.Column("group_jid", sa.String(length=255), nullable=False),
        sa.Column("participant", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["group_jid"], ["group.group_jid"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("identity", "group_jid"),
    )
    op.create_index(
        "ix_group_member_group_jid", "group_member", ["group_jid"], unique=False
    )

    # Private-chat history and the daily quota both read one chat by time.
    op.create_index(
        "ix_message_chat_jid_timestamp",
        "message",
        ["chat_jid", "timestamp"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_message_chat_jid_timestamp", table_name="message")
    op.drop_index("ix_group_member_group_jid", table_name="group_member")
    op.drop_table("group_member")
    op.drop_column("sender", "last_catchup_at")
    op.drop_column("group", "dm_queries_enabled")
