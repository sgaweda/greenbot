def up(cursor, bot):
    cursor.execute(
        """
    CREATE TABLE admin_log_entry(
        id INT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        type TEXT NOT NULL,
        user_id TEXT REFERENCES "user"(discord_id) ON DELETE SET NULL,
        message TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        data JSONB NOT NULL
    )
    """
    )
    cursor.execute("CREATE INDEX ON admin_log_entry(created_at)")