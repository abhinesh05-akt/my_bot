import asyncpg
import ssl


class Database:
    def __init__(self, url: str):
        self.url = url
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        is_local = any(
            host in self.url
            for host in ("localhost", "127.0.0.1")
        )

        ssl_context = None

        if not is_local:
            ssl_context = ssl.create_default_context()

            # Supabase Session Pooler compatibility
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        self.pool = await asyncpg.create_pool(
            self.url,
            ssl=ssl_context,
            min_size=1,
            max_size=10,
            statement_cache_size=0,
        )

    async def disconnect(self):
        if self.pool:
            await self.pool.close()

    async def execute(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def init_schema(self):
        await self.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                channel_id TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        await self.execute("""
            CREATE TABLE IF NOT EXISTS batches (
                id SERIAL PRIMARY KEY,
                folder_id INTEGER REFERENCES folders(id),
                total_links INTEGER DEFAULT 0,
                channel_message_id TEXT,
                name TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        await self.execute("""
            ALTER TABLE batches
            ADD COLUMN IF NOT EXISTS name TEXT
        """)

        await self.execute("""
            ALTER TABLE batches
            ADD COLUMN IF NOT EXISTS folder_id INTEGER
            REFERENCES folders(id)
        """)

        await self.execute("""
            CREATE TABLE IF NOT EXISTS audios (
                id SERIAL PRIMARY KEY,
                batch_id INTEGER REFERENCES batches(id),
                drive_link TEXT NOT NULL,
                telegram_file_id TEXT
            )
        """)

        await self.execute("""
            CREATE TABLE IF NOT EXISTS sent_logs (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                batch_id INTEGER NOT NULL,
                message_ids TEXT NOT NULL,
                delete_at TIMESTAMP NOT NULL
            )
        """)

        await self.execute("""
            CREATE TABLE IF NOT EXISTS folder_pages (
                id SERIAL PRIMARY KEY,
                folder_id INTEGER REFERENCES folders(id),
                page_index INTEGER NOT NULL,
                channel_message_id TEXT,
                UNIQUE(folder_id, page_index)
            )
        """)

        await self.execute("""
            CREATE TABLE IF NOT EXISTS force_join_channels (
                id SERIAL PRIMARY KEY,
                channel_id TEXT NOT NULL UNIQUE,
                invite_link TEXT NOT NULL,
                title TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        await self.execute("""
            CREATE TABLE IF NOT EXISTS join_requests (
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                requested_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (channel_id, user_id)
            )
        """)