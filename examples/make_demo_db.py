"""Create examples/shop.db from seed.sql."""

import sqlite3
from pathlib import Path

here = Path(__file__).parent
db = here / "shop.db"
db.unlink(missing_ok=True)
with sqlite3.connect(db) as conn:
    conn.executescript((here / "seed.sql").read_text())
print(f"wrote {db}")
