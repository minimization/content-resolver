#!/usr/bin/python3

import argparse, yaml, tempfile, os, subprocess, json, jinja2, datetime, copy, re, dnf, pprint
import rpm_showme as showme


# Features of this new release
# - multiarch from the ground up!
# - more resilient
# - better internal data structure
# - user-defined views



###############################################################################
### Some initial stuff ########################################################
###############################################################################

# Error in global settings for Feedback Pipeline
# Settings to be implemented, now hardcoded below
class SettingsError(Exception):
    pass

# Error in user-provided configs
class ConfigError(Exception):
    pass

# Error in downloading repodata
class RepoDownloadError(Exception):
    pass


def log(msg):
    print(msg)

def err_log(msg):
    print("ERROR LOG:  {}".format(msg))

# FIXME: This is hardcorded, and it shouldn't be!
def load_settings():
    settings = {}
    #settings["allowed_arches"] = ["armv7hl","aarch64","i686","ppc64le","s390x","x86_64"]
    # FIXME desabling i686
    settings["allowed_arches"] = ["armv7hl","aarch64","ppc64le","s390x","x86_64"]
    return(settings)




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

        # Only Fedora repos supported at this time.
        # Fedora release.
        config["source"]["fedora_release"] = str(document["data"]["source"]["fedora_release"])

        # List of architectures
        config["source"]["architectures"] = []
        for arch_raw in document["data"]["source"]["architectures"]:
            arch = str(arch_raw)
            if arch not in settings["allowed_arches"]:
                err_log("Warning: {file}.yaml lists an invalid architecture: {arch}. Ignoring.".format(
                    file=document_id,
                    arch=arch))
                continue
            config["source"]["architectures"].append(str(arch))
    except KeyError:
        raise ConfigError("Error: {file} is invalid.".format(file=yml_file))
    
    # Step 2: Optional fields
    # none here

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
        raise ConfigError("Error: {file} is invalid.".format(file=yml_file))

    # Step 2: Optional fields

    # Architecture-specific packages.
    config["arch_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["arch_packages"][arch] = []
    if "arch_packages" in document["data"]:
        for arch, pkgs in document["data"]["arch_packages"].items():
            if arch not in settings["allowed_arches"]:
                err_log("Warning: {file}.yaml lists an invalid architecture: {arch}. Ignoring.".format(
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

        # Packages defining this workload.
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
        raise ConfigError("Error: {file} is invalid.".format(file=yml_file))

    # Step 2: Optional fields

    # Architecture-specific packages.
    config["arch_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["arch_packages"][arch] = []
    if "arch_packages" in document["data"]:
        for arch, pkgs in document["data"]["arch_packages"].items():
            if arch not in settings["allowed_arches"]:
                err_log("Error: {file}.yaml lists an invalid architecture: {arch}. Ignoring.".format(
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
        raise ConfigError("Error: {file} is invalid.".format(file=yml_file))

    # Step 2: Optional fields
    # none here

    return config


def _load_config_view(document_id, document, settings):
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
        raise ConfigError("Error: {file} is invalid.".format(file=yml_file))

    # Step 2: Optional fields
    # none here

    return config


def get_configs(directory, settings):
    log("")
    log("###############################################################################")
    log("### Loading user-provided configs #############################################")
    log("###############################################################################")
    log("")


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
                    raise ConfigError("Error: {file} is invalid.".format(file=yml_file))


                # === Case: Repository config ===
                if document["document"] == "feeback-pipeline-repository":
                    configs["repos"][document_id] = _load_config_repo(document_id, document, settings)

                # === Case: Environment config ===
                if document["document"] == "feeback-pipeline-environment":
                    configs["envs"][document_id] = _load_config_env(document_id, document, settings)

                # === Case: Workload config ===
                if document["document"] == "feeback-pipeline-workload":
                    configs["workloads"][document_id] = _load_config_workload(document_id, document, settings)
                
                # === Case: Label config ===
                if document["document"] == "feeback-pipeline-label":
                    configs["labels"][document_id] = _load_config_label(document_id, document, settings)

                # === Case: View config ===
                if document["document"] == "feeback-pipeline-view":
                    configs["views"][document_id] = _load_config_view(document_id, document, settings)


        except ConfigError as err:
            err_log("Config load error: {err}".format(err=err))
            continue
    
    log("Done!  Loaded:")
    log("  - {} repositories".format(len(configs["repos"])))
    log("  - {} environments".format(len(configs["envs"])))
    log("  - {} workloads".format(len(configs["workloads"])))
    log("  - {} labels".format(len(configs["labels"])))
    log("  - {} views".format(len(configs["views"])))
    log("")

    return configs



###############################################################################
### Analyzing stuff! ##########################################################
###############################################################################

# Configs:
#   TYPE:           KEY:          ID:
# - repo            repos         repo_id
# - conf_env        envs          env_id
# - conf_workload   workloads     workload_id
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
# tmp contents:
# - dnf_cachedir-{repo}-{arch}
# - dnf_generic_installroot-{repo}-{arch}
# - dnf_env_installroot-{conf_env}-{repo}-{arch}
#
#

def _analyze_pkgs(tmp, repo, arch):
    log("Analyzing pkgs for {repo_name} ({repo_id}) {arch}".format(
            repo_name=repo["name"],
            repo_id=repo["id"],
            arch=arch
        ))
    
    with dnf.Base() as base:

        # Local DNF cache
        cachedir_name = "dnf_cachedir-{repo}-{arch}".format(
            repo=repo["id"],
            arch=arch
        )
        base.conf.cachedir = os.path.join(tmp, cachedir_name)

        # Generic installroot
        root_name = "dnf_generic_installroot-{repo}-{arch}".format(
            repo=repo["id"],
            arch=arch
        )
        base.conf.installroot = os.path.join(tmp, root_name)

        # Architecture
        base.conf.arch = arch
        base.conf.ignorearch = True

        # Repository
        base.conf.substitutions['releasever'] = repo["source"]["fedora_release"]

        # Load repos
        log("  Loading repos...")
        base.read_all_repos()
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
            pkg_nevr = "{name}-{evr}".format(name=pkg_object.name, evr=pkg_object.evr)
            pkg = {}
            pkg["name"] = pkg_object.name
            pkg["installsize"] = pkg_object.installsize
            pkg["description"] = pkg_object.description
            #pkg["provides"] = pkg_object.provides
            #pkg["requires"] = pkg_object.requires
            #pkg["recommends"] = pkg_object.recommends
            #pkg["suggests"] = pkg_object.suggests
            pkg["summary"] = pkg_object.summary
            pkgs[pkg_nevr] = pkg
        
        log("  Done!  ({pkg_count} packages in total)".format(
            pkg_count=len(pkgs)
        ))
        log("")

    return pkgs


def _analyze_env(tmp, conf_env, repo, arch):
    env = {}
    
    env["conf_env_id"] = conf_env["id"]
    env["pkg_ids"] = []

    env["errors"] = {}
    env["errors"]["non_existing_pkgs"] = []

    env["succeeded"] = True

    with dnf.Base() as base:

        # Local DNF cache
        cachedir_name = "dnf_cachedir-{repo}-{arch}".format(
            repo=repo["id"],
            arch=arch
        )
        base.conf.cachedir = os.path.join(tmp, cachedir_name)

        # Environment installroot
        root_name = "dnf_env_installroot-{conf_env}-{repo}-{arch}".format(
            conf_env=conf_env["id"],
            repo=repo["id"],
            arch=arch
        )
        base.conf.installroot = os.path.join(tmp, root_name)

        # Architecture
        base.conf.arch = arch
        base.conf.ignorearch = True

        # Repository
        base.conf.substitutions['releasever'] = repo["source"]["fedora_release"]

        # Additional DNF Settings
        base.conf.tsflags.append('justdb')

        # Environment config
        if "include-weak-deps" not in conf_env["options"]:
            base.conf.install_weak_deps = False
        if "include-docs" not in conf_env["options"]:
            base.conf.tsflags.append('nodocs')

        # Load repos
        log("  Loading repos...")
        base.read_all_repos()
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
            err = "Failed to download repodata while analyzing environment '{conf_env}' from '{repo}' {arch}:".format(
                conf_env=conf_env["id"],
                repo=repo["id"],
                arch=arch
            )
            err_log(err)
            raise RepoDownloadError(err)


        # Packages
        log("  Adding packages...")
        for pkg in conf_env["packages"]:
            try:
                base.install(pkg)
            except dnf.exceptions.MarkingError:
                env["errors"]["non_existing_pkgs"].append(pkg)
                continue

        # Architecture-specific packages
        for pkg in conf_env["arch_packages"][arch]:
            try:
                base.install(pkg)
            except dnf.exceptions.MarkingError:
                env["errors"]["non_existing_pkgs"].append(pkg)
                continue
        
        # Resolve dependencies
        log("  Resolving dependencies...")
        base.resolve()

        # Write the result into RPMDB.
        # The transaction needs us to download all the packages. :(
        # So let's do that to make it happy.
        log("  Downloading packages...")
        base.download_packages(base.transaction.install_set)
        log("  Running DNF transaction, writing RPMDB...")
        try:
            base.do_transaction()
        except dnf.exceptions.TransactionCheckError as err:
            err_log("Failed to analyze environment '{conf_env}' from '{repo}' {arch}:".format(
                    conf_env=conf_env["id"],
                    repo=repo["id"],
                    arch=arch
                ))
            err_log("  - {err}".format(err=err))
            env["succeeded"] = False
            return env

        # DNF Query
        log("  Creating a DNF Query object...")
        query = base.sack.query().filterm(pkg=base.transaction.install_set)

        for pkg in query:
            pkg_id = "{name}-{evr}".format(
                name=pkg.name,
                evr=pkg.evr
            )
            env["pkg_ids"].append(pkg_id)
        
        log("  Done!  ({pkg_count} packages in total)".format(
            pkg_count=len(env["pkg_ids"])
        ))
        log("")
    
    return env


def _analyze_envs(tmp, configs):
    envs = {}

    for conf_env_id, conf_env in configs["envs"].items():
        for repo_id in conf_env["repositories"]:
            repo = configs["repos"][repo_id]
            for arch in repo["source"]["architectures"]:
                log("Analyzing {env_name} ({env_id}) from {repo_name} ({repo}) {arch}...".format(
                    env_name=conf_env["name"],
                    env_id=conf_env_id,
                    repo_name=repo["name"],
                    repo=repo_id,
                    arch=arch
                ))

                env_id = "{conf_env_id}:{repo_id}:{arch}".format(
                    conf_env_id=conf_env_id,
                    repo_id=repo_id,
                    arch=arch
                )
                envs[env_id] = _analyze_env(tmp, conf_env, repo, arch)
                
    
    return envs


def _analyze_workload(tmp, conf_workload, conf_env, repo, arch):
    workload = {}

    workload["conf_workload_id"] = conf_workload["id"]
    workload["conf_env_id"] = conf_env["id"]

    workload["pkg_env_ids"] = []
    workload["pkg_added_ids"] = []

    workload["errors"] = {}
    workload["errors"]["non_existing_pkgs"] = []

    workload["succeeded"] = True

    with dnf.Base() as base:

        # Local DNF cache
        cachedir_name = "dnf_cachedir-{repo}-{arch}".format(
            repo=repo["id"],
            arch=arch
        )
        base.conf.cachedir = os.path.join(tmp, cachedir_name)

        # Environment installroot
        # Since we're not writing anything into the installroot,
        # let's just use the base image's installroot!
        root_name = "dnf_env_installroot-{conf_env}-{repo}-{arch}".format(
            conf_env=conf_env["id"],
            repo=repo["id"],
            arch=arch
        )
        base.conf.installroot = os.path.join(tmp, root_name)

        # Architecture
        base.conf.arch = arch
        base.conf.ignorearch = True

        # Repository
        base.conf.substitutions['releasever'] = repo["source"]["fedora_release"]

        # Environment config
        if "include-weak-deps" not in conf_workload["options"]:
            base.conf.install_weak_deps = False
        if "include-docs" not in conf_workload["options"]:
            base.conf.tsflags.append('nodocs')

        # Load repos
        log("  Loading repos...")
        base.read_all_repos()

        # Now I need to load the local RPMDB.
        # However, if the environment is empty, it wasn't created, so I need to treat
        # it differently. So let's check!
        if len(conf_env["packages"]) or len(conf_env["arch_packages"][arch]):
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
                    log("  Failed to download repodata. Trying again!")
            if not success:
                err = "Failed to download repodata while analyzing workload '{workload_id} on '{env_id}' from '{repo}' {arch}...".format(
                        workload_id=conf_workload_id,
                        env_id=conf_env_id,
                        repo_name=repo["name"],
                        repo=repo_id,
                        arch=arch)
                err_log(err)
                raise RepoDownloadError(err)
        
        # Packages
        log("  Adding packages...")
        for pkg in conf_workload["packages"]:
            try:
                base.install(pkg)
            except dnf.exceptions.MarkingError:
                workload["errors"]["non_existing_pkgs"].append(pkg)
                continue

        # Architecture-specific packages
        for pkg in conf_workload["arch_packages"][arch]:
            try:
                base.install(pkg)
            except dnf.exceptions.MarkingError:
                workload["errors"]["non_existing_pkgs"].append(pkg)
                continue

        # Resolve dependencies
        log("  Resolving dependencies...")
        base.resolve()

        # DNF Query
        log("  Creating a DNF Query object...")
        query_env = base.sack.query()
        query_added = base.sack.query().filterm(pkg=base.transaction.install_set)
        pkgs_env = set(query_env.installed())
        pkgs_added = set(base.transaction.install_set)
        pkgs_all = set.union(pkgs_env, pkgs_added)
        query_all = base.sack.query().filterm(pkg=pkgs_all)
        
        for pkg in pkgs_env:
            pkg_id = "{name}-{evr}".format(
                name=pkg.name,
                evr=pkg.evr
            )
            workload["pkg_env_ids"].append(pkg_id)
        
        for pkg in pkgs_added:
            pkg_id = "{name}-{evr}".format(
                name=pkg.name,
                evr=pkg.evr
            )
            workload["pkg_added_ids"].append(pkg_id)
        
        pkg_env_count = len(workload["pkg_env_ids"])
        pkg_added_count = len(workload["pkg_added_ids"])
        log("  Done!  ({pkg_count} packages in total. That's {pkg_env_count} in the environment, and {pkg_added_count} added.)".format(
            pkg_count=str(pkg_env_count + pkg_added_count),
            pkg_env_count=pkg_env_count,
            pkg_added_count=pkg_added_count
        ))
        log("")

    return workload


def _analyze_workloads(tmp, configs):
    workloads = {}

    # Here, I need to mix and match workloads & envs based on labels
    workload_env_map = {}
    for conf_workload_id, conf_workload in configs["workloads"].items():
        workload_env_map[conf_workload_id] = set()
        for label in conf_workload["labels"]:
            for conf_env_id, conf_env in configs["envs"].items():
                if label in conf_env["labels"]:
                    workload_env_map[conf_workload_id].add(conf_env_id)
    
    for conf_workload_id, conf_workload in configs["workloads"].items():
        for conf_env_id in workload_env_map[conf_workload_id]:
            conf_env = configs["envs"][conf_env_id]
            for repo_id in conf_env["repositories"]:
                repo = configs["repos"][repo_id]
                for arch in repo["source"]["architectures"]:
                    log("Analyzing {workload_name} ({workload_id}) on {env_name} ({env_id}) from {repo_name} ({repo}) {arch}...".format(
                        workload_name=conf_workload["name"],
                        workload_id=conf_workload_id,
                        env_name=conf_env["name"],
                        env_id=conf_env_id,
                        repo_name=repo["name"],
                        repo=repo_id,
                        arch=arch
                    ))

                    workload_id = "{conf_workload_id}:{conf_env_id}:{repo_id}:{arch}".format(
                        conf_workload_id=conf_workload_id,
                        conf_env_id=conf_env_id,
                        repo_id=repo_id,
                        arch=arch
                    )

                    workloads[workload_id] = _analyze_workload(tmp, conf_workload, conf_env, repo, arch)


    return workloads


def analyze_things(configs, settings):
    log("")
    log("###############################################################################")
    log("### Analyzing stuff! ##########################################################")
    log("###############################################################################")
    log("")

    data = {}

    data["pkgs"] = {}
    data["envs"] = {}
    data["workloads"] = {}
    data["views"] = {}

    with tempfile.TemporaryDirectory() as tmp:

        # FIXME temporary override
        tmp = "/tmp/fixed-tmp"

        # List of arches
        all_arches = settings["allowed_arches"]

        # Packages
        log("")
        log("=====  Analyzing Repos & Packages =====")
        log("")
        for _,repo in configs["repos"].items():
            repo_id = repo["id"]
            data["pkgs"][repo_id] = {}
            for arch in all_arches:
                data["pkgs"][repo_id][arch] = _analyze_pkgs(tmp, repo, arch)

        # Environments
        log("")
        log("=====  Analyzing Environments =====")
        log("")
        data["envs"] = _analyze_envs(tmp, configs)

        # Workloads
        log("")
        log("=====  Analyzing Workloads =====")
        log("")
        data["workloads"] = _analyze_workloads(tmp, configs)


    return data

###############################################################################
### Create useful information! ################################################
###############################################################################





###############################################################################
### Utilities #################################################################
###############################################################################


def dump_data(path, data):
    with open(path, 'w') as file:
        json.dump(data, file)


def load_data(path):
    with open(path, 'r') as file:
        data = json.load(file)

    return data


###############################################################################
### Main ######################################################################
###############################################################################


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("configs", help="Directory with YAML configuration files. Only files ending with '.yaml' are accepted.")
    parser.add_argument("output", help="Directory to contain the output.")
    args = parser.parse_args()

    settings = load_settings()
    configs = get_configs(args.configs, settings)
    data = analyze_things(configs, settings)

    #dump_data("data.json", data)


if __name__ == "__main__":
    main()
