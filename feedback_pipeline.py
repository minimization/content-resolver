#!/usr/bin/python3

import argparse, yaml, tempfile, os, subprocess, json, jinja2, datetime, copy, re, dnf, pprint, urllib.request, sys, koji
import concurrent.futures
import rpm_showme as showme
from functools import lru_cache
import multiprocessing, asyncio


# Features of this new release
# - multiarch from the ground up!
# - more resilient
# - better internal data structure
# - user-defined views


###############################################################################
### Help ######################################################################
###############################################################################


# Configs:
#   TYPE:           KEY:          ID:
# - repo            repos         repo_id
# - env_conf        envs          env_id
# - workload_conf   workloads     workload_id
# - label           labels        label_id
# - conf_view       views         view_id
#
# Data:
#   TYPE:         KEY:                 ID:
# - pkg           pkgs/repo_id/arch    NEVR
# - env           envs                 env_id:repo_id:arch_id
# - workload      workloads            workload_id:env_id:repo_id:arch_id
# - view          views                view_id:repo_id:arch_id
#
#
#



###############################################################################
### Some initial stuff ########################################################
###############################################################################


class SettingsError(Exception):
    # Error in global settings for Feedback Pipeline
    # Settings to be implemented, now hardcoded below
    pass


class ConfigError(Exception):
    # Error in user-provided configs
    pass


class RepoDownloadError(Exception):
    # Error in downloading repodata
    pass


class BuildGroupAnalysisError(Exception):
    # Error while processing buildroot build group
    pass


class KojiRootLogError(Exception):
    pass


class AnalysisError(Exception):
    pass


def log(msg):
    print(msg, file=sys.stderr)


def err_log(msg):
    print("ERROR LOG:  {}".format(msg), file=sys.stderr)


class SetEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, set):
            return list(obj)
        if isinstance(obj, jinja2.Environment):
            return ""
        return json.JSONEncoder.default(self, obj)


def dump_data(path, data):
    with open(path, 'w') as file:
        json.dump(data, file, cls=SetEncoder)


def load_data(path):
    with open(path, 'r') as file:
        data = json.load(file)
    return data


def size(num, suffix='B'):
    for unit in ['','k','M','G']:
        if abs(num) < 1024.0:
            return "%3.1f %s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f %s%s" % (num, 'T', suffix)


def pkg_id_to_name(pkg_id):
    pkg_name = pkg_id.rsplit("-",2)[0]
    return pkg_name


def workload_id_to_conf_id(workload_id):
    workload_conf_id = workload_id.split(":")[0]
    return workload_conf_id


def pkg_placeholder_name_to_id(placeholder_name):
    placeholder_id = "{name}-000-placeholder.placeholder".format(name=placeholder_name)
    return placeholder_id


def pkg_placeholder_name_to_nevr(placeholder_name):
    placeholder_id = "{name}-000-placeholder".format(name=placeholder_name)
    return placeholder_id


def url_to_id(url):

    # strip the protocol
    if url.startswith("https://"):
        url = url[8:]
    elif url.startswith("http://"):
        url = url[7:]
    
    # strip a potential leading /
    if url.endswith("/"):
        url = url[:-1]
    
    # and replace all non-alphanumeric characters with -
    regex = re.compile('[^0-9a-zA-Z]')
    return regex.sub("-", url)


def datetime_now_string():
    return datetime.datetime.now().strftime("%m/%d/%Y, %H:%M:%S")


def load_settings():
    settings = {}

    parser = argparse.ArgumentParser()
    parser.add_argument("configs", help="Directory with YAML configuration files. Only files ending with '.yaml' are accepted.")
    parser.add_argument("output", help="Directory to contain the output.")
    parser.add_argument("--use-cache", dest="use_cache", action='store_true', help="Use local data instead of pulling Content Resolver. Saves a lot of time! Needs a 'cache_data.json' file at the same location as the script is at.")
    parser.add_argument("--dev-buildroot", dest="dev_buildroot", action='store_true', help="Buildroot grows pretty quickly. Use a fake one for development.")
    parser.add_argument("--dnf-cache-dir", dest="dnf_cache_dir_override", help="Override the dnf cache_dir.")
    args = parser.parse_args()

    settings["configs"] = args.configs
    settings["output"] = args.output
    settings["use_cache"] = args.use_cache
    settings["dev_buildroot"] = args.dev_buildroot
    settings["dnf_cache_dir_override"] = args.dnf_cache_dir_override

    settings["root_log_deps_cache_path"] = "cache_root_log_deps.json"

    settings["max_subprocesses"] = 10

    settings["allowed_arches"] = ["armv7hl", "aarch64", "ppc64le", "s390x", "x86_64"]

    settings["weird_packages_that_can_not_be_installed"] = ["glibc32"]

    return settings





###############################################################################
### Loading user-provided configs #############################################
###############################################################################

# Configs:
#   TYPE:         KEY:          ID:
# - repo          repos         repo_id
# - env           envs          env_id
# - workload      workloads     workload_id
# - label         labels        label_id
# - view          views         view_id

def _load_config_repo(document_id, document, settings):
    raise NotImplementedError("Repo v1 is not supported. Please migrate to repo v2.")


