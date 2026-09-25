import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from models import Sender
from utils.names import display_names

pytestmark = pytest.mark.integration


async def test_names_that_are_phone_numbers_are_dropped(db_session: AsyncSession):
    db_session.add_all(
        [
            Sender(jid="972501111111@s.whatsapp.net", push_name="Dana"),
            Sender(jid="972502222222@s.whatsapp.net", push_name="+972 50-222-2222"),
            Sender(jid="972503333333@s.whatsapp.net", push_name=None),
        ]
    )
    await db_session.commit()

    names = await display_names(
        db_session, ["972501111111", "972502222222", "972503333333"]
    )

    assert names == {"972501111111": "Dana"}
