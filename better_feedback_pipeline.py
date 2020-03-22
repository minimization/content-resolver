#!/usr/bin/python3

import argparse, yaml, tempfile, os, subprocess, json, jinja2, datetime, copy, re, dnf, pprint
import rpm_showme as showme


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

def size(num, suffix='B'):
    for unit in ['','k','M','G']:
        if abs(num) < 1024.0:
            return "%3.1f %s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f %s%s" % (num, 'T', suffix)

def load_settings():
    settings = {}

    parser = argparse.ArgumentParser()
    parser.add_argument("configs", help="Directory with YAML configuration files. Only files ending with '.yaml' are accepted.")
    parser.add_argument("output", help="Directory to contain the output.")
    args = parser.parse_args()

    settings["configs"] = args.configs
    settings["output"] = args.output

    # FIXME: This is hardcorded, and it shouldn't be!
    #settings["allowed_arches"] = ["armv7hl","aarch64","i686","ppc64le","s390x","x86_64"]
    # FIXME Limiting arches for faster results during development
    settings["allowed_arches"] = ["aarch64","x86_64"]
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


def get_configs(settings):
    log("")
    log("###############################################################################")
    log("### Loading user-provided configs #############################################")
    log("###############################################################################")
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
# tmp contents:
# - dnf_cachedir-{repo}-{arch}
# - dnf_generic_installroot-{repo}-{arch}
# - dnf_env_installroot-{env_conf}-{repo}-{arch}
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
            pkg["id"] = pkg_nevr
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


