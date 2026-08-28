"""Task set for the SQL demo.

Questions quote literal values in double quotes so that a deterministic
parameter extractor can bind them into compiled query templates.  The task
stream deliberately repeats patterns (e.g. many "address of customer X"
questions) so Layer-1 direct replay has a chance to kick in.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

from .schemas import SqlTask


def _customer_names(db_path: Path, k: int, seed: int) -> list[tuple[str, str]]:
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT first_name, last_name FROM customers ORDER BY customer_id").fetchall()
    con.close()
    rng = random.Random(seed)
    return rng.sample(rows, k=min(k, len(rows)))


def build_tasks(db_path: Path, *, seed: int = 11, n_address: int = 8) -> list[SqlTask]:
    names = _customer_names(db_path, n_address + 12, seed)
    tasks: list[SqlTask] = []
    i = 0

    def add(pattern: str, question: str, gold: str, **params: object) -> None:
        nonlocal i
        i += 1
        tasks.append(SqlTask(task_id=f"sql_{i:03d}", question=question, gold_sql=gold, pattern=pattern, params=dict(params)))

    # --- customer_address: the headline pattern ------------------------------
    address_sql = (
        "SELECT a.street, a.city, a.postal_code, co.name "
        "FROM customers c JOIN addresses a ON c.address_id = a.address_id "
        "JOIN countries co ON a.country_id = co.country_id "
        "WHERE c.first_name = '{fn}' AND c.last_name = '{ln}'"
    )
    for fn, ln in names[:n_address]:
        add(
            "customer_address",
            f'Get the address (street, city, postal code, country) of the customer "{fn} {ln}".',
            address_sql.format(fn=fn, ln=ln),
            first_name=fn, last_name=ln,
        )

    # --- customer_email --------------------------------------------------------
    for fn, ln in names[n_address:n_address + 3]:
        add(
            "customer_email",
            f'What is the email address of the customer "{fn} {ln}"?',
            f"SELECT email FROM customers WHERE first_name = '{fn}' AND last_name = '{ln}'",
            first_name=fn, last_name=ln,
        )

    # --- customer_order_count --------------------------------------------------
    for fn, ln in names[n_address + 3:n_address + 6]:
        add(
            "customer_order_count",
            f'How many orders has the customer "{fn} {ln}" placed?',
            f"SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.customer_id "
            f"WHERE c.first_name = '{fn}' AND c.last_name = '{ln}'",
            first_name=fn, last_name=ln,
        )

    # --- customer_total_spent ---------------------------------------------------
    for fn, ln in names[n_address + 6:n_address + 9]:
        add(
            "customer_total_spent",
            f'What is the total amount spent (sum of quantity times unit price over all order items) by the customer "{fn} {ln}"?',
            f"SELECT SUM(oi.quantity * oi.unit_price) FROM order_items oi "
            f"JOIN orders o ON oi.order_id = o.order_id JOIN customers c ON o.customer_id = c.customer_id "
            f"WHERE c.first_name = '{fn}' AND c.last_name = '{ln}'",
            first_name=fn, last_name=ln,
        )

    # --- customers_in_city ------------------------------------------------------
    for city in ["Warsaw", "Berlin", "Paris"]:
        add(
            "customers_in_city",
            f'List the first and last names of all customers whose address is in the city "{city}".',
            f"SELECT c.first_name, c.last_name FROM customers c JOIN addresses a ON c.address_id = a.address_id WHERE a.city = '{city}'",
            city=city,
        )

    # --- product_price ----------------------------------------------------------
    for pname in ["Wireless Mouse", "Coffee Maker", "Yoga Mat"]:
        add(
            "product_price",
            f'What is the unit price of the product named "{pname}"?',
            f"SELECT unit_price FROM products WHERE name = '{pname}'",
            name=pname,
        )

    # --- products_in_category ---------------------------------------------------
    for cat in ["Books", "Sports"]:
        add(
            "products_in_category",
            f'List the names of all products in the category "{cat}".',
            f"SELECT p.name FROM products p JOIN categories ca ON p.category_id = ca.category_id WHERE ca.name = '{cat}'",
            category=cat,
        )

    # --- orders_by_status -------------------------------------------------------
    for status in ["cancelled", "pending"]:
        add(
            "orders_by_status",
            f'How many orders have the status "{status}"?',
            f"SELECT COUNT(*) FROM orders WHERE status = '{status}'",
            status=status,
        )

    # --- top_customers (no params, harder) --------------------------------------
    add(
        "top_customers_by_spend",
        "Return the first name, last name and total spend (sum of quantity times unit price) of the 3 customers "
        "with the highest total spend, ordered from highest to lowest.",
        "SELECT c.first_name, c.last_name, SUM(oi.quantity * oi.unit_price) AS total "
        "FROM customers c JOIN orders o ON o.customer_id = c.customer_id "
        "JOIN order_items oi ON oi.order_id = o.order_id "
        "GROUP BY c.customer_id ORDER BY total DESC LIMIT 3",
    )

    # Second wave of address questions for extra replay opportunities
    for fn, ln in names[n_address + 9:n_address + 12]:
        add(
            "customer_address",
            f'Get the address (street, city, postal code, country) of the customer "{fn} {ln}".',
            address_sql.format(fn=fn, ln=ln),
            first_name=fn, last_name=ln,
        )

    return tasks


def shuffled(tasks: list[SqlTask], seed: int) -> list[SqlTask]:
    rng = random.Random(seed)
    out = list(tasks)
    rng.shuffle(out)
    return out
