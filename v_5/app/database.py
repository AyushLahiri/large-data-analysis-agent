"""Atlas connections configured through environment variables or libpq settings."""
import os
import psycopg2


def get_db_connection():
    """Open the project's PostgreSQL connection without embedding credentials.

    Inputs: ATLAS_DATABASE_URL, or PGHOST/PGPORT/PGDATABASE/PGUSER plus libpq's
        PGPASSWORD or password file. Returns: an open psycopg2 connection.
    Called by SQL helpers; the caller owns transactions and closes the connection.
    """
    dsn = os.environ.get("ATLAS_DATABASE_URL")
    if dsn:
        return psycopg2.connect(dsn, connect_timeout=5)
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=os.environ.get("PGPORT", "5433"),
        dbname=os.environ.get("PGDATABASE", "atlas"),
        user=os.environ.get("PGUSER", "postgres"),
        connect_timeout=5,
    )
