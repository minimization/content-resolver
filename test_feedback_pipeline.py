#!/usr/bin/python3

import feedback_pipeline
import filecmp
import os
from shutil import rmtree, copytree, ignore_patterns

def create_mock_settings(settings={}):
    settings["configs"] = "test_input"
    settings["output"] = "output"
    settings["use_cache"] = False
    settings["dev_buildroot"] = True
    settings["dnf_cache_dir_override"] = "/tmp/test_cr" 
    settings["root_log_deps_cache_path"] = "cache_root_log_deps.json"
    settings["max_subprocesses"] = 10
    settings["allowed_arches"] = ["armv7hl", "aarch64", "ppc64le", "s390x", "x86_64"]
    settings["weird_packages_that_can_not_be_installed"] = ["glibc32"]
    settings["global_refresh_time_started"]= '28 November 2022 16:23 UTC'

    return settings

def test_mock_settings():
    rmtree("output", ignore_errors=True)
    rmtree("json_output", ignore_errors=True)
    os.mkdir("output")
    os.mkdir("output/history")

    settings = create_mock_settings()
    configs = feedback_pipeline.get_configs(settings)
    analyzer = feedback_pipeline.Analyzer(configs, settings)
    data = analyzer.analyze_things()

    feedback_pipeline.dump_data("cache_configs.json", configs)
    feedback_pipeline.dump_data("cache_data.json", data)
    query = feedback_pipeline.Query(data, configs, settings)
    feedback_pipeline.generate_pages(query)
    feedback_pipeline.generate_data_files(query)
    feedback_pipeline.generate_historic_data(query)
    copytree("output", "json_output", ignore=ignore_patterns('_static','*.html', '*.txt'))
    comparison = filecmp.dircmp("json_output", "test_output")
    assert comparison.left_only == []
    assert comparison.right_only == []


if __name__ == "__main__":
    test_mock_settings()
