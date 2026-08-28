"""Build the deterministic demo SQLite database (customers / addresses / orders).

Run:  python -m democase_sql.db.build_db [--path democase_sql/db/demo.sqlite]
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "demo.sqlite"

DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE countries (
    country_id   INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    iso_code     TEXT NOT NULL UNIQUE
);

CREATE TABLE addresses (
    address_id   INTEGER PRIMARY KEY,
    street       TEXT NOT NULL,
    city         TEXT NOT NULL,
    postal_code  TEXT NOT NULL,
    country_id   INTEGER NOT NULL REFERENCES countries(country_id)
);

CREATE TABLE customers (
    customer_id  INTEGER PRIMARY KEY,
    first_name   TEXT NOT NULL,
    last_name    TEXT NOT NULL,
    email        TEXT NOT NULL UNIQUE,
    phone        TEXT,
    address_id   INTEGER REFERENCES addresses(address_id),
    created_at   TEXT NOT NULL
);

CREATE TABLE categories (
    category_id  INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE
);

CREATE TABLE products (
    product_id   INTEGER PRIMARY KEY,
    sku          TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    category_id  INTEGER NOT NULL REFERENCES categories(category_id),
    unit_price   REAL NOT NULL,
    in_stock     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE orders (
    order_id     INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date   TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending','shipped','delivered','cancelled')),
    shipping_address_id INTEGER REFERENCES addresses(address_id)
);

CREATE TABLE order_items (
    order_item_id INTEGER PRIMARY KEY,
    order_id     INTEGER NOT NULL REFERENCES orders(order_id),
    product_id   INTEGER NOT NULL REFERENCES products(product_id),
    quantity     INTEGER NOT NULL,
    unit_price   REAL NOT NULL
);
"""

COUNTRIES = [("Poland", "PL"), ("Germany", "DE"), ("France", "FR"), ("Spain", "ES"), ("Italy", "IT")]

CITIES = {
    "PL": [("Warsaw", "00-001"), ("Krakow", "30-001"), ("Gdansk", "80-001"), ("Wroclaw", "50-001")],
    "DE": [("Berlin", "10115"), ("Munich", "80331"), ("Hamburg", "20095")],
    "FR": [("Paris", "75001"), ("Lyon", "69001"), ("Marseille", "13001")],
    "ES": [("Madrid", "28001"), ("Barcelona", "08001"), ("Valencia", "46001")],
    "IT": [("Rome", "00100"), ("Milan", "20121"), ("Naples", "80100")],
}

STREETS = ["Main St", "Oak Ave", "Maple Rd", "River Ln", "Hill St", "Park Ave", "Lake Rd", "Forest Dr"]

FIRST_NAMES = [
    "Alice", "Bob", "Carol", "David", "Eva", "Frank", "Grace", "Henry", "Irena", "Jan",
    "Karolina", "Lukas", "Maria", "Nikolas", "Olga", "Piotr", "Quentin", "Rosa", "Stefan", "Tomasz",
    "Ursula", "Victor", "Wanda", "Xavier", "Yvonne", "Zofia", "Anna", "Marek", "Julia", "Pawel",
]
LAST_NAMES = [
    "Smith", "Kowalski", "Mueller", "Dubois", "Garcia", "Rossi", "Nowak", "Schmidt", "Martin", "Lopez",
    "Bianchi", "Wisniewski", "Fischer", "Bernard", "Fernandez", "Romano", "Zielinski", "Weber", "Petit", "Sanchez",
]

