#!/usr/bin/python3

import json, requests


def log(msg):
    pass
    #print(msg)

def load_data(path):
    with open(path, 'r') as file:
        data = json.load(file)
    return data


def pkg_id_to_name(pkg_id):
    pkg_name = pkg_id.rsplit("-",2)[0]
    return pkg_name


def real_repo_name(repo_name):
    repos = {
        "BaseOS": "BaseOS",
        "AppStream": "AppStream",
        "CRB": "CRB",
        "HA": "HighAvailability",
        "NFV": "NFV",
        "RT": "RT",
        "RS": "ResilientStorage",
        "SAP": "SAP",
        "SAPHANA": "SAPHANA"
    }

    return repos[repo_name]


def load_settings():
    settings = {}

    settings["allowed_arches"] = ["aarch64","ppc64le","s390x","x86_64"]

    settings["repos"] = {
        "BaseOS": ["aarch64", "ppc64le", "s390x", "x86_64"],
        "AppStream": ["aarch64", "ppc64le", "s390x", "x86_64"],
        "CRB": ["aarch64", "ppc64le", "s390x", "x86_64"],
        "buildroot-only": ["aarch64", "ppc64le", "s390x", "x86_64"],
        "HA": ["aarch64", "ppc64le", "s390x", "x86_64"],
        "NFV": ["x86_64"],
        "RT": ["x86_64"],
        "RS": ["ppc64le", "s390x", "x86_64"],
        "SAP": ["ppc64le", "s390x", "x86_64"],
        "SAPHANA": ["ppc64le", "x86_64"]
    }

    settings["repo_names_sorted_for_print"] = [
        "BaseOS",
        "AppStream",
        "CRB",
        "buildroot-only",
        "HA",
        "NFV",
        "RS",
        "RT",
        "SAP",
        "SAPHANA",
    ]

    settings["addon_repos"] = [
        "HA",
        "NFV",
        "RS",
        "RT",
        "SAP",
        "SAPHANA",
    ]

    return settings




#   "pcre2-10.40-1.eln120.1": {
#       "name": "pcre2",
#       "source_name": "pcre2",
#       "arches_arches": {
#           "aarch64": [
#               "aarch64"
#           ],
#           "x86_64": [
#               "i686",
#               "x86_64"
#           ]
#       },
#       "placeholder": false,
#       "hard_dependency_of_pkg_nevrs": [
#           "grep-3.8-1.eln121",
#           "libselinux-3.4-5.eln120",
#           "glib2-2.73.3-3.eln121"
#       ],
#       "weak_dependency_of_pkg_nevrs": [
#           "systemd-251.4-53.eln121"
#       ],
#       "in_workload_conf_ids_req": [],
#       "level_number": 0
#   },


###############################################################################
### Main ######################################################################
###############################################################################