def _load_config_repo_v2(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

        # Where does this repository come from?
        # Right now, only Fedora repositories are supported,
        # defined by their releasever.
        config["source"] = {}
        config["source"]["repos"] = {}
        if "repos" not in config["source"]:
            raise KeyError

        # Only Fedora repos supported at this time.
        # Fedora release.
        config["source"]["releasever"] = str(document["data"]["source"]["releasever"])

        # List of architectures
        config["source"]["architectures"] = []
        for arch_raw in document["data"]["source"]["architectures"]:
            arch = str(arch_raw)
            if arch not in settings["allowed_arches"]:
                log("  Warning: {file}.yaml lists an unsupported architecture: {arch}. Moving on...".format(
                    file=document_id,
                    arch=arch))
                continue
            config["source"]["architectures"].append(str(arch))
    except KeyError:
        raise ConfigError("'{file}.yaml' - There's something wrong with the mandatory fields. Sorry I don't have more specific info.".format(file=document_id))
    

    for id, repo_data in document["data"]["source"]["repos"].items():
        name = repo_data.get("name", id)
        priority = repo_data.get("priority", 100)
        exclude = repo_data.get("exclude", None)
        limit_arches = repo_data.get("limit_arches", None)
        koji_api_url = repo_data.get("koji_api_url", None)
        koji_files_url = repo_data.get("koji_files_url", None)

        config["source"]["repos"][id] = {}
        config["source"]["repos"][id]["id"] = id
        config["source"]["repos"][id]["name"] = name
        try:
            config["source"]["repos"][id]["baseurl"] = repo_data["baseurl"]
        except KeyError:
            raise ConfigError("'{file}.yaml' - is invalid. Repo {id} doesn't list baseurl.".format(
                file=yml_file,
                id=id))
        config["source"]["repos"][id]["priority"] = priority
        config["source"]["repos"][id]["exclude"] = exclude
        config["source"]["repos"][id]["limit_arches"] = limit_arches
        config["source"]["repos"][id]["koji_api_url"] = koji_api_url
        config["source"]["repos"][id]["koji_files_url"] = koji_files_url

    # Step 2: Optional fields

    config["source"]["composeinfo"] = document["data"]["source"].get("composeinfo", None)

    config["source"]["base_buildroot_override"] = []
    if "base_buildroot_override" in document["data"]["source"]:
        for pkg_name in document["data"]["source"]["base_buildroot_override"]:
            config["source"]["base_buildroot_override"].append(str(pkg_name))

    return config


def _load_config_env(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

        # Different instances of the environment, one per repository.
        config["repositories"] = []
        for repo in document["data"]["repositories"]:
            config["repositories"].append(str(repo))
        
        # Packages defining this environment.
        # This list includes packages for all
        # architectures — that's the one to use by default.
        config["packages"] = []
        for pkg in document["data"]["packages"]:
            config["packages"].append(str(pkg))
        
        # Labels connect things together.
        # Workloads get installed in environments with the same label.
        # They also get included in views with the same label.
        config["labels"] = []
        for repo in document["data"]["labels"]:
            config["labels"].append(str(repo))

    except KeyError:
        raise ConfigError("'{file}.yaml' - There's something wrong with the mandatory fields. Sorry I don't have more specific info.".format(file=document_id))

    # Step 2: Optional fields

    # Architecture-specific packages.
    config["arch_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["arch_packages"][arch] = []
    if "arch_packages" in document["data"]:
        for arch, pkgs in document["data"]["arch_packages"].items():
            if arch not in settings["allowed_arches"]:
                log("  Warning: {file}.yaml lists an unsupported architecture: {arch}. Moving on...".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            for pkg_raw in pkgs:
                pkg = str(pkg_raw)
                config["arch_packages"][arch].append(pkg)
    
    # Extra installation options.
    # The following are now supported:
    # - "include-docs" - include documentation packages
    # - "include-weak-deps" - automatically pull in "recommends" weak dependencies
    config["options"] = []
    if "options" in document["data"]:
        if "include-docs" in document["data"]["options"]:
            config["options"].append("include-docs")
        if "include-weak-deps" in document["data"]["options"]:
            config["options"].append("include-weak-deps")
    
    # Comps groups
    config["groups"] = []
    if "groups" in document["data"]:
        for module in document["data"]["groups"]:
            config["groups"].append(module)

    return config


def _load_config_workload(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])
        
        # Labels connect things together.
        # Workloads get installed in environments with the same label.
        # They also get included in views with the same label.
        config["labels"] = []
        for repo in document["data"]["labels"]:
            config["labels"].append(str(repo))

    except KeyError:
        raise ConfigError("'{file}.yaml' - There's something wrong with the mandatory fields. Sorry I don't have more specific info.".format(file=document_id))

    # Step 2: Optional fields

    # Packages defining this workload.
    # This list includes packages for all
    # architectures — that's the one to use by default.
    config["packages"] = []
    # This workaround allows for "packages" to be left empty in the config
    try:
        for pkg in document["data"]["packages"]:
            config["packages"].append(str(pkg))
    except (TypeError, KeyError):
        pass # Because it's now valid
        #log("  Warning: {file} has an empty 'packages' field defined which is invalid. Moving on...".format(
        #    file=document_id
        #))

    # Architecture-specific packages.
    config["arch_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["arch_packages"][arch] = []
    if "arch_packages" in document["data"]:
        for arch, pkgs in document["data"]["arch_packages"].items():
            if arch not in settings["allowed_arches"]:
                log("  Warning: {file}.yaml lists an unsupported architecture: {arch}. Moving on...".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            # This workaround allows for "arch_packages/ARCH" to be left empty in the config
            try:
                for pkg_raw in pkgs:
                    pkg = str(pkg_raw)
                    config["arch_packages"][arch].append(pkg)
            except TypeError:
                log("  Warning: {file} has an empty 'arch_packages/{arch}' field defined which is invalid. Moving on...".format(
                    file=document_id,
                    arch=arch
                ))
    
    # Extra installation options.
    # The following are now supported:
    # - "include-docs" - include documentation packages
    # - "include-weak-deps" - automatically pull in "recommends" weak dependencies
    config["options"] = []
    if "options" in document["data"]:
        if "include-docs" in document["data"]["options"]:
            config["options"].append("include-docs")
        if "include-weak-deps" in document["data"]["options"]:
            config["options"].append("include-weak-deps")
        if "strict" in document["data"]["options"]:
            config["options"].append("strict")
    
    # Disable module streams.
    config["modules_disable"] = []
    if "modules_disable" in document["data"]:
        for module in document["data"]["modules_disable"]:
            config["modules_disable"].append(module)
    
    # Enable module streams.
    config["modules_enable"] = []
    if "modules_enable" in document["data"]:
        for module in document["data"]["modules_enable"]:
            config["modules_enable"].append(module)
    
    # Comps groups
    config["groups"] = []
    if "groups" in document["data"]:
        for module in document["data"]["groups"]:
            config["groups"].append(module)

    # Package placeholders
    # Add packages to the workload that don't exist (yet) in the repositories.
    config["package_placeholders"] = {}
    config["package_placeholders"]["pkgs"] = {}
    config["package_placeholders"]["srpms"] = {}
    if "package_placeholders" in document["data"]:

        # So yeah, this is kind of awful but also brilliant.
        # The old syntax of package placeholders was a dict,
        # but the new one is a list. 
        # So I can be backwards compatible!
        #
        # The old format
        if isinstance(document["data"]["package_placeholders"], dict):
            for pkg_name, pkg_data in document["data"]["package_placeholders"].items():
                pkg_description = pkg_data.get("description", "Description not provided.")
                pkg_requires = pkg_data.get("requires", [])
                pkg_buildrequires = pkg_data.get("buildrequires", [])
                limit_arches = pkg_data.get("limit_arches", None)
                srpm = pkg_data.get("srpm", pkg_name)

                config["package_placeholders"]["pkgs"][pkg_name] = {}
                config["package_placeholders"]["pkgs"][pkg_name]["name"] = pkg_name
                config["package_placeholders"]["pkgs"][pkg_name]["description"] = pkg_description
                config["package_placeholders"]["pkgs"][pkg_name]["requires"] = pkg_requires
                config["package_placeholders"]["pkgs"][pkg_name]["limit_arches"] = limit_arches
                config["package_placeholders"]["pkgs"][pkg_name]["srpm"] = srpm

                # Because the old format isn't great, it needs a srpm
                # to be defined for every rpm, including the build requires.
                # That can cause conflicts. 
                # So the best thing (I think) is to just take the first one and ignore
                # the others. This is better than nothing. And people should move
                # to the new format anyway.
                if srpm not in config["package_placeholders"]["srpms"]:
                    config["package_placeholders"]["srpms"][srpm] = {}
                    config["package_placeholders"]["srpms"][srpm]["name"] = srpm
                    config["package_placeholders"]["srpms"][srpm]["buildrequires"] = pkg_buildrequires
                    config["package_placeholders"]["srpms"][srpm]["limit_arches"] = limit_arches

        
        #
        # The new format
        elif isinstance(document["data"]["package_placeholders"], list):
            for srpm in document["data"]["package_placeholders"]:
                srpm_name = srpm["srpm_name"]
                if not srpm_name:
                    continue

                build_dependencies = srpm.get("build_dependencies", [])
                limit_arches = srpm.get("limit_arches", [])
                rpms = srpm.get("rpms", [])

                config["package_placeholders"]["srpms"][srpm_name] = {}
                config["package_placeholders"]["srpms"][srpm_name]["name"] = srpm_name
                config["package_placeholders"]["srpms"][srpm_name]["buildrequires"] = build_dependencies
                config["package_placeholders"]["srpms"][srpm_name]["limit_arches"] = limit_arches

                for rpm in rpms:
                    rpm_name = rpm.get("rpm_name", None)
                    if not rpm_name:
                        continue
                    
                    description = rpm.get("description", "Description not provided.")
                    dependencies = rpm.get("dependencies", [])
                    rpm_limit_arches = rpm.get("limit_arches", [])

                    if limit_arches and rpm_limit_arches:
                        rpm_limit_arches = list(set(limit_arches) & set(rpm_limit_arches))

                    config["package_placeholders"]["pkgs"][rpm_name] = {}
                    config["package_placeholders"]["pkgs"][rpm_name]["name"] = rpm_name
                    config["package_placeholders"]["pkgs"][rpm_name]["description"] = description
                    config["package_placeholders"]["pkgs"][rpm_name]["requires"] = dependencies
                    config["package_placeholders"]["pkgs"][rpm_name]["limit_arches"] = rpm_limit_arches
                    config["package_placeholders"]["pkgs"][rpm_name]["srpm"] = srpm_name


    return config


def _load_config_label(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

    except KeyError:
        raise ConfigError("'{file}.yaml' - There's something wrong with the mandatory fields. Sorry I don't have more specific info.".format(file=document_id))

    # Step 2: Optional fields
    # none here

    return config


def _load_config_compose_view(document_id, document, settings):
    config = {}
    config["id"] = document_id
    config["type"] = "compose"

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

        # Labels connect things together.
        # Workloads get installed in environments with the same label.
        # They also get included in views with the same label.
        config["labels"] = []
        for repo in document["data"]["labels"]:
            config["labels"].append(str(repo))

        # Choose one repository that gets used as a source.
        config["repository"] = str(document["data"]["repository"])

    except KeyError:
        raise ConfigError("'{file}.yaml' - There's something wrong with the mandatory fields. Sorry I don't have more specific info.".format(file=document_id))

    # Step 2: Optional fields

    # Buildroot strategy
    config["buildroot_strategy"] = "none"
    if "buildroot_strategy" in document["data"]:
        if str(document["data"]["buildroot_strategy"]) in ["none", "root_logs"]:
            config["buildroot_strategy"] = str(document["data"]["buildroot_strategy"])
    
    # Limit this view only to the following architectures
    config["architectures"] = []
    if "architectures" in document["data"]:
        for arch in document["data"]["architectures"]:
            config["architectures"].append(str(arch))
    if not len(config["architectures"]):
        config["architectures"] = settings["allowed_arches"]
    
    # Packages to be flagged as unwanted
    config["unwanted_packages"] = []
    if "unwanted_packages" in document["data"]:
        for pkg in document["data"]["unwanted_packages"]:
            config["unwanted_packages"].append(str(pkg))

    # Packages to be flagged as unwanted  on specific architectures
    config["unwanted_arch_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["unwanted_arch_packages"][arch] = []
    if "unwanted_arch_packages" in document["data"]:
        for arch, pkgs in document["data"]["unwanted_arch_packages"].items():
            if arch not in settings["allowed_arches"]:
                log("  Warning: {file}.yaml lists an unsupported architecture: {arch}. Moving on...".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            for pkg_raw in pkgs:
                pkg = str(pkg_raw)
                config["unwanted_arch_packages"][arch].append(pkg)
    
    # SRPMs (components) to be flagged as unwanted
    config["unwanted_source_packages"] = []
    if "unwanted_source_packages" in document["data"]:
        for pkg in document["data"]["unwanted_source_packages"]:
            config["unwanted_source_packages"].append(str(pkg))

    return config


def _load_config_addon_view(document_id, document, settings):
    config = {}
    config["id"] = document_id
    config["type"] = "addon"

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

        # Labels connect things together.
        # Workloads get installed in environments with the same label.
        # They also get included in views with the same label.
        config["labels"] = []
        for repo in document["data"]["labels"]:
            config["labels"].append(str(repo))

        # Choose one repository that gets used as a source.
        config["base_view_id"] = str(document["data"]["base_view_id"])

    except KeyError:
        raise ConfigError("'{file}.yaml' - There's something wrong with the mandatory fields. Sorry I don't have more specific info.".format(file=document_id))
    
    # Step 2: Optional fields

    # Packages to be flagged as unwanted
    config["unwanted_packages"] = []
    if "unwanted_packages" in document["data"]:
        for pkg in document["data"]["unwanted_packages"]:
            config["unwanted_packages"].append(str(pkg))

    # Packages to be flagged as unwanted  on specific architectures
    config["unwanted_arch_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["unwanted_arch_packages"][arch] = []
    if "unwanted_arch_packages" in document["data"]:
        for arch, pkgs in document["data"]["unwanted_arch_packages"].items():
            if arch not in settings["allowed_arches"]:
                log("  Warning: {file}.yaml lists an unsupported architecture: {arch}. Moving on...".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            for pkg_raw in pkgs:
                pkg = str(pkg_raw)
                config["unwanted_arch_packages"][arch].append(pkg)
    
    # SRPMs (components) to be flagged as unwanted
    config["unwanted_source_packages"] = []
    if "unwanted_source_packages" in document["data"]:
        for pkg in document["data"]["unwanted_source_packages"]:
            config["unwanted_source_packages"].append(str(pkg))



    return config


def _load_config_unwanted(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

        # Labels connect things together.
        # Workloads get installed in environments with the same label.
        # They also get included in views with the same label.
        config["labels"] = []
        for repo in document["data"]["labels"]:
            config["labels"].append(str(repo))
    
    except KeyError:
        raise ConfigError("'{file}.yaml' - There's something wrong with the mandatory fields. Sorry I don't have more specific info.".format(file=document_id))
    
    # Step 2: Optional fields

    # Packages to be flagged as unwanted
    config["unwanted_packages"] = []
    if "unwanted_packages" in document["data"]:
        for pkg in document["data"]["unwanted_packages"]:
            config["unwanted_packages"].append(str(pkg))

    # Packages to be flagged as unwanted  on specific architectures
    config["unwanted_arch_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["unwanted_arch_packages"][arch] = []
    if "unwanted_arch_packages" in document["data"]:
        for arch, pkgs in document["data"]["unwanted_arch_packages"].items():
            if arch not in settings["allowed_arches"]:
                log("  Warning: {file}.yaml lists an unsupported architecture: {arch}. Moving on...".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            for pkg_raw in pkgs:
                pkg = str(pkg_raw)
                config["unwanted_arch_packages"][arch].append(pkg)
    
    # SRPMs (components) to be flagged as unwanted
    config["unwanted_source_packages"] = []
    if "unwanted_source_packages" in document["data"]:
        for pkg in document["data"]["unwanted_source_packages"]:
            config["unwanted_source_packages"].append(str(pkg))

    # SRPMs (components) to be flagged as unwanted on specific architectures
    config["unwanted_arch_source_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["unwanted_arch_source_packages"][arch] = []
    if "unwanted_arch_source_packages" in document["data"]:
        for arch, pkgs in document["data"]["unwanted_arch_source_packages"].items():
            if arch not in settings["allowed_arches"]:
                log("  Warning: {file}.yaml lists an unsupported architecture: {arch}. Moving on...".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            for pkg_raw in pkgs:
                pkg = str(pkg_raw)
                config["unwanted_arch_source_packages"][arch].append(pkg)
    return config


def _load_config_buildroot(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

        # What view is this for
        config["view_id"] = str(document["data"]["view_id"])

    except KeyError:
        raise ConfigError("'{file}.yaml' - There's something wrong with the mandatory fields. Sorry I don't have more specific info.".format(file=document_id))

    # Step 2: Optional fields
    config["base_buildroot"] = {}
    for arch in settings["allowed_arches"]:
        config["base_buildroot"][arch] = []
    if "base_buildroot" in document["data"]:
        for arch, pkgs in document["data"]["base_buildroot"].items():
            if arch not in settings["allowed_arches"]:
                log("  Warning: {file}.yaml lists an unsupported architecture: {arch}. Moving on...".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            if pkgs:
                for pkg_raw in pkgs:
                    pkg = str(pkg_raw)
                    config["base_buildroot"][arch].append(pkg)

    config["source_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["source_packages"][arch] = {}
    if "source_packages" in document["data"]:
        for arch, srpms_dict in document["data"]["source_packages"].items():
            if arch not in settings["allowed_arches"]:
                log("  Warning: {file}.yaml lists an unsupported architecture: {arch}. Moving on...".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            if not srpms_dict:
                continue
            for srpm_name, srpm_data in srpms_dict.items():
                requires = []
                if "requires" in srpm_data:
                    try:
                        for pkg_raw in srpm_data["requires"]:
                            requires.append(str(pkg_raw))
                    except TypeError:
                        log("  Warning: {file} has an empty 'requires' field defined which is invalid. Moving on...".format(
                            file=document_id
                        ))
                        continue
                
                config["source_packages"][arch][str(srpm_name)] = {}
                config["source_packages"][arch][str(srpm_name)]["requires"] = requires

    return config


def _load_json_data_buildroot_pkg_relations(document_id, document, settings):
    config = {}
    config["id"] = document_id

    try:
        # View ID
        config["view_id"] = document["data"]["view_id"]

        # Arch
        arch = document["data"]["arch"]
        if arch not in settings["allowed_arches"]:
            raise ConfigError("Error: '{file}.json' lists an unsupported architecture: {arch}.".format(
                file=document_id,
                arch=arch
            ))
        config["arch"] = arch

        #pkg_relations
        config["pkg_relations"] = document["data"]["pkgs"]
        
    except KeyError:
        raise ConfigError("'{file}.yaml' - There's something wrong with the mandatory fields. Sorry I don't have more specific info.".format(file=document_id))
    
    return config


def get_configs(settings):
    log("")

    directory = settings["configs"]

    if "allowed_arches" not in settings:
        err_log("System error: allowed_arches not configured")
        raise SettingsError
    
    if not settings["allowed_arches"]:
        err_log("System error: no allowed_arches not configured")
        raise SettingsError

    configs = {}

    configs["repos"] = {}
    configs["envs"] = {}
    configs["workloads"] = {}
    configs["labels"] = {}
    configs["views"] = {}
    configs["unwanteds"] = {}
    configs["buildroots"] = {}
    configs["buildroot_pkg_relations"] = {}


    # Step 1: Load all configs
    serious_error_messages = set()
    log("Loading yaml files...")
    log("---------------------")
    for yml_file in os.listdir(directory):
        # Only accept yaml files
        if not yml_file.endswith(".yaml"):
            continue
        
        document_id = yml_file.split(".yaml")[0]

        try:
            with open(os.path.join(directory, yml_file), "r") as file:
                # Safely load the config
                try:
                    document = yaml.safe_load(file)
                except yaml.YAMLError as err:
                    raise ConfigError("Error loading a config '{filename}': {err}".format(
                                filename=yml_file,
                                err=err))
                
                # Only accept yaml files stating their purpose!
                if not ("document" in document and "version" in document):
                    raise ConfigError("'{file}.yaml' - doesn't specify the 'document' and/or the 'version' field.".format(file=yml_file))


                # === Case: Repository config ===
                if document["document"] == "feedback-pipeline-repository":
                    if document["version"] == 1:
                        configs["repos"][document_id] = _load_config_repo(document_id, document, settings)
                    
                    elif document["version"] == 2:
                        configs["repos"][document_id] = _load_config_repo_v2(document_id, document, settings)

                # === Case: Environment config ===
                if document["document"] == "feedback-pipeline-environment":
                    configs["envs"][document_id] = _load_config_env(document_id, document, settings)

                # === Case: Workload config ===
                if document["document"] == "feedback-pipeline-workload":
                    configs["workloads"][document_id] = _load_config_workload(document_id, document, settings)
                
                # === Case: Label config ===
                if document["document"] == "feedback-pipeline-label":
                    configs["labels"][document_id] = _load_config_label(document_id, document, settings)

                # === Case: View config ===
                #  (Also including the legacy "feedback-pipeline-compose-view" for backwards compatibility)
                if document["document"] in ["feedback-pipeline-view", "feedback-pipeline-compose-view"]:
                    configs["views"][document_id] = _load_config_compose_view(document_id, document, settings)

                # === Case: View addon config ===
                if document["document"] == "feedback-pipeline-view-addon":
                    configs["views"][document_id] = _load_config_addon_view(document_id, document, settings)

                # === Case: Unwanted config ===
                if document["document"] == "feedback-pipeline-unwanted":
                    configs["unwanteds"][document_id] = _load_config_unwanted(document_id, document, settings)

                # === Case: Buildroot config ===
                if document["document"] == "feedback-pipeline-buildroot":
                    configs["buildroots"][document_id] = _load_config_buildroot(document_id, document, settings)

        except ConfigError as err:
            serious_error_messages.add(str(err))
            continue

    if serious_error_messages:
        log("")
        log("  -------------------------------------------------------------------------")
        log("  | 🔥 ERRORS FOUND 🔥  (the following files will be excluded)")
        log("  |")

        for message in serious_error_messages:
            log("  |  {}".format(message))
        log("  -------------------------------------------------------------------------")
        log("")
    else:
        log("")
        log("  ✅ No serious errors found.")
        log("")
    
    log("  Done!")
    log("")
    log("")
    
    # Step 1.5: Load all external data sources
    serious_error_messages = set()
    log("Loading json files...")
    log("---------------------")
    log("")
    for json_file in os.listdir(directory):
        # Only accept yaml files
        if not json_file.endswith(".json"):
            continue
        
        document_id = json_file.split(".json")[0]

        try:
            try:
                json_data = load_data(os.path.join(directory, json_file))
            except:
                raise ConfigError("Error loading a JSON data file '{filename}': {err}".format(
                                filename=json_file,
                                err=err))
            
            # Only accept json files stating their purpose!
            if not ("document_type" in json_data and "version" in json_data):
                raise ConfigError("'{file}.yaml' - doesn't specify the 'document' and/or the 'version' field.".format(file=json_file))


            # === Case: Buildroot pkg relations data ===
            if json_data["document_type"] == "buildroot-binary-relations":
                configs["buildroot_pkg_relations"][document_id] = _load_json_data_buildroot_pkg_relations(document_id, json_data, settings)

        except ConfigError as err:
            serious_error_messages.add(str(err))
            continue
    
    if serious_error_messages:
        log("")
        log("  -------------------------------------------------------------------------")
        log("  | 🔥 ERRORS FOUND 🔥  (the following files will be excluded)")
        log("  |")

        for message in serious_error_messages:
            log("  |  {}".format(message))
        log("  -------------------------------------------------------------------------")
        log("")
    else:
        log("")
        log("  ✅ No serious errors found.")
        log("")

        
    log("  Done!")
    log("")
    log("")



    # Step 2: cross check configs for references and other validation
    #
    # Also, for some configs, such as the view addon, add some fields
    # from its base view
    #
    # They need to be checked in some logical order, because
    # invalid configs get removed. So, for example, I need to first
    # check the compose views before checking the addon views,
    # because if I need to ditch a proper view, I can't use any
    # of the addon views either.
    log("Additional validations...")
    log("-------------------------")

    # Delete views referencing non-existing repos
    for view_conf_id, view_conf in configs["views"].items():
        if view_conf["type"] == "compose":
            if view_conf["repository"] not in configs["repos"]:
                log("   View {} is referencing a non-existing repository. Removing it.".format(view_conf_id))
                del configs["views"][view_conf_id]

    # Delete add-on views referencing non-existing or invalid base view
    for view_conf_id, view_conf in configs["views"].items():
        if view_conf["type"] == "addon":
            base_view_id = view_conf["base_view_id"]
            if base_view_id not in configs["views"]:
                log("   Addon view {} is referencing a non-existing base_view_id. Removing it.".format(view_conf_id))
                del configs["views"][view_conf_id]
    
            else:
                base_view = configs["views"][base_view_id]
                if base_view["type"] != "compose":
                    log("   Addon view {} is referencing an addon base_view_id, which is not supported. Removing it.".format(view_conf_id))
                    del configs["views"][view_conf_id]

            
            # Ading some extra fields onto the addon view
            configs["views"][view_conf_id]["repository"] = configs["views"][base_view_id]["repository"]
            configs["views"][view_conf_id]["architectures"] = configs["views"][base_view_id]["architectures"]
    
    # Adjust view architecture based on repository architectures
    for view_conf_id, view_conf in configs["views"].items():
        if view_conf["type"] == "compose":
            if not len(view_conf["architectures"]):
                view_conf["architectures"] = settings["allowed_arches"]
            actual_arches = set()
            for arch in view_conf["architectures"]:
                repo_id = view_conf["repository"]
                if arch in configs["repos"][repo_id]["source"]["architectures"]:
                    actual_arches.add(arch)
            view_conf["architectures"] = sorted(list(actual_arches))

    # Adjust addon view architecture based on its base view architectures        
    for view_conf_id, view_conf in configs["views"].items():
        if view_conf["type"] == "addon":
            if not len(view_conf["architectures"]):
                view_conf["architectures"] = settings["allowed_arches"]
            actual_arches = set()
            for arch in view_conf["architectures"]:
                base_view_id = view_conf["base_view_id"]
                if arch in configs["views"][base_view_id]["architectures"]:
                    actual_arches.add(arch)
            view_conf["architectures"] = sorted(list(actual_arches))
    
    # FIXME: Check other configs, too!


    log("")
    log("  ✅ No serious errors found.")
    log("")
    log("  Done!")
    log("")
    log("")

    log("Summary:")
    log("--------")
    log("")
    
    log("Standard yaml configs:")
    log("  - {} repositories".format(len(configs["repos"])))
    log("  - {} environments".format(len(configs["envs"])))
    log("  - {} workloads".format(len(configs["workloads"])))
    #log("  - {} labels".format(len(configs["labels"])))
    log("  - {} views".format(len(configs["views"])))
    log("  - {} exclusion lists".format(len(configs["unwanteds"])))
    log("")
    log("Additional configs: (soon to be deprecated)")
    log("  - {} buildroots".format(len(configs["buildroots"])))
    log("  - {} buildroot pkg relations JSONs".format(len(configs["buildroot_pkg_relations"])))
    log("")
    


    return configs





###############################################################################
### Analysis ##################################################################
###############################################################################


class Analyzer():

    ###############################################################################
    ### Analyzing stuff! ##########################################################
    ###############################################################################

    # Configs:
    #   TYPE:           KEY:          ID:
    # - repo            repos         repo_id
    # - env_conf        envs          env_id
    # - workload_conf   workloads     workload_id
    # - label           labels        label_id
    # - conf_view       views         view_id
    #
    # Data:
    #   TYPE:         KEY:                 ID:
    # - pkg           pkgs/repo_id/arch    NEVR
    # - env           envs                 env_id:repo_id:arch_id
    # - workload      workloads            workload_id:env_id:repo_id:arch_id
    # - view          views                view_id:repo_id:arch_id
    #
    # self.tmp_dnf_cachedir is either "dnf_cachedir" in TemporaryDirectory or set by --dnf-cache-dir
    # contents:
    # - "dnf_cachedir-{repo}-{arch}"                     <-- internal DNF cache
    #
    # self.tmp_installroots is "installroots" in TemporaryDirectory
    # contents:
    # - "dnf_generic_installroot-{repo}-{arch}"          <-- installroots for _analyze_pkgs
    # - "dnf_env_installroot-{env_conf}-{repo}-{arch}"   <-- installroots for envs and workloads and buildroots
    #
    # 

    def __init__(self, configs, settings):
        self.workload_queue = {}
        self.workload_queue_counter_total = 0
        self.workload_queue_counter_current = 0
        self.current_subprocesses = 0

        self.configs = configs
        self.settings = settings

        self.global_dnf_repo_cache = {}
        self.data = {}
        self.cache = {}

        self.cache["root_log_deps"] = {}
        self.cache["root_log_deps"]["current"] = {}
        self.cache["root_log_deps"]["next"] = {}

        try:
            self.cache["root_log_deps"]["current"] = load_data(self.settings["root_log_deps_cache_path"])
        except FileNotFoundError:
            pass

    
    def _load_repo_cached(self, base, repo, arch):
        repo_id = repo["id"]

        exists = True
        
        if repo_id not in self.global_dnf_repo_cache:
            exists = False
            self.global_dnf_repo_cache[repo_id] = {}

        elif arch not in self.global_dnf_repo_cache[repo_id]:
            exists = False
        
        if exists:
            #log("  Loading repos from cache...")

            for repo in self.global_dnf_repo_cache[repo_id][arch]:
                base.repos.add(repo)

        else:
            #log("  Loading repos using DNF...")

            for repo_name, repo_data in repo["source"]["repos"].items():
                if repo_data["limit_arches"]:
                    if arch not in repo_data["limit_arches"]:
                        #log("  Skipping {} on {}".format(repo_name, arch))
                        continue
                #log("  Including {}".format(repo_name))

                additional_repo = dnf.repo.Repo(
                    name=repo_name,
                    parent_conf=base.conf
                )
                additional_repo.baseurl = repo_data["baseurl"]
                additional_repo.priority = repo_data["priority"]
                additional_repo.exclude = repo_data["exclude"]
                base.repos.add(additional_repo)

            # Additional repository (if configured)
            #if repo["source"]["additional_repository"]:
            #    additional_repo = dnf.repo.Repo(name="additional-repository",parent_conf=base.conf)
            #    additional_repo.baseurl = [repo["source"]["additional_repository"]]
            #    additional_repo.priority = 1
            #    base.repos.add(additional_repo)

            # All other system repos
            #base.read_all_repos()

            self.global_dnf_repo_cache[repo_id][arch] = []
            for repo in base.repos.iter_enabled():
                self.global_dnf_repo_cache[repo_id][arch].append(repo)
    

    def _analyze_pkgs(self, repo, arch):
        log("Analyzing pkgs for {repo_name} ({repo_id}) {arch}".format(
                repo_name=repo["name"],
                repo_id=repo["id"],
                arch=arch
            ))
        
        with dnf.Base() as base:

            base.conf.debuglevel = 0
            base.conf.errorlevel = 0
            base.conf.logfilelevel = 0

            # Local DNF cache
            cachedir_name = "dnf_cachedir-{repo}-{arch}".format(
                repo=repo["id"],
                arch=arch
            )
            base.conf.cachedir = os.path.join(self.tmp_dnf_cachedir, cachedir_name)

            # Generic installroot
            root_name = "dnf_generic_installroot-{repo}-{arch}".format(
                repo=repo["id"],
                arch=arch
            )
            base.conf.installroot = os.path.join(self.tmp_installroots, root_name)

            # Architecture
            base.conf.arch = arch
            base.conf.ignorearch = True

            # Releasever
            base.conf.substitutions['releasever'] = repo["source"]["releasever"]

            for repo_name, repo_data in repo["source"]["repos"].items():
                if repo_data["limit_arches"]:
                    if arch not in repo_data["limit_arches"]:
                        log("  Skipping {} on {}".format(repo_name, arch))
                        continue
                log("  Including {}".format(repo_name))

                additional_repo = dnf.repo.Repo(
                    name=repo_name,
                    parent_conf=base.conf
                )
                additional_repo.baseurl = repo_data["baseurl"]
                additional_repo.priority = repo_data["priority"]
                base.repos.add(additional_repo)

            # Additional repository (if configured)
            #if repo["source"]["additional_repository"]:
            #    additional_repo = dnf.repo.Repo(name="additional-repository",parent_conf=base.conf)
            #    additional_repo.baseurl = [repo["source"]["additional_repository"]]
            #    additional_repo.priority = 1
            #    base.repos.add(additional_repo)

            # Load repos
            log("  Loading repos...")
            #base.read_all_repos()


            # At this stage, I need to get all packages from the repo listed.
            # That also includes modular packages. Modular packages in non-enabled
            # streams would be normally hidden. So I mark all the available repos as
            # hotfix repos to make all packages visible, including non-enabled streams.
            for dnf_repo in base.repos.all():
                dnf_repo.module_hotfixes = True

            # This sometimes fails, so let's try at least N times
            # before totally giving up!
            MAX_TRIES = 10
            attempts = 0
            success = False
            while attempts < MAX_TRIES:
                try:
                    base.fill_sack(load_system_repo=False)
                    success = True
                    break
                except dnf.exceptions.RepoError as err:
                    attempts +=1
                    log("  Failed to download repodata. Trying again!")
            if not success:
                err = "Failed to download repodata while analyzing repo '{repo_name} ({repo_id}) {arch}".format(
                repo_name=repo["name"],
                repo_id=repo["id"],
                arch=arch
                )
                err_log(err)
                raise RepoDownloadError(err)

            # DNF query
            query = base.sack.query

            # Get all packages
            all_pkgs_set = set(query())
            pkgs = {}
            for pkg_object in all_pkgs_set:
                pkg_nevra = "{name}-{evr}.{arch}".format(
                    name=pkg_object.name,
                    evr=pkg_object.evr,
                    arch=pkg_object.arch
                )
                pkg_nevr = "{name}-{evr}".format(
                    name=pkg_object.name,
                    evr=pkg_object.evr
                )
                pkg = {}
                pkg["id"] = pkg_nevra
                pkg["name"] = pkg_object.name
                pkg["evr"] = pkg_object.evr
                pkg["nevr"] = pkg_nevr
                pkg["arch"] = pkg_object.arch
                pkg["installsize"] = pkg_object.installsize
                pkg["description"] = pkg_object.description
                #pkg["provides"] = pkg_object.provides
                #pkg["requires"] = pkg_object.requires
                #pkg["recommends"] = pkg_object.recommends
                #pkg["suggests"] = pkg_object.suggests
                pkg["summary"] = pkg_object.summary
                pkg["source_name"] = pkg_object.source_name
                pkg["sourcerpm"] = pkg_object.sourcerpm
                pkg["reponame"] = pkg_object.reponame

                pkgs[pkg_nevra] = pkg
            
            # There shouldn't be multiple packages of the same NVR
            # But the world isn't as simple! So add all reponames
            # to every package, in case it's in multiple repos

            repo_priorities = {}
            for repo_name, repo_data in repo["source"]["repos"].items():
                repo_priorities[repo_name] = repo_data["priority"]

            for pkg_object in all_pkgs_set:
                pkg_nevra = "{name}-{evr}.{arch}".format(
                    name=pkg_object.name,
                    evr=pkg_object.evr,
                    arch=pkg_object.arch
                )
                reponame = pkg_object.reponame

                if "all_reponames" not in pkgs[pkg_nevra]:
                    pkgs[pkg_nevra]["all_reponames"] = set()
                
                pkgs[pkg_nevra]["all_reponames"].add(reponame)
            
            for pkg_nevra, pkg in pkgs.items():
                pkgs[pkg_nevra]["highest_priority_reponames"] = set()

                all_repo_priorities = set()
                for reponame in pkg["all_reponames"]:
                    all_repo_priorities.add(repo_priorities[reponame])
                
                highest_repo_priority = sorted(list(all_repo_priorities))[0]

                for reponame in pkg["all_reponames"]:
                    if repo_priorities[reponame] == highest_repo_priority:
                        pkgs[pkg_nevra]["highest_priority_reponames"].add(reponame)

            log("  Done!  ({pkg_count} packages in total)".format(
                pkg_count=len(pkgs)
            ))
            log("")

        return pkgs
    
    def _analyze_repos(self):
        self.data["repos"] = {}
        for _,repo in self.configs["repos"].items():
            repo_id = repo["id"]
            self.data["pkgs"][repo_id] = {}
            self.data["repos"][repo_id] = {}
            for arch in repo["source"]["architectures"]:
                self.data["pkgs"][repo_id][arch] = self._analyze_pkgs(repo, arch)
            
            # Reading the optional composeinfo
            self.data["repos"][repo_id]["compose_date"] = None
            self.data["repos"][repo_id]["compose_days_ago"] = 0
            if repo["source"]["composeinfo"]:
                # At this point, this is all I can do. Hate me or not, it gets us
                # what we need and won't brake anything in case things go badly. 
                try:
                    with urllib.request.urlopen(repo["source"]["composeinfo"]) as response:
                        composeinfo_raw_response = response.read()

                    composeinfo_data = json.loads(composeinfo_raw_response)
                    self.data["repos"][repo_id]["composeinfo"] = composeinfo_data

                    compose_date = datetime.datetime.strptime(composeinfo_data["payload"]["compose"]["date"], "%Y%m%d").date()
                    self.data["repos"][repo_id]["compose_date"] = compose_date.strftime("%Y-%m-%d")

                    date_now = datetime.datetime.now().date()
                    self.data["repos"][repo_id]["compose_days_ago"] = (date_now - compose_date).days

                except:
                    pass

    def _analyze_package_relations(self, dnf_query, package_placeholders = None):
        relations = {}

        for pkg in dnf_query:
            pkg_id = "{name}-{evr}.{arch}".format(
                name=pkg.name,
                evr=pkg.evr,
                arch=pkg.arch
            )
            
            required_by = set()
            recommended_by = set()
            suggested_by = set()

            for dep_pkg in dnf_query.filter(requires=[pkg]):
                dep_pkg_id = "{name}-{evr}.{arch}".format(
                    name=dep_pkg.name,
                    evr=dep_pkg.evr,
                    arch=dep_pkg.arch
                )
                required_by.add(dep_pkg_id)

            for dep_pkg in dnf_query.filter(recommends=[pkg]):
                dep_pkg_id = "{name}-{evr}.{arch}".format(
                    name=dep_pkg.name,
                    evr=dep_pkg.evr,
                    arch=dep_pkg.arch
                )
                recommended_by.add(dep_pkg_id)
            
            for dep_pkg in dnf_query.filter(suggests=[pkg]):
                dep_pkg_id = "{name}-{evr}.{arch}".format(
                    name=dep_pkg.name,
                    evr=dep_pkg.evr,
                    arch=dep_pkg.arch
                )
                suggested_by.add(dep_pkg_id)
            
            relations[pkg_id] = {}
            relations[pkg_id]["required_by"] = sorted(list(required_by))
            relations[pkg_id]["recommended_by"] = sorted(list(recommended_by))
            relations[pkg_id]["suggested_by"] = sorted(list(suggested_by))
            relations[pkg_id]["source_name"] = pkg.source_name
            relations[pkg_id]["reponame"] = pkg.reponame
        
        if package_placeholders:
            for placeholder_name,placeholder_data in package_placeholders.items():
                placeholder_id = pkg_placeholder_name_to_id(placeholder_name)

                relations[placeholder_id] = {}
                relations[placeholder_id]["required_by"] = []
                relations[placeholder_id]["recommended_by"] = []
                relations[placeholder_id]["suggested_by"] = []
                relations[placeholder_id]["reponame"] = None
            
            for placeholder_name,placeholder_data in package_placeholders.items():
                placeholder_id = pkg_placeholder_name_to_id(placeholder_name)
                for placeholder_dependency_name in placeholder_data["requires"]:
                    for pkg_id in relations:
                        pkg_name = pkg_id_to_name(pkg_id)
                        if pkg_name == placeholder_dependency_name:
                            relations[pkg_id]["required_by"].append(placeholder_id)
        
        return relations


    def _analyze_env_without_leaking(self, env_conf, repo, arch):

        # DNF leaks memory and file descriptors :/
        # 
        # So, this workaround runs it in a subprocess that should have its resources
        # freed when done!

        queue_result = multiprocessing.Queue()
        process = multiprocessing.Process(target=self._analyze_env_process, args=(queue_result, env_conf, repo, arch))
        process.start()
        process.join()

        # This basically means there was an exception in the processing and the process crashed
        if queue_result.empty():
            raise AnalysisError
        
        env = queue_result.get()

        return env


    def _analyze_env_process(self, queue_result, env_conf, repo, arch):

        env = self._analyze_env(env_conf, repo, arch)
        queue_result.put(env)


    def _analyze_env(self, env_conf, repo, arch):
        env = {}
        
        env["env_conf_id"] = env_conf["id"]
        env["pkg_ids"] = []
        env["repo_id"] = repo["id"]
        env["arch"] = arch

        env["pkg_relations"] = []

        env["errors"] = {}
        env["errors"]["non_existing_pkgs"] = []

        env["succeeded"] = True

        with dnf.Base() as base:

            base.conf.debuglevel = 0
            base.conf.errorlevel = 0
            base.conf.logfilelevel = 0

            # Local DNF cache
            cachedir_name = "dnf_cachedir-{repo}-{arch}".format(
                repo=repo["id"],
                arch=arch
            )
            base.conf.cachedir = os.path.join(self.tmp_dnf_cachedir, cachedir_name)

            # Environment installroot
            root_name = "dnf_env_installroot-{env_conf}-{repo}-{arch}".format(
                env_conf=env_conf["id"],
                repo=repo["id"],
                arch=arch
            )
            base.conf.installroot = os.path.join(self.tmp_installroots, root_name)

            # Architecture
            base.conf.arch = arch
            base.conf.ignorearch = True

            # Releasever
            base.conf.substitutions['releasever'] = repo["source"]["releasever"]

            # Additional DNF Settings
            base.conf.tsflags.append('justdb')
            base.conf.tsflags.append('noscripts')

            # Environment config
            if "include-weak-deps" not in env_conf["options"]:
                base.conf.install_weak_deps = False
            if "include-docs" not in env_conf["options"]:
                base.conf.tsflags.append('nodocs')

            # Load repos
            #log("  Loading repos...")
            #base.read_all_repos()
            self._load_repo_cached(base, repo, arch)

            # This sometimes fails, so let's try at least N times
            # before totally giving up!
            MAX_TRIES = 10
            attempts = 0
            success = False
            while attempts < MAX_TRIES:
                try:
                    base.fill_sack(load_system_repo=False)
                    success = True
                    break
                except dnf.exceptions.RepoError as err:
                    attempts +=1
                    log("  Failed to download repodata. Trying again!")
            if not success:
                err = "Failed to download repodata while analyzing environment '{env_conf}' from '{repo}' {arch}:".format(
                    env_conf=env_conf["id"],
                    repo=repo["id"],
                    arch=arch
                )
                err_log(err)
                raise RepoDownloadError(err)


            # Packages
            log("  Adding packages...")
            for pkg in env_conf["packages"]:
                try:
                    base.install(pkg)
                except dnf.exceptions.MarkingError:
                    env["errors"]["non_existing_pkgs"].append(pkg)
                    continue
            
            # Groups
            log("  Adding groups...")
            if env_conf["groups"]:
                base.read_comps(arch_filter=True)
            for grp_spec in env_conf["groups"]:
                group = base.comps.group_by_pattern(grp_spec)
                if not group:
                    env["errors"]["non_existing_pkgs"].append(grp_spec)
                    continue
                base.group_install(group.id, ['mandatory', 'default'])

            # Architecture-specific packages
            for pkg in env_conf["arch_packages"][arch]:
                try:
                    base.install(pkg)
                except dnf.exceptions.MarkingError:
                    env["errors"]["non_existing_pkgs"].append(pkg)
                    continue
            
            # Resolve dependencies
            log("  Resolving dependencies...")
            try:
                base.resolve()
            except dnf.exceptions.DepsolveError as err:
                err_log("Failed to analyze environment '{env_conf}' from '{repo}' {arch}:".format(
                        env_conf=env_conf["id"],
                        repo=repo["id"],
                        arch=arch
                    ))
                err_log("  - {err}".format(err=err))
                env["succeeded"] = False
                env["errors"]["message"] = str(err)
                return env

            # Write the result into RPMDB.
            # The transaction needs us to download all the packages. :(
            # So let's do that to make it happy.
            log("  Downloading packages...")
            base.download_packages(base.transaction.install_set)
            log("  Running DNF transaction, writing RPMDB...")
            try:
                base.do_transaction()
            except (dnf.exceptions.TransactionCheckError, dnf.exceptions.Error) as err:
                err_log("Failed to analyze environment '{env_conf}' from '{repo}' {arch}:".format(
                        env_conf=env_conf["id"],
                        repo=repo["id"],
                        arch=arch
                    ))
                err_log("  - {err}".format(err=err))
                env["succeeded"] = False
                env["errors"]["message"] = str(err)
                return env

            # DNF Query
            log("  Creating a DNF Query object...")
            query = base.sack.query().filterm(pkg=base.transaction.install_set)

            for pkg in query:
                pkg_id = "{name}-{evr}.{arch}".format(
                    name=pkg.name,
                    evr=pkg.evr,
                    arch=pkg.arch
                )
                env["pkg_ids"].append(pkg_id)
            
            env["pkg_relations"] = self._analyze_package_relations(query)

            log("  Done!  ({pkg_count} packages in total)".format(
                pkg_count=len(env["pkg_ids"])
            ))
            log("")
        
        return env


    def _analyze_envs(self):
        envs = {}

        # Look at all env configs...
        for env_conf_id, env_conf in self.configs["envs"].items():
            # For each of those, look at all repos it lists...
            for repo_id in env_conf["repositories"]:
                # And for each of the repo, look at all arches it supports.
                repo = self.configs["repos"][repo_id]
                for arch in repo["source"]["architectures"]:
                    # Now it has
                    #    all env confs *
                    #    repos each config lists *
                    #    archeas each repo supports
                    # Analyze all of that!
                    log("Analyzing {env_name} ({env_id}) from {repo_name} ({repo}) {arch}...".format(
                        env_name=env_conf["name"],
                        env_id=env_conf_id,
                        repo_name=repo["name"],
                        repo=repo_id,
                        arch=arch
                    ))

                    env_id = "{env_conf_id}:{repo_id}:{arch}".format(
                        env_conf_id=env_conf_id,
                        repo_id=repo_id,
                        arch=arch
                    )
                    envs[env_id] = self._analyze_env(env_conf, repo, arch)
                    
        self.data["envs"] = envs


    def _return_failed_workload_env_err(self, workload_conf, env_conf, repo, arch):
        workload = {}

        workload["workload_conf_id"] = workload_conf["id"]
        workload["env_conf_id"] = env_conf["id"]
        workload["repo_id"] = repo["id"]
        workload["arch"] = arch

        workload["pkg_env_ids"] = []
        workload["pkg_added_ids"] = []
        workload["pkg_placeholder_ids"] = []

        workload["pkg_relations"] = []

        workload["errors"] = {}
        workload["errors"]["non_existing_pkgs"] = []
        workload["succeeded"] = False
        workload["env_succeeded"] = False

        workload["errors"]["message"] = """
        Failed to analyze this workload because of an error while analyzing the environment.

        Please see the associated environment results for a detailed error message.
        """

        return workload


    def _analyze_workload(self, workload_conf, env_conf, repo, arch):

        workload = {}

        workload["workload_conf_id"] = workload_conf["id"]
        workload["env_conf_id"] = env_conf["id"]
        workload["repo_id"] = repo["id"]
        workload["arch"] = arch

        workload["pkg_env_ids"] = []
        workload["pkg_added_ids"] = []
        workload["pkg_placeholder_ids"] = []
        workload["srpm_placeholder_names"] = []

        workload["enabled_modules"] = []

        workload["pkg_relations"] = []

        workload["errors"] = {}
        workload["errors"]["non_existing_pkgs"] = []
        workload["errors"]["non_existing_placeholder_deps"] = []

        workload["warnings"] = {}
        workload["warnings"]["non_existing_pkgs"] = []
        workload["warnings"]["non_existing_placeholder_deps"] = []
        workload["warnings"]["message"] = None

        workload["succeeded"] = True
        workload["env_succeeded"] = True


        # Figure out the workload labels
        # It can only have labels that are in both the workload_conf and the env_conf
        workload["labels"] = list(set(workload_conf["labels"]) & set(env_conf["labels"]))

        with dnf.Base() as base:

            base.conf.debuglevel = 0
            base.conf.errorlevel = 0
            base.conf.logfilelevel = 0

            # Local DNF cache
            cachedir_name = "dnf_cachedir-{repo}-{arch}".format(
                repo=repo["id"],
                arch=arch
            )
            base.conf.cachedir = os.path.join(self.tmp_dnf_cachedir, cachedir_name)

            # Environment installroot
            # Since we're not writing anything into the installroot,
            # let's just use the base image's installroot!
            root_name = "dnf_env_installroot-{env_conf}-{repo}-{arch}".format(
                env_conf=env_conf["id"],
                repo=repo["id"],
                arch=arch
            )
            base.conf.installroot = os.path.join(self.tmp_installroots, root_name)

            # Architecture
            base.conf.arch = arch
            base.conf.ignorearch = True

            # Releasever
            base.conf.substitutions['releasever'] = repo["source"]["releasever"]

            # Environment config
            if "include-weak-deps" not in workload_conf["options"]:
                base.conf.install_weak_deps = False
            if "include-docs" not in workload_conf["options"]:
                base.conf.tsflags.append('nodocs')

            # Load repos
            #log("  Loading repos...")
            #base.read_all_repos()
            self._load_repo_cached(base, repo, arch)

            # Now I need to load the local RPMDB.
            # However, if the environment is empty, it wasn't created, so I need to treat
            # it differently. So let's check!
            if len(env_conf["packages"]) or len(env_conf["arch_packages"][arch]):
                # It's not empty! Load local data.
                base.fill_sack(load_system_repo=True)
            else:
                # It's empty. Treat it like we're using an empty installroot.
                # This sometimes fails, so let's try at least N times
                # before totally giving up!
                MAX_TRIES = 10
                attempts = 0
                success = False
                while attempts < MAX_TRIES:
                    try:
                        base.fill_sack(load_system_repo=False)
                        success = True
                        break
                    except dnf.exceptions.RepoError as err:
                        attempts +=1
                        #log("  Failed to download repodata. Trying again!")
                if not success:
                    err = "Failed to download repodata while analyzing workload '{workload_id} on '{env_id}' from '{repo}' {arch}...".format(
                            workload_id=workload_conf_id,
                            env_id=env_conf_id,
                            repo_name=repo["name"],
                            repo=repo_id,
                            arch=arch)
                    err_log(err)
                    raise RepoDownloadError(err)
            
            # Disabling modules
            if workload_conf["modules_disable"]:
                try:
                    #log("  Disabling modules...")
                    module_base = dnf.module.module_base.ModuleBase(base)
                    module_base.disable(workload_conf["modules_disable"])
                except dnf.exceptions.MarkingErrors as err:
                    workload["succeeded"] = False
                    workload["errors"]["message"] = str(err)
                    #log("  Failed!  (Error message will be on the workload results page.")
                    #log("")
                    return workload


            # Enabling modules
            if workload_conf["modules_enable"]:
                try:
                    #log("  Enabling modules...")
                    module_base = dnf.module.module_base.ModuleBase(base)
                    module_base.enable(workload_conf["modules_enable"])
                except dnf.exceptions.MarkingErrors as err:
                    workload["succeeded"] = False
                    workload["errors"]["message"] = str(err)
                    #log("  Failed!  (Error message will be on the workload results page.")
                    #log("")
                    return workload
            
            # Get a list of enabled modules
            # The official DNF API doesn't support it. I got this from the DNF folks
            # (thanks!) as a solution, but just keeping it in a generic try/except
            # as it's not an official API. 
            enabled_modules = set()
            try:
                all_modules = base._moduleContainer.getModulePackages()
                for module in all_modules:
                    if base._moduleContainer.isEnabled(module):
                        module_name = module.getName()
                        module_stream = module.getStream()
                        module_nsv = "{module_name}:{module_stream}".format(
                            module_name=module_name,
                            module_stream=module_stream
                        )
                        enabled_modules.add(module_nsv)
            except:
                #log("  Something went wrong with getting a list of enabled modules. (This uses non-API DNF calls. Skipping.)")
                enabled_modules = set()
            workload["enabled_modules"] = list(enabled_modules)


            # Packages
            #log("  Adding packages...")
            for pkg in workload_conf["packages"]:
                try:
                    base.install(pkg)
                except dnf.exceptions.MarkingError:
                    if pkg in self.settings["weird_packages_that_can_not_be_installed"]:
                        continue
                    else:
                        if "strict" in workload_conf["options"]:
                            workload["errors"]["non_existing_pkgs"].append(pkg)
                        else:
                            workload["warnings"]["non_existing_pkgs"].append(pkg)
                        continue
            
            # Groups
            #log("  Adding groups...")
            if workload_conf["groups"]:
                base.read_comps(arch_filter=True)
            for grp_spec in workload_conf["groups"]:
                group = base.comps.group_by_pattern(grp_spec)
                if not group:
                    workload["errors"]["non_existing_pkgs"].append(grp_spec)
                    continue
                base.group_install(group.id, ['mandatory', 'default'])
            
            
                # TODO: Mark group packages as required... the following code doesn't work
                #for pkg in group.packages_iter():
                #    print(pkg.name)
                #    workload_conf["packages"].append(pkg.name)
                
                    
            
            # Filter out the relevant package placeholders for this arch
            package_placeholders = {}
            for placeholder_name, placeholder_data in workload_conf["package_placeholders"]["pkgs"].items():
                # If this placeholder is not limited to just a usbset of arches, add it
                if not placeholder_data["limit_arches"]:
                    package_placeholders[placeholder_name] = placeholder_data
                # otherwise it is limited. In that case, only add it if the current arch is on its list
                elif arch in placeholder_data["limit_arches"]:
                    package_placeholders[placeholder_name] = placeholder_data
            
            # Same for SRPM placeholders
            srpm_placeholders = {}
            for placeholder_name, placeholder_data in workload_conf["package_placeholders"]["srpms"].items():
                # If this placeholder is not limited to just a usbset of arches, add it
                if not placeholder_data["limit_arches"]:
                    srpm_placeholders[placeholder_name] = placeholder_data
                # otherwise it is limited. In that case, only add it if the current arch is on its list
                elif arch in placeholder_data["limit_arches"]:
                    srpm_placeholders[placeholder_name] = placeholder_data

            # Dependencies of package placeholders
            #log("  Adding package placeholder dependencies...")
            for placeholder_name, placeholder_data in package_placeholders.items():
                for pkg in placeholder_data["requires"]:
                    try:
                        base.install(pkg)
                    except dnf.exceptions.MarkingError:
                        if "strict" in workload_conf["options"]:
                            workload["errors"]["non_existing_placeholder_deps"].append(pkg)
                        else:
                            workload["warnings"]["non_existing_placeholder_deps"].append(pkg)
                        continue

            # Architecture-specific packages
            for pkg in workload_conf["arch_packages"][arch]:
                try:
                    base.install(pkg)
                except dnf.exceptions.MarkingError:
                    if "strict" in workload_conf["options"]:
                        workload["errors"]["non_existing_pkgs"].append(pkg)
                    else:
                        workload["warnings"]["non_existing_pkgs"].append(pkg)
                    continue

            if workload["errors"]["non_existing_pkgs"] or workload["errors"]["non_existing_placeholder_deps"]:
                error_message_list = []
                if workload["errors"]["non_existing_pkgs"]:
                    error_message_list.append("The following required packages are not available:")
                    for pkg_name in workload["errors"]["non_existing_pkgs"]:
                        pkg_string = "  - {pkg_name}".format(
                            pkg_name=pkg_name
                        )
                        error_message_list.append(pkg_string)
                if workload["errors"]["non_existing_placeholder_deps"]:
                    error_message_list.append("The following dependencies of package placeholders are not available:")
                    for pkg_name in workload["errors"]["non_existing_placeholder_deps"]:
                        pkg_string = "  - {pkg_name}".format(
                            pkg_name=pkg_name
                        )
                        error_message_list.append(pkg_string)
                error_message = "\n".join(error_message_list)
                workload["succeeded"] = False
                workload["errors"]["message"] = str(error_message)
                #log("  Failed!  (Error message will be on the workload results page.")
                #log("")
                return workload
            
            if workload["warnings"]["non_existing_pkgs"] or workload["warnings"]["non_existing_placeholder_deps"]:
                error_message_list = []
                if workload["warnings"]["non_existing_pkgs"]:
                    error_message_list.append("The following required packages are not available (and were skipped):")
                    for pkg_name in workload["warnings"]["non_existing_pkgs"]:
                        pkg_string = "  - {pkg_name}".format(
                            pkg_name=pkg_name
                        )
                        error_message_list.append(pkg_string)
                if workload["warnings"]["non_existing_placeholder_deps"]:
                    error_message_list.append("The following dependencies of package placeholders are not available (and were skipped):")
                    for pkg_name in workload["warnings"]["non_existing_placeholder_deps"]:
                        pkg_string = "  - {pkg_name}".format(
                            pkg_name=pkg_name
                        )
                        error_message_list.append(pkg_string)
                error_message = "\n".join(error_message_list)
                workload["warnings"]["message"] = str(error_message)

            # Resolve dependencies
            #log("  Resolving dependencies...")
            try:
                base.resolve()
            except dnf.exceptions.DepsolveError as err:
                workload["succeeded"] = False
                workload["errors"]["message"] = str(err)
                #log("  Failed!  (Error message will be on the workload results page.")
                #log("")
                return workload

            # DNF Query
            #log("  Creating a DNF Query object...")
            query_env = base.sack.query()
            query_added = base.sack.query().filterm(pkg=base.transaction.install_set)
            pkgs_env = set(query_env.installed())
            pkgs_added = set(base.transaction.install_set)
            pkgs_all = set.union(pkgs_env, pkgs_added)
            query_all = base.sack.query().filterm(pkg=pkgs_all)
            
            for pkg in pkgs_env:
                pkg_id = "{name}-{evr}.{arch}".format(
                    name=pkg.name,
                    evr=pkg.evr,
                    arch=pkg.arch
                )
                workload["pkg_env_ids"].append(pkg_id)
            
            for pkg in pkgs_added:
                pkg_id = "{name}-{evr}.{arch}".format(
                    name=pkg.name,
                    evr=pkg.evr,
                    arch=pkg.arch
                )
                workload["pkg_added_ids"].append(pkg_id)

            # No errors so far? That means the analysis has succeeded,
            # so placeholders can be added to the list as well.
            # (Failed workloads need to have empty results, that's why)
            for placeholder_name in package_placeholders:
                workload["pkg_placeholder_ids"].append(pkg_placeholder_name_to_id(placeholder_name))
            
            for srpm_placeholder_name in srpm_placeholders:
                workload["srpm_placeholder_names"].append(srpm_placeholder_name)
            
            workload["pkg_relations"] = self._analyze_package_relations(query_all, package_placeholders)
            
            pkg_env_count = len(workload["pkg_env_ids"])
            pkg_added_count = len(workload["pkg_added_ids"])
            #log("  Done!  ({pkg_count} packages in total. That's {pkg_env_count} in the environment, and {pkg_added_count} added.)".format(
            #    pkg_count=str(pkg_env_count + pkg_added_count),
            #    pkg_env_count=pkg_env_count,
            #    pkg_added_count=pkg_added_count
            #))
            #log("")

        return workload

    
    def _analyze_workload_process(self, queue_result, workload_conf, env_conf, repo, arch):

        workload = self._analyze_workload(workload_conf, env_conf, repo, arch)
        queue_result.put(workload)


    async def _analyze_workloads_subset_async(self, task_queue, results):

        for task in task_queue:
            workload_conf = task["workload_conf"]
            env_conf = task["env_conf"]
            repo = task["repo"]
            arch = task["arch"]

            workload_id = "{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}".format(
                workload_conf_id=workload_conf["id"],
                env_conf_id=env_conf["id"],
                repo_id=repo["id"],
                arch=arch
            )

            # Max processes
            while True:
                if self.current_subprocesses < self.settings["max_subprocesses"]:
                    self.current_subprocesses += 1
                    break
                else:
                    await asyncio.sleep(.1)

            # Log progress
            self.workload_queue_counter_current += 1
            log("[{} of {}]".format(self.workload_queue_counter_current, self.workload_queue_counter_total))
            log("Analyzing workload: {}".format(workload_id))
            log("")

            queue_result = multiprocessing.Queue()
            process = multiprocessing.Process(target=self._analyze_workload_process, args=(queue_result, workload_conf, env_conf, repo, arch), daemon=True)
            process.start()

            # Now wait a bit for the result.
            # This is a terrible way to implement an async way to
            # wait for the result with a 222 seconds timeout.
            # But it works. If anyone knows how to make it nicer, let me know! :D

            # 2 seconds
            for _ in range(1, 20):
                if queue_result.empty():
                    await asyncio.sleep(.1)
                else:
                    break
            
            # 20 seconds
            for _ in range(1, 20):
                if queue_result.empty():
                    await asyncio.sleep(1)
                else:
                    break
            
            # 200 seconds
            for _ in range(1, 20):
                if queue_result.empty():
                    await asyncio.sleep(10)
                else:
                    break

            self.current_subprocesses -= 1

            # This basically means there was an exception in the processing and the process crashed
            if queue_result.empty():
                log("")
                log("")
                log("--------------------------------------------------------------------------")
                log("")
                log("ERROR: Workload analysis failed")
                log("")
                log("Details:")
                log("  workload_conf: {}".format(workload_conf["id"]))
                log("  env_conf:      {}".format(env_conf["id"]))
                log("  repo:          {}".format(repo["id"]))
                log("  arch:          {}".format(arch))
                log("")
                log("More details somewhere above.")
                log("")
                log("--------------------------------------------------------------------------")
                log("")
                log("")
                sys.exit(1)
        
            workload = queue_result.get()
            
            results[workload_id] = workload


    async def _analyze_workloads_async(self, results):

        tasks = []

        for repo in self.workload_queue:
            for arch in self.workload_queue[repo]:

                task_queue = self.workload_queue[repo][arch]

                tasks.append(asyncio.create_task(self._analyze_workloads_subset_async(task_queue, results)))
        
        for task in tasks:
            await task

        log("DONE!")

    
    def _queue_workload_processing(self, workload_conf, env_conf, repo, arch):
        
        repo_id = repo["id"]

        if repo_id not in self.workload_queue:
            self.workload_queue[repo_id] = {}
        
        if arch not in self.workload_queue[repo_id]:
            self.workload_queue[repo_id][arch] = []

        workload_task = {
            "workload_conf": workload_conf,
            "env_conf" : env_conf,
            "repo" : repo,
            "arch" : arch
        }

        self.workload_queue[repo_id][arch].append(workload_task)
        self.workload_queue_counter_total += 1


    def _reset_workload_processing_queue(self):
        self.workload_queue = {}
        self.workload_queue_counter_total = 0
        self.workload_queue_counter_current = 0


    def _analyze_workloads(self):

        # Initialise
        self.data["workloads"] = {}
        self._reset_workload_processing_queue()

        # Here, I need to mix and match workloads & envs based on labels
        workload_env_map = {}
        # Look at all workload configs...
        for workload_conf_id, workload_conf in self.configs["workloads"].items():
            workload_env_map[workload_conf_id] = set()
            # ... and all of their labels.
            for label in workload_conf["labels"]:
                # And for each label, find all env configs...
                for env_conf_id, env_conf in self.configs["envs"].items():
                    # ... that also have the label.
                    if label in env_conf["labels"]:
                        # And save those.
                        workload_env_map[workload_conf_id].add(env_conf_id)
        
        # And now, look at all workload configs...
        for workload_conf_id, workload_conf in self.configs["workloads"].items():
            # ... and for each, look at all env configs it should be analyzed in.
            for env_conf_id in workload_env_map[workload_conf_id]:
                # Each of those envs can have multiple repos associated...
                env_conf = self.configs["envs"][env_conf_id]
                for repo_id in env_conf["repositories"]:
                    # ... and each repo probably has multiple architecture.
                    repo = self.configs["repos"][repo_id]
                    arches = repo["source"]["architectures"]

        # And now, look at all workload configs...
        for workload_conf_id, workload_conf in self.configs["workloads"].items():
            # ... and for each, look at all env configs it should be analyzed in.
            for env_conf_id in workload_env_map[workload_conf_id]:
                # Each of those envs can have multiple repos associated...
                env_conf = self.configs["envs"][env_conf_id]
                for repo_id in env_conf["repositories"]:
                    # ... and each repo probably has multiple architecture.
                    repo = self.configs["repos"][repo_id]
                    for arch in repo["source"]["architectures"]:

                        # And now it has:
                        #   all workload configs *
                        #   all envs that match those *
                        #   all repos of those envs *
                        #   all arches of those repos.
                        # That's a lot of stuff! Let's analyze all of that!

                        # Before even started, look if the env succeeded. If not, there's
                        # no point in doing anything here.
                        env_id = "{env_conf_id}:{repo_id}:{arch}".format(
                            env_conf_id=env_conf["id"],
                            repo_id=repo["id"],
                            arch=arch
                        )
                        env = self.data["envs"][env_id]

                        if env["succeeded"]:
                            self._queue_workload_processing(workload_conf, env_conf, repo, arch)

                        else:
                            workload_id = "{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}".format(
                                workload_conf_id=workload_conf_id,
                                env_conf_id=env_conf_id,
                                repo_id=repo_id,
                                arch=arch
                            )
                            self.data["workloads"][workload_id] = _return_failed_workload_env_err(workload_conf, env_conf, repo, arch)

        asyncio.run(self._analyze_workloads_async(self.data["workloads"]))


    def _init_view_pkg(self, input_pkg, arch, placeholder=False, level=0):
        if placeholder:
            pkg = {
                "id": pkg_placeholder_name_to_id(input_pkg["name"]),
                "name": input_pkg["name"],
                "evr": "000-placeholder",
                "nevr": pkg_placeholder_name_to_nevr(input_pkg["name"]),
                "arch": "placeholder",
                "installsize": 0,
                "description": input_pkg["description"],
                "summary": input_pkg["description"],
                "source_name": input_pkg["srpm"],
                "sourcerpm": "{}-000-placeholder".format(input_pkg["srpm"]),
                "q_arch": input_pkg,
                "reponame": "n/a",
                "all_reponames": set(),
                "highest_priority_reponames": set()
            }

        else:
            pkg = dict(input_pkg)

        pkg["view_arch"] = arch

        pkg["placeholder"] = placeholder

        pkg["in_workload_ids_all"] = set()
        pkg["in_workload_ids_req"] = set()
        pkg["in_workload_ids_dep"] = set()
        pkg["in_workload_ids_env"] = set()

        pkg["in_buildroot_of_srpm_id_all"] = set()
        pkg["in_buildroot_of_srpm_id_req"] = set()
        pkg["in_buildroot_of_srpm_id_dep"] = set()
        pkg["in_buildroot_of_srpm_id_env"] = set()

        pkg["unwanted_completely_in_list_ids"] = set()
        pkg["unwanted_buildroot_in_list_ids"] = set()

        pkg["level"] = []

        # Level 0 is runtime
        pkg["level"].append({
            "all": pkg["in_workload_ids_all"],
            "req": pkg["in_workload_ids_req"],
            "dep": pkg["in_workload_ids_dep"],
            "env": pkg["in_workload_ids_env"],
        })

        # Level 1 and higher is buildroot
        for _ in range(level):
            pkg["level"].append({
                "all": set(),
                "req": set(),
                "dep": set(),
                "env": set()
            })

        pkg["required_by"] = set()
        pkg["recommended_by"] = set()
        pkg["suggested_by"] = set()

        return pkg


    def _init_view_srpm(self, pkg, level=0):

        srpm_id = pkg["sourcerpm"].rsplit(".src.rpm")[0]

        srpm = {}
        srpm["id"] = srpm_id
        srpm["name"] = pkg["source_name"]
        srpm["reponame"] = pkg["reponame"]
        srpm["pkg_ids"] = set()

        srpm["placeholder"] = False
        srpm["placeholder_directly_required_pkg_names"] = []

        srpm["in_workload_ids_all"] = set()
        srpm["in_workload_ids_req"] = set()
        srpm["in_workload_ids_dep"] = set()
        srpm["in_workload_ids_env"] = set()

        srpm["in_buildroot_of_srpm_id_all"] = set()
        srpm["in_buildroot_of_srpm_id_req"] = set()
        srpm["in_buildroot_of_srpm_id_dep"] = set()
        srpm["in_buildroot_of_srpm_id_env"] = set()

        srpm["unwanted_completely_in_list_ids"] = set()
        srpm["unwanted_buildroot_in_list_ids"] = set()

        srpm["level"] = []

        # Level 0 is runtime
        srpm["level"].append({
            "all": srpm["in_workload_ids_all"],
            "req": srpm["in_workload_ids_req"],
            "dep": srpm["in_workload_ids_dep"],
            "env": srpm["in_workload_ids_env"],
        })

        # Level 1 and higher is buildroot
        for _ in range(level):
            srpm["level"].append({
                "all": set(),
                "req": set(),
                "dep": set(),
                "env": set()
            })

        return srpm


    def _analyze_view(self, view_conf, arch, views):
        view_conf_id = view_conf["id"]

        log("Analyzing view: {view_name} ({view_conf_id}) for {arch}".format(
            view_name=view_conf["name"],
            view_conf_id=view_conf_id,
            arch=arch
        ))

        view_id = "{view_conf_id}:{arch}".format(
            view_conf_id=view_conf_id,
            arch=arch
        )

        repo_id = view_conf["repository"]

        # Setting up the data buckets for this view
        view = {}

        view["id"] = view_id
        view["view_conf_id"] = view_conf_id
        view["arch"] = arch

        view["workload_ids"] = []
        view["pkgs"] = {}
        view["source_pkgs"] = {}
        view["modules"] = {}

        # Workloads
        for workload_id, workload in self.data["workloads"].items():
            if workload["repo_id"] != repo_id:
                continue
            
            if workload["arch"] != arch:
                continue

            if not set(workload["labels"]) & set(view_conf["labels"]):
                continue

            view["workload_ids"].append(workload_id)

        log("  Includes {} workloads.".format(len(view["workload_ids"])))

        # Packages
        for workload_id in view["workload_ids"]:
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]

            # Packages in the environment
            for pkg_id in workload["pkg_env_ids"]:

                # Initialise
                if pkg_id not in view["pkgs"]:
                    pkg = self.data["pkgs"][repo_id][arch][pkg_id]
                    view["pkgs"][pkg_id] = self._init_view_pkg(pkg, arch)
                
                # It's in this wokrload
                view["pkgs"][pkg_id]["in_workload_ids_all"].add(workload_id)

                # And in the environment
                view["pkgs"][pkg_id]["in_workload_ids_env"].add(workload_id)

                # Is it also required?
                if view["pkgs"][pkg_id]["name"] in workload_conf["packages"]:
                    view["pkgs"][pkg_id]["in_workload_ids_req"].add(workload_id)
                elif view["pkgs"][pkg_id]["name"] in workload_conf["arch_packages"][arch]:
                    view["pkgs"][pkg_id]["in_workload_ids_req"].add(workload_id)
                
                # pkg_relations
                view["pkgs"][pkg_id]["required_by"].update(workload["pkg_relations"][pkg_id]["required_by"])
                view["pkgs"][pkg_id]["recommended_by"].update(workload["pkg_relations"][pkg_id]["recommended_by"])
                view["pkgs"][pkg_id]["suggested_by"].update(workload["pkg_relations"][pkg_id]["suggested_by"])

            # Packages added by this workload (required or dependency)
            for pkg_id in workload["pkg_added_ids"]:

                # Initialise
                if pkg_id not in view["pkgs"]:
                    pkg = self.data["pkgs"][repo_id][arch][pkg_id]
                    view["pkgs"][pkg_id] = self._init_view_pkg(pkg, arch)
                
                # It's in this wokrload
                view["pkgs"][pkg_id]["in_workload_ids_all"].add(workload_id)

                # Is it required?
                if view["pkgs"][pkg_id]["name"] in workload_conf["packages"]:
                    view["pkgs"][pkg_id]["in_workload_ids_req"].add(workload_id)
                elif view["pkgs"][pkg_id]["name"] in workload_conf["arch_packages"][arch]:
                    view["pkgs"][pkg_id]["in_workload_ids_req"].add(workload_id)
                
                # Or a dependency?
                else:
                    view["pkgs"][pkg_id]["in_workload_ids_dep"].add(workload_id)
                
                # pkg_relations
                view["pkgs"][pkg_id]["required_by"].update(workload["pkg_relations"][pkg_id]["required_by"])
                view["pkgs"][pkg_id]["recommended_by"].update(workload["pkg_relations"][pkg_id]["recommended_by"])
                view["pkgs"][pkg_id]["suggested_by"].update(workload["pkg_relations"][pkg_id]["suggested_by"])

            # And finally the non-existing, imaginary, package placeholders!
            for pkg_id in workload["pkg_placeholder_ids"]:

                # Initialise
                if pkg_id not in view["pkgs"]:
                    placeholder = workload_conf["package_placeholders"]["pkgs"][pkg_id_to_name(pkg_id)]
                    view["pkgs"][pkg_id] = self._init_view_pkg(placeholder, arch, placeholder=True)
                
                # It's in this wokrload
                view["pkgs"][pkg_id]["in_workload_ids_all"].add(workload_id)

                # Placeholders are by definition required
                view["pkgs"][pkg_id]["in_workload_ids_req"].add(workload_id)
            
            # ... including the SRPM placeholders
            for srpm_name in workload["srpm_placeholder_names"]:
                srpm_id = pkg_placeholder_name_to_nevr(srpm_name)

                # Initialise
                if srpm_id not in view["source_pkgs"]:
                    sourcerpm = "{}.src.rpm".format(srpm_id)
                    view["source_pkgs"][srpm_id] = self._init_view_srpm({"sourcerpm": sourcerpm, "source_name": srpm_name, "reponame": None})
                
                # It's a placeholder
                view["source_pkgs"][srpm_id]["placeholder"] = True

                # Build requires
                view["source_pkgs"][srpm_id]["placeholder_directly_required_pkg_names"] = workload_conf["package_placeholders"]["srpms"][srpm_name]["buildrequires"]
            
            # Oh! And modules
            for module_id in workload["enabled_modules"]:

                # Initiate
                if module_id not in view["modules"]:
                    view["modules"][module_id] = {}
                    view["modules"][module_id]["id"] = module_id
                    view["modules"][module_id]["in_workload_ids_all"] = set()
                    view["modules"][module_id]["in_workload_ids_req"] = set()
                    view["modules"][module_id]["in_workload_ids_dep"] = set()
                
                # It's in this workload
                view["modules"][module_id]["in_workload_ids_all"].add(workload_id)
                
                # Is it required?
                if module_id in workload_conf["modules_enable"]:
                    view["modules"][module_id]["in_workload_ids_req"].add(workload_id)
                else:
                    view["modules"][module_id]["in_workload_ids_dep"].add(workload_id)

        
        # If this is an addon view, remove all packages that are already in the parent view
        if view_conf["type"] == "addon":
            base_view_conf_id = view_conf["base_view_id"]

            base_view_id = "{base_view_conf_id}:{arch}".format(
                base_view_conf_id=base_view_conf_id,
                arch=arch
            )

            for base_view_pkg_id in views[base_view_id]["pkgs"]:
                if base_view_pkg_id in view["pkgs"]:
                    del view["pkgs"][base_view_pkg_id]

        # Done with packages!
        log("  Includes {} packages.".format(len(view["pkgs"])))
        log("  Includes {} modules.".format(len(view["modules"])))

        # But not with source packages, that's an entirely different story!
        for pkg_id, pkg in view["pkgs"].items():
            srpm_id = pkg["sourcerpm"].rsplit(".src.rpm")[0]

            if srpm_id not in view["source_pkgs"]:
                view["source_pkgs"][srpm_id] = self._init_view_srpm(pkg)

            # Include some information from the RPM
            view["source_pkgs"][srpm_id]["pkg_ids"].add(pkg_id)

            view["source_pkgs"][srpm_id]["in_workload_ids_all"].update(pkg["in_workload_ids_all"])
            view["source_pkgs"][srpm_id]["in_workload_ids_req"].update(pkg["in_workload_ids_req"])
            view["source_pkgs"][srpm_id]["in_workload_ids_dep"].update(pkg["in_workload_ids_dep"])
            view["source_pkgs"][srpm_id]["in_workload_ids_env"].update(pkg["in_workload_ids_env"])
        
        log("  Includes {} source packages.".format(len(view["source_pkgs"])))


        log("  DONE!")
        log("")

        return view


    def _analyze_views(self):

        views = {}

        # First, analyse the standard views
        for view_conf_id in self.configs["views"]:
            view_conf = self.configs["views"][view_conf_id]

            if view_conf["type"] == "compose":
                for arch in view_conf["architectures"]:
                    view = self._analyze_view(view_conf, arch, views)
                    view_id = view["id"]

                    views[view_id] = view
        
        # Second, analyse the addon views
        # This is important as they need the standard views already available
        for view_conf_id in self.configs["views"]:
            view_conf = self.configs["views"][view_conf_id]

            if view_conf["type"] == "addon":
                base_view_conf_id = view_conf["base_view_id"]
                base_view_conf = self.configs["views"][base_view_conf_id]

                for arch in set(view_conf["architectures"]) & set(base_view_conf["architectures"]):
                    view = self._analyze_view(view_conf, arch, views)
                    view_id = view["id"]

                    views[view_id] = view
        
        self.data["views"] = views


    def _populate_buildroot_with_view_srpms(self, view_conf, arch):
        view_conf_id = view_conf["id"]

        log("Initialising buildroot packages of: {view_name} ({view_conf_id}) for {arch}".format(
            view_name=view_conf["name"],
            view_conf_id=view_conf_id,
            arch=arch
        ))

        view_id = "{view_conf_id}:{arch}".format(
            view_conf_id=view_conf_id,
            arch=arch
        )

        view = self.data["views"][view_id]
        repo_id = view_conf["repository"]

        # Initialise the srpms section
        if repo_id not in self.data["buildroot"]["srpms"]:
            self.data["buildroot"]["srpms"][repo_id] = {}

        if arch not in self.data["buildroot"]["srpms"][repo_id]:
            self.data["buildroot"]["srpms"][repo_id][arch] = {}

        # Initialise each srpm
        for srpm_id, srpm in view["source_pkgs"].items():

            if srpm["placeholder"]:
                directly_required_pkg_names = srpm["placeholder_directly_required_pkg_names"]
            
            else:
                # This is the same set in both koji_srpms and srpms
                directly_required_pkg_names = set()

                # Do I need to extract the build dependencies from koji root_logs?
                # Then also save the srpms in the koji_srpm section
                if view_conf["buildroot_strategy"] == "root_logs":
                    srpm_reponame = srpm["reponame"]
                    koji_api_url = self.configs["repos"][repo_id]["source"]["repos"][srpm_reponame]["koji_api_url"]
                    koji_files_url = self.configs["repos"][repo_id]["source"]["repos"][srpm_reponame]["koji_files_url"]
                    koji_id = url_to_id(koji_api_url)

                    # Initialise the koji_srpms section
                    if koji_id not in self.data["buildroot"]["koji_srpms"]:
                        # SRPMs
                        self.data["buildroot"]["koji_srpms"][koji_id] = {}
                        # URLs
                        self.data["buildroot"]["koji_urls"][koji_id] = {}
                        self.data["buildroot"]["koji_urls"][koji_id]["api"] = koji_api_url
                        self.data["buildroot"]["koji_urls"][koji_id]["files"] = koji_files_url
                    
                    if arch not in self.data["buildroot"]["koji_srpms"][koji_id]:
                        self.data["buildroot"]["koji_srpms"][koji_id][arch] = {}

                    # Initialise srpms in the koji_srpms section
                    if srpm_id not in self.data["buildroot"]["koji_srpms"][koji_id][arch]:
                        self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id] = {}
                        self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]["id"] = srpm_id
                        self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]["directly_required_pkg_names"] = directly_required_pkg_names
                    else:
                        directly_required_pkg_names = self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]["directly_required_pkg_names"]

            # Initialise srpms in the srpms section
            if srpm_id not in self.data["buildroot"]["srpms"][repo_id][arch]:
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id] = {}
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["id"] = srpm_id
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["directly_required_pkg_names"] = directly_required_pkg_names
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_relations"] = {}
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_env_ids"] = set()
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_added_ids"] = set()
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["errors"] = {}
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["errors"]["non_existing_pkgs"] = set()
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["errors"]["message"] = ""
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["succeeded"] = False
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["queued"] = False
                self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["processed"] = False


        log("  DONE!")
        log("")


    def _get_build_deps_from_a_root_log(self, root_log):
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
                    # I need to deal with the following thing...
                    #
                    # DEBUG util.py:446:   gobject-introspection-devel     aarch64 1.70.0-1.fc36              build 1.1 M
                    # DEBUG util.py:446:   graphene-devel                  aarch64 1.10.6-3.fc35              build 159 k
                    # DEBUG util.py:446:   gstreamer1-plugins-bad-free-devel
                    # DEBUG util.py:446:                                   aarch64 1.19.2-1.fc36              build 244 k
                    # DEBUG util.py:446:   json-glib-devel                 aarch64 1.6.6-1.fc36               build 173 k
                    # DEBUG util.py:446:   libXcomposite-devel             aarch64 0.4.5-6.fc35               build  16 k  
                    #
                    # The "gstreamer1-plugins-bad-free-devel" package name is too long to fit in the column,
                    # so it gets split on two lines.
                    #
                    # Which if I take the usual file_line.split()[2] I get the correct name,
                    # but the next line gives me "aarch64" as a package name which is wrong.
                    #
                    # So the usual line has file_line.split() == 8
                    # The one with the long package name has file_line.split() == 3
                    # and the one following it has file_line.split() == 7
                    # 
                    # One more thing... long release!
                    #
                    # DEBUG util.py:446:   qrencode-devel               aarch64 4.0.2-8.fc35                  build  13 k
                    # DEBUG util.py:446:   systemtap-sdt-devel          aarch64 4.6~pre16291338gf2c14776-1.fc36
                    # DEBUG util.py:446:                                                                      build  71 k
                    # DEBUG util.py:446:   tpm2-tss-devel               aarch64 3.1.0-4.fc36                  build 315 k
                    #
                    # So the good one here is file_line.split() == 5.
                    # And the following is also file_line.split() == 5. Fun!
                    # 
                    # So if it ends with B, k, M, G it's the wrong line, so skip, otherwise take the package name.
                    #
                    # I can also anticipate both get long... that would mean I need to skip file_line.split() == 4.

                    if len(file_line.split()) == 8 or len(file_line.split()) == 3:
                        pkg_name = file_line.split()[2]
                        required_pkgs.append(pkg_name)

                    elif len(file_line.split()) == 7 or len(file_line.split()) == 4:
                        continue

                    elif len(file_line.split()) == 5:
                        if file_line.split()[4] in ["B", "k", "M", "G"]:
                            continue
                        else:
                            pkg_name = file_line.split()[2]
                            required_pkgs.append(pkg_name)

                    else:
                        raise KojiRootLogError
            

            # 4/
            # I'm done. So I can break out of the loop.
            elif state == 4:
                break
                

        return required_pkgs


    def _resolve_srpm_using_root_log(self, srpm_id, arch, koji_session, koji_files_url):
        
        # Buildroot grows pretty quickly. Use a fake one for development.
        if self.settings["dev_buildroot"]:
            # Making sure there are 3 passes at least, but that it won't get overwhelmed
            if srpm_id.rsplit("-",2)[0] in ["bash", "make", "unzip"]:
                return ["gawk", "xz", "findutils"]
            
            elif srpm_id.rsplit("-",2)[0] in ["gawk", "xz", "findutils"]:
                return ['cpio', 'diffutils']
            
            return ["bash", "make", "unzip"]

        # Shim is special.
        if srpm_id.rsplit("-",2)[0] in ["shim"]:
            log(    "It's shim! It gets sometiems tagged from wherever... Let's not even bother!")
            return []
        
        # Starting for real!
        log("    Talking to Koji API...")
        # This sometimes hangs, so I'm giving it a timeout and
        # a few extra tries before totally giving up!
        MAX_TRIES = 10
        attempts = 0
        success = False
        while attempts < MAX_TRIES:
            try:
                koji_pkg_data = koji_session.getRPM("{}.src".format(srpm_id))
                koji_logs = koji_session.getBuildLogs(koji_pkg_data["build_id"])
                success = True
                break
            except:
                attempts +=1
                log("    Error talking to Koji API... retrying...")
        if not success:
            raise KojiRootLogError("Could not talk to Koji API")

        koji_log_path = None

        for koji_log in koji_logs:
            if koji_log["name"] == "root.log":
                if koji_log["dir"] == arch or koji_log["dir"] == "noarch":
                    koji_log_path = koji_log["path"]
        
        root_log_url = "{koji_files_url}/{koji_log_path}".format(
            koji_files_url=koji_files_url,
            koji_log_path=koji_log_path
        )

        log("    Downloading the root.log file...")
        # This sometimes hangs, so I'm giving it a timeout and
        # a few extra tries before totally giving up!
        MAX_TRIES = 10
        attempts = 0
        success = False
        while attempts < MAX_TRIES:
            try:
                with urllib.request.urlopen(root_log_url, timeout=20) as response:
                    root_log_data = response.read()
                    root_log_contents = root_log_data.decode('utf-8')
                success = True
                break
            except:
                attempts +=1
                log("    Error getting the root log... retrying...")
        if not success:
            raise KojiRootLogError("Could not get a root.log file")

        log("    Parsing the root.log file...")
        directly_required_pkg_names = self._get_build_deps_from_a_root_log(root_log_contents)

        log("    Done!")
        return directly_required_pkg_names


    def _resolve_srpms_using_root_logs(self, pass_counter):
        # This function is idempotent!
        # 
        # That means it can be run many times without affecting the old results.

        log("== Resolving SRPMs using root logs - pass {} ========".format(pass_counter))

        # Prepare a counter for the log
        total_srpms_to_resolve = 0
        for koji_id in self.data["buildroot"]["koji_srpms"]:
            for arch in self.data["buildroot"]["koji_srpms"][koji_id]:
                total_srpms_to_resolve += len(self.data["buildroot"]["koji_srpms"][koji_id][arch])
        srpms_to_resolve_counter = 0

        # I need to keep sessions open to Koji
        # And because in some cases (in mixed repos) packages
        # could have been in different koji instances, I need
        # multiple Koji sesions!
        koji_sessions = {}

        for koji_id in self.data["buildroot"]["koji_srpms"]:
            koji_urls = self.data["buildroot"]["koji_urls"][koji_id]

            # If the cache is empty, initialise it
            if koji_id not in self.cache["root_log_deps"]["current"]:
                self.cache["root_log_deps"]["current"][koji_id] = {}
            if koji_id not in self.cache["root_log_deps"]["next"]:
                self.cache["root_log_deps"]["next"][koji_id] = {}

            # Initiate Koji sessions
            if koji_id not in koji_sessions:
                koji_sessions[koji_id] = koji.ClientSession(koji_urls["api"], opts = {"timeout": 20})

            for arch in self.data["buildroot"]["koji_srpms"][koji_id]:

                # If the cache is empty, initialise it
                if arch not in self.cache["root_log_deps"]["current"][koji_id]:
                    self.cache["root_log_deps"]["current"][koji_id][arch] = {}
                if arch not in self.cache["root_log_deps"]["next"][koji_id]:
                    self.cache["root_log_deps"]["next"][koji_id][arch] = {}
                

                for srpm_id, srpm in self.data["buildroot"]["koji_srpms"][koji_id][arch].items():
                    srpms_to_resolve_counter += 1
                
                    log("")
                    log("[ Buildroot - pass {} - {} of {} ]".format(pass_counter, srpms_to_resolve_counter, total_srpms_to_resolve))
                    log("Koji root_log {srpm_id} {arch}".format(
                        srpm_id=srpm_id,
                        arch=arch
                    ))
                    if not srpm["directly_required_pkg_names"]:
                        if srpm_id in self.cache["root_log_deps"]["current"][koji_id][arch]:
                            log("  Using Cache!")
                            directly_required_pkg_names = self.cache["root_log_deps"]["current"][koji_id][arch][srpm_id]

                        elif srpm_id in self.cache["root_log_deps"]["next"][koji_id][arch]:
                            log("  Using Cache!")
                            directly_required_pkg_names = self.cache["root_log_deps"]["next"][koji_id][arch][srpm_id]
                        
                        else:
                            log("  Resolving...")
                            directly_required_pkg_names = self._resolve_srpm_using_root_log(srpm_id, arch, koji_sessions[koji_id], koji_urls["files"])
                        
                        self.cache["root_log_deps"]["next"][koji_id][arch][srpm_id] = directly_required_pkg_names

                        # Here it's important to add the packages to the already initiated
                        # set, because its reference is shared between the koji_srpms and the srpm sections
                        self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]["directly_required_pkg_names"].update(directly_required_pkg_names)
                    else:
                        log("  Skipping! (already done before)")
        log("")
        log("  DONE!")
        log("")


    def _analyze_build_groups(self):

        log("")
        log("Analyzing build groups...")
        log("")

        # Need to analyse build groups for all repo_ids
        # and arches of buildroot["srpms"]
        for repo_id in self.data["buildroot"]["srpms"]:
            self.data["buildroot"]["build_groups"][repo_id] = {}

            for arch in self.data["buildroot"]["srpms"][repo_id]:

                generated_id = "CR-buildroot-base-env-{repo_id}-{arch}".format(
                    repo_id=repo_id,
                    arch=arch
                )

                # Using the _analyze_env function! 
                # So I need to reconstruct a fake env_conf
                fake_env_conf = {}
                fake_env_conf["id"] = generated_id
                fake_env_conf["options"] = []
                if self.configs["repos"][repo_id]["source"]["base_buildroot_override"]:
                    fake_env_conf["packages"] = self.configs["repos"][repo_id]["source"]["base_buildroot_override"]
                    fake_env_conf["groups"] = []
                else:
                    fake_env_conf["packages"] = []
                    fake_env_conf["groups"] = ["build"]
                fake_env_conf["arch_packages"] = {}
                fake_env_conf["arch_packages"][arch] = []

                log("Resolving build group: {repo_id} {arch}".format(
                    repo_id=repo_id,
                    arch=arch
                ))
                repo = self.configs["repos"][repo_id]
                fake_env = self._analyze_env(fake_env_conf, repo, arch)

                # If this fails, the buildroot can't be resolved.
                # Fail the entire content resolver build!
                if not fake_env["succeeded"]:
                    raise BuildGroupAnalysisError

                self.data["buildroot"]["build_groups"][repo_id][arch] = fake_env
                self.data["buildroot"]["build_groups"][repo_id][arch]["generated_id"] = generated_id

        log("")
        log("  DONE!")
        log("")


    def _expand_buildroot_srpms(self):
        # This function is idempotent!
        # 
        # That means it can be run many times without affecting the old results.

        log("Expanding the SRPM set...")

        counter = 0

        for repo_id in self.data["buildroot"]["srpms"]:
            for arch in self.data["buildroot"]["srpms"][repo_id]:
                top_lvl_srpm_ids = set(self.data["buildroot"]["srpms"][repo_id][arch])
                for top_lvl_srpm_id in top_lvl_srpm_ids:
                    top_lvl_srpm = self.data["buildroot"]["srpms"][repo_id][arch][top_lvl_srpm_id]

                    for pkg_id in top_lvl_srpm["pkg_relations"]:
                        srpm_id = self.data["pkgs"][repo_id][arch][pkg_id]["sourcerpm"].rsplit(".src.rpm")[0]

                        if srpm_id in self.data["buildroot"]["srpms"][repo_id][arch]:
                            continue

                        # Adding a new one!
                        counter += 1
                        
                        srpm_reponame = self.data["pkgs"][repo_id][arch][pkg_id]["reponame"]

                        # This is the same set in both koji_srpms and srpms
                        directly_required_pkg_names = set()

                        koji_api_url = self.configs["repos"][repo_id]["source"]["repos"][srpm_reponame]["koji_api_url"]
                        koji_files_url = self.configs["repos"][repo_id]["source"]["repos"][srpm_reponame]["koji_files_url"]
                        koji_id = url_to_id(koji_api_url)

                        # Initialise the srpm in the koji_srpms section
                        if srpm_id not in self.data["buildroot"]["koji_srpms"][koji_id][arch]:
                            self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id] = {}
                            self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]["id"] = srpm_id
                            self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]["directly_required_pkg_names"] = directly_required_pkg_names
                        else:
                            directly_required_pkg_names = self.data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]["directly_required_pkg_names"]

                        # Initialise the srpm in the srpms section
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id] = {}
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["id"] = srpm_id
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["directly_required_pkg_names"] = directly_required_pkg_names
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_relations"] = {}
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_env_ids"] = set()
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_added_ids"] = set()
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["errors"] = {}
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["errors"]["non_existing_pkgs"] = set()
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["errors"]["message"] = ""
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["succeeded"] = False
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["queued"] = False
                        self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["processed"] = False

        log("  Found {} new SRPMs!".format(counter))
        log("  DONE!")
        log("")

        return counter


    def _analyze_srpm_buildroots(self, pass_counter):
        # This function is idempotent!
        # 
        # That means it can be run many times without affecting the old results.

        log("")
        log("Analyzing SRPM buildroots...")
        log("")

        # Initialise things for the workload resolver
        self._reset_workload_processing_queue()
        fake_workload_results = {}

        # Prepare a counter for the log
        total_srpms_to_resolve = 0
        for repo_id in self.data["buildroot"]["srpms"]:
            for arch in self.data["buildroot"]["srpms"][repo_id]:
                for srpm_id, srpm in self.data["buildroot"]["srpms"][repo_id][arch].items():
                    if srpm["processed"]:
                        continue
                    total_srpms_to_resolve += 1
        srpms_to_resolve_counter = 0

        for repo_id in self.data["buildroot"]["srpms"]:
            for arch in self.data["buildroot"]["srpms"][repo_id]:
                for srpm_id, srpm in self.data["buildroot"]["srpms"][repo_id][arch].items():

                    if srpm["queued"] or srpm["processed"]:
                        continue

                    # Using the _analyze_workload function!
                    # So I need to reconstruct a fake workload_conf and a fake env_conf
                    fake_workload_conf = {}
                    fake_workload_conf["labels"] = []
                    fake_workload_conf["id"] = srpm_id
                    fake_workload_conf["options"] = []
                    fake_workload_conf["modules_disable"] = []
                    fake_workload_conf["modules_enable"] = []
                    fake_workload_conf["packages"] = srpm["directly_required_pkg_names"]
                    fake_workload_conf["groups"] = []
                    fake_workload_conf["package_placeholders"] = {}
                    fake_workload_conf["package_placeholders"]["pkgs"] = {}
                    fake_workload_conf["package_placeholders"]["srpms"] = {}
                    fake_workload_conf["arch_packages"] = {}
                    fake_workload_conf["arch_packages"][arch] = []

                    fake_env_conf = {}
                    fake_env_conf["labels"] = []
                    fake_env_conf["id"] = self.data["buildroot"]["build_groups"][repo_id][arch]["generated_id"]
                    fake_env_conf["packages"] = ["bash"] # This just needs to pass the "if len(packages)" test as True
                    fake_env_conf["arch_packages"] = {}
                    fake_env_conf["arch_packages"][arch] = []

                    srpms_to_resolve_counter += 1
                    
                    #log("[ Buildroot - pass {} - {} of {} ]".format(pass_counter, srpms_to_resolve_counter, total_srpms_to_resolve))
                    #log("Resolving SRPM buildroot: {repo_id} {arch} {srpm_id}".format(
                    #    repo_id=repo_id,
                    #    arch=arch,
                    #    srpm_id=srpm_id
                    #))
                    repo = self.configs["repos"][repo_id]

                    #fake_workload = self._analyze_workload(fake_workload_conf, fake_env_conf, repo, arch)
                    self._queue_workload_processing(fake_workload_conf, fake_env_conf, repo, arch)

                    # Save the buildroot data
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["queued"] = True

        asyncio.run(self._analyze_workloads_async(fake_workload_results))

        for repo_id in self.data["buildroot"]["srpms"]:
            for arch in self.data["buildroot"]["srpms"][repo_id]:
                for srpm_id, srpm in self.data["buildroot"]["srpms"][repo_id][arch].items():

                    if srpm["processed"]:
                        continue

                    fake_workload_id = "{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}".format(
                        workload_conf_id=srpm_id,
                        env_conf_id=self.data["buildroot"]["build_groups"][repo_id][arch]["generated_id"],
                        repo_id=repo_id,
                        arch=arch
                    )

                    fake_workload = fake_workload_results[fake_workload_id]

                    # Save the buildroot data
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["succeeded"] = fake_workload["succeeded"]
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_relations"] = fake_workload["pkg_relations"]
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_env_ids"] = fake_workload["pkg_env_ids"]
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["pkg_added_ids"] = fake_workload["pkg_added_ids"]
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["errors"] = fake_workload["errors"]
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["warnings"] = fake_workload["warnings"]
                    self.data["buildroot"]["srpms"][repo_id][arch][srpm_id]["processed"] = True

        log("")
        log("  DONE!")
        log("")


    def _analyze_buildroot(self):

        self.data["buildroot"] = {}
        self.data["buildroot"]["koji_srpms"] = {}
        self.data["buildroot"]["koji_urls"] = {}
        self.data["buildroot"]["srpms"] = {}
        self.data["buildroot"]["build_groups"] = {}

        # Currently, only "compose" view types are supported.
        # The "addon" type is not.

        # Get SRPMs from views
        #
        # This populates:
        #   data["buildroot"]["koji_srpms"]...
        # and also initiates:
        #   data["buildroot"]["srpms"]...
        for view_conf_id in self.configs["views"]:
            view_conf = self.configs["views"][view_conf_id]

            if view_conf["type"] == "compose":
                if view_conf["buildroot_strategy"] == "root_logs":
                    for arch in view_conf["architectures"]:
                        self._populate_buildroot_with_view_srpms(view_conf, arch)

        # Time to resolve the build groups!
        # 
        # This initialises and populates:
        #   buildroot["build_groups"]
        self._analyze_build_groups()

        pass_counter = 0
        while True:
            pass_counter += 1

            log("")
            log("== Buildroot resolution - pass {} ========".format(pass_counter))
            log("")
            log("")
            # Get the directly_required_pkg_names from koji root logs
            # 
            # Adds stuff to existing:
            #   data["buildroot"]["koji_srpms"]...
            # ... which also updates:
            #   data["buildroot"]["srpms"]...
            # ... because it's interlinked.
            self._resolve_srpms_using_root_logs(pass_counter)

            # And now resolving the actual buildroot
            self._analyze_srpm_buildroots(pass_counter)

            # Resolving dependencies could have added new SRPMs into the mix that also
            # need their buildroots resolved! So let's find out if there are any
            new_srpms_count = self._expand_buildroot_srpms()

            if not new_srpms_count:
                log("")
                log("All passes completed!")
                log("")
                break


    def _add_missing_levels_to_pkg_or_srpm(self, pkg_or_srpm, level):

        pkg_current_max_level = len(pkg_or_srpm["level"]) - 1
        for _ in range(level - pkg_current_max_level):
            pkg_or_srpm["level"].append({
                "all": set(),
                "req": set(),
                "dep": set(),
                "env": set()
            })


    def _add_buildroot_to_view(self, view_conf, arch):

        view_conf_id = view_conf["id"]

        view_id = "{view_conf_id}:{arch}".format(
            view_conf_id=view_conf_id,
            arch=arch
        )

        repo_id = view_conf["repository"]

        view = self.data["views"][view_id]


        log("")
        log("Adding buildroot to view {}...".format(view_id))

        # Starting with all SRPMs in this view
        srpm_ids_to_process = set(view["source_pkgs"])

        # Starting on level 1, the first buildroot level
        # (it's 0 because it gets incremented to 1 immediately after the loop starts)
        level = 0

        while True:
            level += 1
            added_pkg_ids = set()

            log("  Pass {}...".format(level))

            # This is similar to adding workloads in _analyze_view()
            for buildroot_srpm_id in srpm_ids_to_process:
                buildroot_srpm = self.data["buildroot"]["srpms"][repo_id][arch][buildroot_srpm_id]


                # Packages in the base buildroot (which would be the environment in workloads)
                for pkg_id in buildroot_srpm["pkg_env_ids"]:
                    added_pkg_ids.add(pkg_id)

                    # Initialise
                    if pkg_id not in view["pkgs"]:
                        pkg = self.data["pkgs"][repo_id][arch][pkg_id]
                        view["pkgs"][pkg_id] = self._init_view_pkg(pkg, arch, level=level)
                    
                    # Add missing levels to the pkg
                    self._add_missing_levels_to_pkg_or_srpm(view["pkgs"][pkg_id], level)

                    # It's in this buildroot
                    view["pkgs"][pkg_id]["in_buildroot_of_srpm_id_all"].add(buildroot_srpm_id)
                    view["pkgs"][pkg_id]["level"][level]["all"].add(buildroot_srpm_id)

                    # And in the base buildroot specifically
                    view["pkgs"][pkg_id]["in_buildroot_of_srpm_id_env"].add(buildroot_srpm_id)
                    view["pkgs"][pkg_id]["level"][level]["env"].add(buildroot_srpm_id)

                    # Is it also required?
                    if view["pkgs"][pkg_id]["name"] in buildroot_srpm["directly_required_pkg_names"]:
                        view["pkgs"][pkg_id]["in_buildroot_of_srpm_id_req"].add(buildroot_srpm_id)
                        view["pkgs"][pkg_id]["level"][level]["req"].add(buildroot_srpm_id)
                    
                    # pkg_relations
                    view["pkgs"][pkg_id]["required_by"].update(buildroot_srpm["pkg_relations"][pkg_id]["required_by"])
                    view["pkgs"][pkg_id]["recommended_by"].update(buildroot_srpm["pkg_relations"][pkg_id]["recommended_by"])
                    view["pkgs"][pkg_id]["suggested_by"].update(buildroot_srpm["pkg_relations"][pkg_id]["suggested_by"])

                # Packages needed on top of the base buildroot (required or dependency)
                for pkg_id in buildroot_srpm["pkg_added_ids"]:
                    added_pkg_ids.add(pkg_id)

                    # Initialise
                    if pkg_id not in view["pkgs"]:
                        pkg = self.data["pkgs"][repo_id][arch][pkg_id]
                        view["pkgs"][pkg_id] = self._init_view_pkg(pkg, arch, level=level)
                    
                    # Add missing levels to the pkg
                    self._add_missing_levels_to_pkg_or_srpm(view["pkgs"][pkg_id], level)
                    
                    # It's in this buildroot
                    view["pkgs"][pkg_id]["in_buildroot_of_srpm_id_all"].add(buildroot_srpm_id)
                    view["pkgs"][pkg_id]["level"][level]["all"].add(buildroot_srpm_id)

                    # Is it also required?
                    if view["pkgs"][pkg_id]["name"] in buildroot_srpm["directly_required_pkg_names"]:
                        view["pkgs"][pkg_id]["in_buildroot_of_srpm_id_req"].add(buildroot_srpm_id)
                        view["pkgs"][pkg_id]["level"][level]["req"].add(buildroot_srpm_id)
                    
                    # Or a dependency?
                    else:
                        view["pkgs"][pkg_id]["in_buildroot_of_srpm_id_dep"].add(buildroot_srpm_id)
                        view["pkgs"][pkg_id]["level"][level]["dep"].add(buildroot_srpm_id)
                    
                    # pkg_relations
                    view["pkgs"][pkg_id]["required_by"].update(buildroot_srpm["pkg_relations"][pkg_id]["required_by"])
                    view["pkgs"][pkg_id]["recommended_by"].update(buildroot_srpm["pkg_relations"][pkg_id]["recommended_by"])
                    view["pkgs"][pkg_id]["suggested_by"].update(buildroot_srpm["pkg_relations"][pkg_id]["suggested_by"])
            
            # Resetting the SRPMs, so only the new ones can be added
            srpm_ids_to_process = set()

            # SRPMs
            for pkg_id in added_pkg_ids:
                pkg = view["pkgs"][pkg_id]
                srpm_id = pkg["sourcerpm"].rsplit(".src.rpm")[0]

                # Initialise
                if srpm_id not in view["source_pkgs"]:
                    view["source_pkgs"][srpm_id] = self._init_view_srpm(pkg, level=level)
                    srpm_ids_to_process.add(srpm_id)
                    
                # Add missing levels to the pkg
                self._add_missing_levels_to_pkg_or_srpm(view["source_pkgs"][srpm_id], level)

                view["source_pkgs"][srpm_id]["pkg_ids"].add(pkg_id)

                # Include some information from the RPM
                view["source_pkgs"][srpm_id]["in_buildroot_of_srpm_id_all"].update(pkg["in_buildroot_of_srpm_id_all"])
                view["source_pkgs"][srpm_id]["in_buildroot_of_srpm_id_req"].update(pkg["in_buildroot_of_srpm_id_req"])
                view["source_pkgs"][srpm_id]["in_buildroot_of_srpm_id_dep"].update(pkg["in_buildroot_of_srpm_id_dep"])
                view["source_pkgs"][srpm_id]["in_buildroot_of_srpm_id_env"].update(pkg["in_buildroot_of_srpm_id_env"])
                view["source_pkgs"][srpm_id]["level"][level]["all"].update(pkg["level"][level]["all"])
                view["source_pkgs"][srpm_id]["level"][level]["req"].update(pkg["level"][level]["req"])
                view["source_pkgs"][srpm_id]["level"][level]["dep"].update(pkg["level"][level]["dep"])
                view["source_pkgs"][srpm_id]["level"][level]["env"].update(pkg["level"][level]["env"])

            log ("    added {} RPMs".format(len(added_pkg_ids)))
            log ("    added {} SRPMs".format(len(srpm_ids_to_process)))

            # More iterations needed?
            if not srpm_ids_to_process:
                log("  All passes completed!")
                log("")
                break


    def _add_buildroot_to_views(self):

        log("")
        log("Adding Buildroot to views...")
        log("")

        # First, the standard views
        for view_conf_id in self.configs["views"]:
            view_conf = self.configs["views"][view_conf_id]

            if view_conf["type"] == "compose":
                if view_conf["buildroot_strategy"] == "root_logs":
                    for arch in view_conf["architectures"]:
                        self._add_buildroot_to_view(view_conf, arch)

        # And the addon is not supported now

        log("")
        log("  DONE!")
        log("")


    def _init_pkg_or_srpm_relations_fields(self, target_pkg, type = None):
        # I kept them all listed so they're easy to copy

        # Workload IDs
        target_pkg["in_workload_ids_all"] = set()
        target_pkg["in_workload_ids_req"] = set()
        target_pkg["in_workload_ids_dep"] = set()
        target_pkg["in_workload_ids_env"] = set()
        
        # Workload Conf IDs
        target_pkg["in_workload_conf_ids_all"] = set()
        target_pkg["in_workload_conf_ids_req"] = set()
        target_pkg["in_workload_conf_ids_dep"] = set()
        target_pkg["in_workload_conf_ids_env"] = set()

        # Buildroot SRPM IDs
        target_pkg["in_buildroot_of_srpm_id_all"] = set()
        target_pkg["in_buildroot_of_srpm_id_req"] = set()
        target_pkg["in_buildroot_of_srpm_id_dep"] = set()
        target_pkg["in_buildroot_of_srpm_id_env"] = set()

        # Buildroot SRPM Names
        target_pkg["in_buildroot_of_srpm_name_all"] = {} # of set() of srpm_ids
        target_pkg["in_buildroot_of_srpm_name_req"] = {} # of set() of srpm_ids
        target_pkg["in_buildroot_of_srpm_name_dep"] = {} # of set() of srpm_ids
        target_pkg["in_buildroot_of_srpm_name_env"] = {} # of set() of srpm_ids

        # Unwanted
        target_pkg["unwanted_completely_in_list_ids"] = set()
        target_pkg["unwanted_buildroot_in_list_ids"] = set()

        # Level number
        target_pkg["level_number"] = 999

        # Levels
        target_pkg["level"] = []

        # Maintainer recommendation
        target_pkg["maintainer_recommendation"] = {}
        target_pkg["maintainer_recommendation_details"] = {}
        target_pkg["best_maintainers"] = set()

        if type == "rpm":

            # Dependency of RPM NEVRs
            target_pkg["dependency_of_pkg_nevrs"] = set()
            target_pkg["hard_dependency_of_pkg_nevrs"] = set()
            target_pkg["weak_dependency_of_pkg_nevrs"] = set()

            # Dependency of RPM Names
            target_pkg["dependency_of_pkg_names"] = {} # of set() of nevrs
            target_pkg["hard_dependency_of_pkg_names"] = {} # of set() of nevrs
            target_pkg["weak_dependency_of_pkg_names"] = {} # if set() of nevrs

    
    def _populate_pkg_or_srpm_relations_fields(self, target_pkg, source_pkg, type = None, view = None):

        # source_pkg is the arch-specific binary package
        # target_pkg is a representation of that pages for all arches
        #
        # This function adds information from the arch-specific package to the general one.
        # It gets called for all the arches.
        #

        if type == "rpm" and not view:
            raise ValueError("This function requires a view when using type = 'rpm'!")

        # Unwanted
        target_pkg["unwanted_completely_in_list_ids"].update(source_pkg["unwanted_completely_in_list_ids"])
        target_pkg["unwanted_buildroot_in_list_ids"].update(source_pkg["unwanted_buildroot_in_list_ids"])


        # Dependency relationships
        for list_type in ["all", "req", "dep", "env"]:
            target_pkg["in_workload_ids_{}".format(list_type)].update(source_pkg["in_workload_ids_{}".format(list_type)])

            target_pkg["in_buildroot_of_srpm_id_{}".format(list_type)].update(source_pkg["in_buildroot_of_srpm_id_{}".format(list_type)])

            for workload_id in source_pkg["in_workload_ids_{}".format(list_type)]:
                workload_conf_id = workload_id_to_conf_id(workload_id)
                target_pkg["in_workload_conf_ids_{}".format(list_type)].add(workload_conf_id)

            for srpm_id in source_pkg["in_buildroot_of_srpm_id_{}".format(list_type)]:
                srpm_name = pkg_id_to_name(srpm_id)

                if srpm_name not in target_pkg["in_buildroot_of_srpm_name_{}".format(list_type)]:
                    target_pkg["in_buildroot_of_srpm_name_{}".format(list_type)][srpm_name] = set()
                
                target_pkg["in_buildroot_of_srpm_name_{}".format(list_type)][srpm_name].add(srpm_id)
        
        # Level number
        level_number = 0
        for level in source_pkg["level"]:
            if level["all"]:
                if level_number < target_pkg["level_number"]:
                    target_pkg["level_number"] = level_number
            level_number += 1

        # All the levels!
        level = 0
        for level_data in source_pkg["level"]:
            # 'level' is the number
            # 'level_data' is the ["all"][workload_id] or ["all"][srpm_id] or
            #                     ["req"][workload_id] or ["req"][srpm_id] or 
            #                     ["dep"][workload_id] or ["dep"][srpm_id] or
            #                     ["env"][workload_id] or ["env"][srpm_id]

            # If I could do 'if level in target_pkg["level"]' I'd do that instead...
            # But it's a list, so have to do this instead
            if len(target_pkg["level"]) == level:
                target_pkg["level"].append(dict())

            for level_scope, those_ids in level_data.items():
                # 'level_scope' is "all" or "req" etc.
                # 'those_ids' is a list of srpm_ids or workload_ids

                if level_scope not in target_pkg["level"][level]:
                    target_pkg["level"][level][level_scope] = set()
                
                target_pkg["level"][level][level_scope].update(those_ids)
            
            level +=1
 
        
        if type == "rpm":
            # Hard dependency of
            for pkg_id in source_pkg["required_by"]:
                pkg_name = pkg_id_to_name(pkg_id)
                
                # This only happens in addon views, and only rarely.
                # Basically means that a package in the addon view is required
                # by a package in the base view.
                # Doesn't make sense?
                # Think of 'glibc-all-langpacks' being in the addon,
                # while the proper langpacks along with 'glibc' are in the base view.
                # 
                # In that case, 'glibc' is not in the addon, but 'glibc-all-langpacks'
                # requires it.
                #
                # I'm not implementing it now, as it's such a corner case.
                # So just skip it. All the data will remain correct,
                # it's just the 'glibc-all-langpacks' page won't show
                # "required by 'glibc'" that's all.
                if pkg_id not in view["pkgs"]:
                    view_conf_id = view["view_conf_id"]
                    view_conf = self.configs["views"][view_conf_id]
                    if view_conf["type"] == "addon":
                        continue

                pkg = view["pkgs"][pkg_id]
                pkg_nevr = "{name}-{evr}".format(
                    name=pkg["name"],
                    evr=pkg["evr"]
                )
                target_pkg["hard_dependency_of_pkg_nevrs"].add(pkg_nevr)

                if pkg_name not in target_pkg["hard_dependency_of_pkg_names"]:
                    target_pkg["hard_dependency_of_pkg_names"][pkg_name] = set()
                target_pkg["hard_dependency_of_pkg_names"][pkg_name].add(pkg_nevr)

            # Weak dependency of
            for list_type in ["recommended", "suggested"]:
                for pkg_id in source_pkg["{}_by".format(list_type)]:
                    pkg_name = pkg_id_to_name(pkg_id)

                    # This only happens in addon views, and only rarely.
                    # (see the long comment above)
                    if pkg_id not in view["pkgs"]:
                        view_conf_id = view["view_conf_id"]
                        view_conf = self.configs["views"][view_conf_id]
                        if view_conf["type"] == "addon":
                            continue

                    pkg = view["pkgs"][pkg_id]
                    pkg_nevr = "{name}-{evr}".format(
                        name=pkg["name"],
                        evr=pkg["evr"]
                    )
                    target_pkg["weak_dependency_of_pkg_nevrs"].add(pkg_nevr)

                    if pkg_name not in target_pkg["weak_dependency_of_pkg_names"]:
                        target_pkg["weak_dependency_of_pkg_names"][pkg_name] = set()
                    target_pkg["weak_dependency_of_pkg_names"][pkg_name].add(pkg_nevr)
            
            # All types of dependency
            target_pkg["dependency_of_pkg_nevrs"].update(target_pkg["hard_dependency_of_pkg_nevrs"])
            target_pkg["dependency_of_pkg_nevrs"].update(target_pkg["weak_dependency_of_pkg_nevrs"])

            for pkg_name, pkg_nevrs in target_pkg["hard_dependency_of_pkg_names"].items():
                if pkg_name not in target_pkg["dependency_of_pkg_names"]:
                    target_pkg["dependency_of_pkg_names"][pkg_name] = set()
                
                target_pkg["dependency_of_pkg_names"][pkg_name].update(pkg_nevrs)

            for pkg_name, pkg_nevrs in target_pkg["weak_dependency_of_pkg_names"].items():
                if pkg_name not in target_pkg["dependency_of_pkg_names"]:
                    target_pkg["dependency_of_pkg_names"][pkg_name] = set()
                
                target_pkg["dependency_of_pkg_names"][pkg_name].update(pkg_nevrs)

            

        # TODO: add the levels


    def _generate_views_all_arches(self):

        views_all_arches = {}

        for view_conf_id, view_conf in self.configs["views"].items():

            #if view_conf["type"] == "compose":
            if True:

                repo_id = view_conf["repository"]

                view_all_arches = {}

                view_all_arches["id"] = view_conf_id
                view_all_arches["has_buildroot"] = False

                if view_conf["type"] == "compose":
                    if view_conf["buildroot_strategy"] == "root_logs":
                        view_all_arches["has_buildroot"] = True
                else:
                    view_all_arches["has_buildroot"] = False

                view_all_arches["everything_succeeded"] = True
                view_all_arches["no_warnings"] = True

                view_all_arches["workloads"] = {}

                view_all_arches["pkgs_by_name"] = {}
                view_all_arches["pkgs_by_nevr"] = {}

                view_all_arches["source_pkgs_by_name"] = {}

                view_all_arches["modules"] = {}

                view_all_arches["numbers"] = {}
                view_all_arches["numbers"]["pkgs"] = {}
                view_all_arches["numbers"]["pkgs"]["runtime"] = 0
                view_all_arches["numbers"]["pkgs"]["env"] = 0
                view_all_arches["numbers"]["pkgs"]["req"] = 0
                view_all_arches["numbers"]["pkgs"]["dep"] = 0
                view_all_arches["numbers"]["pkgs"]["build"] = 0
                view_all_arches["numbers"]["pkgs"]["build_base"] = 0
                view_all_arches["numbers"]["pkgs"]["build_level_1"] = 0
                view_all_arches["numbers"]["pkgs"]["build_level_2_plus"] = 0
                view_all_arches["numbers"]["srpms"] = {}
                view_all_arches["numbers"]["srpms"]["runtime"] = 0
                view_all_arches["numbers"]["srpms"]["env"] = 0
                view_all_arches["numbers"]["srpms"]["req"] = 0
                view_all_arches["numbers"]["srpms"]["dep"] = 0
                view_all_arches["numbers"]["srpms"]["build"] = 0
                view_all_arches["numbers"]["srpms"]["build_base"] = 0
                view_all_arches["numbers"]["srpms"]["build_level_1"] = 0
                view_all_arches["numbers"]["srpms"]["build_level_2_plus"] = 0


                for arch in view_conf["architectures"]:
                    view_id = "{view_conf_id}:{arch}".format(
                        view_conf_id=view_conf_id,
                        arch=arch
                    )

                    view = self.data["views"][view_id]

                    # Workloads
                    for workload_id in view["workload_ids"]:
                        workload = self.data["workloads"][workload_id]
                        workload_conf_id = workload["workload_conf_id"]
                        workload_conf = self.configs["workloads"][workload_conf_id]

                        if workload_conf_id not in view_all_arches["workloads"]:
                            view_all_arches["workloads"][workload_conf_id] = {}
                            view_all_arches["workloads"][workload_conf_id]["workload_conf_id"] = workload_conf_id
                            view_all_arches["workloads"][workload_conf_id]["name"] = workload_conf["name"]
                            view_all_arches["workloads"][workload_conf_id]["maintainer"] = workload_conf["maintainer"]
                            view_all_arches["workloads"][workload_conf_id]["succeeded"] = True
                            view_all_arches["workloads"][workload_conf_id]["no_warnings"] = True
                            # ...
                        
                        if not workload["succeeded"]:
                            view_all_arches["workloads"][workload_conf_id]["succeeded"] = False
                            view_all_arches["everything_succeeded"] = False
                        
                        if workload["warnings"]["message"]:
                            view_all_arches["workloads"][workload_conf_id]["no_warnings"] = False
                            view_all_arches["no_warnings"] = False


                    # Binary Packages
                    for package in view["pkgs"].values():

                        # Binary Packages by name
                        key = "pkgs_by_name"
                        identifier = package["name"]

                        # Init
                        if identifier not in view_all_arches[key]:
                            view_all_arches[key][identifier] = {}
                            view_all_arches[key][identifier]["name"] = package["name"]
                            view_all_arches[key][identifier]["placeholder"] = package["placeholder"]
                            view_all_arches[key][identifier]["source_name"] = package["source_name"]
                            view_all_arches[key][identifier]["nevrs"] = {}
                            view_all_arches[key][identifier]["arches"] = set()
                            view_all_arches[key][identifier]["highest_priority_reponames_per_arch"] = {}

                            self._init_pkg_or_srpm_relations_fields(view_all_arches[key][identifier], type="rpm")

                        if package["nevr"] not in view_all_arches[key][identifier]["nevrs"]:
                            view_all_arches[key][identifier]["nevrs"][package["nevr"]] = set()
                        view_all_arches[key][identifier]["nevrs"][package["nevr"]].add(arch)

                        view_all_arches[key][identifier]["arches"].add(arch)

                        if arch not in view_all_arches[key][identifier]["highest_priority_reponames_per_arch"]:
                            view_all_arches[key][identifier]["highest_priority_reponames_per_arch"][arch] = set()
                        view_all_arches[key][identifier]["highest_priority_reponames_per_arch"][arch].update(package["highest_priority_reponames"])

                        self._populate_pkg_or_srpm_relations_fields(view_all_arches[key][identifier], package, type="rpm", view=view)

                        # Binary Packages by nevr
                        key = "pkgs_by_nevr"
                        identifier = package["nevr"]

                        if identifier not in view_all_arches[key]:
                            view_all_arches[key][identifier] = {}
                            view_all_arches[key][identifier]["name"] = package["name"]
                            view_all_arches[key][identifier]["placeholder"] = package["placeholder"]
                            view_all_arches[key][identifier]["evr"] = package["evr"]
                            view_all_arches[key][identifier]["source_name"] = package["source_name"]
                            view_all_arches[key][identifier]["arches"] = set()
                            view_all_arches[key][identifier]["reponame_per_arch"] = {}
                            view_all_arches[key][identifier]["highest_priority_reponames_per_arch"] = {}
                            view_all_arches[key][identifier]["category"] = None

                            self._init_pkg_or_srpm_relations_fields(view_all_arches[key][identifier], type="rpm")
                        
                        view_all_arches[key][identifier]["arches"].add(arch)
                        view_all_arches[key][identifier]["reponame_per_arch"][arch] = package["reponame"]
                        view_all_arches[key][identifier]["highest_priority_reponames_per_arch"][arch] = package["highest_priority_reponames"]

                        self._populate_pkg_or_srpm_relations_fields(view_all_arches[key][identifier], package, type="rpm", view=view)

                    
                    # Source Packages
                    for package in view["source_pkgs"].values():

                        # Source Packages by name
                        key = "source_pkgs_by_name"
                        identifier = package["name"]

                        if identifier not in view_all_arches[key]:
                            view_all_arches[key][identifier] = {}
                            view_all_arches[key][identifier]["name"] = package["name"]
                            view_all_arches[key][identifier]["placeholder"] = package["placeholder"]
                            if view_all_arches["has_buildroot"]:
                                view_all_arches[key][identifier]["buildroot_succeeded"] = True
                                view_all_arches[key][identifier]["buildroot_no_warnings"] = True
                            view_all_arches[key][identifier]["errors"] = {}
                            view_all_arches[key][identifier]["warnings"] = {}
                            view_all_arches[key][identifier]["pkg_names"] = set()
                            view_all_arches[key][identifier]["pkg_nevrs"] = set()
                            view_all_arches[key][identifier]["arches"] = set()
                            view_all_arches[key][identifier]["category"] = None

                            self._init_pkg_or_srpm_relations_fields(view_all_arches[key][identifier])
                        

                        if view_all_arches["has_buildroot"]:
                            if not self.data["buildroot"]["srpms"][repo_id][arch][package["id"]]["succeeded"]:
                                view_all_arches["everything_succeeded"] = False
                                view_all_arches[key][identifier]["buildroot_succeeded"] = False
                                view_all_arches[key][identifier]["errors"][arch] = self.data["buildroot"]["srpms"][repo_id][arch][package["id"]]["errors"]
                            if self.data["buildroot"]["srpms"][repo_id][arch][package["id"]]["warnings"]["message"]:
                                view_all_arches["no_warnings"] = False
                                view_all_arches[key][identifier]["buildroot_no_warnings"] = False
                                view_all_arches[key][identifier]["warnings"][arch] = self.data["buildroot"]["srpms"][repo_id][arch][package["id"]]["warnings"]

                            
                        view_all_arches[key][identifier]["arches"].add(arch)

                        self._populate_pkg_or_srpm_relations_fields(view_all_arches[key][identifier], package, type="srpm")
                    

                    # Add binary packages to source packages
                    for pkg_id, pkg in view["pkgs"].items():

                        source_name = pkg["source_name"]

                        # Add package names
                        view_all_arches["source_pkgs_by_name"][source_name]["pkg_names"].add(pkg["name"])

                        # Add package nevrs
                        pkg_nevr = "{name}-{evr}".format(
                            name=pkg["name"],
                            evr=pkg["evr"]
                        )
                        view_all_arches["source_pkgs_by_name"][source_name]["pkg_nevrs"].add(pkg_nevr)
                                            
                    
                    # Modules
                    for module_id, module in view["modules"].items():

                        if module_id not in view_all_arches["modules"]:
                            view_all_arches["modules"][module_id] = {}
                            view_all_arches["modules"][module_id]["id"] = module_id
                            # ...
                

                # RPMs
                for pkg in view_all_arches["pkgs_by_nevr"].values():
                    category = None
                    if pkg["in_workload_ids_env"]:
                        category = "env"
                    elif pkg["in_workload_ids_req"]:
                        category = "req"
                    elif pkg["in_workload_ids_dep"]:
                        category = "dep"
                    elif pkg["in_buildroot_of_srpm_id_env"]:
                        category = "build_base"
                    elif pkg["in_buildroot_of_srpm_id_req"] or pkg["in_buildroot_of_srpm_id_dep"]:
                        if pkg["level_number"] == 1:
                            category = "build_level_1"
                        elif pkg["level_number"] > 1:
                            category = "build_level_2_plus"
                    
                    view_all_arches["numbers"]["pkgs"][category] += 1
                
                view_all_arches["numbers"]["pkgs"]["runtime"] = view_all_arches["numbers"]["pkgs"]["env"] + view_all_arches["numbers"]["pkgs"]["req"] + view_all_arches["numbers"]["pkgs"]["dep"]
                view_all_arches["numbers"]["pkgs"]["build"] = view_all_arches["numbers"]["pkgs"]["build_base"] + view_all_arches["numbers"]["pkgs"]["build_level_1"] + view_all_arches["numbers"]["pkgs"]["build_level_2_plus"]
                
                # SRPMs
                for pkg in view_all_arches["source_pkgs_by_name"].values():
                    category = None
                    if pkg["in_workload_ids_env"]:
                        category = "env"
                    elif pkg["in_workload_ids_req"]:
                        category = "req"
                    elif pkg["in_workload_ids_dep"]:
                        category = "dep"
                    elif pkg["in_buildroot_of_srpm_id_env"]:
                        category = "build_base"
                    elif pkg["in_buildroot_of_srpm_id_req"] or pkg["in_buildroot_of_srpm_id_dep"]:
                        if pkg["level_number"] == 1:
                            category = "build_level_1"
                        elif pkg["level_number"] > 1:
                            category = "build_level_2_plus"
                    
                    view_all_arches["numbers"]["srpms"][category] += 1
                
                view_all_arches["numbers"]["srpms"]["runtime"] = \
                    view_all_arches["numbers"]["srpms"]["env"] + \
                    view_all_arches["numbers"]["srpms"]["req"] + \
                    view_all_arches["numbers"]["srpms"]["dep"]

                view_all_arches["numbers"]["srpms"]["build"] = \
                    view_all_arches["numbers"]["srpms"]["build_base"] + \
                    view_all_arches["numbers"]["srpms"]["build_level_1"] + \
                    view_all_arches["numbers"]["srpms"]["build_level_2_plus"]





                # Done
                views_all_arches[view_conf_id] = view_all_arches
        
        self.data["views_all_arches"] = views_all_arches


    def _add_unwanted_packages_to_view(self, view, view_conf):

        arch = view["arch"]

        # Find exclusion lists mathing this view's label(s)
        unwanted_conf_ids = set()
        for view_label in view_conf["labels"]:
            for unwanted_conf_id, unwanted in self.configs["unwanteds"].items():
                for unwanted_label in unwanted["labels"]:
                    if view_label == unwanted_label:
                        unwanted_conf_ids.add(unwanted_conf_id)
        
        # Dicts
        pkgs_unwanted_buildroot = {}
        pkgs_unwanted_completely = {}
        srpms_unwanted_buildroot = {}
        srpms_unwanted_completely = {}

        # Populate the dicts
        for unwanted_conf_id in unwanted_conf_ids:
            unwanted_conf = self.configs["unwanteds"][unwanted_conf_id]

            # Pkgs
            for pkg_name in unwanted_conf["unwanted_packages"]:
                if pkg_name not in pkgs_unwanted_completely:
                    pkgs_unwanted_completely[pkg_name] = set()
                pkgs_unwanted_completely[pkg_name].add(unwanted_conf_id)

            # Arch Pkgs
            for pkg_name in unwanted_conf["unwanted_arch_packages"][arch]:
                if pkg_name not in pkgs_unwanted_completely:
                    pkgs_unwanted_completely[pkg_name] = set()
                pkgs_unwanted_completely[pkg_name].add(unwanted_conf_id)

            # SRPMs
            for pkg_source_name in unwanted_conf["unwanted_source_packages"]:
                if pkg_source_name not in srpms_unwanted_completely:
                    srpms_unwanted_completely[pkg_source_name] = set()
                srpms_unwanted_completely[pkg_source_name].add(unwanted_conf_id)

        # Add it to the packages
        for pkg_id, pkg in view["pkgs"].items():
            pkg_name = pkg["name"]
            srpm_name = pkg["source_name"]

            if pkg_name in pkgs_unwanted_completely:
                list_ids = pkgs_unwanted_completely[pkg_name]
                view["pkgs"][pkg_id]["unwanted_completely_in_list_ids"].update(list_ids)

            if srpm_name in srpms_unwanted_completely:
                list_ids = srpms_unwanted_completely[srpm_name]
                view["pkgs"][pkg_id]["unwanted_completely_in_list_ids"].update(list_ids)
        
        # Add it to the srpms
        for srpm_id, srpm in view["source_pkgs"].items():
            srpm_name = srpm["name"]

            if srpm_name in srpms_unwanted_completely:
                list_ids = srpms_unwanted_completely[srpm_name]
                view["source_pkgs"][srpm_id]["unwanted_completely_in_list_ids"].update(list_ids)


    def _add_unwanted_packages_to_views(self):

        log("")
        log("Adding Unwanted Packages to views...")
        log("")

        # First, the standard views
        for view_conf_id in self.configs["views"]:
            view_conf = self.configs["views"][view_conf_id]

            if view_conf["type"] == "compose":
                if view_conf["buildroot_strategy"] == "root_logs":
                    for arch in view_conf["architectures"]:

                        view_id = "{view_conf_id}:{arch}".format(
                            view_conf_id=view_conf_id,
                            arch=arch
                        )

                        view = self.data["views"][view_id]

                        self._add_unwanted_packages_to_view(view, view_conf)


    def _recommend_maintainers(self):

        # Packages can be on one or more _levels_:
        #   level 0 is runtime
        #   level 1 is build deps of the previous level
        #   level 2 is build deps of the previous level
        #   ... etc.
        #
        # Within a level, they can be on one or more _sublevels_:
        #   level 0 sublevel 0 is explicitly required
        #   level 0 sublevel 1 is runtiem deps of the previous sublevel
        #   level 0 sublevel 2 is runtiem deps of the previous sublevel
        #   ... etc
        #   level 1 sublevel 0 is direct build deps of the previous level
        #   level 1 sublevel 1 is runtime deps of the previous sublevel
        #   level 1 sublevel 2 is runtiem deps of the previous sublevel
        #   ... etc
        #
        # I'll call a combination of these a _score_ because I can't think of
        # anything better at this point. It's a tuple! 
        # 
        # (0, 0)
        #  |  '-- sub-level 0 == explicitly required
        #  '---- level 0 == runtime
        # 


        for view_conf_id in self.configs["views"]:
            view_conf = self.configs["views"][view_conf_id]
            view_all_arches = self.data["views_all_arches"][view_conf_id]

            # Skip addons for now
            # TODO: Implement support for addons
            if view_conf["type"] == "addon":
                continue

            log("  {}".format(view_conf_id))

            # Level 0
            level = str(0)
            sublevel = str(0)
            score = (level, sublevel)

            log("    {}".format(score))

            # There's not much point in analyzing packages on multple levels.
            # For example, if someone explicitly requires glibc, I don't need to track
            # details up until the very end of the dependency chain...
            this_level_srpms = set()
            previous_level_srpms = set()

            # Take all explicitly required packages and assign them
            # to the maintainer of their workloads.
            #
            # Or of this is the buildroot levels, 
            for pkg_name, pkg in view_all_arches["pkgs_by_name"].items():
                source_name = pkg["source_name"]

                # Only want explicitly required ones
                for workload_id in pkg["in_workload_ids_req"]:
                    workload = self.data["workloads"][workload_id]
                    workload_conf_id = workload["workload_conf_id"]
                    workload_conf = self.configs["workloads"][workload_conf_id]

                    workload_maintainer = workload_conf["maintainer"]

                    # 1/  maintainer_recommendation

                    if workload_maintainer not in pkg["maintainer_recommendation"]:
                        #pkg["maintainer_recommendation"][workload_maintainer] = set()
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][workload_maintainer] = set()
                    
                    #pkg["maintainer_recommendation"][workload_maintainer].add(score)
                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][workload_maintainer].add(score)

                    # 2/  maintainer_recommendation_details

                    if level not in pkg["maintainer_recommendation_details"]:
                        #pkg["maintainer_recommendation_details"][level] = {}
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level] = {}
                    
                    if sublevel not in pkg["maintainer_recommendation_details"][level]:
                        #pkg["maintainer_recommendation_details"][level][sublevel] = {}
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel] = {}
                    
                    if workload_maintainer not in pkg["maintainer_recommendation_details"][level][sublevel]:
                        #pkg["maintainer_recommendation_details"][level][sublevel][workload_maintainer] = {}
                        #pkg["maintainer_recommendation_details"][level][sublevel][workload_maintainer]["reasons"] = {}
                        #pkg["maintainer_recommendation_details"][level][sublevel][workload_maintainer]["locations"] = {}
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][workload_maintainer] = {}
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][workload_maintainer]["reasons"] = set()
                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][workload_maintainer]["locations"] = set()

                    #pkg["maintainer_recommendation_details"][level][sublevel][workload_maintainer]["locations"].add(workload_conf_id)
                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][workload_maintainer]["locations"].add(workload_conf_id)

            # Lie to the while loop so it runs at least once
            level_changes_made = True
            level_change_detection = set()


            while level_changes_made:

                # Level 1 and higher
                if int(level) > 0:

                    level_changes_made = False

                    log("    {}".format(score))

                    # Take all the direct build dependencies
                    # of the previous group, and assign them to the maintainers of packages
                    # that pulled them in
                    for pkg_name, pkg in view_all_arches["pkgs_by_name"].items():
                        source_name = pkg["source_name"]

                        # Don't process packages on multiple levels. (more details above)
                        if source_name in previous_level_srpms:
                            continue

                        # Look at all SRPMs that directly pull this RPM into the buildroot...
                        for buildroot_srpm_name in pkg["in_buildroot_of_srpm_name_req"]:
                            buildroot_srpm = view_all_arches["source_pkgs_by_name"][buildroot_srpm_name]

                            # ... and if they're in the previous group, assign their maintainer(s)

                            # But limit this to only the ones with the highest score.
                            all_the_previous_sublevels_of_this_buildroot_srpm = set()
                            for buildroot_srpm_maintainer, buildroot_srpm_maintainer_scores in buildroot_srpm["maintainer_recommendation"].items():
                                for buildroot_srpm_maintainer_score in buildroot_srpm_maintainer_scores:
                                    buildroot_srpm_maintainer_score_level, buildroot_srpm_maintainer_score_sublevel = buildroot_srpm_maintainer_score
                                    if not buildroot_srpm_maintainer_score_level == prev_level:
                                        continue
                                    all_the_previous_sublevels_of_this_buildroot_srpm.add(buildroot_srpm_maintainer_score_sublevel)
                            if not all_the_previous_sublevels_of_this_buildroot_srpm:
                                continue
                            the_highest_sublevel_of_this_buildroot_srpm = min(all_the_previous_sublevels_of_this_buildroot_srpm)
                            the_score_I_care_about = (prev_level, the_highest_sublevel_of_this_buildroot_srpm)

                            for buildroot_srpm_maintainer, buildroot_srpm_maintainer_scores in buildroot_srpm["maintainer_recommendation"].items():

                                if the_score_I_care_about in buildroot_srpm_maintainer_scores:

                                    level_change_detection_tuple = (buildroot_srpm_name, pkg_name)
                                    if level_change_detection_tuple not in level_change_detection:
                                        level_changes_made = True
                                        level_change_detection.add(level_change_detection_tuple)

                                    # 1/  maintainer_recommendation

                                    if buildroot_srpm_maintainer not in pkg["maintainer_recommendation"]:
                                        #pkg["maintainer_recommendation"][workload_maintainer] = set()
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][buildroot_srpm_maintainer] = set()

                                    #pkg["maintainer_recommendation"][buildroot_srpm_maintainer].add(score)
                                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][buildroot_srpm_maintainer].add(score)

                                    # 2/  maintainer_recommendation_details

                                    if level not in pkg["maintainer_recommendation_details"]:
                                        #pkg["maintainer_recommendation_details"][level] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level] = {}
                                    
                                    if sublevel not in pkg["maintainer_recommendation_details"][level]:
                                        #pkg["maintainer_recommendation_details"][level][sublevel] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel] = {}
                                    
                                    if buildroot_srpm_maintainer not in pkg["maintainer_recommendation_details"][level][sublevel]:
                                        #pkg["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer] = {}
                                        #pkg["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer]["reasons"] = {}
                                        #pkg["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer]["locations"] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer]["reasons"] = set()
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer]["locations"] = set()

                                    #pkg["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer]["locations"].add(buildroot_srpm_name)
                                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][buildroot_srpm_maintainer]["locations"].add(buildroot_srpm_name)


                # Time to look at runtime dependencies!
                #
                # Take all packages that depend on the previous group and assign them
                # to the maintainer of their superior package. Do this in a loop until
                # there's nothing to assign.
                #
                # So this will deal with scores 0.1, 0.2, 0.3, ...

                # Lie to the while loop so it runs at least once
                sublevel_changes_made = True
                sublevel_change_detection = set()

                while sublevel_changes_made:

                    # Reset its memories. Let it make some new real memories!!
                    sublevel_changes_made = False

                    # Jump another sub-level down
                    prev_score = score
                    prev_sublevel = sublevel
                    #sublevel += 1
                    sublevel = str(int(sublevel) + 1)
                    score = (level, sublevel)

                    log("    {}".format(score))

                    for pkg_name, pkg in view_all_arches["pkgs_by_name"].items():
                        source_name = pkg["source_name"]

                        # Don't process packages on multiple levels. (more details above)
                        if source_name in previous_level_srpms:
                            continue

                        # Look at all of its superior packages (packages that require it)...
                        for superior_pkg_name in pkg["hard_dependency_of_pkg_names"]:
                            superior_pkg = view_all_arches["pkgs_by_name"][superior_pkg_name]
                            superior_srpm_name = superior_pkg["source_name"]

                            # ... and if they're in the previous group, assign their maintainer(s)
                            for superior_pkg_maintainer, superior_pkg_maintainer_scores in superior_pkg["maintainer_recommendation"].items():
                                if prev_score in superior_pkg_maintainer_scores:

                                    sublevel_change_detection_tuple = (superior_pkg_name, pkg_name, superior_pkg_maintainer)
                                    if sublevel_change_detection_tuple in sublevel_change_detection:
                                        continue
                                    else:
                                        sublevel_changes_made = True
                                        sublevel_change_detection.add(sublevel_change_detection_tuple)
                                    
                                    # 1/  maintainer_recommendation

                                    if superior_pkg_maintainer not in pkg["maintainer_recommendation"]:
                                        #pkg["maintainer_recommendation"][workload_maintainer] = set()
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][superior_pkg_maintainer] = set()

                                    #pkg["maintainer_recommendation"][superior_pkg_maintainer].add(score)
                                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation"][superior_pkg_maintainer].add(score)

                                    # 2/  maintainer_recommendation_details

                                    if level not in pkg["maintainer_recommendation_details"]:
                                        #pkg["maintainer_recommendation_details"][level] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level] = {}
                                    
                                    if sublevel not in pkg["maintainer_recommendation_details"][level]:
                                        #pkg["maintainer_recommendation_details"][level][sublevel] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel] = {}
                                    
                                    if superior_pkg_maintainer not in pkg["maintainer_recommendation_details"][level][sublevel]:
                                        #pkg["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer] = {}
                                        #pkg["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["reasons"] = {}
                                        #pkg["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["locations"] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer] = {}
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["reasons"] = set()
                                        self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["locations"] = set()

                                    # Copy the locations from the superior package one sublevel up
                                    locations = superior_pkg["maintainer_recommendation_details"][level][prev_sublevel][superior_pkg_maintainer]["locations"]
                                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["locations"].update(locations)

                                    reason = (superior_pkg_name, superior_srpm_name, pkg_name)
                                    #pkg["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["reasons"].add(reason)
                                    self.data["views_all_arches"][view_conf_id]["pkgs_by_name"][pkg_name]["maintainer_recommendation_details"][level][sublevel][superior_pkg_maintainer]["reasons"].add(reason)

                
                # Now add this info to the source packages
                for pkg_name, pkg in view_all_arches["pkgs_by_name"].items():
                    source_name = pkg["source_name"]

                    # 1/  maintainer_recommendation

                    for maintainer, maintainer_scores in pkg["maintainer_recommendation"].items():

                        if maintainer not in self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation"]:
                            self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation"][maintainer] = set()
                        
                        self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation"][maintainer].update(maintainer_scores)


                        # Add it here so it's not processed again in the another level
                        this_level_srpms.add(source_name)
                    
                    # 2/  maintainer_recommendation_details

                    for loop_level, loop_sublevels in pkg["maintainer_recommendation_details"].items():

                        if loop_level not in self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"]:
                            self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level] = {}

                        for loop_sublevel, maintainers in loop_sublevels.items():

                            if loop_sublevel not in self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level]:
                                self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level][loop_sublevel] = {}

                            for maintainer, maintainer_details in maintainers.items():

                                if maintainer not in self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level][loop_sublevel]:
                                    self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level][loop_sublevel][maintainer] = {}
                                    self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level][loop_sublevel][maintainer]["reasons"] = set()
                                    self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level][loop_sublevel][maintainer]["locations"] = set()

                                reasons = maintainer_details["reasons"]
                                locations = maintainer_details["locations"]

                                self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level][loop_sublevel][maintainer]["reasons"].update(reasons)
                                self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["maintainer_recommendation_details"][loop_level][loop_sublevel][maintainer]["locations"].update(locations)




                # And set stuff for the next level
                prev_level = level
                level = str(int(level) + 1)
                #level += 1
                sublevel = str(0)
                score = (level, sublevel)
                previous_level_srpms.update(this_level_srpms)
                this_level_srpms = set()



            # And elect the best owners for each srpm
            for source_name, srpm in view_all_arches["source_pkgs_by_name"].items():

                if not srpm["maintainer_recommendation_details"]:
                    continue

                level_numbers = set()
                for level_string in srpm["maintainer_recommendation_details"].keys():
                    level_numbers.add(int(level_string))

                lowest_level_int = min(level_numbers)
                lowest_level = str(min(level_numbers))

                if not srpm["maintainer_recommendation_details"][lowest_level]:
                    continue

                sublevel_numbers = set()
                for sublevel_string in srpm["maintainer_recommendation_details"][lowest_level].keys():
                    sublevel_numbers.add(int(sublevel_string))
                lowest_sublevel = str(min(sublevel_numbers))

                maintainers_with_the_best_score = set(srpm["maintainer_recommendation_details"][lowest_level][lowest_sublevel].keys())

                highest_number_of_dependencies = 0
                best_maintainers = set()
                for maint in maintainers_with_the_best_score:

                    # If we're looking at a direct build dependency, count the number of locations == SRPMs that directly need this
                    # And in all other cases count the reasons == the number of packages that runtime require
                    # (in case of 0,0 len(reasons) is always 1 as it just says "directly required" so that works fine)
                    if lowest_level_int > 0 and lowest_sublevel == "0":
                        number_of_dependencies = len(srpm["maintainer_recommendation_details"][lowest_level][lowest_sublevel][maint]["locations"])
                    else:
                        number_of_dependencies = len(srpm["maintainer_recommendation_details"][lowest_level][lowest_sublevel][maint]["reasons"])

                    if number_of_dependencies > highest_number_of_dependencies:
                        highest_number_of_dependencies = number_of_dependencies
                        best_maintainers = set()

                    if number_of_dependencies == highest_number_of_dependencies:
                        best_maintainers.add(maint)

                self.data["views_all_arches"][view_conf_id]["source_pkgs_by_name"][source_name]["best_maintainers"].update(best_maintainers)



                     
        log("")
        log("  DONE!")
        log("")


    def analyze_things(self):
        log("")
        log("###############################################################################")
        log("### Analyzing stuff! ##########################################################")
        log("###############################################################################")
        log("")

        self.data["pkgs"] = {}
        self.data["envs"] = {}
        self.data["workloads"] = {}
        self.data["views"] = {}

        with tempfile.TemporaryDirectory() as tmp:

            if self.settings["dnf_cache_dir_override"]:
                self.tmp_dnf_cachedir = self.settings["dnf_cache_dir_override"]
            else:
                self.tmp_dnf_cachedir = os.path.join(tmp, "dnf_cachedir")
            self.tmp_installroots = os.path.join(tmp, "installroots")

            # List of supported arches
            all_arches = self.settings["allowed_arches"]

            # Repos
            log("")
            log("=====  Analyzing Repos =====")
            log("")
            self._analyze_repos()

            # Environments
            log("")
            log("=====  Analyzing Environments =====")
            log("")
            self._analyze_envs()

            # Workloads
            log("")
            log("=====  Analyzing Workloads =====")
            log("")
            self._analyze_workloads()

            # Views
            #
            # This creates:
            #    data["views"][view_id]["id"]
            #    data["views"][view_id]["view_conf_id"]
            #    data["views"][view_id]["arch"]
            #    data["views"][view_id]["workload_ids"]
            #    data["views"][view_id]["pkgs"]
            #    data["views"][view_id]["source_pkgs"]
            #    data["views"][view_id]["modules"]
            #
            log("")
            log("=====  Analyzing Views =====")
            log("")
            self._analyze_views()

            # Buildroot
            # This is partially similar to workloads, because it's resolving
            # the full dependency tree of the direct build dependencies of SRPMs
            #
            # So compared to workloads:
            #   direct build dependencies are like required packages in workloads
            #   the dependencies are like dependencies in workloads
            #   the "build" group is like environments in workloads
            #
            # This completely creates:
            #   data["buildroot"]["koji_srpms"][koji_id][arch][srpm_id]...
            #   data["buildroot"]["srpms"][repo_id][arch][srpm_id]...
            # 
            log("")
            log("=====  Analyzing Buildroot =====")
            log("")
            self._analyze_buildroot()

            # Add buildroot packages to views
            # 
            # Further extends the following with buildroot packages:
            #   data["views"][view_id]["pkgs"]
            #   data["views"][view_id]["source_pkgs"]
            #
            log("")
            log("=====  Adding Buildroot to Views =====")
            log("")
            self._add_buildroot_to_views()

            # Generate combined views for all arches
            log("")
            log("=====  Generating views_all_arches =====")
            log("")
            self. _generate_views_all_arches()

            # Unwanted packages
            log("")
            log("=====  Adding Unwanted Packages to Views =====")
            log("")
            self._add_unwanted_packages_to_views()

            # Recommend package maintainers in views
            log("")
            log("=====  Recommending maintainers =====")
            log("")
            self._recommend_maintainers()


            # Finally, save the cache for next time
            dump_data(self.settings["root_log_deps_cache_path"], self.cache["root_log_deps"]["next"])

            

        return self.data





