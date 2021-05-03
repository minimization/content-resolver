#!/usr/bin/python3

# Takes the data.json from tiny.distro.builders and creates:
# cache_configs.json
# cache_data.json
# cache_settings.json
# to support reproducing local builds with the content already resolved.
# Use feedback_pipeline.py with the --use-cache option.

import os, json

class SetEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, set):
            return list(obj)
        return json.JSONEncoder.default(self, obj)

def dump_data(path, data):
    with open(path, 'w') as file:
        json.dump(data, file, cls=SetEncoder)


def load_data(path):
    with open(path, 'r') as file:
        data = json.load(file)
    return data


all_data = load_data("data.json")

dump_data("cache_configs.json", all_data["configs"])
dump_data("cache_data.json", all_data["data"])
dump_data("cache_settings.json", all_data["settings"])