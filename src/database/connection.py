"""PostgreSQL database connection utilities."""

import os
import psycopg2
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

class PostgreSQLConnection:
    """PostgreSQL database connection manager."""
    
    def __init__(self):
        """Initialize connection manager."""
        self.connection: Optional[psycopg2.extensions.connection] = None
        
    def connect(self) -> psycopg2.extensions.connection:
        """Connect to PostgreSQL database."""
        if self.connection is None or self.connection.closed:
            self.connection = psycopg2.connect(
                host=os.getenv('PGHOST'),
                port=os.getenv('PGPORT'),
                database=os.getenv('PGDATABASE'),
                user=os.getenv('PGUSER'),
                password=os.getenv('PGPASSWORD'),
                sslmode='require'
            )
        return self.connection
    
    def close(self):
        """Close database connection."""
        if self.connection and not self.connection.closed:
            self.connection.close()
            
    def __enter__(self):
        """Context manager entry."""
        return self.connect()
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()