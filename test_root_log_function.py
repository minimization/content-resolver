#! /usr/bin/python3

from feedback_pipeline import Analyzer
import sys
import urllib

# This is a starting point for a test of the function parsing root logs.
# Set the url below to any root log, and then you can see what it detected.

if len(sys.argv) > 1:
    root_log_url = sys.argv[1]
else:
    root_log_url = "https://kojipkgs.fedoraproject.org//packages/gstreamer1-vaapi/1.22.9/1.fc39/data/logs/x86_64/root.log"

with urllib.request.urlopen(root_log_url) as response:
    root_log_data = response.read()
    root_log_contents = root_log_data.decode('utf-8')

required_pkg_names = Analyzer._get_build_deps_from_a_root_log(None, root_log_contents)

print(required_pkg_names)