def main():
    settings = load_settings()

    log("Loading data...")

    downloaded_pkg_data = json.loads(requests.get("https://tiny.distro.builders/view-packages--view-eln.json").text)

    log("Done!")
    log("")

    # Turn NVRs into names, because that's all I need for the repo split

    log("Making names out of NVRs...")

    pkgs_data = {}
    #
    # pkgs_data[arch][pkg_name]...
    #
    for arch in settings["allowed_arches"]:
        pkgs_data[arch] = {}

        for pkg_id, pkg_data in downloaded_pkg_data["pkgs"].items():
            pkg_name = pkg_id_to_name(pkg_id)

            if arch not in pkg_data["arches_arches"]:
                continue

            if "placeholder" in pkg_data["arches_arches"][arch]:
                if len(pkg_data["arches_arches"][arch]) == 1:
                    continue
                
                non_placeholder_arches = set()
                for rpm_arch in pkg_data["arches_arches"][arch]:
                    if rpm_arch == "placeholder":
                        continue
                    non_placeholder_arches.add(rpm_arch)
                
                pkg_data["arches_arches"][arch] = list(non_placeholder_arches)

                del non_placeholder_arches

            # Init 
            if pkg_name not in pkgs_data[arch]:
                pkgs_data[arch][pkg_name] = {}

                # Stuff from Content Resolver
                pkgs_data[arch][pkg_name]["name"] = pkg_name
                pkgs_data[arch][pkg_name]["source_name"] = pkg_data["source_name"]
                pkgs_data[arch][pkg_name]["rpm_arches"] = pkg_data["arches_arches"][arch]
                pkgs_data[arch][pkg_name]["required_in_workloads"] = False
                pkgs_data[arch][pkg_name]["required_by"] = set()
                pkgs_data[arch][pkg_name]["level_number"] = pkg_data["level_number"]

                # Users can wish for any package to be in any repo.
                # All this algorighm cares about is the repos, it doesn't
                # need to know who wanted what.
                pkgs_data[arch][pkg_name]["user_repo_wishes"] = set()

            
            # Dependencies
            for dep_id in pkg_data["hard_dependency_of_pkg_nevrs"]:
                dep_name = pkg_id_to_name(dep_id)
                pkgs_data[arch][pkg_name]["required_by"].add(dep_name)

            # Required in a workload?
            if pkg_data["in_workload_conf_ids_req"]:
                pkgs_data[arch][pkg_name]["required_in_workloads"] = True

            # Level number - runtime or buildroot
            # Keep the lower level number (so runtime gets priority before build)
            # Levels:
            #   0 - runtime
            #   1 and higher - buildroot
            #
            if pkg_data["level_number"] < pkgs_data[arch][pkg_name]["level_number"]:
                pkgs_data[arch][pkg_name]["level_number"] = pkg_data["level_number"]
        
    log("  Original NVRs: {}".format(len(downloaded_pkg_data["pkgs"])))
    log("  Names:")
    for arch, pkgs in pkgs_data.items():
        log("    {}:    {}".format(arch, len(pkgs)))
    log("Done!")
    log("")


    # Initiate the repos

    log("Initiating repos...")

    repos = {}
    #
    # repos[arch][repo_name]...
    #
    for arch in settings["allowed_arches"]:
        repos[arch] = {}

        for repo_name, repo_arches in settings["repos"].items():
            if arch not in repo_arches:
                continue

            repos[arch][repo_name] = set()

    log("Done!")
    log("")


    # Record wishes
    #
    # This adds repo names into pkgs_data[arch][pkg_name]["user_repo_wishes"]
    #

    log("Recording people's wishes...")

    wishes_hardcoded = {

        # I found these in this in an old file here:
        # https://github.com/minimization/content-resolver-input/blob/main/configs/eln-repo-split.yaml
        # It's a start!
        "BaseOS": [
            "attr",
            "authselect",
            "autofs",
            "base-doc",
            "bash",
            "bash-completion",
            "bc",
            "chrony",
            "cockpit",
            "cockpit-bridge",
            "cockpit-system",
            "cockpit-ws",
            "coreutils",
            "coreutils-common",
            "coreutils-single",
            "cpio",
            "cronie",
            "curl",
            "dhcp-client",
            "dnf",
            "dracut",
            "fedora-release-eln",
            "fedora-repos-eln",
            "file",
            "glibc",
            "grep",
            "grub2-common",
            "iptables",
            "iptables-arptables",
            "iptables-ebtables",
            "iptables-utils",
            "kernel",
            "kmod",
            "linux-firmware",
            "lvm2",
            "lz4",
            "lzo",
            "nftables",
            "openssh-clients",
            "openssh-server",
            "openssl",
            "passwd",
            "policycoreutils",
            "procps-ng",
            "psmisc",
            "rpm",
            "selinux-policy",
            "selinux-policy-targeted",
            "strace",
            "sudo",
            "systemd",
            "yum",
        ],

        # It's the default, so leaving it empty
        "AppStream" : [],

        # These are packages in CRB in ELN at the time of writing the script
        # and at the same time explicitly required in workloads
        "CRB": [
            "ModemManager",
            "ModemManager-devel",
            "ModemManager-glib-devel",
            "NetworkManager-libnm-devel",
            "OpenIPMI-devel",
            "Xaw3d-devel",
            "accel-config-devel",
            "asciidoc-doc",
            "babl-devel",
            "babl-devel-docs",
            "bison-devel",
            "bluez-libs-devel",
            "boost-b2",
            "boost-doc",
            "boost-doctools",
            "boost-examples",
            "boost-graph-mpich",
            "boost-graph-openmpi",
            "boost-mpich",
            "boost-mpich-devel",
            "boost-mpich-python3",
            "boost-openmpi",
            "boost-openmpi-devel",
            "boost-openmpi-python3",
            "boost-static",
            "cryptsetup-devel",
            "cups-filters-devel",
            "daxctl-devel",
            "devhelp",
            "device-mapper-devel",
            "device-mapper-event-devel",
            "docbook-style-dsssl",
            "docbook-utils",
            "docbook5-schemas",
            "dovecot",
            "dovecot-devel",
            "doxygen",
            "doxygen-latex",
            "dyninst-devel",
            "dyninst-doc",
            "dyninst-testsuite",
            "eigen3-devel",
            "evolution-devel",
            "flatpak",
            "freeipmi-devel",
            "fuse-devel",
            "gcc-plugin-devel",
            "gdbm-devel",
            "ghostscript",
            "ghostscript-tools-fonts",
            "ghostscript-tools-printing",
            "glib2-static",
            "glibc-nss-devel",
            "glibc-static",
            "graphviz-devel",
            "graphviz-gd",
            "gvfs",
            "help2man",
            "http-parser-devel",
            "imath-devel",
            "jasper-devel",
            "java-11-openjdk-demo-fastdebug",
            "java-11-openjdk-demo-slowdebug",
            "java-11-openjdk-devel-fastdebug",
            "java-11-openjdk-devel-slowdebug",
            "java-11-openjdk-fastdebug",
            "java-11-openjdk-headless-fastdebug",
            "java-11-openjdk-headless-slowdebug",
            "java-11-openjdk-jmods-fastdebug",
            "java-11-openjdk-jmods-slowdebug",
            "java-11-openjdk-slowdebug",
            "java-11-openjdk-src-fastdebug",
            "java-11-openjdk-src-slowdebug",
            "java-11-openjdk-static-libs-fastdebug",
            "java-11-openjdk-static-libs-slowdebug",
            "kernel-cross-headers",
            "kernel-tools-libs-devel",
            "ksc",
            "latex2html",
            "libbabeltrace-devel",
            "libburn-devel",
            "libdnf-devel",
            "libfabric-devel",
            "libgphoto2-devel",
            "libgs-devel",
            "libica-devel",
            "libisoburn-devel",
            "libisofs-devel",
            "libjose-devel",
            "libknet1",
            "libknet1-devel",
            "libluksmeta-devel",
            "libmad",
            "libmaxminddb-devel",
            "libnfsidmap-devel",
            "libocxl-devel",
            "libpwquality-devel",
            "librabbitmq-devel",
            "librados-devel",
            "librbd-devel",
            "librdkafka-devel",
            "libreoffice-sdk",
            "libreoffice-sdk-doc",
            "librtas-devel",
            "librx",
            "librx-devel",
            "libsemanage-devel",
            "libsepol-static",
            "libservicelog-devel",
            "libsndfile-devel",
            "libstdc++-static",
            "libtirpc-devel",
            "libvpd-devel",
            "linuxdoc-tools",
            "lvm2-devel",
            "mariadb-connector-c-test",
            "mariadb-devel",
            "mariadb-embedded-devel",
            "mariadb-test",
            "memkind-devel",
            "mpich",
            "nautilus",
            "ndctl-devel",
            "netpbm-devel",
            "netpbm-doc",
            "opencryptoki-devel",
            "opencsd-devel",
            "openexr-devel",
            "openjade",
            "opensm-devel",
            "opensp",
            "opensp-devel",
            "papi-testsuite",
            "perl-SGMLSpm",
            "perl-Test-NoWarnings",
            "perl-Unicode-EastAsianWidth",
            "ppp",
            "ppp-devel",
            "python3",
            "python3-mpich",
            "python3-openmpi",
            "python3-tkinter",
            "python3-wcwidth",
            "qatlib-devel",
            "qclib-devel",
            "qt5-qtbase-static",
            "qt5-qtdeclarative-static",
            "qt5-qttools-static",
            "rpcsvc-proto-devel",
            "rubygem-diff-lcs",
            "rubygem-rspec",
            "rubygem-rspec-core",
            "rubygem-rspec-expectations",
            "rubygem-rspec-mocks",
            "rubygem-rspec-support",
            "rubygem-thread_order",
            "s390utils-devel",
            "sblim-cmpi-devel",
            "sblim-sfcc-devel",
            "sendmail-milter",
            "sendmail-milter-devel",
            "shim-unsigned-x64",
            "spice-protocol",
            "swig",
            "swig-doc",
            "swig-gdb",
            "tcl",
            "tesseract-devel",
            "texi2html",
            "texinfo",
            "texinfo-tex",
            "texlive-lib-devel",
            "tk",
            "tog-pegasus-devel",
            "tpm2-abrmd-devel",
            "tpm2-tss-devel",
            "tss2-devel",
            "unixODBC-devel",
            "urw-base35-fonts-devel",
            "uuid-devel",
            "volume_key-devel",
            "xmltoman",
            "zlib-static",
        ],

        # all in this repo in ELN at the time of writing this script
        "HA": [
            "booth",
            "booth-arbitrator",
            "booth-core",
            "booth-site",
            "booth-test",
            "corosync",
            "corosync-qdevice",
            "corosync-qnetd",
            "corosynclib",
            "corosynclib-devel",
            "fence-agents-aliyun",
            "fence-agents-all",
            "fence-agents-amt-ws",
            "fence-agents-apc",
            "fence-agents-apc-snmp",
            "fence-agents-aws",
            "fence-agents-azure-arm",
            "fence-agents-bladecenter",
            "fence-agents-brocade",
            "fence-agents-cisco-mds",
            "fence-agents-cisco-ucs",
            "fence-agents-drac5",
            "fence-agents-eaton-snmp",
            "fence-agents-emerson",
            "fence-agents-eps",
            "fence-agents-gce",
            "fence-agents-heuristics-ping",
            "fence-agents-hpblade",
            "fence-agents-ibmblade",
            "fence-agents-ifmib",
            "fence-agents-ilo-moonshot",
            "fence-agents-ilo-mp",
            "fence-agents-ilo-ssh",
            "fence-agents-ilo2",
            "fence-agents-intelmodular",
            "fence-agents-ipdu",
            "fence-agents-ipmilan",
            "fence-agents-kdump",
            "fence-agents-lpar",
            "fence-agents-mpath",
            "fence-agents-openstack",
            "fence-agents-redfish",
            "fence-agents-rhevm",
            "fence-agents-rsa",
            "fence-agents-rsb",
            "fence-agents-sbd",
            "fence-agents-scsi",
            "fence-agents-vmware-rest",
            "fence-agents-vmware-soap",
            "fence-agents-wti",
            "fence-agents-zvm",
            "ha-cloud-support",
            "libknet1",
            "libknet1-compress-bzip2-plugin",
            "libknet1-compress-lz4-plugin",
            "libknet1-compress-lzma-plugin",
            "libknet1-compress-lzo2-plugin",
            "libknet1-compress-plugins-all",
            "libknet1-compress-zlib-plugin",
            "libknet1-compress-zstd-plugin",
            "libknet1-crypto-nss-plugin",
            "libknet1-crypto-openssl-plugin",
            "libknet1-crypto-plugins-all",
            "libknet1-plugins-all",
            "libnozzle1",
            "libqb-devel",
            "libtool-ltdl-devel",
            "openwsman-python3",
            "pacemaker",
            "pacemaker-cli",
            "pacemaker-cluster-libs",
            "pacemaker-cts",
            "pacemaker-doc",
            "pacemaker-libs",
            "pacemaker-libs-devel",
            "pacemaker-nagios-plugins-metadata",
            "pacemaker-remote",
            "pacemaker-schemas",
            "pcs",
            "pcs-snmp",
            "resource-agents",
            "resource-agents-cloud",
            "resource-agents-paf",
            "sbd",
            "spausedd",
        ],

        # all in this repo in ELN at the time of writing this script
        "RS": [
            "booth",
            "booth-arbitrator",
            "booth-core",
            "booth-site",
            "booth-test",
            "corosync",
            "corosync-qdevice",
            "corosync-qnetd",
            "corosynclib",
            "corosynclib-devel",
            "ctdb",
            "dlm",
            "fence-agents-aliyun",
            "fence-agents-all",
            "fence-agents-amt-ws",
            "fence-agents-apc",
            "fence-agents-apc-snmp",
            "fence-agents-aws",
            "fence-agents-azure-arm",
            "fence-agents-bladecenter",
            "fence-agents-brocade",
            "fence-agents-cisco-mds",
            "fence-agents-cisco-ucs",
            "fence-agents-drac5",
            "fence-agents-eaton-snmp",
            "fence-agents-emerson",
            "fence-agents-eps",
            "fence-agents-gce",
            "fence-agents-heuristics-ping",
            "fence-agents-hpblade",
            "fence-agents-ibmblade",
            "fence-agents-ifmib",
            "fence-agents-ilo-moonshot",
            "fence-agents-ilo-mp",
            "fence-agents-ilo-ssh",
            "fence-agents-ilo2",
            "fence-agents-intelmodular",
            "fence-agents-ipdu",
            "fence-agents-ipmilan",
            "fence-agents-kdump",
            "fence-agents-lpar",
            "fence-agents-mpath",
            "fence-agents-openstack",
            "fence-agents-redfish",
            "fence-agents-rhevm",
            "fence-agents-rsa",
            "fence-agents-rsb",
            "fence-agents-sbd",
            "fence-agents-scsi",
            "fence-agents-vmware-rest",
            "fence-agents-vmware-soap",
            "fence-agents-wti",
            "fence-agents-zvm",
            "gfs2-utils",
            "ha-cloud-support",
            "libknet1",
            "libknet1-compress-bzip2-plugin",
            "libknet1-compress-lz4-plugin",
            "libknet1-compress-lzma-plugin",
            "libknet1-compress-lzo2-plugin",
            "libknet1-compress-plugins-all",
            "libknet1-compress-zlib-plugin",
            "libknet1-compress-zstd-plugin",
            "libknet1-crypto-nss-plugin",
            "libknet1-crypto-openssl-plugin",
            "libknet1-crypto-plugins-all",
            "libknet1-plugins-all",
            "libnozzle1",
            "libqb-devel",
            "libtool-ltdl-devel",
            "openwsman-python3",
            "pacemaker",
            "pacemaker-cli",
            "pacemaker-cluster-libs",
            "pacemaker-cts",
            "pacemaker-doc",
            "pacemaker-libs",
            "pacemaker-libs-devel",
            "pacemaker-nagios-plugins-metadata",
            "pacemaker-remote",
            "pacemaker-schemas",
            "pcs",
            "pcs-snmp",
            "resource-agents",
            "resource-agents-cloud",
            "resource-agents-paf",
            "sbd",
            "spausedd",
        ],

        # all in this repo in ELN at the time of writing this script
        "RT": [
            "kernel-rt",
            "kernel-rt-core",
            "kernel-rt-debug",
            "kernel-rt-debug-core",
            "kernel-rt-debug-devel",
            "kernel-rt-debug-modules",
            "kernel-rt-debug-modules-extra",
            "kernel-rt-devel",
            "kernel-rt-modules",
            "kernel-rt-modules-extra",
            "realtime-setup",
            "rteval",
            "rteval-loads",
            "tuned-profiles-realtime",
        ],

        # all in this repo in ELN at the time of writing this script
        "NFV": [
            "kernel-rt",
            "kernel-rt-core",
            "kernel-rt-debug",
            "kernel-rt-debug-core",
            "kernel-rt-debug-devel",
            "kernel-rt-debug-kvm",
            "kernel-rt-debug-modules",
            "kernel-rt-debug-modules-extra",
            "kernel-rt-devel",
            "kernel-rt-kvm",
            "kernel-rt-modules",
            "kernel-rt-modules-extra",
            "realtime-setup",
            "rteval",
            "rteval-loads",
            "tuned-profiles-nfv",
            "tuned-profiles-nfv-guest",
            "tuned-profiles-nfv-host",
            "tuned-profiles-realtime",
        ],

        # all in this repo in ELN at the time of writing this script
        "SAP": [
            "compat-locales-sap",
            "compat-locales-sap-common",
            "resource-agents-sap",
            "sap-cluster-connector",
            "tuned-profiles-sap",
            "vhostmd",
            "vm-dump-metrics",
        ],

        # all in this repo in ELN at the time of writing this script
        "SAPHANA": [
            "resource-agents-sap-hana",
            "resource-agents-sap-hana-scaleout",
            "rhel-system-roles-sap",
            "tuned-profiles-sap-hana",
            "vhostmd",
            "vm-dump-metrics",
        ]
    }

    for arch, pkg_names in pkgs_data.items():
        for pkg_name in pkg_names:
            for wish_repo_name, wish_pkg_names in wishes_hardcoded.items():

                if pkg_name not in wish_pkg_names:
                    continue

                if wish_repo_name not in settings["repos"]:
                    log("ERROR: {}: {} repo is unknown".format(pkg_name, wish_repo_name))
                    continue

                if arch not in settings["repos"][wish_repo_name]:
                    log("ERROR: {}: {} repo doesn't have {}".format(pkg_name, wish_repo_name, arch))
                    continue

                pkgs_data[arch][pkg_name]["user_repo_wishes"].add(wish_repo_name)
                log("  {} - {} - {}".format(arch, pkg_name, pkgs_data[arch][pkg_name]["user_repo_wishes"]))

    log("Done!")
    log("")


    # Do the sorting based on what people want

    log("Doing the sorting based on what people want...")

    for arch, arch_pkgs_data in pkgs_data.items():
        log("  {}...".format(arch))

        # First the packages themselves, not the deps
        for pkg_name, pkg_data in arch_pkgs_data.items():

            # Runtime package processing
            if pkg_data["level_number"] == 0:
                if "BaseOS" in pkg_data["user_repo_wishes"]:
                    repos[arch]["BaseOS"].add(pkg_name)

            # Buildroot package processing
            else:
                if "CRB" in pkg_data["user_repo_wishes"]:
                    repos[arch]["CRB"].add(pkg_name)

        # And now the deps
        moved_deps = set()
        while True:

            moved_deps_len = len(moved_deps)

            for pkg_name, pkg_data in arch_pkgs_data.items():

                # Runtime package processing
                if pkg_data["level_number"] == 0:
                    for required_by in pkg_data["required_by"]:
                        if required_by in repos[arch]["BaseOS"]:
                            repos[arch]["BaseOS"].add(pkg_name)
                            moved_deps.add(pkg_name)

                # Buildroot package processing
                else:
                    for required_by in pkg_data["required_by"]:
                        if required_by in repos[arch]["CRB"]:
                            repos[arch]["CRB"].add(pkg_name)
                            moved_deps.add(pkg_name)

            # If no more packages have been moved, stop
            if moved_deps_len == len(moved_deps):
                break
    
        del moved_deps
        del moved_deps_len

    log("Done!")
    log("")


    # Default placement:
    # - everything that's runtime (Environment, Required, Dependency) goes to AppStream
    # - everything that's build (Base Buildroot, Buildroot level N) goes to buildroot-only

    log("Put everything else in its default place")

    for arch, arch_repos in repos.items():
        log("  {}...".format(arch))

        # See what's already in a repo
        pkgs_in_repos = set()
        for repo_name, repo_pkgs in arch_repos.items():
            pkgs_in_repos.update(repo_pkgs)
        
        for pkg_name, pkg_data in pkgs_data[arch].items():

            # Skip everything that's already in a repo
            if pkg_name in pkgs_in_repos:
                continue

            # Runtime package processing
            if pkg_data["level_number"] == 0:
                repos[arch]["AppStream"].add(pkg_name)

            # Buildroot package processing
            else:
                repos[arch]["buildroot-only"].add(pkg_name)
        
        del pkgs_in_repos

    log("Done!")
    log("")


    # Separate CRB
    # Yep, some workloads are meant to be in CRB. So let's just do that.
    # Pull out everything that's marked as CRB, unless it can't be pulled out because something else requires it.
    # And then pull out everything that's only needed by CRB packages.

    log("Pulling out CRB")

    for arch, arch_repos in repos.items():
        log("  {}...".format(arch))

        # Put all packages the users want in CRB here
        crb_packages = set()

        for pkg_name in repos[arch]["AppStream"]:
            pkg_data = pkgs_data[arch][pkg_name]
            
            if "CRB" in pkg_data["user_repo_wishes"]:
                crb_packages.add(pkg_name)
        
        # Validate it's possible. That means packages in
        # 'crb_packages' can only be required by packages
        # in 'crb_packages'. If that's not the case,
        # take them out.
        crb_packages_impossible = set()

        while True:

            crb_packages_impossible_len = len(crb_packages_impossible)

            for pkg_name in crb_packages:
                pkg_data = pkgs_data[arch][pkg_name]

                for required_by in pkg_data["required_by"]:
                    if required_by not in crb_packages:
                        crb_packages_impossible.add(pkg_name)
                
                del pkg_data
            
            crb_packages = crb_packages - crb_packages_impossible
            
            # If no more changes, end the while loop
            if crb_packages_impossible_len == len(crb_packages_impossible):
                break
        
        del crb_packages_impossible
        
        repos[arch]["AppStream"] = repos[arch]["AppStream"] - crb_packages
        repos[arch]["CRB"].update(crb_packages)

        # Add dependencies that are only needed for CRB
        # However, don't move "required" packages here,
        # because the default behavior for these is to be in AppStream.

        while True:

            crb_packages_len = len(crb_packages)
        
            for pkg_name in repos[arch]["AppStream"]:
                pkg_data = pkgs_data[arch][pkg_name]

                if pkg_data["required_in_workloads"]:
                    continue

                crb_candidate = True

                for required_by in pkg_data["required_by"]:
                    if required_by in repos[arch]["AppStream"]:
                        crb_candidate = False
                
                if crb_candidate:
                    crb_packages.add(pkg_name)
                
                del crb_candidate
                del pkg_data

            repos[arch]["AppStream"] = repos[arch]["AppStream"] - crb_packages
            repos[arch]["CRB"].update(crb_packages)

            # If nothing else has been pulled out, end the while loop
            if crb_packages_len == len(crb_packages):
                break
        
        del crb_packages
        del crb_packages_len

    log("Done!")
    log("")
    

    # Moving packages from buildroot-only to CRB if any other package from
    # the same SRPM is in BaseOS, AppStream, or CRB

    log("Moving packages from buildroot-only to CRB...")

    for arch, arch_pkgs_data in pkgs_data.items():
        log("  {}...".format(arch))

        shipped_srpm_names = set()
        rpms_to_move = set()

        # Get all the shipped SRPM names
        for repo in ["AppStream", "BaseOS", "CRB"]:
            repo_pkgs = repos[arch][repo]

            for pkg_name in repo_pkgs:
                srpm_name = pkgs_data[arch][pkg_name]["source_name"]
                shipped_srpm_names.add(srpm_name)
        
        del repo_pkgs

        # And find RPMs of those SRPMs in buildroot-only
        for pkg_name in repos[arch]["buildroot-only"]:
            srpm_name = pkgs_data[arch][pkg_name]["source_name"]

            if srpm_name in shipped_srpm_names:
                rpms_to_move.add(pkg_name)

        del shipped_srpm_names
        del srpm_name

        # Move them
        for pkg_name in rpms_to_move:
            repos[arch]["CRB"].add(pkg_name)
            repos[arch]["buildroot-only"].remove(pkg_name)

        del rpms_to_move

        # Move the deps
        moved_deps = set()
        while True:

            moved_deps_len = len(moved_deps)

            for pkg_name, pkg_data in arch_pkgs_data.items():

                if pkg_name not in repos[arch]["buildroot-only"]:
                    continue

                for required_by in pkg_data["required_by"]:
                    if required_by in repos[arch]["CRB"]:
                        repos[arch]["CRB"].add(pkg_name)
                        repos[arch]["buildroot-only"].discard(pkg_name)
                        moved_deps.add(pkg_name)

            if moved_deps_len == len(moved_deps):
                break

        del moved_deps_len
        del moved_deps

    log("Done!")
    log("")


    # Addons

    log("Separating addons (HA, NFV, RS, RT, SAP, SAPHANA)")

    for arch, arch_pkgs_data in pkgs_data.items():
        log("  {}...".format(arch))

        removed_addon_pkgs = set()

        for pkg_name, pkg_data in arch_pkgs_data.items():

            # I only want to deal with AppStream packages here
            if pkg_name not in repos[arch]["AppStream"]:
                continue

            for repo_wish in pkg_data["user_repo_wishes"]:
                if repo_wish in settings["addon_repos"]:

                    repos[arch][repo_wish].add(pkg_name)

                    # If it's in BaseOS, something requires it, so it can't be removed
                    if pkg_name in repos[arch]["BaseOS"]:
                        continue
                    
                    repos[arch]["AppStream"].discard(pkg_name)
                    removed_addon_pkgs.add(pkg_name)

        
        # If they're needed in the main repos,
        # add them back to the main repos
        returned_addon_pkgs = set()
        while True:
            returned_addon_pkgs_len = len(returned_addon_pkgs)

            for pkg_name in removed_addon_pkgs:
                pkg_data = pkgs_data[arch][pkg_name]

                for required_by in pkg_data["required_by"]:
                    if required_by in repos[arch]["AppStream"]:
                        repos[arch]["AppStream"].add(pkg_name)
                        returned_addon_pkgs.add(pkg_name)
            
            if returned_addon_pkgs_len == len(returned_addon_pkgs):
                break

        del removed_addon_pkgs
        del returned_addon_pkgs
        del returned_addon_pkgs_len
        del pkg_data

    log("Done!")
    log("")


    # Printing

    log("")
    log("Wheeeeeeeee!")
    log("")

    all_pkgs = set()

    for arch, arch_repos in repos.items():
        log(arch)
        for repo, repo_pkgs in arch_repos.items():
            log("  {}:  {}".format(repo, len(repo_pkgs)))
        log("")

    


    # Print the prepopulate.json

    prepopulate_json = {}

    for arch, arch_repos in repos.items():

        for repo_name, repo_pkgs in arch_repos.items():
            
            if repo_name == "buildroot-only":
                continue
                
            if real_repo_name(repo_name) not in prepopulate_json:
                prepopulate_json[real_repo_name(repo_name)] = {}
            
            if arch not in prepopulate_json[real_repo_name(repo_name)]:
                prepopulate_json[real_repo_name(repo_name)][arch] = {}
            
            for pkg_name in repo_pkgs:
                pkg_data = pkgs_data[arch][pkg_name]
                srpm_name = pkg_data["source_name"]

                rpm_arches = pkg_data["rpm_arches"]

                if srpm_name not in prepopulate_json[real_repo_name(repo_name)][arch]:
                    prepopulate_json[real_repo_name(repo_name)][arch][srpm_name] = []

                for rpm_arch in rpm_arches:
                    pkg_name_dot_arch = "{name}.{rpm_arch}".format(
                        name=pkg_name,
                        rpm_arch=rpm_arch
                    )
                    prepopulate_json[real_repo_name(repo_name)][arch][srpm_name].append(pkg_name_dot_arch)

    
    print(json.dumps(prepopulate_json, indent=4))




if __name__ == "__main__":
    main()