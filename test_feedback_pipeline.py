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
    expected_pkg_added_ids = set([
        'filesystem-3.14-5.fc34.aarch64',
        'glibc-minimal-langpack-2.33-5.fc34.aarch64',
        'tzdata-2021a-1.fc34.noarch',
        'basesystem-11-11.fc34.noarch',
        'bash-5.1.0-2.fc34.aarch64',
        'ncurses-libs-6.2-4.20200222.fc34.aarch64',
        'ncurses-base-6.2-4.20200222.fc34.noarch',
        'libgcc-11.0.1-0.3.fc34.aarch64',
        'glibc-2.33-5.fc34.aarch64',
        'setup-2.13.7-3.fc34.noarch',
        'glibc-common-2.33-5.fc34.aarch64'
    ])

    with open(f"{feedback_pipeline_output}/workload--bash--base-test--repo-test--aarch64.json") as w:
        workload = json.load(w)
        assert set(workload["data"]["pkg_added_ids"]) == expected_pkg_added_ids

def test_bash_test_repo_view(feedback_pipeline_output):
    expected_pkgs = {'alternatives-1.15-2.fc34',
         'basesystem-11-11.fc34',
         'bash-5.1.0-2.fc34',
         'ca-certificates-2020.2.41-7.fc34',
         'coreutils-8.32-21.fc34',
         'coreutils-common-8.32-21.fc34',
         'crypto-policies-20210213-1.git5c710c0.fc34',
         'fedora-gpg-keys-34-1',
         'fedora-release-34-1',
         'fedora-release-common-34-1',
         'fedora-release-identity-basic-34-1',
         'fedora-repos-34-1',
         'fedora-repos-eln-34-1',
         'fedora-repos-rawhide-34-1',
         'filesystem-3.14-5.fc34',
         'gc-8.0.4-5.fc34',
         'glibc-2.33-5.fc34',
         'glibc-common-2.33-5.fc34',
         'glibc-minimal-langpack-2.33-5.fc34',
         'gmp-1:6.2.0-6.fc34',
         'grep-3.6-2.fc34',
         'guile22-2.2.7-2.fc34',
         'libacl-2.3.1-1.fc34',
         'libattr-2.5.1-1.fc34',
         'libcap-2.48-2.fc34',
         'libffi-3.1-28.fc34',
         'libgcc-11.0.1-0.3.fc34',
         'libselinux-3.2-1.fc34',
         'libsepol-3.2-1.fc34',
         'libstdc++-11.0.1-0.3.fc34',
         'libtasn1-4.16.0-4.fc34',
         'libtool-ltdl-2.4.6-40.fc34',
         'libunistring-0.9.10-10.fc34',
         'libxcrypt-4.4.18-1.fc34',
         'make-1:4.3-5.fc34',
         'ncurses-base-6.2-4.20200222.fc34',
         'ncurses-libs-6.2-4.20200222.fc34',
         'openssl-libs-1:1.1.1k-1.fc34',
         'p11-kit-0.23.22-3.fc34',
         'p11-kit-trust-0.23.22-3.fc34',
         'pcre-8.44-3.fc34.1',
         'pcre2-10.36-4.fc34',
         'pcre2-syntax-10.36-4.fc34',
         'pizza-package-000-placeholder',
         'readline-8.1-2.fc34',
         'sed-4.8-7.fc34',
         'setup-2.13.7-3.fc34',
         'tar-2:1.34-1.fc34',
         'tzdata-2021a-1.fc34',
         'zlib-1.2.11-26.fc34'}
     
    with open(f"{feedback_pipeline_output}/view-packages--view-test.json") as w:
        view = json.load(w)
        assert set(view['pkgs'].keys()) == expected_pkgs



if __name__ == "__main__":
    test_mock_argv()