###############################################################################
### Query gives an easy access to the data! ###################################
###############################################################################


class Query():
    def __init__(self, data, configs, settings):
        self.data = data
        self.configs = configs
        self.settings = settings

        self.computed_data = {}

    def size(self, num, suffix='B'):
        for unit in ['','k','M','G']:
            if abs(num) < 1024.0:
                return "%3.1f %s%s" % (num, unit, suffix)
            num /= 1024.0
        return "%.1f %s%s" % (num, 'T', suffix)
        

    @lru_cache(maxsize = None)
    def workloads(self, workload_conf_id, env_conf_id, repo_id, arch, list_all=False, output_change=None):
        # accepts none in any argument, and in those cases, answers for all instances

        # It can output just one part of the id.
        # That's useful to, for example, list all arches associated with a workload_conf_id
        if output_change:
            list_all = True
            if output_change not in ["workload_conf_ids", "env_conf_ids", "repo_ids", "arches"]:
                raise ValueError('output_change must be one of: "workload_conf_ids", "env_conf_ids", "repo_ids", "arches"')

        matching_ids = set()

        # list considered workload_conf_ids
        if workload_conf_id:
            workload_conf_ids = [workload_conf_id]
        else:
            workload_conf_ids = self.configs["workloads"].keys()

        # list considered env_conf_ids
        if env_conf_id:
            env_conf_ids = [env_conf_id]
        else:
            env_conf_ids = self.configs["envs"].keys()
        
        # list considered repo_ids
        if repo_id:
            repo_ids = [repo_id]
        else:
            repo_ids = self.configs["repos"].keys()
            
        # list considered arches
        if arch:
            arches = [arch]
        else:
            arches = self.settings["allowed_arches"]
        
        # And now try looping through all of that, and return True on a first occurance
        # This is a terrible amount of loops. But most cases will have just one item
        # in most of those, anyway. No one is expected to run this method with
        # a "None" for every argument!
        for workload_conf_id in workload_conf_ids:
            for env_conf_id in env_conf_ids:
                for repo_id in repo_ids:
                    for arch in arches:
                        workload_id = "{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}".format(
                            workload_conf_id=workload_conf_id,
                            env_conf_id=env_conf_id,
                            repo_id=repo_id,
                            arch=arch
                        )
                        if workload_id in self.data["workloads"].keys():
                            if not list_all:
                                return True
                            if output_change:
                                if output_change == "workload_conf_ids":
                                    matching_ids.add(workload_conf_id)
                                if output_change == "env_conf_ids":
                                    matching_ids.add(env_conf_id)
                                if output_change == "repo_ids":
                                    matching_ids.add(repo_id)
                                if output_change == "arches":
                                    matching_ids.add(arch)
                            else:
                                matching_ids.add(workload_id)
        
        if not list_all:
            return False
        return sorted(list(matching_ids))
    
    @lru_cache(maxsize = None)
    def workloads_id(self, id, list_all=False, output_change=None):
        # Accepts both env and workload ID, and returns workloads that match that
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.workloads(None, env_conf_id, repo_id, arch, list_all, output_change)
        
        # It's a workload! Why would you want that, anyway?!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.workloads(workload_conf_id, env_conf_id, repo_id, arch, list_all, output_change)
        
        raise ValueError("That seems to be an invalid ID!")

    @lru_cache(maxsize = None)
    def envs(self, env_conf_id, repo_id, arch, list_all=False, output_change=None):
        # accepts none in any argument, and in those cases, answers for all instances

        # It can output just one part of the id.
        # That's useful to, for example, list all arches associated with a workload_conf_id
        if output_change:
            list_all = True
            if output_change not in ["env_conf_ids", "repo_ids", "arches"]:
                raise ValueError('output_change must be one of: "env_conf_ids", "repo_ids", "arches"')
        
        matching_ids = set()

        # list considered env_conf_ids
        if env_conf_id:
            env_conf_ids = [env_conf_id]
        else:
            env_conf_ids = self.configs["envs"].keys()
        
        # list considered repo_ids
        if repo_id:
            repo_ids = [repo_id]
        else:
            repo_ids = self.configs["repos"].keys()
            
        # list considered arches
        if arch:
            arches = [arch]
        else:
            arches = self.settings["allowed_arches"]
        
        # And now try looping through all of that, and return True on a first occurance
        # This is a terrible amount of loops. But most cases will have just one item
        # in most of those, anyway. No one is expected to run this method with
        # a "None" for every argument!
        for env_conf_id in env_conf_ids:
            for repo_id in repo_ids:
                for arch in arches:
                    env_id = "{env_conf_id}:{repo_id}:{arch}".format(
                        env_conf_id=env_conf_id,
                        repo_id=repo_id,
                        arch=arch
                    )
                    if env_id in self.data["envs"].keys():
                        if not list_all:
                            return True
                        if output_change:
                            if output_change == "env_conf_ids":
                                matching_ids.add(env_conf_id)
                            if output_change == "repo_ids":
                                matching_ids.add(repo_id)
                            if output_change == "arches":
                                matching_ids.add(arch)
                        else:
                            matching_ids.add(env_id)
        
        # This means nothing has been found!
        if not list_all:
            return False
        return sorted(list(matching_ids))
    
    @lru_cache(maxsize = None)
    def envs_id(self, id, list_all=False, output_change=None):
        # Accepts both env and workload ID, and returns workloads that match that
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.envs(env_conf_id, repo_id, arch, list_all, output_change)
        
        # It's a workload!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.envs(env_conf_id, repo_id, arch, list_all, output_change)
        
        raise ValueError("That seems to be an invalid ID!")
    
    @lru_cache(maxsize = None)
    def workload_pkgs(self, workload_conf_id, env_conf_id, repo_id, arch, output_change=None):
        # Warning: mixing repos and arches works, but might cause mess on the output

        # Default output is just a flat list. Extra fields will be added into each package:
        # q_in          - set of workload_ids including this pkg
        # q_required_in - set of workload_ids where this pkg is required (top-level)
        # q_env_in      - set of workload_ids where this pkg is in env
        # q_arch        - architecture

        # Other outputs:
        #   - "ids"         — a list ids
        #   - "binary_names"  — a list of RPM names
        #   - "source_nvr"  — a list of SRPM NVRs
        #   - "source_names"  — a list of SRPM names
        if output_change:
            list_all = True
            if output_change not in ["ids", "binary_names", "source_nvr", "source_names"]:
                raise ValueError('output_change must be one of: "ids", "binary_names", "source_nvr", "source_names"')
        
        # Step 1: get all the matching workloads!
        workload_ids = self.workloads(workload_conf_id, env_conf_id, repo_id, arch, list_all=True)

        # I'll need repo_ids and arches to access the packages
        repo_ids = self.workloads(workload_conf_id, env_conf_id, repo_id, arch, output_change="repo_ids")
        arches = self.workloads(workload_conf_id, env_conf_id, repo_id, arch, output_change="arches")

        # Replicating the same structure as in data["pkgs"]
        # That is: [repo_id][arch][pkg_id]
        pkgs = {}
        for repo_id in repo_ids:
            pkgs[repo_id] = {}
            for arch in arches:
                pkgs[repo_id][arch] = {}

        # Workloads are already paired with envs, repos, and arches
        # (there is one for each combination)
        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_arch = workload["arch"]
            workload_repo_id = workload["repo_id"]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]

            # First, get all pkgs in the env
            for pkg_id in workload["pkg_env_ids"]:

                # Add it to the list if it's not there already.
                # Create a copy since it's gonna be modified, and include only what's needed
                pkg = self.data["pkgs"][workload_repo_id][workload_arch][pkg_id]
                if pkg_id not in pkgs[workload_repo_id][workload_arch]:
                    pkgs[workload_repo_id][workload_arch][pkg_id] = {}
                    pkgs[workload_repo_id][workload_arch][pkg_id]["id"] = pkg_id
                    pkgs[workload_repo_id][workload_arch][pkg_id]["name"] = pkg["name"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["evr"] = pkg["evr"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["arch"] = pkg["arch"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["installsize"] = pkg["installsize"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["description"] = pkg["description"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["summary"] = pkg["summary"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["source_name"] = pkg["source_name"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_arch"] = workload_arch
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_in"] = set()
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"] = set()
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_env_in"] = set()
                
                # It's here, so add it
                pkgs[workload_repo_id][workload_arch][pkg_id]["q_in"].add(workload_id)
                # Browsing env packages, so add it
                pkgs[workload_repo_id][workload_arch][pkg_id]["q_env_in"].add(workload_id)
                # Is it required?
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["packages"]:
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"].add(workload_id)
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["arch_packages"][workload_arch]:
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"].add(workload_id)
            
            # Second, add all the other packages
            for pkg_id in workload["pkg_added_ids"]:

                # Add it to the list if it's not there already
                # and initialize extra fields
                pkg = self.data["pkgs"][workload_repo_id][workload_arch][pkg_id]
                if pkg_id not in pkgs[workload_repo_id][workload_arch]:
                    pkgs[workload_repo_id][workload_arch][pkg_id] = {}
                    pkgs[workload_repo_id][workload_arch][pkg_id]["id"] = pkg_id
                    pkgs[workload_repo_id][workload_arch][pkg_id]["name"] = pkg["name"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["evr"] = pkg["evr"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["arch"] = pkg["arch"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["installsize"] = pkg["installsize"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["description"] = pkg["description"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["summary"] = pkg["summary"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["source_name"] = pkg["source_name"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_arch"] = workload_arch
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_in"] = set()
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"] = set()
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_env_in"] = set()
                
                # It's here, so add it
                pkgs[workload_repo_id][workload_arch][pkg_id]["q_in"].add(workload_id)
                # Not adding it to q_env_in
                # Is it required?
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["packages"]:
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"].add(workload_id)
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["arch_packages"][workload_arch]:
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"].add(workload_id)
            
            # Third, add package placeholders if any
            for placeholder_id in workload["pkg_placeholder_ids"]:
                placeholder = workload_conf["package_placeholders"]["pkgs"][pkg_id_to_name(placeholder_id)]
                if placeholder_id not in pkgs[workload_repo_id][workload_arch]:
                    pkgs[workload_repo_id][workload_arch][placeholder_id] = {}
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["id"] = placeholder_id
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["name"] = placeholder["name"]
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["evr"] = "000-placeholder"
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["arch"] = "placeholder"
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["installsize"] = 0
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["description"] = placeholder["description"]
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["summary"] = placeholder["description"]
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["source_name"] = placeholder["srpm"]
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["q_arch"] = workload_arch
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["q_in"] = set()
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["q_required_in"] = set()
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["q_env_in"] = set()

                # It's here, so add it
                pkgs[workload_repo_id][workload_arch][placeholder_id]["q_in"].add(workload_id)
                # All placeholders are required
                pkgs[workload_repo_id][workload_arch][placeholder_id]["q_required_in"].add(workload_id)

        # Is it supposed to only output ids?
        if output_change:
            pkg_names = set()
            for repo_id in repo_ids:
                for arch in arches:
                    for pkg_id, pkg in pkgs[repo_id][arch].items():
                        if output_change == "ids":
                            pkg_names.add(pkg["id"])
                        elif output_change == "binary_names":
                            pkg_names.add(pkg["name"])
                        elif output_change == "source_nvr":
                            pkg_names.add(pkg["sourcerpm"])
                        elif output_change == "source_names":
                            pkg_names.add(pkg["source_name"])
            
            names_sorted = sorted(list(pkg_names))
            return names_sorted
                        

        # And now I just need to flatten that dict and return all packages as a list
        final_pkg_list = []
        for repo_id in repo_ids:
            for arch in arches:
                for pkg_id, pkg in pkgs[repo_id][arch].items():
                    final_pkg_list.append(pkg)

        # And sort them by nevr which is their ID
        final_pkg_list_sorted = sorted(final_pkg_list, key=lambda k: k['id'])

        return final_pkg_list_sorted


    @lru_cache(maxsize = None)
    def workload_pkgs_id(self, id, output_change=None):
        # Accepts both env and workload ID, and returns pkgs for workloads that match
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.workload_pkgs(None, env_conf_id, repo_id, arch, output_change)
        
        # It's a workload!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.workload_pkgs(workload_conf_id, env_conf_id, repo_id, arch, output_change)
        
        raise ValueError("That seems to be an invalid ID!")
    
    @lru_cache(maxsize = None)
    def env_pkgs(self, env_conf_id, repo_id, arch):
        # Warning: mixing repos and arches works, but might cause mess on the output

        # Output is just a flat list. Extra fields will be added into each package:
        # q_in          - set of env_ids including this pkg
        # q_required_in - set of env_ids where this pkg is required (top-level)
        # q_arch        - architecture

        
        # Step 1: get all the matching envs!
        env_ids = self.envs(env_conf_id, repo_id, arch, list_all=True)

        # I'll need repo_ids and arches to access the packages
        repo_ids = self.envs(env_conf_id, repo_id, arch, output_change="repo_ids")
        arches = self.envs(env_conf_id, repo_id, arch, output_change="arches")

        # Replicating the same structure as in data["pkgs"]
        # That is: [repo_id][arch][pkg_id]
        pkgs = {}
        for repo_id in repo_ids:
            pkgs[repo_id] = {}
            for arch in arches:
                pkgs[repo_id][arch] = {}

        # envs are already paired with repos, and arches
        # (there is one for each combination)
        for env_id in env_ids:
            env = self.data["envs"][env_id]
            env_arch = env["arch"]
            env_repo_id = env["repo_id"]
            env_conf_id = env["env_conf_id"]

            for pkg_id in env["pkg_ids"]:

                # Add it to the list if it's not there already.
                # Create a copy since it's gonna be modified, and include only what's needed
                pkg = self.data["pkgs"][env_repo_id][env_arch][pkg_id]
                if pkg_id not in pkgs[env_repo_id][env_arch]:
                    pkgs[env_repo_id][env_arch][pkg_id] = {}
                    pkgs[env_repo_id][env_arch][pkg_id]["id"] = pkg_id
                    pkgs[env_repo_id][env_arch][pkg_id]["name"] = pkg["name"]
                    pkgs[env_repo_id][env_arch][pkg_id]["evr"] = pkg["evr"]
                    pkgs[env_repo_id][env_arch][pkg_id]["arch"] = pkg["arch"]
                    pkgs[env_repo_id][env_arch][pkg_id]["installsize"] = pkg["installsize"]
                    pkgs[env_repo_id][env_arch][pkg_id]["description"] = pkg["description"]
                    pkgs[env_repo_id][env_arch][pkg_id]["summary"] = pkg["summary"]
                    pkgs[env_repo_id][env_arch][pkg_id]["source_name"] = pkg["source_name"]
                    pkgs[env_repo_id][env_arch][pkg_id]["sourcerpm"] = pkg["sourcerpm"]
                    pkgs[env_repo_id][env_arch][pkg_id]["q_arch"] = env_arch
                    pkgs[env_repo_id][env_arch][pkg_id]["q_in"] = set()
                    pkgs[env_repo_id][env_arch][pkg_id]["q_required_in"] = set()
                
                # It's here, so add it
                pkgs[env_repo_id][env_arch][pkg_id]["q_in"].add(env_id)
                # Is it required?
                if pkg["name"] in self.configs["envs"][env_conf_id]["packages"]:
                    pkgs[env_repo_id][env_arch][pkg_id]["q_required_in"].add(env_id)
                if pkg["name"] in self.configs["envs"][env_conf_id]["arch_packages"][env_arch]:
                    pkgs[env_repo_id][env_arch][pkg_id]["q_required_in"].add(env_id)

        # And now I just need to flatten that dict and return all packages as a list
        final_pkg_list = []
        for repo_id in repo_ids:
            for arch in arches:
                for pkg_id, pkg in pkgs[repo_id][arch].items():
                    final_pkg_list.append(pkg)

        # And sort them by nevr which is their ID
        final_pkg_list_sorted = sorted(final_pkg_list, key=lambda k: k['id'])

        return final_pkg_list_sorted
    
    @lru_cache(maxsize = None)
    def env_pkgs_id(self, id):
        # Accepts both env and workload ID, and returns pkgs for envs that match
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.env_pkgs(env_conf_id, repo_id, arch)
        
        # It's a workload!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.env_pkgs(env_conf_id, repo_id, arch)
        
        raise ValueError("That seems to be an invalid ID!")

    @lru_cache(maxsize = None)
    def workload_size(self, workload_conf_id, env_conf_id, repo_id, arch):
        # A total size of a workload (or multiple combined!)
        pkgs = self.workload_pkgs(workload_conf_id, env_conf_id, repo_id, arch)
        size = 0
        for pkg in pkgs:
            size += pkg["installsize"]
        return size

    @lru_cache(maxsize = None)
    def env_size(self, env_conf_id, repo_id, arch):
        # A total size of an env (or multiple combined!)
        pkgs = self.env_pkgs(env_conf_id, repo_id, arch)
        size = 0
        for pkg in pkgs:
            size += pkg["installsize"]
        return size

    @lru_cache(maxsize = None)
    def workload_size_id(self, id):
        # Accepts both env and workload ID, and returns pkgs for envs that match
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.workload_size(None, env_conf_id, repo_id, arch)
        
        # It's a workload!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.workload_size(workload_conf_id, env_conf_id, repo_id, arch)
        
        raise ValueError("That seems to be an invalid ID!")
    
    @lru_cache(maxsize = None)
    def env_size_id(self, id):
        # Accepts both env and workload ID, and returns pkgs for envs that match
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.env_size(env_conf_id, repo_id, arch)
        
        # It's a workload!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.env_size(env_conf_id, repo_id, arch)
        
        raise ValueError("That seems to be an invalid ID!")
    
    def workload_url_slug(self, workload_conf_id, env_conf_id, repo_id, arch):
        slug = "{workload_conf_id}--{env_conf_id}--{repo_id}--{arch}".format(
            workload_conf_id=workload_conf_id,
            env_conf_id=env_conf_id,
            repo_id=repo_id,
            arch=arch
        )
        return slug
    
    def env_url_slug(self, env_conf_id, repo_id, arch):
        slug = "{env_conf_id}--{repo_id}--{arch}".format(
            env_conf_id=env_conf_id,
            repo_id=repo_id,
            arch=arch
        )
        return slug

    def workload_id_string(self, workload_conf_id, env_conf_id, repo_id, arch):
        slug = "{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}".format(
            workload_conf_id=workload_conf_id,
            env_conf_id=env_conf_id,
            repo_id=repo_id,
            arch=arch
        )
        return slug
    
    def env_id_string(self, env_conf_id, repo_id, arch):
        slug = "{env_conf_id}:{repo_id}:{arch}".format(
            env_conf_id=env_conf_id,
            repo_id=repo_id,
            arch=arch
        )
        return slug
    
    def url_slug_id(self, any_id):
        return any_id.replace(":", "--")
    
    @lru_cache(maxsize = None)
    def workloads_in_view(self, view_conf_id, arch, maintainer=None):
        view_conf = self.configs["views"][view_conf_id]
        repo_id = view_conf["repository"]
        labels = view_conf["labels"]
        
        if arch and arch not in self.settings["allowed_arches"]:
            raise ValueError("Unsupported arch: {arch}".format(
                arch=arch
            ))
        
        if arch and arch not in self.arches_in_view(view_conf_id):
            return []

        # First, get a set of workloads matching the repo and the arch
        too_many_workload_ids = set()
        workload_ids = self.workloads(None,None,repo_id,arch,list_all=True)
        too_many_workload_ids.update(workload_ids)

        # Second, limit that set further by matching the label
        final_workload_ids = set()
        for workload_id in too_many_workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]

            if maintainer:
                workload_maintainer = workload_conf["maintainer"]
                if workload_maintainer != maintainer:
                    continue

            workload_labels = workload["labels"]
            for workload_label in workload_labels:
                if workload_label in labels:
                    final_workload_ids.add(workload_id)

        return sorted(list(final_workload_ids))
    
    @lru_cache(maxsize = None)
    def arches_in_view(self, view_conf_id, maintainer=None):

        if len(self.configs["views"][view_conf_id]["architectures"]):
            arches = self.configs["views"][view_conf_id]["architectures"]
            return sorted(arches)
        
        return self.settings["allowed_arches"]
    
    @lru_cache(maxsize = None)
    def pkgs_in_view(self, view_conf_id, arch, output_change=None, maintainer=None):

        # Extra fields will be added into each package:
        # q_in          - set of workload_ids including this pkg
        # q_required_in - set of workload_ids where this pkg is required (top-level)
        # q_env_in      - set of workload_ids where this pkg is in env
        # q_dep_in      - set of workload_ids where this pkg is a dependency (that means not required)
        # q_maintainers - set of workload maintainers 

        # Other outputs:
        #   - "ids"         — a list of ids (NEVRA)
        #   - "nevrs"         — a list of NEVR
        #   - "binary_names"  — a list of RPM names
        #   - "source_nvr"  — a list of SRPM NVRs
        #   - "source_names"  — a list of SRPM names
        if output_change:
            list_all = True
            if output_change not in ["ids", "nevrs", "binary_names", "source_nvr", "source_names"]:
                raise ValueError('output_change must be one of: "ids", "nevrs", "binary_names", "source_nvr", "source_names"')

        
        # -----
        # Step 1: get all packages from all workloads in this view
        # -----

        workload_ids = self.workloads_in_view(view_conf_id, arch)
        repo_id = self.configs["views"][view_conf_id]["repository"]

        # This has just one repo and one arch, so a flat list of IDs is enough
        pkgs = {}
        
        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]

            # First, get all pkgs in the env
            for pkg_id in workload["pkg_env_ids"]:
                # Add it to the list if it's not there already.
                # Create a copy since it's gonna be modified, and include only what's needed
                pkg = self.data["pkgs"][repo_id][arch][pkg_id]
                if pkg_id not in pkgs:
                    pkgs[pkg_id] = {}
                    pkgs[pkg_id]["id"] = pkg_id
                    pkgs[pkg_id]["name"] = pkg["name"]
                    pkgs[pkg_id]["evr"] = pkg["evr"]
                    pkgs[pkg_id]["arch"] = pkg["arch"]
                    pkgs[pkg_id]["installsize"] = pkg["installsize"]
                    pkgs[pkg_id]["description"] = pkg["description"]
                    pkgs[pkg_id]["summary"] = pkg["summary"]
                    pkgs[pkg_id]["source_name"] = pkg["source_name"]
                    pkgs[pkg_id]["sourcerpm"] = pkg["sourcerpm"]
                    pkgs[pkg_id]["q_arch"] = arch
                    pkgs[pkg_id]["q_in"] = set()
                    pkgs[pkg_id]["q_required_in"] = set()
                    pkgs[pkg_id]["q_dep_in"] = set()
                    pkgs[pkg_id]["q_env_in"] = set()
                    pkgs[pkg_id]["q_maintainers"] = set()
                
                # It's here, so add it
                pkgs[pkg_id]["q_in"].add(workload_id)
                # Browsing env packages, so add it
                pkgs[pkg_id]["q_env_in"].add(workload_id)
                # Is it required?
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["packages"]:
                    pkgs[pkg_id]["q_required_in"].add(workload_id)
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["arch_packages"][arch]:
                    pkgs[pkg_id]["q_required_in"].add(workload_id)

            # Second, add all the other packages
            for pkg_id in workload["pkg_added_ids"]:

                # Add it to the list if it's not there already
                # and initialize extra fields
                pkg = self.data["pkgs"][repo_id][arch][pkg_id]
                if pkg_id not in pkgs:
                    pkgs[pkg_id] = {}
                    pkgs[pkg_id]["id"] = pkg_id
                    pkgs[pkg_id]["name"] = pkg["name"]
                    pkgs[pkg_id]["evr"] = pkg["evr"]
                    pkgs[pkg_id]["arch"] = pkg["arch"]
                    pkgs[pkg_id]["installsize"] = pkg["installsize"]
                    pkgs[pkg_id]["description"] = pkg["description"]
                    pkgs[pkg_id]["summary"] = pkg["summary"]
                    pkgs[pkg_id]["source_name"] = pkg["source_name"]
                    pkgs[pkg_id]["sourcerpm"] = pkg["sourcerpm"]
                    pkgs[pkg_id]["q_arch"] = arch
                    pkgs[pkg_id]["q_in"] = set()
                    pkgs[pkg_id]["q_required_in"] = set()
                    pkgs[pkg_id]["q_dep_in"] = set()
                    pkgs[pkg_id]["q_env_in"] = set()
                    pkgs[pkg_id]["q_maintainers"] = set()
                
                # It's here, so add it
                pkgs[pkg_id]["q_in"].add(workload_id)
                # Not adding it to q_env_in
                # Is it required?
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["packages"]:
                    pkgs[pkg_id]["q_required_in"].add(workload_id)
                elif pkg["name"] in self.configs["workloads"][workload_conf_id]["arch_packages"][arch]:
                    pkgs[pkg_id]["q_required_in"].add(workload_id)
                else:
                    pkgs[pkg_id]["q_dep_in"].add(workload_id)
                # Maintainer
                pkgs[pkg_id]["q_maintainers"].add(workload_conf["maintainer"])

            # Third, add package placeholders if any
            for placeholder_id in workload["pkg_placeholder_ids"]:
                placeholder = workload_conf["package_placeholders"]["pkgs"][pkg_id_to_name(placeholder_id)]
                if placeholder_id not in pkgs:
                    pkgs[placeholder_id] = {}
                    pkgs[placeholder_id]["id"] = placeholder_id
                    pkgs[placeholder_id]["name"] = placeholder["name"]
                    pkgs[placeholder_id]["evr"] = "000-placeholder"
                    pkgs[placeholder_id]["arch"] = "placeholder"
                    pkgs[placeholder_id]["installsize"] = 0
                    pkgs[placeholder_id]["description"] = placeholder["description"]
                    pkgs[placeholder_id]["summary"] = placeholder["description"]
                    pkgs[placeholder_id]["source_name"] = placeholder["srpm"]
                    pkgs[placeholder_id]["sourcerpm"] = "{}-000-placeholder".format(placeholder["srpm"])
                    pkgs[placeholder_id]["q_arch"] = arch
                    pkgs[placeholder_id]["q_in"] = set()
                    pkgs[placeholder_id]["q_required_in"] = set()
                    pkgs[placeholder_id]["q_dep_in"] = set()
                    pkgs[placeholder_id]["q_env_in"] = set()
                    pkgs[placeholder_id]["q_maintainers"] = set()
                
                # It's here, so add it
                pkgs[placeholder_id]["q_in"].add(workload_id)
                # All placeholders are required
                pkgs[placeholder_id]["q_required_in"].add(workload_id)
                # Maintainer
                pkgs[placeholder_id]["q_maintainers"].add(workload_conf["maintainer"])

        
        # -----
        # Step 2: narrow the package list down based on various criteria
        # -----

        # Is this an addon view?
        # Then I need to remove all packages that are already 
        # in the base view
        view_conf = self.configs["views"][view_conf_id]
        if view_conf["type"] == "addon":
            base_view_id = view_conf["base_view_id"]

            # I always need to get all package IDs
            base_pkg_ids = self.pkgs_in_view(base_view_id, arch, output_change="ids")
            for base_pkg_id in base_pkg_ids:
                if base_pkg_id in pkgs:
                    del pkgs[base_pkg_id]


        # Filtering by a maintainer?
        # Filter out packages not belonging to the maintainer
        # It's filtered out at this stage to keep the context of fields like
        # "q_required_in" etc. to be the whole view
        pkg_ids_to_delete = set()
        if maintainer:
            for pkg_id, pkg in pkgs.items():
                if maintainer not in pkg["q_maintainers"]:
                    pkg_ids_to_delete.add(pkg_id)
        for pkg_id in pkg_ids_to_delete:
            del pkgs[pkg_id]

        
                
        # -----
        # Step 3: Make the output to be the right format
        # -----

        # Is it supposed to only output ids?
        if output_change:
            pkg_names = set()
            for pkg_id, pkg in pkgs.items():
                if output_change == "ids":
                    pkg_names.add(pkg["id"])
                elif output_change == "nevrs":
                    pkg_names.add("{name}-{evr}".format(
                        name=pkg["name"],
                        evr=pkg["evr"]
                    ))
                elif output_change == "binary_names":
                    pkg_names.add(pkg["name"])
                elif output_change == "source_nvr":
                    pkg_names.add(pkg["sourcerpm"])
                elif output_change == "source_names":
                    pkg_names.add(pkg["source_name"])
            
            names_sorted = sorted(list(pkg_names))
            return names_sorted
                        

        # And now I just need to flatten that dict and return all packages as a list
        final_pkg_list = []
        for pkg_id, pkg in pkgs.items():
            final_pkg_list.append(pkg)

        # And sort them by nevr which is their ID
        final_pkg_list_sorted = sorted(final_pkg_list, key=lambda k: k['id'])

        return final_pkg_list_sorted
    

    @lru_cache(maxsize = None)
    def view_buildroot_pkgs(self, view_conf_id, arch, output_change=None, maintainer=None):
        # Other outputs:
        #   - "source_names"  — a list of SRPM names
        if output_change:
            if output_change not in ["source_names"]:
                raise ValueError('output_change must be one of: "source_names"')

        pkgs = {}

        buildroot_conf_id = None
        for conf_id, conf in self.configs["buildroots"].items():
            if conf["view_id"] == view_conf_id:
                buildroot_conf_id = conf_id

        if not buildroot_conf_id:
            if output_change == "source_names":
                return []
            return {}

        # Populate pkgs

        base_buildroot = self.configs["buildroots"][buildroot_conf_id]["base_buildroot"][arch]
        source_pkgs = self.configs["buildroots"][buildroot_conf_id]["source_packages"][arch]

        for pkg_name in base_buildroot:
            if pkg_name not in pkgs:
                pkgs[pkg_name] = {}
                pkgs[pkg_name]["required_by"] = set()
                pkgs[pkg_name]["base_buildroot"] = True
                pkgs[pkg_name]["srpm_name"] = None

        for srpm_name, srpm_data in source_pkgs.items():
            for pkg_name in srpm_data["requires"]:
                if pkg_name not in pkgs:
                    pkgs[pkg_name] = {}
                    pkgs[pkg_name]["required_by"] = set()
                    pkgs[pkg_name]["base_buildroot"] = False
                    pkgs[pkg_name]["srpm_name"] = None
                pkgs[pkg_name]["required_by"].add(srpm_name)

        for buildroot_pkg_relations_conf_id, buildroot_pkg_relations_conf in self.configs["buildroot_pkg_relations"].items():
            if view_conf_id != buildroot_pkg_relations_conf["view_id"]:
                continue

            if arch != buildroot_pkg_relations_conf["arch"]:
                continue
        
            buildroot_pkg_relations = buildroot_pkg_relations_conf["pkg_relations"]

            for this_pkg_id in buildroot_pkg_relations:
                this_pkg_name = pkg_id_to_name(this_pkg_id)

                if this_pkg_name in pkgs:

                    if this_pkg_id in buildroot_pkg_relations and not pkgs[this_pkg_name]["srpm_name"]:
                        pkgs[this_pkg_name]["srpm_name"] = buildroot_pkg_relations[this_pkg_id]["source_name"]


        if output_change == "source_names":
            srpms = set()

            for pkg_name, pkg in pkgs.items():
                if pkg["srpm_name"]:
                    srpms.add(pkg["srpm_name"])

            srpm_names_sorted = sorted(list(srpms))
            return srpm_names_sorted
        
        return pkgs
    
    
    @lru_cache(maxsize = None)
    def workload_succeeded(self, workload_conf_id, env_conf_id, repo_id, arch):
        workload_ids = self.workloads(workload_conf_id, env_conf_id, repo_id, arch, list_all=True)

        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            if not workload["succeeded"]:
                return False
        return True
    
    @lru_cache(maxsize = None)
    def workload_warnings(self, workload_conf_id, env_conf_id, repo_id, arch):
        workload_ids = self.workloads(workload_conf_id, env_conf_id, repo_id, arch, list_all=True)

        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            if workload["warnings"]["message"]:
                return True
        return False
    
    @lru_cache(maxsize = None)
    def env_succeeded(self, env_conf_id, repo_id, arch):
        env_ids = self.envs(env_conf_id, repo_id, arch, list_all=True)

        for env_id in env_ids:
            env = self.data["envs"][env_id]
            if not env["succeeded"]:
                return False
        return True
    
    @lru_cache(maxsize = None)
    def view_succeeded(self, view_conf_id, arch, maintainer=None):
        workload_ids = self.workloads_in_view(view_conf_id, arch)

        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]

            if maintainer:
                workload_maintainer = workload_conf["maintainer"]
                if workload_maintainer != maintainer:
                    continue

            if not workload["succeeded"]:
                return False
        return True
    

    def _srpm_name_to_rpm_names(self, srpm_name, repo_id):
        all_pkgs_by_arch = self.data["pkgs"][repo_id]

        pkg_names = set()

        for arch, pkgs in all_pkgs_by_arch.items():
            for pkg_id, pkg in pkgs.items():
                if pkg["source_name"] == srpm_name:
                    pkg_names.add(pkg["name"])

        return pkg_names

    
    @lru_cache(maxsize = None)
    def view_unwanted_pkgs(self, view_conf_id, arch, output_change=None, maintainer=None):

        # Other outputs:
        #   - "unwanted_proposals"  — a list of SRPM names
        #   - "unwanted_confirmed"  — a list of SRPM names
        output_lists = ["unwanted_proposals", "unwanted_confirmed"]
        if output_change:
            if output_change not in output_lists:
                raise ValueError('output_change must be one of: "source_names"')
        
            output_lists = output_change


        view_conf = self.configs["views"][view_conf_id]
        repo_id = view_conf["repository"]

        # Find exclusion lists mathing this view's label(s)
        unwanted_ids = set()
        for view_label in view_conf["labels"]:
            for unwanted_id, unwanted in self.configs["unwanteds"].items():
                if maintainer:
                    unwanted_maintainer = unwanted["maintainer"]
                    if unwanted_maintainer != maintainer:
                        continue
                for unwanted_label in unwanted["labels"]:
                    if view_label == unwanted_label:
                        unwanted_ids.add(unwanted_id)
        
        # This will be the package list
        unwanted_pkg_names = {}

        arches = self.settings["allowed_arches"]
        if arch:
            arches = [arch]

        ### Step 1: Get packages from this view's config (unwanted confirmed)
        if "unwanted_confirmed" in output_lists:
            if not maintainer:
                for pkg_name in view_conf["unwanted_packages"]:
                    pkg = {}
                    pkg["name"] = pkg_name
                    pkg["unwanted_in_view"] = True
                    pkg["unwanted_list_ids"] = []

                    unwanted_pkg_names[pkg_name] = pkg

                for arch in arches:
                    for pkg_name in view_conf["unwanted_arch_packages"][arch]:
                        if pkg_name in unwanted_pkg_names:
                            continue
                        
                        pkg = {}
                        pkg["name"] = pkg_name
                        pkg["unwanted_in_view"] = True
                        pkg["unwanted_list_ids"] = []

                        unwanted_pkg_names[pkg_name] = pkg
                
                for pkg_source_name in view_conf["unwanted_source_packages"]:
                    for pkg_name in self._srpm_name_to_rpm_names(pkg_source_name, repo_id):
                        
                        if pkg_name in unwanted_pkg_names:
                            continue

                        pkg = {}
                        pkg["name"] = pkg_name
                        pkg["unwanted_in_view"] = True
                        pkg["unwanted_list_ids"] = []

                        unwanted_pkg_names[pkg_name] = pkg


        ### Step 2: Get packages from the various exclusion lists (unwanted proposal)
        if "unwanted_proposals" in output_lists:
            for unwanted_id in unwanted_ids:
                unwanted_conf = self.configs["unwanteds"][unwanted_id]

                for pkg_name in unwanted_conf["unwanted_packages"]:
                    if pkg_name in unwanted_pkg_names:
                        unwanted_pkg_names[pkg_name]["unwanted_list_ids"].append(unwanted_id)
                        continue
                    
                    pkg = {}
                    pkg["name"] = pkg_name
                    pkg["unwanted_in_view"] = False
                    pkg["unwanted_list_ids"] = [unwanted_id]

                    unwanted_pkg_names[pkg_name] = pkg
            
                for arch in arches:
                    for pkg_name in unwanted_conf["unwanted_arch_packages"][arch]:
                        if pkg_name in unwanted_pkg_names:
                            unwanted_pkg_names[pkg_name]["unwanted_list_ids"].append(unwanted_id)
                            continue
                        
                        pkg = {}
                        pkg["name"] = pkg_name
                        pkg["unwanted_in_view"] = True
                        pkg["unwanted_list_ids"] = []

                        unwanted_pkg_names[pkg_name] = pkg
                
                for pkg_source_name in unwanted_conf["unwanted_source_packages"]:
                    for pkg_name in self._srpm_name_to_rpm_names(pkg_source_name, repo_id):

                        if pkg_name in unwanted_pkg_names:
                            unwanted_pkg_names[pkg_name]["unwanted_list_ids"].append(unwanted_id)
                            continue
                        
                        pkg = {}
                        pkg["name"] = pkg_name
                        pkg["unwanted_in_view"] = False
                        pkg["unwanted_list_ids"] = [unwanted_id]

                        unwanted_pkg_names[pkg_name] = pkg

        #self.cache["view_unwanted_pkgs"][view_conf_id][arch] = unwanted_pkg_names

        return unwanted_pkg_names


    @lru_cache(maxsize = None)
    def view_placeholder_srpms(self, view_conf_id, arch):
        if not arch:
            raise ValueError("arch must be specified, can't be None")

        workload_ids = self.workloads_in_view(view_conf_id, arch)

        placeholder_srpms = {}
        # {
        #    "SRPM_NAME": {
        #        "build_requires": set() 
        #    } 
        # } 

        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]

            for pkg_placeholder_name, pkg_placeholder in workload_conf["package_placeholders"]["srpms"].items():
                # Placeholders can be limited to specific architectures.
                # If that's the case, check if it's available on this arch, otherwise skip it.
                if pkg_placeholder["limit_arches"]:
                    if arch not in pkg_placeholder["limit_arches"]:
                        continue

                srpm_name = pkg_placeholder["name"]

                buildrequires = pkg_placeholder["buildrequires"]

                if srpm_name not in placeholder_srpms:
                    placeholder_srpms[srpm_name] = {}
                    placeholder_srpms[srpm_name]["build_requires"] = set()
                
                placeholder_srpms[srpm_name]["build_requires"].update(buildrequires)
        
        return placeholder_srpms


    @lru_cache(maxsize = None)
    def view_modules(self, view_conf_id, arch, maintainer=None):
        workload_ids = self.workloads_in_view(view_conf_id, arch, maintainer)

        modules = {}

        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]

            required_modules = workload_conf["modules_enable"]

            for module_id in workload["enabled_modules"]:
                if module_id not in modules:
                    modules[module_id] = {}
                    modules[module_id]["id"] = module_id
                    modules[module_id]["q_in"] = set()
                    modules[module_id]["q_required_in"] = set()
                    modules[module_id]["q_dep_in"] = set()
                
                modules[module_id]["q_in"].add(workload_id)

                if module_id in required_modules:
                    modules[module_id]["q_required_in"].add(workload_id)
                else:
                    modules[module_id]["q_dep_in"].add(workload_id)
                

        return modules


    @lru_cache(maxsize = None)
    def view_maintainers(self, view_conf_id, arch):
        workload_ids = self.workloads_in_view(view_conf_id, arch)

        maintainers = set()

        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]
            maintainers.add(workload_conf["maintainer"])

        return maintainers


    @lru_cache(maxsize = None)
    def maintainers(self):

        maintainers = {}

        for workload_id in self.workloads(None, None, None, None, list_all=True):
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]
            maintainer = workload_conf["maintainer"]

            if maintainer not in maintainers:
                maintainers[maintainer] = {}
                maintainers[maintainer]["name"] = maintainer
                maintainers[maintainer]["all_succeeded"] = True
            
            if not workload["succeeded"]:
                maintainers[maintainer]["all_succeeded"] = False

        for env_id in self.envs(None, None, None, list_all=True):
            env = self.data["envs"][env_id]
            env_conf_id = env["env_conf_id"]
            env_conf = self.configs["envs"][env_conf_id]
            maintainer = env_conf["maintainer"]

            if maintainer not in maintainers:
                maintainers[maintainer] = {}
                maintainers[maintainer]["name"] = maintainer
                maintainers[maintainer]["all_succeeded"] = True
            
            if not env["succeeded"]:
                maintainers[maintainer]["all_succeeded"] = False

        return maintainers
    

    @lru_cache(maxsize = None)
    def view_pkg_name_details(self, pkg_name, view_conf_id):
        raise NotImplementedError

    
    @lru_cache(maxsize = None)
    def view_srpm_name_details(self, srpm_name, view_conf_id):
        raise NotImplementedError
    




###############################################################################
### Generating html pages! ####################################################
###############################################################################
#
# def generate_pages(query)
#

def _generate_html_page(template_name, template_data, page_name, settings):
    log("Generating the '{page_name}' page...".format(
        page_name=page_name
    ))

    output = settings["output"]

    template_env = settings["jinja2_template_env"]

    template = template_env.get_template("{template_name}.html".format(
        template_name=template_name
    ))

    if not template_data:
        template_data = {}
    template_data["global_refresh_time_started"] = settings["global_refresh_time_started"]

    page = template.render(**template_data)

    filename = ("{page_name}.html".format(
        page_name=page_name.replace(":", "--")
    ))

    log("  Writing file...  ({filename})".format(
        filename=filename
    ))
    with open(os.path.join(output, filename), "w") as file:
        file.write(page)
    
    log("  Done!")
    log("")


def _generate_workload_pages(query):
    log("Generating workload pages...")

    # Workload overview pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for repo_id in query.workloads(workload_conf_id,None,None,None,output_change="repo_ids"):
            template_data = {
                "query": query,
                "workload_conf_id": workload_conf_id,
                "repo_id": repo_id
            }

            page_name = "workload-overview--{workload_conf_id}--{repo_id}".format(
                workload_conf_id=workload_conf_id,
                repo_id=repo_id
            )
            _generate_html_page("workload_overview", template_data, page_name, query.settings)
    
    # Workload detail pages
    for workload_id in query.workloads(None,None,None,None,list_all=True):
        workload = query.data["workloads"][workload_id]
        
        workload_conf_id = workload["workload_conf_id"]
        workload_conf = query.configs["workloads"][workload_conf_id]

        env_conf_id = workload["env_conf_id"]
        env_conf = query.configs["envs"][env_conf_id]

        repo_id = workload["repo_id"]
        repo = query.configs["repos"][repo_id]


        template_data = {
            "query": query,
            "workload_id": workload_id,
            "workload": workload,
            "workload_conf": workload_conf,
            "env_conf": env_conf,
            "repo": repo
        }

        page_name = "workload--{workload_id}".format(
            workload_id=workload_id
        )
        _generate_html_page("workload", template_data, page_name, query.settings)
        page_name = "workload-dependencies--{workload_id}".format(
            workload_id=workload_id
        )
        _generate_html_page("workload_dependencies", template_data, page_name, query.settings)
    
    # Workload compare arches pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for env_conf_id in query.workloads(workload_conf_id,None,None,None,output_change="env_conf_ids"):
            for repo_id in query.workloads(workload_conf_id,env_conf_id,None,None,output_change="repo_ids"):

                arches = query.workloads(workload_conf_id,env_conf_id,repo_id,None,output_change="arches")

                workload_conf = query.configs["workloads"][workload_conf_id]
                env_conf = query.configs["envs"][env_conf_id]
                repo = query.configs["repos"][repo_id]

                columns = {}
                rows = set()
                for arch in arches:
                    columns[arch] = {}

                    pkgs = query.workload_pkgs(workload_conf_id,env_conf_id,repo_id,arch)
                    for pkg in pkgs:
                        name = pkg["name"]
                        rows.add(name)
                        columns[arch][name] = pkg

                template_data = {
                    "query": query,
                    "workload_conf_id": workload_conf_id,
                    "workload_conf": workload_conf,
                    "env_conf_id": env_conf_id,
                    "env_conf": env_conf,
                    "repo_id": repo_id,
                    "repo": repo,
                    "columns": columns,
                    "rows": rows
                }

                page_name = "workload-cmp-arches--{workload_conf_id}--{env_conf_id}--{repo_id}".format(
                    workload_conf_id=workload_conf_id,
                    env_conf_id=env_conf_id,
                    repo_id=repo_id
                )

                _generate_html_page("workload_cmp_arches", template_data, page_name, query.settings)
    
    # Workload compare envs pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for repo_id in query.workloads(workload_conf_id,None,None,None,output_change="repo_ids"):
            for arch in query.workloads(workload_conf_id,None,repo_id,None,output_change="arches"):

                env_conf_ids = query.workloads(workload_conf_id,None,repo_id,arch,output_change="env_conf_ids")

                workload_conf = query.configs["workloads"][workload_conf_id]
                repo = query.configs["repos"][repo_id]

                columns = {}
                rows = set()
                for env_conf_id in env_conf_ids:
                    columns[env_conf_id] = {}

                    pkgs = query.workload_pkgs(workload_conf_id,env_conf_id,repo_id,arch)
                    for pkg in pkgs:
                        name = pkg["name"]
                        rows.add(name)
                        columns[env_conf_id][name] = pkg

                template_data = {
                    "query": query,
                    "workload_conf_id": workload_conf_id,
                    "workload_conf": workload_conf,
                    "repo_id": repo_id,
                    "repo": repo,
                    "arch": arch,
                    "columns": columns,
                    "rows": rows
                }

                page_name = "workload-cmp-envs--{workload_conf_id}--{repo_id}--{arch}".format(
                    workload_conf_id=workload_conf_id,
                    repo_id=repo_id,
                    arch=arch
                )

                _generate_html_page("workload_cmp_envs", template_data, page_name, query.settings)
    
    log("  Done!")
    log("")


def _generate_env_pages(query):
    log("Generating env pages...")

    for env_conf_id in query.envs(None,None,None,output_change="env_conf_ids"):
        for repo_id in query.envs(env_conf_id,None,None,output_change="repo_ids"):
            template_data = {
                "query": query,
                "env_conf_id": env_conf_id,
                "repo_id": repo_id
            }

            page_name = "env-overview--{env_conf_id}--{repo_id}".format(
                env_conf_id=env_conf_id,
                repo_id=repo_id
            )
            _generate_html_page("env_overview", template_data, page_name, query.settings)
    
    # env detail pages
    for env_id in query.envs(None,None,None,list_all=True):
        env = query.data["envs"][env_id]

        env_conf_id = env["env_conf_id"]
        env_conf = query.configs["envs"][env_conf_id]

        repo_id = env["repo_id"]
        repo = query.configs["repos"][repo_id]

        template_data = {
            "query": query,
            "env_id": env_id,
            "env": env,
            "env_conf": env_conf,
            "repo": repo
        }

        page_name = "env--{env_id}".format(
            env_id=env_id
        )
        _generate_html_page("env", template_data, page_name, query.settings)

        page_name = "env-dependencies--{env_id}".format(
            env_id=env_id
        )
        _generate_html_page("env_dependencies", template_data, page_name, query.settings)
    
    # env compare arches pages
    for env_conf_id in query.envs(None,None,None,output_change="env_conf_ids"):
        for repo_id in query.envs(env_conf_id,None,None,output_change="repo_ids"):

            arches = query.envs(env_conf_id,repo_id,None,output_change="arches")

            env_conf = query.configs["envs"][env_conf_id]
            repo = query.configs["repos"][repo_id]

            columns = {}
            rows = set()
            for arch in arches:
                columns[arch] = {}

                pkgs = query.env_pkgs(env_conf_id,repo_id,arch)
                for pkg in pkgs:
                    name = pkg["name"]
                    rows.add(name)
                    columns[arch][name] = pkg

            template_data = {
                "query": query,
                "env_conf_id": env_conf_id,
                "env_conf": env_conf,
                "repo_id": repo_id,
                "repo": repo,
                "columns": columns,
                "rows": rows
            }

            page_name = "env-cmp-arches--{env_conf_id}--{repo_id}".format(
                env_conf_id=env_conf_id,
                repo_id=repo_id
            )

            _generate_html_page("env_cmp_arches", template_data, page_name, query.settings)

    log("  Done!")
    log("")


def _generate_maintainer_pages(query):
    log("Generating maintainer pages...")

    for maintainer in query.maintainers():
    
        template_data = {
            "query": query,
            "maintainer": maintainer
        }

        # Overview page
        page_name = "maintainer--{maintainer}".format(
            maintainer=maintainer
        )
        _generate_html_page("maintainer_overview", template_data, page_name, query.settings)

        # My Workloads page
        page_name = "maintainer-workloads--{maintainer}".format(
            maintainer=maintainer
        )
        _generate_html_page("maintainer_workloads", template_data, page_name, query.settings)

    log("  Done!")
    log("")


def _generate_config_pages(query):
    log("Generating config pages...")

    for conf_type in ["repos", "envs", "workloads", "labels", "views", "unwanteds"]:
        template_data = {
            "query": query,
            "conf_type": conf_type
        }
        page_name = "configs_{conf_type}".format(
            conf_type=conf_type
        )
        _generate_html_page("configs", template_data, page_name, query.settings)

    # Config repo pages
    for repo_id,repo_conf in query.configs["repos"].items():
        template_data = {
            "query": query,
            "repo_conf": repo_conf
        }
        page_name = "config-repo--{repo_id}".format(
            repo_id=repo_id
        )
        _generate_html_page("config_repo", template_data, page_name, query.settings)
    
    # Config env pages
    for env_conf_id,env_conf in query.configs["envs"].items():
        template_data = {
            "query": query,
            "env_conf": env_conf
        }
        page_name = "config-env--{env_conf_id}".format(
            env_conf_id=env_conf_id
        )
        _generate_html_page("config_env", template_data, page_name, query.settings)

    # Config workload pages
    for workload_conf_id,workload_conf in query.configs["workloads"].items():
        template_data = {
            "query": query,
            "workload_conf": workload_conf
        }
        page_name = "config-workload--{workload_conf_id}".format(
            workload_conf_id=workload_conf_id
        )
        _generate_html_page("config_workload", template_data, page_name, query.settings)

    # Config label pages
    for label_conf_id,label_conf in query.configs["labels"].items():
        template_data = {
            "query": query,
            "label_conf": label_conf
        }
        page_name = "config-label--{label_conf_id}".format(
            label_conf_id=label_conf_id
        )
        _generate_html_page("config_label", template_data, page_name, query.settings)

    # Config view pages
    for view_conf_id,view_conf in query.configs["views"].items():
        template_data = {
            "query": query,
            "view_conf": view_conf
        }
        page_name = "config-view--{view_conf_id}".format(
            view_conf_id=view_conf_id
        )
        _generate_html_page("config_view", template_data, page_name, query.settings)
    
    # Config unwanted pages
    for unwanted_conf_id,unwanted_conf in query.configs["unwanteds"].items():
        template_data = {
            "query": query,
            "unwanted_conf": unwanted_conf
        }
        page_name = "config-unwanted--{unwanted_conf_id}".format(
            unwanted_conf_id=unwanted_conf_id
        )
        _generate_html_page("config_unwanted", template_data, page_name, query.settings)

    log("  Done!")
    log("")


def _generate_repo_pages(query):
    log("Generating repo pages...")

    for repo_id, repo in query.configs["repos"].items():
        for arch in repo["source"]["architectures"]:
            template_data = {
                "query": query,
                "repo": repo,
                "arch": arch
            }
            page_name = "repo--{repo_id}--{arch}".format(
                repo_id=repo_id,
                arch=arch
            )
            _generate_html_page("repo", template_data, page_name, query.settings)


    log("  Done!")
    log("")


def _generate_view_pages(query):
    log("Generating view pages... (the new function)")

    for view_conf_id, view_conf in query.configs["views"].items():

        # Common data
        view_all_arches = query.data["views_all_arches"][view_conf_id]
        template_data = {
            "query": query,
            "view_conf": view_conf,
            "view_all_arches": view_all_arches
        }

        # Generate the overview page
        page_name = "view--{view_conf_id}".format(
            view_conf_id=view_conf_id
        )
        _generate_html_page("view_overview", template_data, page_name, query.settings)

        # Generate the packages page
        page_name = "view-packages--{view_conf_id}".format(
            view_conf_id=view_conf_id
        )
        _generate_html_page("view_packages", template_data, page_name, query.settings)

        # Generate the source packages page
        page_name = "view-sources--{view_conf_id}".format(
            view_conf_id=view_conf_id
        )
        _generate_html_page("view_sources", template_data, page_name, query.settings)

        # Generate the modules page
        page_name = "view-modules--{view_conf_id}".format(
            view_conf_id=view_conf_id
        )
        _generate_html_page("view_modules", template_data, page_name, query.settings)

        # Generate the unwanted packages page
        page_name = "view-unwanted--{view_conf_id}".format(
            view_conf_id=view_conf_id
        )
        _generate_html_page("view_unwanted", template_data, page_name, query.settings)

        # Generate the workloads page
        page_name = "view-workloads--{view_conf_id}".format(
            view_conf_id=view_conf_id
        )
        _generate_html_page("view_workloads", template_data, page_name, query.settings)

        # Generate the errors page
        page_name = "view-errors--{view_conf_id}".format(
            view_conf_id=view_conf_id
        )
        _generate_html_page("view_errors", template_data, page_name, query.settings)




        # Generate the arch lists
        for arch in view_conf["architectures"]:

            view_id = "{view_conf_id}:{arch}".format(
                view_conf_id=view_conf_id,
                arch=arch
            )

            view = query.data["views"][view_id]

            template_data = {
                "query": query,
                "view_conf": view_conf,
                "view": view,
                "arch": arch,
            }
            page_name = "view--{view_conf_id}--{arch}".format(
                view_conf_id=view_conf_id,
                arch=arch
            )
            #_generate_html_page("view_packages", template_data, page_name, query.settings)
            # ...

        
        # Generate the RPM pages
        for pkg_name, pkg in view_all_arches["pkgs_by_name"].items():

            template_data = {
                "query": query,
                "view_conf": view_conf,
                "view_all_arches": view_all_arches,
                "pkg": pkg,
            }
            page_name = "view-rpm--{view_conf_id}--{pkg_name}".format(
                view_conf_id=view_conf_id,
                pkg_name=pkg_name
            )
            _generate_html_page("view_rpm", template_data, page_name, query.settings)
        

        # Generate the SRPM pages
        for srpm_name, srpm in view_all_arches["source_pkgs_by_name"].items():

            template_data = {
                "query": query,
                "view_conf": view_conf,
                "view_all_arches": view_all_arches,
                "srpm": srpm,
            }
            page_name = "view-srpm--{view_conf_id}--{srpm_name}".format(
                view_conf_id=view_conf_id,
                srpm_name=srpm_name
            )
            _generate_html_page("view_srpm", template_data, page_name, query.settings)



def _dump_all_data(query):
    log("Dumping all data...")

    data = {}
    data["data"] = query.data
    data["configs"] = query.configs
    data["settings"] = query.settings
    data["computed_data"] = query.computed_data

    file_name = "data.json"
    file_path = os.path.join(query.settings["output"], file_name)
    dump_data(file_path, data)

    log("  Done!")
    log("")


def generate_pages(query):

    log("")
    log("###############################################################################")
    log("### Generating html pages! ####################################################")
    log("###############################################################################")
    log("")

    # Create the jinja2 thingy
    template_loader = jinja2.FileSystemLoader(searchpath="./templates/")
    template_env = jinja2.Environment(
        loader=template_loader,
        trim_blocks=True,
        lstrip_blocks=True
    )
    query.settings["jinja2_template_env"] = template_env

    # Copy static files
    log("Copying static files...")
    src_static_dir = os.path.join("templates", "_static")
    output_static_dir = os.path.join(query.settings["output"])
    subprocess.run(["cp", "-R", src_static_dir, output_static_dir])
    log("  Done!")
    log("")

    # Generate the landing page
    _generate_html_page("homepage", None, "index", query.settings)

    # Generate the main menu page
    _generate_html_page("results", None, "results", query.settings)

    # Generate config pages
    _generate_config_pages(query)

    # Generate the top-level results pages
    template_data = {
        "query": query
    }
    _generate_html_page("repos", template_data, "repos", query.settings)
    _generate_html_page("envs", template_data, "envs", query.settings)
    _generate_html_page("workloads", template_data, "workloads", query.settings)
    _generate_html_page("labels", template_data, "labels", query.settings)
    _generate_html_page("views", template_data, "views", query.settings)
    _generate_html_page("maintainers", template_data, "maintainers", query.settings)
    
    # Generate repo pages
    _generate_repo_pages(query)

    # Generate maintainer pages
    _generate_maintainer_pages(query)

    # Generate env_overview pages
    _generate_env_pages(query)

    # Generate workload_overview pages
    _generate_workload_pages(query)

    # Generate view pages
    _generate_view_pages(query)

    # Dump all data
    # The data is now pretty huge and not really needed anyway
    #if not query.settings["use_cache"]:
    #    _dump_all_data(query)

    # Generate the errors page
    template_data = {
        "query": query
    }
    _generate_html_page("errors", template_data, "errors", query.settings)





###############################################################################
### Generating data files! ####################################################
###############################################################################
#
# def generate_data_files(query)
#

def _generate_json_file(data, page_name, settings):
    log("Generating the '{page_name}' JSON file...".format(
        page_name=page_name
    ))

    output = settings["output"]

    filename = ("{page_name}.json".format(
        page_name=page_name.replace(":", "--")
    ))
    log("  Writing file...  ({filename})".format(
        filename=filename
    ))
    dump_data(os.path.join(output, filename), data)
    
    log("  Done!")
    log("")


def _generate_txt_file(data_list, file_name, settings):

    file_contents = "\n".join(data_list)

    filename = ("{file_name}.txt".format(
        file_name=file_name.replace(":", "--")
    ))

    output = settings["output"]

    log("  Writing file...  ({filename})".format(
        filename=filename
    ))
    with open(os.path.join(output, filename), "w") as file:
        file.write(file_contents)


def _generate_view_lists(query):
    log("Generating view lists...")

    for view_conf_id, view_conf in query.configs["views"].items():

        # all      RPM    NEVRAs      view-all-binary-package-list
        # all      RPM    NEVRs       view-all-binary-package-nevr-list
        # all      RPM    Names       view-all-binary-package-name-list
        # 
        # all      SRPM   NEVRs       view-all-source-package-list
        # all      SRPM   Names       view-all-source-package-name-list
        # 
        # 
        # runtime  RPM    NEVRAs      view-binary-package-list
        # runtime  RPM    NEVRs       view-binary-package-nevr-list
        # runtime  RPM    Names       view-binary-package-name-list
        # 
        # runtime  SRPM   NEVRs       view-source-package-list
        # runtime  SRPM   Names       view-source-package-name-list
        # 
        # 
        # build    RPM    NEVRAs      view-buildroot-package-list
        # build    RPM    NEVRs       view-buildroot-package-nevr-list
        # build    RPM    Names       view-buildroot-package-name-list
        # 
        # build    SRPM   NEVRs       view-buildroot-package-nevr-list
        # build    SRPM   Names       view-buildroot-source-package-name-list


        all_arches_lists = {}

        for arch in view_conf["architectures"]:

            lists = {}

            view_id = "{view_conf_id}:{arch}".format(
                view_conf_id=view_conf_id,
                arch=arch
            )

            view = query.data["views"][view_id]

            # all      RPM    NEVRAs      view-all-binary-package-list
            # all      RPM    NEVRs       view-all-binary-package-nevr-list
            # all      RPM    Names       view-all-binary-package-name-list
            lists["view-all-binary-package-list"] = set()
            lists["view-all-binary-package-nevr-list"] = set()
            lists["view-all-binary-package-name-list"] = set()

            # all      SRPM   NEVRs       view-all-source-package-list
            # all      SRPM   Names       view-all-source-package-name-list
            lists["view-all-source-package-list"] = set()
            lists["view-all-source-package-name-list"] = set()

            # runtime  RPM    NEVRAs      view-binary-package-list
            # runtime  RPM    NEVRs       view-binary-package-nevr-list
            # runtime  RPM    Names       view-binary-package-name-list
            lists["view-binary-package-list"] = set()
            lists["view-binary-package-nevr-list"] = set()
            lists["view-binary-package-name-list"] = set()

            # runtime  SRPM   NEVRs       view-source-package-list
            # runtime  SRPM   Names       view-source-package-name-list
            lists["view-source-package-list"] = set()
            lists["view-source-package-name-list"] = set()

            # build    RPM    NEVRAs      view-buildroot-package-list
            # build    RPM    NEVRs       view-buildroot-package-nevr-list
            # build    RPM    Names       view-buildroot-package-name-list
            lists["view-buildroot-package-list"] = set()
            lists["view-buildroot-package-nevr-list"] = set()
            lists["view-buildroot-package-name-list"] = set()

            # build    SRPM   NEVRs       view-buildroot-source-package-list
            # build    SRPM   Names       view-buildroot-source-package-name-list
            lists["view-buildroot-source-package-list"] = set()
            lists["view-buildroot-source-package-name-list"] = set()

            for pkg_id, pkg in view["pkgs"].items():
                lists["view-all-binary-package-list"].add(pkg_id)
                lists["view-all-binary-package-nevr-list"].add(pkg["nevr"])
                lists["view-all-binary-package-name-list"].add(pkg["name"])

                srpm_id = pkg["sourcerpm"].rsplit(".src.rpm")[0]

                lists["view-all-source-package-list"].add(srpm_id)
                lists["view-all-source-package-name-list"].add(pkg["source_name"])

                if pkg["in_workload_ids_all"]:
                    lists["view-binary-package-list"].add(pkg_id)
                    lists["view-binary-package-nevr-list"].add(pkg["nevr"])
                    lists["view-binary-package-name-list"].add(pkg["name"])

                    lists["view-source-package-list"].add(srpm_id)
                    lists["view-source-package-name-list"].add(pkg["source_name"])

                else:
                    lists["view-buildroot-package-list"].add(pkg_id)
                    lists["view-buildroot-package-nevr-list"].add(pkg["nevr"])
                    lists["view-buildroot-package-name-list"].add(pkg["name"])

                    lists["view-buildroot-source-package-list"].add(srpm_id)
                    lists["view-buildroot-source-package-name-list"].add(pkg["source_name"])
            
            
            for list_name, list_content in lists.items():

                # Generate the arch-specific lists
                file_name = "{list_name}--{view_conf_id}--{arch}".format(
                    list_name=list_name,
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_txt_file(sorted(list(list_content)), file_name, query.settings)

                # Populate the all-arch lists
                if list_name not in all_arches_lists:
                    all_arches_lists[list_name] = set()
                all_arches_lists[list_name].update(list_content)
        
        
        for list_name, list_content in all_arches_lists.items():

            # Generate the all-arch lists
            file_name = "{list_name}--{view_conf_id}".format(
                list_name=list_name,
                view_conf_id=view_conf_id
            )
            _generate_txt_file(sorted(list(list_content)), file_name, query.settings)
    
    log("Done!")
    log("")


def _generate_env_json_files(query):

    log("Generating JSON files for environments...")
    
    # == envs
    log("")
    log("Envs:")
    for env_conf_id, env_conf in query.configs["envs"].items():

        # === Config

        log("")
        log("  Config for: {}".format(env_conf_id))

        # Where to save
        data_name = "env-conf--{env_conf_id_slug}".format(
            env_conf_id_slug = query.url_slug_id(env_conf_id)
        )

        # What to save
        output_data = {}
        output_data["id"] = env_conf_id
        output_data["type"] = "env_conf"
        output_data["data"] = query.configs["envs"][env_conf_id]

        # And save it
        _generate_json_file(output_data, data_name, query.settings)


        # === Results

        for env_id in query.envs(env_conf_id, None, None, list_all=True):
            env = query.data["envs"][env_id]

            log("  Results: {}".format(env_id))

            # Where to save
            data_name = "env--{env_id_slug}".format(
                env_id_slug = query.url_slug_id(env_id)
            )

            # What to save
            output_data = {}
            output_data["id"] = env_id
            output_data["type"] = "env"
            output_data["data"] = query.data["envs"][env_id]
            output_data["pkg_query"] = query.env_pkgs_id(env_id)

            # And save it
            _generate_json_file(output_data, data_name, query.settings)

    log("  Done!")
    log("")


def _generate_workload_json_files(query):

    log("Generating JSON files for workloads...")

    # == Workloads
    log("")
    log("Workloads:")
    for workload_conf_id, workload_conf in query.configs["workloads"].items():

        # === Config

        log("")
        log("  Config for: {}".format(workload_conf_id))

        # Where to save
        data_name = "workload-conf--{workload_conf_id_slug}".format(
            workload_conf_id_slug = query.url_slug_id(workload_conf_id)
        )

        # What to save
        output_data = {}
        output_data["id"] = workload_conf_id
        output_data["type"] = "workload_conf"
        output_data["data"] = query.configs["workloads"][workload_conf_id]

        # And save it
        _generate_json_file(output_data, data_name, query.settings)


        # === Results

        for workload_id in query.workloads(workload_conf_id, None, None, None, list_all=True):
            workload = query.data["workloads"][workload_id]

            log("  Results: {}".format(workload_id))

            # Where to save
            data_name = "workload--{workload_id_slug}".format(
                workload_id_slug = query.url_slug_id(workload_id)
            )

            # What to save
            output_data = {}
            output_data["id"] = workload_id
            output_data["type"] = "workload"
            output_data["data"] = query.data["workloads"][workload_id]
            output_data["pkg_query"] = query.workload_pkgs_id(workload_id)

            # And save it
            _generate_json_file(output_data, data_name, query.settings)

    log("  Done!")
    log("")


def _generate_view_json_files(query):

    log("Generating JSON files for views...")
    for view_conf_id, view_conf in query.configs["views"].items():
        view_all_arches = query.data["views_all_arches"][view_conf_id]

        # Where to save
        data_name = "view-packages--{view_id_slug}".format(
            view_id_slug = query.url_slug_id(view_conf_id)
        )

        # What to save
        output_data = {}
        output_data["id"] = view_conf_id
        output_data["pkgs"] = {}

        keys_to_save = [
            "name",
            "source_name",
            "arches",
            "placeholder",
            "hard_dependency_of_pkg_nevrs",
            "weak_dependency_of_pkg_nevrs",
            "level_number"
        ]

        for pkg_id, pkg in view_all_arches["pkgs_by_nevr"].items():
            output_data["pkgs"][pkg_id] = {}

            for key in keys_to_save:
                output_data["pkgs"][pkg_id][key] = pkg[key]

        # And save it
        _generate_json_file(output_data, data_name, query.settings)


    log("  Done!")
    log("")


def _generate_maintainers_json_file(query):

    log("Generating the maintainers json file...")

    maintainer_data = query.maintainers()
    _generate_json_file(maintainer_data, "maintainers", query.settings)

    log("  Done!")
    log("")


def generate_data_files(query):

    log("")
    log("###############################################################################")
    log("### Generating data files! ####################################################")
    log("###############################################################################")
    log("")

    # Generate the package lists for views
    _generate_view_lists(query)

    # Generate the JSON files for envs 
    _generate_env_json_files(query)

    # Generate the JSON files for workloads 
    _generate_workload_json_files(query)

    # Generate the JSON files for views
    _generate_view_json_files(query)

    # Generate data for the top-level results pages
    _generate_maintainers_json_file(query)





###############################################################################
### Historic Data #############################################################
###############################################################################
#
# def generate_historic_data(query)
#


def _save_current_historic_data(query):
    # This is the historic data for charts
    # Package lists are above 

    log("Generating current historic data...")

    # Where to save it
    year = datetime.datetime.now().strftime("%Y")
    week = datetime.datetime.now().strftime("%W")
    filename = "historic_data-{year}-week_{week}.json".format(
        year=year,
        week=week
    )
    output_dir = os.path.join(query.settings["output"], "history")
    file_path = os.path.join(output_dir, filename)

    # What to save there
    history_data = {}
    history_data["date"] = str(datetime.datetime.now().strftime("%Y-%m-%d"))
    history_data["workloads"] = {}
    history_data["envs"] = {}
    history_data["repos"] = {}
    history_data["views"] = {}

    # Workloads
    for workload_id in query.workloads(None,None,None,None,list_all=True):
        workload = query.data["workloads"][workload_id]

        if not workload["succeeded"]:
            continue

        workload_history = {}
        workload_history["size"] = query.workload_size_id(workload_id)
        workload_history["pkg_count"] = len(query.workload_pkgs_id(workload_id))

        history_data["workloads"][workload_id] = workload_history
    
    # Environments
    for env_id in query.envs(None,None,None,list_all=True):
        env = query.data["envs"][env_id]

        if not env["succeeded"]:
            continue

        env_history = {}
        env_history["size"] = query.env_size_id(env_id)
        env_history["pkg_count"] = len(query.env_pkgs_id(env_id))

        history_data["envs"][env_id] = env_history

    # Repositories
    for repo_id in query.configs["repos"].keys():
        history_data["repos"][repo_id] = {}

        for arch, pkgs in query.data["pkgs"][repo_id].items():

            repo_history = {}
            repo_history["pkg_count"] = len(pkgs)
            
            history_data["repos"][repo_id][arch] = repo_history
    
    # Views (new)
    for view_conf_id, view_conf in query.configs["views"].items():
        view_all_arches = query.data["views_all_arches"][view_conf_id]

        history_data["views"][view_conf_id] = {}

        history_data["views"][view_conf_id]["srpm_count_env"] = view_all_arches["numbers"]["srpms"]["env"]
        history_data["views"][view_conf_id]["srpm_count_req"] = view_all_arches["numbers"]["srpms"]["req"]
        history_data["views"][view_conf_id]["srpm_count_dep"] = view_all_arches["numbers"]["srpms"]["dep"]

        if view_all_arches["has_buildroot"]:
            history_data["views"][view_conf_id]["srpm_count_build_base"] = view_all_arches["numbers"]["srpms"]["build_base"]
            history_data["views"][view_conf_id]["srpm_count_build_level_1"] = view_all_arches["numbers"]["srpms"]["build_level_1"]
            history_data["views"][view_conf_id]["srpm_count_build_level_2_plus"] = view_all_arches["numbers"]["srpms"]["build_level_2_plus"]

    # And save it
    log("  Saving in: {file_path}".format(
        file_path=file_path
    ))
    dump_data(file_path, history_data)

    log("  Done!")
    log("")


def _read_historic_data(query):
    log("Reading historic data...")

    directory = os.path.join(query.settings["output"], "history")

    # Do some basic validation of the filename
    all_filenames = os.listdir(directory)
    valid_filenames = []
    for filename in all_filenames:
        if bool(re.match("historic_data-....-week_...json", filename)):
            valid_filenames.append(filename)
    valid_filenames.sort()

    # Get the data
    historic_data = {}

    for filename in valid_filenames:
        with open(os.path.join(directory, filename), "r") as file:
            try:
                document = json.load(file)

                date = datetime.datetime.strptime(document["date"],"%Y-%m-%d")
                year = date.strftime("%Y")
                week = date.strftime("%W")
                key = "{year}-week_{week}".format(
                    year=year,
                    week=week
                )
            except (KeyError, ValueError):
                err_log("Invalid file in historic data: {filename}. Ignoring.".format(
                    filename=filename
                ))
                continue

            historic_data[key] = document

    return historic_data

    log("  Done!")
    log("")


def _generate_chartjs_data(historic_data, query):

    # Data for workload pages
    for workload_id in query.workloads(None, None, None, None, list_all=True):

        entry_data = {}

        # First, get the dates as chart labels
        entry_data["labels"] = []
        for _,entry in historic_data.items():
            date = entry["date"]
            entry_data["labels"].append(date)

        # Second, get the actual data for everything that's needed
        entry_data["datasets"] = []

        workload = query.data["workloads"][workload_id]
        workload_conf_id = workload["workload_conf_id"]
        workload_conf = query.configs["workloads"][workload_conf_id]

        dataset = {}
        dataset["data"] = []
        dataset["label"] = workload_conf["name"]
        dataset["fill"] = "false"

        for _,entry in historic_data.items():
            try:
                size = entry["workloads"][workload_id]["size"]

                # The chart needs the size in MB, but just as a number
                size_mb = "{0:.1f}".format(size/1024/1024)
                dataset["data"].append(size_mb)
            except KeyError:
                dataset["data"].append("null")

        entry_data["datasets"].append(dataset)

        entry_name = "chartjs-data--workload--{workload_id}".format(
            workload_id=workload_id
        )
        _generate_json_file(entry_data, entry_name, query.settings)
    
    # Data for workload overview pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for repo_id in query.workloads(workload_conf_id,None,None,None,output_change="repo_ids"):

            entry_data = {}

            # First, get the dates as chart labels
            entry_data["labels"] = []
            for _,entry in historic_data.items():
                date = entry["date"]
                entry_data["labels"].append(date)

            # Second, get the actual data for everything that's needed
            entry_data["datasets"] = []

            for workload_id in query.workloads(workload_conf_id, None, repo_id, None, list_all=True):

                workload = query.data["workloads"][workload_id]
                env_conf_id = workload["env_conf_id"]
                env_conf = query.configs["envs"][env_conf_id]

                dataset = {}
                dataset["data"] = []
                dataset["label"] = "in {name} {arch}".format(
                    name=env_conf["name"],
                    arch=workload["arch"]
                )
                dataset["fill"] = "false"


                for _,entry in historic_data.items():
                    try:
                        size = entry["workloads"][workload_id]["size"]

                        # The chart needs the size in MB, but just as a number
                        size_mb = "{0:.1f}".format(size/1024/1024)
                        dataset["data"].append(size_mb)
                    except KeyError:
                        dataset["data"].append("null")

                entry_data["datasets"].append(dataset)

            entry_name = "chartjs-data--workload-overview--{workload_conf_id}--{repo_id}".format(
                workload_conf_id=workload_conf_id,
                repo_id=repo_id
            )
            _generate_json_file(entry_data, entry_name, query.settings)
    
    # Data for workload cmp arches pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for env_conf_id in query.workloads(workload_conf_id,None,None,None,output_change="env_conf_ids"):
            for repo_id in query.workloads(workload_conf_id,env_conf_id,None,None,output_change="repo_ids"):

                workload_conf = query.configs["workloads"][workload_conf_id]
                env_conf = query.configs["envs"][env_conf_id]
                repo = query.configs["repos"][repo_id]

                entry_data = {}

                # First, get the dates as chart labels
                entry_data["labels"] = []
                for _,entry in historic_data.items():
                    date = entry["date"]
                    entry_data["labels"].append(date)

                # Second, get the actual data for everything that's needed
                entry_data["datasets"] = []

                for workload_id in query.workloads(workload_conf_id,env_conf_id,repo_id,None,list_all=True):

                    workload = query.data["workloads"][workload_id]
                    env_conf_id = workload["env_conf_id"]
                    env_conf = query.configs["envs"][env_conf_id]

                    dataset = {}
                    dataset["data"] = []
                    dataset["label"] = "{arch}".format(
                        arch=workload["arch"]
                    )
                    dataset["fill"] = "false"

                    for _,entry in historic_data.items():
                        try:
                            size = entry["workloads"][workload_id]["size"]

                            # The chart needs the size in MB, but just as a number
                            size_mb = "{0:.1f}".format(size/1024/1024)
                            dataset["data"].append(size_mb)
                        except KeyError:
                            dataset["data"].append("null")

                    entry_data["datasets"].append(dataset)

                entry_name = "chartjs-data--workload-cmp-arches--{workload_conf_id}--{env_conf_id}--{repo_id}".format(
                    workload_conf_id=workload_conf_id,
                    env_conf_id=env_conf_id,
                    repo_id=repo_id
                )
                _generate_json_file(entry_data, entry_name, query.settings)
    
    # Data for workload cmp envs pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for repo_id in query.workloads(workload_conf_id,None,None,None,output_change="repo_ids"):
            for arch in query.workloads(workload_conf_id,None,repo_id,None,output_change="arches"):

                workload_conf = query.configs["workloads"][workload_conf_id]
                env_conf = query.configs["envs"][env_conf_id]
                repo = query.configs["repos"][repo_id]

                entry_data = {}

                # First, get the dates as chart labels
                entry_data["labels"] = []
                for _,entry in historic_data.items():
                    date = entry["date"]
                    entry_data["labels"].append(date)

                # Second, get the actual data for everything that's needed
                entry_data["datasets"] = []

                for workload_id in query.workloads(workload_conf_id,None,repo_id,arch,list_all=True):

                    workload = query.data["workloads"][workload_id]
                    repo = query.configs["repos"][repo_id]

                    dataset = {}
                    dataset["data"] = []
                    dataset["label"] = "{repo} {arch}".format(
                        repo=repo["name"],
                        arch=workload["arch"]
                    )
                    dataset["fill"] = "false"

                    for _,entry in historic_data.items():
                        try:
                            size = entry["workloads"][workload_id]["size"]

                            # The chart needs the size in MB, but just as a number
                            size_mb = "{0:.1f}".format(size/1024/1024)
                            dataset["data"].append(size_mb)
                        except KeyError:
                            dataset["data"].append("null")

                    entry_data["datasets"].append(dataset)

                entry_name = "chartjs-data--workload-cmp-envs--{workload_conf_id}--{repo_id}--{arch}".format(
                    workload_conf_id=workload_conf_id,
                    repo_id=repo_id,
                    arch=arch
                )
                _generate_json_file(entry_data, entry_name, query.settings)
    
    # Data for env pages
    for env_id in query.envs(None, None, None, list_all=True):

        entry_data = {}

        # First, get the dates as chart labels
        entry_data["labels"] = []
        for _,entry in historic_data.items():
            date = entry["date"]
            entry_data["labels"].append(date)

        # Second, get the actual data for everything that's needed
        entry_data["datasets"] = []

        env = query.data["envs"][env_id]
        env_conf_id = env["env_conf_id"]
        env_conf = query.configs["envs"][env_conf_id]

        dataset = {}
        dataset["data"] = []
        dataset["label"] = env_conf["name"]
        dataset["fill"] = "false"


        for _,entry in historic_data.items():
            try:
                size = entry["envs"][env_id]["size"]

                # The chart needs the size in MB, but just as a number
                size_mb = "{0:.1f}".format(size/1024/1024)
                dataset["data"].append(size_mb)
            except KeyError:
                dataset["data"].append("null")

        entry_data["datasets"].append(dataset)

        entry_name = "chartjs-data--env--{env_id}".format(
            env_id=env_id
        )
        _generate_json_file(entry_data, entry_name, query.settings)
    
    # Data for env overview pages
    for env_conf_id in query.envs(None,None,None,output_change="env_conf_ids"):
        for repo_id in query.envs(env_conf_id,None,None,output_change="repo_ids"):

            entry_data = {}

            # First, get the dates as chart labels
            entry_data["labels"] = []
            for _,entry in historic_data.items():
                date = entry["date"]
                entry_data["labels"].append(date)

            # Second, get the actual data for everything that's needed
            entry_data["datasets"] = []

            for env_id in query.envs(env_conf_id, repo_id, None, list_all=True):

                env = query.data["envs"][env_id]
                env_conf_id = env["env_conf_id"]
                env_conf = query.configs["envs"][env_conf_id]

                dataset = {}
                dataset["data"] = []
                dataset["label"] = "in {name} {arch}".format(
                    name=env_conf["name"],
                    arch=env["arch"]
                )
                dataset["fill"] = "false"


                for _,entry in historic_data.items():
                    try:
                        size = entry["envs"][env_id]["size"]

                        # The chart needs the size in MB, but just as a number
                        size_mb = "{0:.1f}".format(size/1024/1024)
                        dataset["data"].append(size_mb)
                    except KeyError:
                        dataset["data"].append("null")

                entry_data["datasets"].append(dataset)

            entry_name = "chartjs-data--env-overview--{env_conf_id}--{repo_id}".format(
                env_conf_id=env_conf_id,
                repo_id=repo_id
            )
            _generate_json_file(entry_data, entry_name, query.settings)
    
    # Data for env cmp arches pages
    for env_conf_id in query.envs(None,None,None,output_change="env_conf_ids"):
        for repo_id in query.envs(env_conf_id,None,None,output_change="repo_ids"):

            env_conf = query.configs["envs"][env_conf_id]
            env_conf = query.configs["envs"][env_conf_id]
            repo = query.configs["repos"][repo_id]

            entry_data = {}

            # First, get the dates as chart labels
            entry_data["labels"] = []
            for _,entry in historic_data.items():
                date = entry["date"]
                entry_data["labels"].append(date)

            # Second, get the actual data for everything that's needed
            entry_data["datasets"] = []

            for env_id in query.envs(env_conf_id,repo_id,None,list_all=True):

                env = query.data["envs"][env_id]

                dataset = {}
                dataset["data"] = []
                dataset["label"] = "{arch}".format(
                    arch=env["arch"]
                )
                dataset["fill"] = "false"

                for _,entry in historic_data.items():
                    try:
                        size = entry["envs"][env_id]["size"]

                        # The chart needs the size in MB, but just as a number
                        size_mb = "{0:.1f}".format(size/1024/1024)
                        dataset["data"].append(size_mb)
                    except KeyError:
                        dataset["data"].append("null")

                entry_data["datasets"].append(dataset)

            entry_name = "chartjs-data--env-cmp-arches--{env_conf_id}--{repo_id}".format(
                env_conf_id=env_conf_id,
                repo_id=repo_id
            )
            _generate_json_file(entry_data, entry_name, query.settings)
    
    # Data for view pages 
    for view_conf_id in query.configs["views"].keys():
        view_all_arches = query.data["views_all_arches"][view_conf_id]

        entry_data = {}

        # First, get the dates as chart labels
        entry_data["labels"] = []

        for _,entry in historic_data.items():
            date = entry["date"]
            entry_data["labels"].append(date)

        # Second, get the actual data for everything that's needed
        entry_data["datasets"] = []

        if view_all_arches["has_buildroot"]:
            dataset_names = [
                "env",
                "req",
                "dep",
                "build_base",
                "build_level_1",
                "build_level_2_plus"
            ]
        else:
            dataset_names = [
                "env",
                "req",
                "dep"
            ]

        dataset_metadata = {
            "env": {
                "name": "Environment",
                "color": "#ffc107"
            },
            "req": {
                "name": "Required",
                "color": "#28a745"
            },
            "dep": {
                "name": "Dependency",
                "color": "#6c757d"
            },
            "build_base": {
                "name": "Base Buildroot",
                "color": "#a39e87"
            },
            "build_level_1": {
                "name": "Buildroot level 1",
                "color": "#999"
            },
            "build_level_2_plus": {
                "name": "Buildroot levels 2+",
                "color": "#bbb"
            },
        }

        for dataset_name in dataset_names:
            dataset_key = "srpm_count_{}".format(dataset_name)

            dataset = {}
            dataset["data"] = []
            dataset["label"] = dataset_metadata[dataset_name]["name"]
            dataset["backgroundColor"] = dataset_metadata[dataset_name]["color"]

            loop_index = 0
            for _,entry in historic_data.items():
                try:
                    srpm_count = entry["views"][view_conf_id][dataset_key]

                    # It's a stack chart, so I need to show the numbers on top of each other
                    if dataset_name == "env":
                        srpm_count_compound = srpm_count
                    else:
                        srpm_count_compound = entry_data["datasets"][-1]["data"][loop_index] + srpm_count

                    dataset["data"].append(srpm_count_compound)
                except (KeyError, IndexError):
                    dataset["data"].append("null")

                loop_index += 1

            entry_data["datasets"].append(dataset)

        entry_name = "chartjs-data--view--{view_conf_id}".format(
            view_conf_id=view_conf_id
        )
        _generate_json_file(entry_data, entry_name, query.settings)


def generate_historic_data(query):
    log("")
    log("###############################################################################")
    log("### Historic Data #############################################################")
    log("###############################################################################")
    log("")

    # Step 1: Save current data
    _save_current_historic_data(query)

    # Step 2: Read historic data
    historic_data = _read_historic_data(query)

    # Step 3: Generate Chart.js data
    _generate_chartjs_data(historic_data, query)

    log("Done!")
    log("")



###############################################################################
### Main ######################################################################
###############################################################################


def main():

    # -------------------------------------------------
    # Stage 1: Data collection and analysis using DNF
    # -------------------------------------------------

    # measuring time of execution
    time_started = datetime_now_string()

    settings = load_settings()
    
    settings["global_refresh_time_started"] = datetime.datetime.now().strftime("%-d %B %Y %H:%M UTC")



    if settings["use_cache"]:
        configs = load_data("cache_configs.json")
        data = load_data("cache_data.json")
    else:
        configs = get_configs(settings)
        analyzer = Analyzer(configs, settings)
        data = analyzer.analyze_things()

        if settings["dev_buildroot"]:
            dump_data("cache_configs.json", configs)
            dump_data("cache_data.json", data)

    

    # measuring time of execution
    time_analysis_time = datetime_now_string()


    # -------------------------------------------------
    # Stage 2: Generating pages and data outputs
    # -------------------------------------------------
    
    query = Query(data, configs, settings)

    generate_pages(query)
    generate_data_files(query)
    generate_historic_data(query)


    # -------------------------------------------------
    # Done! Printing final summary
    # -------------------------------------------------

    # measuring time of execution
    time_ended = datetime_now_string()

    log("")
    log("=============================")
    log("Feedback Pipeline build done!")
    log("=============================")
    log("")
    log("  Started:       {}".format(time_started))
    log("  Analysis done: {}".format(time_analysis_time))
    log("  Finished:      {}".format(time_ended))
    log("")



if __name__ == "__main__":
    main()
