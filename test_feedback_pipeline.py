#!/usr/bin/python3

import feedback_pipeline

import json
import os
import pytest
import tempfile

from shutil import rmtree

@pytest.fixture(scope="module")
def feedback_pipeline_output():
    with tempfile.TemporaryDirectory() as tmp:
        os.mkdir(f"{tmp}/history")
        feedback_pipeline.main([
            "--dev-buildroot", "--dnf-cache-dir",
            "/tmp/test_cr", "test_configs", tmp])
        yield tmp


def test_bash_test_repo_workload(feedback_pipeline_output):
    expected_pkg_env_ids = set([
        'tzdata-2021a-1.fc34.noarch',
        'fedora-gpg-keys-34-1.noarch',
        'fedora-release-common-34-1.noarch',
        'glibc-minimal-langpack-2.33-5.fc34.aarch64',
        'libgcc-11.0.1-0.3.fc34.aarch64',
        'setup-2.13.7-3.fc34.noarch',
        'basesystem-11-11.fc34.noarch',
        'glibc-2.33-5.fc34.aarch64',
        'fedora-release-34-1.noarch',
        'ncurses-base-6.2-4.20200222.fc34.noarch',
        'ncurses-libs-6.2-4.20200222.fc34.aarch64',
        'bash-5.1.0-2.fc34.aarch64',
        'filesystem-3.14-5.fc34.aarch64',
        'glibc-common-2.33-5.fc34.aarch64',
        'fedora-release-identity-basic-34-1.noarch',
        'fedora-repos-34-1.noarch'
    ])

    with open(f"{feedback_pipeline_output}/workload--bash-test--base-test--repo-test--aarch64.json") as w:
        workload = json.load(w)
        assert set(workload["data"]["pkg_env_ids"]) == expected_pkg_env_ids

def test_bash_test_repo_view(feedback_pipeline_output):
    expected_pkgs = {}
     
    with open(f"{feedback_pipeline_output}/view-packages--view-test.json") as w:
        expected_pkgs = {
            'fedora-gpg-keys-34-1',
            'fedora-release-34-1',
            'fedora-release-common-34-1',
            'fedora-release-identity-basic-34-1',
            'fedora-repos-34-1',
            'filesystem-3.14-5.fc34',
            'glibc-minimal-langpack-2.33-5.fc34',
            'tzdata-2021a-1.fc34',
            'basesystem-11-11.fc34',
            'bash-5.1.0-2.fc34',
            'ncurses-libs-6.2-4.20200222.fc34',
            'ncurses-base-6.2-4.20200222.fc34',
            'libgcc-11.0.1-0.3.fc34',
            'glibc-2.33-5.fc34',
            'setup-2.13.7-3.fc34',
            'glibc-common-2.33-5.fc34'
        }
        view = json.load(w)
        assert set(view['pkgs'].keys()) == expected_pkgs
