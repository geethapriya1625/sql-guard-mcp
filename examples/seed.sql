-- Small shop schema used by the tests and the demo. Portable across SQLite and Postgres.
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS api_keys;

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    region TEXT NOT NULL,
    created_at DATE NOT NULL
);
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL,
    name TEXT NOT NULL,
    unit_price NUMERIC(10, 2) NOT NULL
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status TEXT NOT NULL,
    placed_at DATE NOT NULL
);
CREATE TABLE order_items (
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    PRIMARY KEY (order_id, product_id)
);
CREATE TABLE api_keys (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    secret TEXT NOT NULL
);

INSERT INTO customers VALUES
 (1, 'Northwind Traders', 'ap@northwind.example', 'US-East', '2025-01-14'),
 (2, 'Contoso GmbH', 'billing@contoso.example', 'EU', '2025-02-03'),
 (3, 'Globex Corporation', 'finance@globex.example', 'US-West', '2025-03-22'),
 (4, 'Initech LLC', 'ops@initech.example', 'US-East', '2025-05-09'),
 (5, 'Umbrella Logistics', 'ar@umbrella.example', 'US-East', '2025-07-30');
INSERT INTO products VALUES
 (1, 'CHR-ERG', 'Ergonomic chair', 240.00),
 (2, 'DSK-STD', 'Standing desk', 650.00),
 (3, 'MON-27', '27in monitor', 310.50),
 (4, 'KB-MECH', 'Mechanical keyboard', 129.99);
INSERT INTO orders VALUES
 (101, 1, 'shipped', '2026-01-10'),
 (102, 2, 'shipped', '2026-01-18'),
 (103, 1, 'cancelled', '2026-02-02'),
 (104, 3, 'pending', '2026-02-20'),
 (105, 4, 'shipped', '2026-03-05'),
 (106, 5, 'pending', '2026-03-11');
INSERT INTO order_items VALUES
 (101, 1, 2), (101, 2, 1), (102, 3, 4), (103, 4, 1),
 (104, 2, 3), (104, 3, 3), (105, 1, 10), (106, 4, 5);
INSERT INTO api_keys VALUES (1, 1, 'sk_live_do_not_leak');