CATEGORIES = ["Books", "Electronics", "Home", "Toys", "Sports"]
PRODUCTS = [
    ("BK-001", "Python Cookbook", "Books", 39.99),
    ("BK-002", "SQL for Data Analysis", "Books", 34.50),
    ("BK-003", "Deep Learning", "Books", 59.00),
    ("EL-001", "Wireless Mouse", "Electronics", 19.99),
    ("EL-002", "Mechanical Keyboard", "Electronics", 89.00),
    ("EL-003", "USB-C Hub", "Electronics", 29.90),
    ("EL-004", "27in Monitor", "Electronics", 249.00),
    ("HM-001", "Coffee Maker", "Home", 74.99),
    ("HM-002", "Desk Lamp", "Home", 24.99),
    ("HM-003", "Blender", "Home", 49.00),
    ("TY-001", "Chess Set", "Toys", 21.00),
    ("TY-002", "Puzzle 1000pc", "Toys", 14.50),
    ("SP-001", "Yoga Mat", "Sports", 18.00),
    ("SP-002", "Running Shoes", "Sports", 99.00),
    ("SP-003", "Dumbbell Set", "Sports", 65.00),
]
STATUSES = ["pending", "shipped", "delivered", "delivered", "delivered", "shipped", "cancelled"]


def build(path: Path = DEFAULT_DB_PATH, *, n_customers: int = 40, n_orders: int = 120, seed: int = 7) -> Path:
    rng = random.Random(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript(DDL)

    con.executemany("INSERT INTO countries(name, iso_code) VALUES (?, ?)", COUNTRIES)
    iso_to_id = {iso: cid for cid, iso in con.execute("SELECT country_id, iso_code FROM countries")}

    # Addresses (one per customer + a few extra shipping addresses)
    address_ids: list[int] = []
    for i in range(n_customers + 15):
        iso = rng.choice(list(CITIES))
        city, postal = rng.choice(CITIES[iso])
        street = f"{rng.randint(1, 200)} {rng.choice(STREETS)}"
        cur = con.execute(
            "INSERT INTO addresses(street, city, postal_code, country_id) VALUES (?, ?, ?, ?)",
            (street, city, postal, iso_to_id[iso]),
        )
        address_ids.append(cur.lastrowid)

    # Customers — unique full names so questions are unambiguous
    used: set[tuple[str, str]] = set()
    customer_ids: list[int] = []
    for i in range(n_customers):
        while True:
            fn, ln = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
            if (fn, ln) not in used:
                used.add((fn, ln))
                break
        email = f"{fn.lower()}.{ln.lower()}@example.com"
        phone = f"+48 {rng.randint(500, 799)} {rng.randint(100, 999)} {rng.randint(100, 999)}" if rng.random() > 0.15 else None
        created = f"2024-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        cur = con.execute(
            "INSERT INTO customers(first_name, last_name, email, phone, address_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (fn, ln, email, phone, address_ids[i], created),
        )
        customer_ids.append(cur.lastrowid)

    con.executemany("INSERT INTO categories(name) VALUES (?)", [(c,) for c in CATEGORIES])
    cat_to_id = {name: cid for cid, name in con.execute("SELECT category_id, name FROM categories")}
    for sku, name, cat, price in PRODUCTS:
        con.execute(
            "INSERT INTO products(sku, name, category_id, unit_price, in_stock) VALUES (?, ?, ?, ?, ?)",
            (sku, name, cat_to_id[cat], price, rng.randint(0, 50)),
        )
    product_rows = list(con.execute("SELECT product_id, unit_price FROM products"))

    for _ in range(n_orders):
        cid = rng.choice(customer_ids)
        date = f"2025-{rng.randint(1, 8):02d}-{rng.randint(1, 28):02d}"
        status = rng.choice(STATUSES)
        ship = rng.choice(address_ids) if rng.random() < 0.2 else None
        cur = con.execute(
            "INSERT INTO orders(customer_id, order_date, status, shipping_address_id) VALUES (?, ?, ?, ?)",
            (cid, date, status, ship),
        )
        oid = cur.lastrowid
        for pid, price in rng.sample(product_rows, k=rng.randint(1, 4)):
            con.execute(
                "INSERT INTO order_items(order_id, product_id, quantity, unit_price) VALUES (?, ?, ?, ?)",
                (oid, pid, rng.randint(1, 3), price),
            )

    con.commit()
    con.close()
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the demo SQLite database")
    ap.add_argument("--path", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    out = build(Path(args.path), seed=args.seed)
    con = sqlite3.connect(out)
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        (n,) = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        print(f"  {table:12s} {n:5d} rows")
    con.close()
    print(f"Database written to {out}")


if __name__ == "__main__":
    main()
