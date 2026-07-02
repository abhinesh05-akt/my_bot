import asyncpg
import ssl


class Database:
    def __init__(self, url: str):
        self.url = url
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        # Cloud Postgres providers don't agree on whether sslmode appears in
        # the URL — Neon usually includes it, Supabase often doesn't even
        # though SSL is still required server-side. String-matching for
        # "sslmode=require" is unreliable across providers. Instead: assume
        # SSL is required unless the host is explicitly local, since that's
        # the only case where SSL is reliably absent.
        is_local = any(h in self.url for h in ("localhost", "127.0.0.1"))

        ssl_context = None
        if not is_local:
            ssl_context = ssl.create_default_context()

        self.pool = await asyncpg.create_pool(
            self.url,
            ssl=ssl_context,
            min_size=1,
            max_size=10,
            statement_cache_size=0,  # required if DATABASE_URL points at a PgBouncer
                                      # transaction-mode pooler (e.g. Supabase port 6543);
                                      # harmless no-op on a direct connection.
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
        # Migrations for deployments created before folders existed.
        await self.execute("""
            ALTER TABLE batches ADD COLUMN IF NOT EXISTS name TEXT
        """)
        await self.execute("""
            ALTER TABLE batches ADD COLUMN IF NOT EXISTS folder_id INTEGER REFERENCES folders(id)
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
        # Ek folder ke batches ko 20-20 ke groups (pages) mein channel par
        # post karne ke liye. Har page apna ek hi message hai jo naye
        # batches add hone par edit hota hai; 20 buttons bharne ke baad
        # naya page (naya message) shuru hota hai.
        await self.execute("""
            CREATE TABLE IF NOT EXISTS folder_pages (
                id SERIAL PRIMARY KEY,
                folder_id INTEGER REFERENCES folders(id),
                page_index INTEGER NOT NULL,
                channel_message_id TEXT,
                UNIQUE(folder_id, page_index)
            )
        """)
