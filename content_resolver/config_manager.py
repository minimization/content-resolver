import os
import yaml
import argparse

from content_resolver.utils import err_log, log

from content_resolver.exceptions import SettingsError, ConfigError

class ConfigManager:
    def __init__(self, config_file=None):
        if config_file is not None:
            self.settings = config_file
        else:
            self.settings = self.load_settings()


    def load_settings(self):
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

        settings["allowed_arches"] = ["aarch64", "ppc64le", "s390x", "x86_64"]

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

    def _load_config_repo(self, document_id, document):
        raise NotImplementedError("Repo v1 is not supported. Please migrate to repo v2.")


    def _load_config_repo_v2(self, document_id, document, settings):
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


    def _load_config_env(self, document_id, document, settings):
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


    def _load_config_workload(self, document_id, document, settings):
        config = {}
        config["id"] = document_id

        # Step 1: Mandatory fields
        try:
            if "data" not in document:
                raise ConfigError(f"Missing 'data' field in {document_id}")

            for key in document["data"]:
                if key not in [
                    "arch_packages",
                    "description",
                    "groups",
                    "labels",
                    "maintainer",
                    "name",
                    "options",
                    "package_placeholders",
                    "packages",
                ]:
                    raise ConfigError(f"Unknown key '{key}' in 'data' section of {document_id}")

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
            if isinstance(document["data"]["package_placeholders"], list):
                for srpm in document["data"]["package_placeholders"]:
                    srpm_name = srpm["srpm_name"]
                    if not srpm_name:
                        continue

                    build_dependencies = srpm.get("build_dependencies", [])
                    limit_arches = srpm.get("limit_arches", [])
                    rpms = srpm.get("rpms", [])

                    all_rpm_arches = set()

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
                        
                        elif limit_arches and not rpm_limit_arches:
                            rpm_limit_arches = limit_arches
                        
                        all_rpm_arches.update(rpm_limit_arches)

                        config["package_placeholders"]["pkgs"][rpm_name] = {}
                        config["package_placeholders"]["pkgs"][rpm_name]["name"] = rpm_name
                        config["package_placeholders"]["pkgs"][rpm_name]["description"] = description
                        config["package_placeholders"]["pkgs"][rpm_name]["requires"] = dependencies
                        config["package_placeholders"]["pkgs"][rpm_name]["limit_arches"] = rpm_limit_arches
                        config["package_placeholders"]["pkgs"][rpm_name]["srpm"] = srpm_name
                    
                    if not limit_arches and all_rpm_arches:
                        config["package_placeholders"]["srpms"][srpm_name]["limit_arches"] = list(all_rpm_arches)



        return config


    def _load_config_label(self, document_id, document, settings):
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


    def _load_config_compose_view(self, document_id, document, settings):
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


    def _load_config_addon_view(self, document_id, document, settings):
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
            config["repository"] = str(document["data"]["repository"])

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


    def _load_config_unwanted(self, document_id, document, settings):
        config = {}
        config["id"] = document_id

        # Step 1: Mandatory fields
        try:
            if "data" not in document:
                raise ConfigError(f"Missing 'data' field in {document_id}")

            for key in document["data"]:
                if key not in [
                    "description",
                    "labels",
                    "maintainer",
                    "name",
                    "unwanted_arch_packages",
                    "unwanted_arch_source_packages",
                    "unwanted_packages",
                    "unwanted_source_packages",
                ]:
                    raise ConfigError(f"Unknown key '{key}' in 'data' section of {document_id}")
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


    def _load_config_buildroot(self, document_id, document, settings):
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


    def _load_json_data_buildroot_pkg_relations(self, document_id, document, settings):
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


    def get_configs(self):
        log("")

        directory = self.settings["configs"]

        if "allowed_arches" not in self.settings:
            err_log("System error: allowed_arches not configured")
            raise SettingsError
        
        if not self.settings["allowed_arches"]:
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
                    if document["document"] not in [
                        "content-resolver-buildroot",
                        "content-resolver-compose-view",
                        "content-resolver-environment",
                        "content-resolver-label",
                        "content-resolver-repository",
                        "content-resolver-unwanted",
                        "content-resolver-view",
                        "content-resolver-view-addon",
                        "content-resolver-workload",
                        "feedback-pipeline-buildroot",
                        "feedback-pipeline-compose-view",
                        "feedback-pipeline-environment",
                        "feedback-pipeline-label",
                        "feedback-pipeline-repository",
                        "feedback-pipeline-unwanted",
                        "feedback-pipeline-view",
                        "feedback-pipeline-view-addon",
                        "feedback-pipeline-workload",
                    ]:
                        raise ConfigError(f"Unknown document type: {document['document']}")

                    if document["document"] in ["content-resolver-repository", "feedback-pipeline-repository"]:
                        if document["version"] == 1:
                            configs["repos"][document_id] = self._load_config_repo(document_id, document, self.settings)
                        
                        elif document["version"] == 2:
                            configs["repos"][document_id] = self._load_config_repo_v2(document_id, document, self.settings)

                    # === Case: Environment config ===
                    if document["document"] in ["content-resolver-environment", "feedback-pipeline-environment"]:
                        configs["envs"][document_id] = self._load_config_env(document_id, document, self.settings)

                    # === Case: Workload config ===
                    if document["document"] in ["content-resolver-workload", "feedback-pipeline-workload"]:
                        configs["workloads"][document_id] = self._load_config_workload(document_id, document, self.settings)
                    
                    # === Case: Label config ===
                    if document["document"] in ["content-resolver-label", "feedback-pipeline-label"]:
                        configs["labels"][document_id] = self._load_config_label(document_id, document, self.settings)

                    # === Case: View config ===
                    #  (Also including the legacy "feedback-pipeline-compose-view" for backwards compatibility)
                    if document["document"] in ["content-resolver-view", "content-resolver-compose-view", "feedback-pipeline-view", "feedback-pipeline-compose-view"]:
                        configs["views"][document_id] = self._load_config_compose_view(document_id, document, self.settings)

                    # === Case: View addon config ===
                    if document["document"] in ["content-resolver-view-addon", "feedback-pipeline-view-addon"]:
                        configs["views"][document_id] = self._load_config_addon_view(document_id, document, self.settings)

                    # === Case: Unwanted config ===
                    if document["document"] in ["content-resolver-unwanted", "feedback-pipeline-unwanted"]:
                        configs["unwanteds"][document_id] = self._load_config_unwanted(document_id, document, self.settings)

                    # === Case: Buildroot config ===
                    if document["document"] in ["content-resolver-buildroot", "feedback-pipeline-buildroot"]:
                        configs["buildroots"][document_id] = self._load_config_buildroot(document_id, document, self.settings)

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
            if settings.get("strict", False):
                raise ConfigError("Config file errors encountered in strict mode")
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
            # Only accept json files
            if not json_file.endswith(".json"):
                continue
            
            document_id = json_file.split(".json")[0]

            try:
                try:
                    json_data = self.load_data(os.path.join(directory, json_file))
                except:
                    raise ConfigError("Error loading a JSON data file '{filename}': {err}".format(
                                    filename=json_file,
                                    err=err))
                
                # Only accept json files stating their purpose!
                if not ("document_type" in json_data and "version" in json_data):
                    raise ConfigError("'{file}.yaml' - doesn't specify the 'document' and/or the 'version' field.".format(file=json_file))


                # === Case: Buildroot pkg relations data ===
                if json_data["document_type"] == "buildroot-binary-relations":
                    configs["buildroot_pkg_relations"][document_id] = self._load_json_data_buildroot_pkg_relations(document_id, json_data, self.settings)

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
            if self.settings.get("strict", False):
                raise ConfigError("Config file errors encountered in strict mode")
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
                configs["views"][view_conf_id]["architectures"] = configs["views"][base_view_id]["architectures"]
        
        # Adjust view architecture based on repository architectures
        for view_conf_id, view_conf in configs["views"].items():
            if view_conf["type"] == "compose":
                if not len(view_conf["architectures"]):
                    view_conf["architectures"] = self.settings["allowed_arches"]
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
                    view_conf["architectures"] = self.settings["allowed_arches"]
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
