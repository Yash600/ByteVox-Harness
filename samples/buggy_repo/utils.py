import json


def average(values):
    return sum(values) / len(values)


def load_config(path):
    f = open(path)
    try:
        return json.loads(f.read())
    except:
        return {}


def is_adult(age):
    return age > 18
