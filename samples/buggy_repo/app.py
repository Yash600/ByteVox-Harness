import sqlite3

from utils import average, load_config

API_KEY = "sk-live-9f8a7b6c5d4e3f2a1b0c"  # TODO: move to env


def get_user(db, name):
    query = "SELECT * FROM users WHERE name = '" + name + "'"
    return db.execute(query).fetchall()


def report_scores(scores):
    return f"average score: {average(scores)}"


def main():
    db = sqlite3.connect("app.db")
    cfg = load_config("config.json")
    print(get_user(db, input("name: ")))
    print(report_scores([]))


if __name__ == "__main__":
    main()
