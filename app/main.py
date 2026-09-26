import asyncio
from contextlib import asynccontextmanager
from warnings import warn

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession
import logging
import logfire

from admin.jobs import JobRegistry
from admin.router import STATIC_DIR as ADMIN_STATIC_DIR
from admin.router import router as admin_router
from admin.topic_routes import router as admin_topics_router
from api import load_new_kbtopics_api, status, summarize_and_send_to_group_api, webhook
import models  # noqa
from config import get_settings
from whatsapp import WhatsAppClient
from whatsapp.init_groups import gather_groups
from voyageai.client_async import AsyncClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Create and configure logger
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=settings.log_level,
    )

    app.state.settings = settings

    app.state.whatsapp = WhatsAppClient(
        settings.whatsapp_host,
        settings.whatsapp_basic_auth_user,
        settings.whatsapp_basic_auth_password,
    )

    if settings.db_uri.startswith("postgresql://"):
        warn("use 'postgresql+asyncpg://' instead of 'postgresql://' in db_uri")
    engine = create_async_engine(
        settings.db_uri,
        pool_size=20,
        max_overflow=40,
        pool_timeout=30,
        pool_pre_ping=True,
        pool_recycle=600,
        future=True,
    )
    logfire.instrument_sqlalchemy(engine)
    async_session = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )

    async def sync_groups_on_startup() -> None:
        async with async_session() as session:
            try:
                await gather_groups(session, app.state.whatsapp)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    asyncio.create_task(sync_groups_on_startup())

    app.state.db_engine = engine
    app.state.async_session = async_session
    app.state.embedding_client = AsyncClient(
        api_key=settings.voyage_api_key, max_retries=settings.voyage_max_retries
    )
    app.state.jobs = JobRegistry()
    try:
        yield
    finally:
        # Cancel admin jobs first: they use the engine being disposed below.
        await app.state.jobs.shutdown()
        await engine.dispose()


# Initialize FastAPI app
app = FastAPI(title="Webhook API", lifespan=lifespan)

logfire.configure()
logfire.instrument_pydantic_ai()
logfire.instrument_fastapi(app)
logfire.instrument_httpx(capture_all=True)
logfire.instrument_system_metrics()


app.include_router(webhook.router)
app.include_router(status.router)
app.include_router(summarize_and_send_to_group_api.router)
app.include_router(load_new_kbtopics_api.router)
app.include_router(admin_router)
app.include_router(admin_topics_router)
# Mounted on the app itself: include_router() does not carry mounts over.
app.mount("/admin/static", StaticFiles(directory=ADMIN_STATIC_DIR), name="admin-static")

if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    print(f"Running on {settings.host}:{settings.port}")

    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=True)
