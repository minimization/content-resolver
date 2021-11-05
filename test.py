import argparse, yaml, tempfile, os, subprocess, json, jinja2, datetime, copy, re, dnf, pprint, urllib.request, sys, koji, logging
import concurrent.futures
import rpm_showme as showme
from functools import lru_cache



logging.basicConfig(level=logging.DEBUG)


def parse_root_log(root_log):
    required_pkgs = []

    # The individual states are nicely described inside the for loop.
    # They're processed in order
    state = 0
    
    for file_line in root_log.splitlines():

        # 0/
        # parts of the log I don't really care about
        if state == 0:

            # The next installation is the build deps!
            # So I start caring. Next state!
            if "Executing command: ['/usr/bin/dnf', 'builddep'" in file_line:
                state += 1
        

        # 1/
        # getting the "already installed" packages to the list
        elif state == 1:

            # "Package already installed" indicates it's directly required,
            # so save it.
            if "is already installed." in file_line:
                pkg_name = file_line.split()[3].rsplit("-",2)[0]
                required_pkgs.append(pkg_name)

            # That's all! Next state!
            elif "Dependencies resolved." in file_line:
                state += 1
        

        # 2/
        # going through the log right before the first package name
        elif state == 2:

            # The next line will be the first package. Next state!
            if "Installing:" in file_line:
                state += 1
        

        # 3/
        # And now just saving the packages until the "installing dependencies" part
        # or the "transaction summary" part if there's no dependencies
        elif state == 3:
            
            if "Installing dependencies:" in file_line:
                state += 1

            elif "Transaction Summary" in file_line:
                state += 1
                
            else:
                pkg_name = file_line.split()[2]
                required_pkgs.append(pkg_name)
        

        # 4/
        # I'm done. So I can break out of the loop.
        elif state == 4:
            break
            

    return required_pkgs


# publicsuffix-list-20210518-2.eln112 is fucked

root_log_url = "https://kojipkgs.fedoraproject.org/packages/publicsuffix-list/20210518/2.eln112/data/logs/noarch/root.log"

with urllib.request.urlopen(root_log_url) as response:
    root_log_data = response.read()
    root_log_contents = root_log_data.decode('utf-8')


#print (root_log_contents)
required_pkgs = parse_root_log(root_log_contents)
#root_log = root_log_contents

#required_pkgs = []

#for line in root_log.splitlines():
##    if len(line) >2:
#        print(line)


print("")
print("")
print("")
print("DONE!")
print("")


for pkg in required_pkgs:
    print(pkg)


print("")
print(required_pkgs)