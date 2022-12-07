#!/usr/bin/python3

import feedback_pipeline

import collections
import json
import os

from jsondiff import diff
from shutil import rmtree


def create_mock_argv(settings={}):
    return [
        "--dev-buildroot", "--dnf-cache-dir",
        "/tmp/test_cr", "test_data/bash/input", "output"]

# We need some of the keys sorted so that we can compare them
# between runs
def recursive_sort(data):
    apply = lambda x: recursive_sort(x)
    if isinstance(data, collections.Mapping):
        return type(data)({k: apply(v) for k, v in data.items()})
    elif isinstance(data, collections.Collection):
        if all(isinstance(x, str) for x in  data):
            return type(data)(sorted(list(data)))
        return data
    else:
        return data

def test_mock_argv():
    rmtree("output", ignore_errors=True)
    rmtree("json_output", ignore_errors=True)
    os.mkdir("output")
    os.mkdir("output/history")
    feedback_pipeline.main(create_mock_argv())
    files_to_check = os.scandir("test_data/bash/output")
    for f in files_to_check:
        with open(f.path) as t0:
          js0 = recursive_sort(json.load(t0))
        with open("output/"+f.name) as t1:
          js1 = recursive_sort(json.load(t1))

        assert diff(js0, js1) == {}, f"when comparing {f.path} and output/{f.name}"

if __name__ == "__main__":
    test_mock_argv()