def _analyze_env(tmp, env_conf, repo, arch):
    env = {}
    
    env["env_conf_id"] = env_conf["id"]
    env["pkg_ids"] = []
    env["repo_id"] = repo["id"]
    env["arch"] = arch

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
        root_name = "dnf_env_installroot-{env_conf}-{repo}-{arch}".format(
            env_conf=env_conf["id"],
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
        if "include-weak-deps" not in env_conf["options"]:
            base.conf.install_weak_deps = False
        if "include-docs" not in env_conf["options"]:
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

        # Architecture-specific packages
        for pkg in env_conf["arch_packages"][arch]:
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
            err_log("Failed to analyze environment '{env_conf}' from '{repo}' {arch}:".format(
                    env_conf=env_conf["id"],
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

    # Look at all env configs...
    for env_conf_id, env_conf in configs["envs"].items():
        # For each of those, look at all repos it lists...
        for repo_id in env_conf["repositories"]:
            # And for each of the repo, look at all arches it supports.
            repo = configs["repos"][repo_id]
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
                envs[env_id] = _analyze_env(tmp, env_conf, repo, arch)
                
    
    return envs


def _analyze_workload(tmp, workload_conf, env_conf, repo, arch):
    workload = {}

    workload["workload_conf_id"] = workload_conf["id"]
    workload["env_conf_id"] = env_conf["id"]
    workload["repo_id"] = repo["id"]
    workload["arch"] = arch

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
        root_name = "dnf_env_installroot-{env_conf}-{repo}-{arch}".format(
            env_conf=env_conf["id"],
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
        if "include-weak-deps" not in workload_conf["options"]:
            base.conf.install_weak_deps = False
        if "include-docs" not in workload_conf["options"]:
            base.conf.tsflags.append('nodocs')

        # Load repos
        log("  Loading repos...")
        base.read_all_repos()

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
                    log("  Failed to download repodata. Trying again!")
            if not success:
                err = "Failed to download repodata while analyzing workload '{workload_id} on '{env_id}' from '{repo}' {arch}...".format(
                        workload_id=workload_conf_id,
                        env_id=env_conf_id,
                        repo_name=repo["name"],
                        repo=repo_id,
                        arch=arch)
                err_log(err)
                raise RepoDownloadError(err)
        
        # Packages
        log("  Adding packages...")
        for pkg in workload_conf["packages"]:
            try:
                base.install(pkg)
            except dnf.exceptions.MarkingError:
                workload["errors"]["non_existing_pkgs"].append(pkg)
                continue

        # Architecture-specific packages
        for pkg in workload_conf["arch_packages"][arch]:
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
    # Look at all workload configs...
    for workload_conf_id, workload_conf in configs["workloads"].items():
        workload_env_map[workload_conf_id] = set()
        # ... and all of their labels.
        for label in workload_conf["labels"]:
            # And for each label, find all env configs...
            for env_conf_id, env_conf in configs["envs"].items():
                # ... that also have the label.
                if label in env_conf["labels"]:
                    # And save those.
                    workload_env_map[workload_conf_id].add(env_conf_id)
    
    # And now, look at all workload configs...
    for workload_conf_id, workload_conf in configs["workloads"].items():
        # ... and for each, look at all env configs it should be analyzed in.
        for env_conf_id in workload_env_map[workload_conf_id]:
            # Each of those envs can have multiple repos associated...
            env_conf = configs["envs"][env_conf_id]
            for repo_id in env_conf["repositories"]:
                # ... and each repo probably has multiple architecture.
                repo = configs["repos"][repo_id]
                for arch in repo["source"]["architectures"]:

                    # And now it has:
                    #   all workload configs *
                    #   all envs that match those *
                    #   all repos of those envs *
                    #   all arches of those repos.
                    # That's a lot of stuff! Let's analyze all of that!
                    log("Analyzing {workload_name} ({workload_id}) on {env_name} ({env_id}) from {repo_name} ({repo}) {arch}...".format(
                        workload_name=workload_conf["name"],
                        workload_id=workload_conf_id,
                        env_name=env_conf["name"],
                        env_id=env_conf_id,
                        repo_name=repo["name"],
                        repo=repo_id,
                        arch=arch
                    ))

                    workload_id = "{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}".format(
                        workload_conf_id=workload_conf_id,
                        env_conf_id=env_conf_id,
                        repo_id=repo_id,
                        arch=arch
                    )

                    workloads[workload_id] = _analyze_workload(tmp, workload_conf, env_conf, repo, arch)


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
### Query gives an easy access to the data! ###################################
###############################################################################

class Query():
    def __init__(self, data, configs, settings):
        self.data = data
        self.configs = configs
        self.settings = settings

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
        return matching_ids
    
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
        return matching_ids
    
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
    
    def workload_pkgs(self, workload_conf_id, env_conf_id, repo_id, arch):
        pass

    def workload_pkgs_id(self, workload_id):
        pass
    
    def env_pkgs(self, workload_conf_id, env_conf_id, repo_id, arch):
        pass
    
    def env_pkgs_id(self, env_id):
        pass



###############################################################################
### Generating html pages! ####################################################
###############################################################################


def _generate_html_page(template_name, template_data, page_name, settings):
    log("Generating the '{page_name}' page...".format(
        page_name=page_name
    ))

    output = settings["output"]

    template_loader = jinja2.FileSystemLoader(searchpath="./templates/")
    template_env = jinja2.Environment(loader=template_loader)

    template = template_env.get_template("{template_name}.html".format(
        template_name=template_name
    ))

    if template_data:
        page = template.render(**template_data)
    else:
        page = template.render()

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


def _generate_workload_overview_pages(query):
    log("Generating workload overview pages...")

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
    
    log("  Done!")
    log("")


def _generate_workload_pages(key, data, configs, settings):
    log("Generating workload pages...")

    for workload_id, workload in data["workloads"].items():

        arch = workload["arch"]
        repo_id = workload["repo_id"]
        workload_conf_id = workload["workload_conf_id"]
        workload_conf = configs["workloads"][workload_conf_id]

        all_pkgs = {}

        for pkg_id in workload["pkg_env_ids"]:
            pkg = data["pkgs"][repo_id][arch][pkg_id]
            pkg["is_required"] = False
            pkg["is_env"] = True
            pkg["size_text"] = size(pkg["installsize"])
            all_pkgs[pkg_id] = pkg
        
        for pkg_id in workload["pkg_added_ids"]:
            pkg = data["pkgs"][repo_id][arch][pkg_id]
            pkg["is_required"] = bool(
                pkg["name"] in workload_conf["packages"] \
                or \
                pkg["name"] in workload_conf["arch_packages"][arch]
            )
            pkg["is_env"] = False
            pkg["size_text"] = size(pkg["installsize"])
            all_pkgs[pkg_id] = pkg

        template_data = {
            "arch": arch,
            "repo_id": repo_id,
            "workload_conf_id": workload_conf_id,
            "workload_conf": workload_conf,
            "workload_id": workload_id,
            "workload": workload,
            "all_pkgs": all_pkgs,
            "configs": configs,
            "key": key
        }

        page_name = "workload--{workload_id}".format(
            workload_id=workload_id
        )

        _generate_html_page("workload", template_data, page_name, settings)

    log("  Done!")
    log("")


def generate_pages(query):
    log("")
    log("###############################################################################")
    log("### Generating html pages! ####################################################")
    log("###############################################################################")
    log("")

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

    # Generate the workloads page
    template_data = {
        "query": query
    }
    _generate_html_page("workloads", template_data, "workloads", query.settings)

    # Generate workload_overview pages
    _generate_workload_overview_pages(query)

    return

    # Generate the workloads page
    _generate_workloads_page
    template_data = {
        "configs": configs,
        "key": key
    }
    _generate_html_page("workloads", template_data, "workloads", query.settings)

    # Generate workload_overview pages
    _generate_workload_overview_pages(key, data, configs, query.settings)

    # Generate workload pages
    _generate_workload_pages(key, data, configs, query.settings)

    # Generate the environments page
    template_data = {
        "configs": configs,
        "key": key
    }
    _generate_html_page("envs", template_data, "envs", query.settings)

    # Generate the views page
    template_data = {
        "configs": configs,
        "key": key
    }
    _generate_html_page("views", template_data, "views", query.settings)




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
    settings = load_settings()
    configs = get_configs(settings)
    data = analyze_things(configs, settings)

    query = Query(data, configs, settings)

    generate_pages(query)

    #dump_data("data.json", data)








def tests_to_be_made_actually_useful_at_some_point_because_this_is_terribble():
    print("")
    print("")
    print("")
    print("test test test")
    print("test test test")
    print("test test test")
    print("test test test")
    print("")
    print("")


    # does_workload_exist(self, workload_conf_id, env_conf_id, repo_id, arch):

    #   env-empty 
    #   env-minimal 
    #   label-eln-compose 
    #   label-fedora-31-all 
    #   label-fedora-rawhide-all 
    #   repo-fedora-31 
    #   repo-fedora-rawhide 
    #   view-eln-compose 
    #   workload-httpd 
    #   workload-nginx 
    #   x86_64
    #   aarch64

    print("Should be False:")
    print(query.workloads("mleko","mleko","mleko","mleko"))
    print("")

    print("Should be True:")
    print(query.workloads("workload-httpd","env-empty","repo-fedora-31","x86_64"))
    print("")

    print("Should be False:")
    print(query.workloads("workload-httpd","env-empty","repo-fedora-31","aarch64"))
    print("")

    print("Should be True:")
    print(query.workloads("workload-httpd","env-empty","repo-fedora-31",None))
    print("")

    print("Should be True:")
    print(query.workloads("workload-nginx",None, None, None))
    print("")

    print("Should be True:")
    print(query.workloads(None,"env-minimal",None,"x86_64"))
    print("")

    print("Should be True:")
    print(query.workloads(None,"env-minimal",None,"aarch64"))
    print("")

    print("Should be False:")
    print(query.workloads(None,"env-minimal","repo-fedora-31","x86_64"))
    print("")

    print("Should be False:")
    print(query.workloads(None,"env-minimal","repo-fedora-31","aarch64"))
    print("")

    print("----------")
    print("")
    print("")

    print("Should be 7:")
    print(len(query.workloads(None,None,None,None,list_all=True)))
    print("")

    print("Should be 3:")
    print(len(query.workloads(None,None,None,"aarch64",list_all=True)))
    print("")

    print("Should be 2:")
    print(len(query.workloads("workload-nginx",None,None,None,list_all=True)))
    print("")

    print("----------")
    print("")
    print("")

    print("Should be 2 workload-nginx:")
    for id in query.workloads("workload-nginx",None,None,None,list_all=True):
        print(id)
    print("")

    print("Should be all 7:")
    for id in query.workloads(None,None,None,None,list_all=True):
        print(id)
    print("")

    print("Should be all 6 rawhide:")
    for id in query.workloads(None,None,"repo-fedora-rawhide",None,list_all=True):
        print(id)
    print("")

    print("Should be all 2 empty rawhide:")
    for id in query.workloads(None,"env-empty","repo-fedora-rawhide",None,list_all=True):
        print(id)
    print("")

    print("Should be nothing:")
    for id in query.workloads("workload-nginx","env-empty","repo-fedora-rawhide",None,list_all=True):
        print(id)
    print("")

    print("----------")
    print("")
    print("")

    print("Should be env-empty:repo-fedora-31:x86_64")
    for id in query.envs_id("workload-httpd:env-empty:repo-fedora-31:x86_64", list_all=True):
        print(id)
    print("")

    print("Should be workload-httpd:env-empty:repo-fedora-31:x86_64")
    for id in query.workloads_id("workload-httpd:env-empty:repo-fedora-31:x86_64", list_all=True):
        print(id)
    print("")

    print("Should be two, workload-httpd:env-minimal:repo-fedora-rawhide:x86_64 and workload-nginx:...")
    for id in query.workloads_id("env-minimal:repo-fedora-rawhide:x86_64", list_all=True):
        print(id)
    print("")

    print("----------")
    print("")
    print("")

    print("Should be all 2 arches:")
    for id in query.workloads("workload-httpd",None, None,None,list_all=True,output_change="arches"):
        print(id)
    print("")

    print("Should be all 2 arches:")
    for id in query.workloads("workload-nginx",None, None,None,list_all=True,output_change="arches"):
        print(id)
    print("")

    print("Should be all 2 env_conf_ids:")
    for id in query.workloads("workload-httpd",None, None,None,list_all=True,output_change="env_conf_ids"):
        print(id)
    print("")

    print("Should be all 1 env_conf_id:")
    for id in query.workloads("workload-nginx",None, None,None,list_all=True,output_change="env_conf_ids"):
        print(id)
    print("")









if __name__ == "__main__":
    main()
