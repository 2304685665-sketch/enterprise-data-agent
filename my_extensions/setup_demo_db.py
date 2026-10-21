import sqlite3

import os
db_path = 'my_extensions/demo.db'
if os.path.exists(db_path):
    os.remove(db_path)
conn = sqlite3.connect(db_path)
cur = conn.cursor()

cur.execute('''
CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    name TEXT,
    industry TEXT,
    founded_year INTEGER
)
''')

cur.execute('''
CREATE TABLE funding_rounds (
    id INTEGER PRIMARY KEY,
    company_id INTEGER,
    round_type TEXT,
    amount_usd REAL,
    round_date TEXT,
    FOREIGN KEY(company_id) REFERENCES companies(id)
)
''')

companies = [
    (1, 'RoboWorks', 'Humanoid Robotics', 2021),
    (2, 'NeuralPath', 'Humanoid Robotics', 2019),
    (3, 'SynthMotion', 'Humanoid Robotics', 2022),
    (4, 'AtlasAI', 'Humanoid Robotics', 2020),
]
cur.executemany('INSERT INTO companies VALUES (?,?,?,?)', companies)

rounds = [
    (1, 1, 'Series B', 45000000, '2026-03-15'),
    (2, 2, 'Series A', 12000000, '2025-11-02'),
    (3, 3, 'Series C', 80000000, '2026-06-20'),
    (4, 4, 'Seed', 5000000, '2024-08-10'),
    (5, 1, 'Series A', 15000000, '2024-01-05'),
]
cur.executemany('INSERT INTO funding_rounds VALUES (?,?,?,?,?)', rounds)

conn.commit()
conn.close()
print('demo.db created with companies + funding_rounds tables')
