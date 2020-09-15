#!/usr/bin/python3

import argparse, yaml, tempfile, os, json, datetime, urllib.request, sys, jinja2, subprocess
from functools import lru_cache

try:
    import yaml, jinja2
except:
    print("")
    print("Error: Can't run, missing dependencies.")
    print("       If you're on Fedora, you can get them by:")
    print("")
    print("       $ sudo dnf install python3-jinja2 python3-yaml")
    print("")
    sys.exit(1)


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

class SetEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, set):
            return list(obj)
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

def pkg_placeholder_name_to_id(placeholder_name):
    placeholder_id = "{name}-000-placeholder.placeholder".format(name=placeholder_name)
    return placeholder_id

def id_to_url_slug(any_id):
    return any_id.replace(":", "--")


def load_settings():
    settings = {}

    parser = argparse.ArgumentParser()
    parser.add_argument("configs", help="Directory with YAML configuration files. Only files ending with '.yaml' are accepted.")
    parser.add_argument("output", help="Directory to contain the output.")
    parser.add_argument("--use-cache", dest="use_cache", action='store_true', help="Use local data instead of pulling Content Resolver. Saves a lot of time! Needs a 'cache_data.json' file at the same location as the script is at.")
    parser.add_argument("--html", dest="html", action='store_true', help="Generate html pages instead of txt files.")
    args = parser.parse_args()

    settings["configs"] = args.configs
    settings["output"] = args.output
    settings["use_cache"] = args.use_cache
    settings["html"] = args.html

    settings["allowed_arches"] = ["aarch64","ppc64le","s390x","x86_64"]

    settings["repos"] = {
        "appstream": ["aarch64", "ppc64le", "s390x", "x86_64"],
        "baseos": ["aarch64", "ppc64le", "s390x", "x86_64"],
        "crb": ["aarch64", "ppc64le", "s390x", "x86_64"],
        "addon-ha": ["aarch64", "ppc64le", "s390x", "x86_64"],
        "addon-nfv": ["x86_64"],
        "addon-rt": ["x86_64"],
        "addon-rs": ["ppc64le", "s390x", "x86_64"],
        "addon-sap": ["ppc64le", "s390x", "x86_64"],
        "addon-saphana": ["ppc64le", "x86_64"]
    }

    settings["addons"] = ["addon-ha", "addon-nfv", "addon-rt", "addon-rs", "addon-sap", "addon-saphana"]
    return settings



###############################################################################
### Loading user-provided configs #############################################
###############################################################################


def _load_config(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        config["name"] = str(document["data"]["name"])
        config["maintainer"] = str(document["data"]["maintainer"])
    except KeyError as error:
        raise ConfigError("Error: {file} is invalid: {error}".format(
            file=yml_file,
            error=error
        ))

    # Step 2: Optional fields

    must_fields = [
        "baseos",
        "addon-ha",
        "addon-nfv",
        "addon-rt",
        "addon-rs",
        "addon-sap",
        "addon-saphana",
        "crb"
    ]

    want_fields = [
        "baseos",
        "appstream",
        "buildroot-only",
        "crb"
    ]

    for target_repo in must_fields:
        if target_repo not in config:
            config[target_repo] = {}
        config[target_repo]["must"] = []

        if target_repo in document["data"] and "must" in document["data"][target_repo]:
            for pkg in document["data"][target_repo]["must"]:
                config[target_repo]["must"].append(str(pkg))
    
    for target_repo in want_fields:
        if target_repo not in config:
            config[target_repo] = {}
        config[target_repo]["want"] = []

        if target_repo in document["data"] and "want" in document["data"][target_repo]:
            for pkg in document["data"][target_repo]["want"]:
                config[target_repo]["want"].append(str(pkg))

    return config


def get_configs(settings):

    directory = settings["configs"]

    if "allowed_arches" not in settings:
        err_log("System error: allowed_arches not configured")
        raise SettingsError
    
    if not settings["allowed_arches"]:
        err_log("System error: no allowed_arches not configured")
        raise SettingsError

    configs = {}

    configs["configs"] = {}

    # Step 1: Load all configs
    log("Loading config files...")
    for yml_file in os.listdir(directory):
        # Only accept yaml files
        if not yml_file.endswith(".yaml"):
            continue

        # Skip those massive buildroot configs!
        if yml_file in ["buildroot-eln.yaml", "buildroot-prototype-eln.yaml", "eln-buildroot-workload.yaml", "prototype-eln-buildroot-workload.yaml"]:
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
                if document["document"] == "eln-repo-split":
                    configs["configs"][document_id] = _load_config(document_id, document, settings)

        except ConfigError as err:
            err_log("Config load error: {err}. Skipping this config.".format(err=err))
            continue
    
    log("  Done!")
    log("")

    
    log("Done!  Loaded:")
    log("  - {} configs".format(len(configs["configs"])))
    log("")


    return configs


def get_data(content_resolver_query):
    # Does the same thing as get_data_download_from_content_resolver
    # except it doesn't download the data from tiny.distro.builders,
    # but takes it from the Query object from feedback-pipeline.py
    # (Good for calling this script from feedback-pipeline.py)

    data = {}
    data["pkgs"] = {}
    data["workloads"] = {}

    view_conf_id = "view-eln"

    all_workload_ids = set()

    arches = content_resolver_query.settings["allowed_arches"]
    
    for arch in arches:
        data["pkgs"][arch] = content_resolver_query.pkgs_in_view(view_conf_id, arch)

        workload_ids = content_resolver_query.workloads_in_view(view_conf_id, arch)
        all_workload_ids.update(workload_ids)

        data["workloads"][arch] = {}
        for workload_id in workload_ids:
            data["workloads"][arch][workload_id] = {}
    
    for workload_id in all_workload_ids:
        arch = workload_id.split(":")[-1]

        output_data = {}
        output_data["date"] = ""
        output_data["id"] = workload_id
        output_data["type"] = "workload"
        output_data["data"] = content_resolver_query.data["workloads"][workload_id]
        output_data["pkg_query"] = content_resolver_query.workload_pkgs_id(workload_id)

        data["workloads"][arch][workload_id] = output_data

    return data

def get_data_download_from_content_resolver(settings):

    log("")
    log("###############################################################################")
    log("### Downloading data from Content Resolver ####################################")
    log("###############################################################################")
    log("")

    data = {}
    data["pkgs"] = {}
    data["workloads"] = {}

    arches = settings["allowed_arches"]

    with tempfile.TemporaryDirectory() as tmp:

        all_workload_ids = set()

        for arch in arches:

            filename = "view--view-eln--{arch}.json".format(arch=arch)

            log("  Downloading {arch} view data...".format(arch=arch))
            opener = urllib.request.URLopener()
            opener.addheader('User-Agent', 'whatever')
            filename, headers = opener.retrieve(
                "https://tiny.distro.builders/{filename}".format(
                    filename=filename
                ),
                os.path.join(tmp, filename)
            )
            log("  Done!")
            log("")


            log("  Processing {arch} view data...".format(arch=arch))
            view_data = load_data(os.path.join(tmp, filename))

            data["pkgs"][arch] = view_data["pkg_query"]

            workload_ids = view_data["workload_ids"]
            all_workload_ids.update(workload_ids)

            data["workloads"][arch] = {}
            for workload_id in workload_ids:
                data["workloads"][arch][workload_id] = {}

            log("  Done!")
            log("")


        download_counter = 0
        for workload_id in all_workload_ids:
            download_counter += 1

            arch = workload_id.split(":")[-1]

            log("  [{current}/{total}]".format(
                current=download_counter,
                total=len(all_workload_ids)
            ))
            log("  Getting workload data: {workload_id}".format(
                workload_id=workload_id
            ))
            log("    Downloading...".format(arch=arch))
            filename = "workload--{slug}.json".format(
                slug=id_to_url_slug(workload_id)
            )
            opener = urllib.request.URLopener()
            opener.addheader('User-Agent', 'whatever')
            filename, headers = opener.retrieve(
                "https://tiny.distro.builders/{filename}".format(
                    filename=filename
                ),
                os.path.join(tmp, filename)
            )

            log("    Processsing...".format(arch=arch))
            workload_data = load_data(os.path.join(tmp, filename))
            data["workloads"][arch][workload_id] = workload_data

            log("    Done!")
            log("")


    log("Done!")
    log("")

    return data

###############################################################################
### Query gives an easy access to the data! ###################################
###############################################################################

class Query():
    def __init__(self, data, configs, settings):
        self.data = data
        self.configs = configs
        self.settings = settings
        self.repos = {}
        self.all_pkgs = {}
        self.warning_output = ""
    
    def warning_log(self, msg):
        print(msg)
        self.warning_output += msg
        self.warning_output += "\n"
    
    def _init_new_pkg(self, pkg_name):
        pkg = {}

        pkg["name"] = pkg_name

        pkg["required_by"] = set()
        pkg["requires"] = set()

        pkg["target_repo"] = "appstream"

        pkg["addon_repos"] = set()
        pkg["pkgs_preventing_addon_separation"] = set()

        pkg["repositories"] = []

        pkg["pulls"] = {}
        pkg["pulls"]["baseos"] = set()

        pkg["musts"] = {}
        pkg["musts"]["baseos"] = set()

        for addon in self.settings["addons"]:
            pkg["musts"][addon] = set()

        pkg["wants"] = {}
        pkg["wants"]["baseos"] = set()
        pkg["wants"]["appstream"] = set()

        self.all_pkgs[pkg_name] = pkg


    
    def sort_out_pkgs(self):




        self.repos["baseos"] = {}
        self.repos["appstream"] = {}
        self.repos["addon-ha"] = {}
        self.repos["addon-nfv"] = {}
        self.repos["addon-rt"] = {}
        self.repos["addon-rs"] = {}
        self.repos["addon-sap"] = {}
        self.repos["addon-saphana"] = {}
        self.repos["buildroot-only"] = {}
        self.repos["crb"] = {}



        print("")
        print("Processing...")

        ### 1/ Initiate self.all_pkgs and set them up with:
        #       * name
        #       * required_by
        #       * requires
        #       * target_repo (just the default: appstream)

        for arch in self.settings["allowed_arches"]:
            for workload_id, workload in self.data["workloads"][arch].items():

                relations_dict = workload["data"]["pkg_relations"]
                if not relations_dict:
                    continue

                for pkg_id, pkg_relations in relations_dict.items():
                    pkg_name = pkg_id_to_name(pkg_id)
                    pkg_required_by = pkg_relations["required_by"]

                    pkg_required_by = [pkg_id_to_name(pkg_id) for pkg_id in pkg_required_by]

                    # 1/ record that this package is required by all the other ones
                    if pkg_name not in self.all_pkgs:
                        self._init_new_pkg(pkg_name)
                    self.all_pkgs[pkg_name]["required_by"].update(pkg_required_by)

                    # 2/ record for all the other ones that they require this package
                    for other_pkg_name in pkg_required_by:
                        if other_pkg_name not in self.all_pkgs:
                            self._init_new_pkg(other_pkg_name)
                        
                        self.all_pkgs[other_pkg_name]["requires"].add(pkg_name)

        print ("  Found {} packages".format(
            len(self.all_pkgs)
        ))


        ### 2/ Set up self.all_pkgs with:
        #       * musts
        #       * wants

        for pkg_name, pkg in self.all_pkgs.items():

            for config_id, config in self.configs["configs"].items():

                # baseos
                if pkg_name in config["baseos"]["must"]:
                    pkg["musts"]["baseos"].add(config_id)
                
                if pkg_name in config["baseos"]["want"]:
                    pkg["wants"]["baseos"].add(config_id)

                # appstream
                if pkg_name in config["appstream"]["want"]:
                    pkg["wants"]["appstream"].add(config_id)
                
                # addons
                for addon in self.settings["addons"]:
                    if pkg_name in config[addon]["must"]:
                        pkg["musts"][addon].add(config_id)

        ### 3/ Set up self.all_pkgs with:
        #       * pulls          
        #       * target_repo (just the default: appstream)

        ##  3.1/  baseos vs. appstream split
        baseos_pkg_names = set()
        while True:
            package_moved = False
            for pkg_name, pkg in self.all_pkgs.items():

                # is it in a baseos list?
                #  -> move it
                if pkg["musts"]["baseos"] and pkg["target_repo"] == "appstream":
                    # Moveing to baseos
                    package_moved = True
                    pkg["target_repo"] = "baseos"

                # does something in baseos require it?
                #  -> move it
                for repo_pkg_name, repo_pkg in self.all_pkgs.items():
                    if repo_pkg["target_repo"] != "baseos":
                        continue

                    if pkg_name in repo_pkg["requires"] and pkg["target_repo"] == "appstream":
                        # Moveing to baseos
                        package_moved = True
                        pkg["target_repo"] = "baseos"
            
            if not package_moved:
                break
        

        ##  3.2/  pulling out addons

        addon_pkg_names = set()
        unpullable_addons = {}

        # Assign addon repos to packages
        for pkg_name, pkg in self.all_pkgs.items():

            for addon in self.settings["addons"]:

                if pkg["musts"][addon]:
                    pkg["addon_repos"].add(addon)
                    addon_pkg_names.add(pkg_name)
        
        # Look if addon packages are not required by something in
        # baseos or appstream. If so, flag those addons as "unpullable"
        for pkg_name in addon_pkg_names:
            pkg = self.all_pkgs[pkg_name]

            for dependent_pkg_name in pkg["required_by"]:
                if dependent_pkg_name not in addon_pkg_names:
                    for addon in pkg["addon_repos"]:
                        if addon not in unpullable_addons:
                            unpullable_addons[addon] = {}
                        
                        if pkg_name not in unpullable_addons[addon]:
                            unpullable_addons[addon][pkg_name] = set()
                        
                        unpullable_addons[addon][pkg_name].add(dependent_pkg_name)
        
        # Mark addon packages in addons that can be pulled out 
        # as not being in either appstream or baseos
        for pkg_name in addon_pkg_names:
            pkg = self.all_pkgs[pkg_name]
            for addon in pkg["addon_repos"]:
                if addon not in unpullable_addons:
                    pkg["target_repo"] = None

        
        # Print errors
        # also add this data to the individual packages
        for addon_name, addon in unpullable_addons.items():
            self.warning_log("")
            self.warning_log("WARNING: The {} addon can't be pulled out.".format(addon_name))
            self.warning_log("         That's because:")
            for pkg_name, dependent_pkg_names in addon.items():
                self.warning_log("           - {} listed in addon is required by:".format(pkg_name))
                for dependent_pkg_name in dependent_pkg_names:
                    dependent_pkg = self.all_pkgs[dependent_pkg_name]
                    self.all_pkgs[pkg_name]["pkgs_preventing_addon_separation"].add((dependent_pkg_name, dependent_pkg["target_repo"]))
                    self.warning_log("                - {} ({})".format(dependent_pkg_name, dependent_pkg["target_repo"]))
                self.warning_log("         (Adding {} to {} should fix the problem.)".format(
                    ", ".join(dependent_pkg_names),
                    addon_name
                ))


        ### Populate repos
        for pkg_name, pkg in self.all_pkgs.items():

            if pkg["target_repo"]:
                repo = pkg["target_repo"]
                pkg["repositories"].append(repo)
                self.repos[repo][pkg_name] = pkg
            
            for addon in self.settings["addons"]:
                if addon in pkg["addon_repos"]:
                    self.repos[addon][pkg_name] = pkg
                    pkg["repositories"].append(addon)
            

        print("  Done!")
        print("")



###############################################################################
### Sorting to target_repos ###################################################
###############################################################################






def _generate_a_flat_list_file(data_list, file_name, settings):

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



def output_txt_files(query):

    log("Generating txt files...")

    for repo in query.settings["repos"]:
        _generate_a_flat_list_file(sorted(query.repos[repo]), repo, query.settings)

    
    log("Done!")
    log("")


def print_summary(query):

    print("Results:")
    print("  baseos: {}".format(len(query.repos["baseos"])))
    print("  appstream: {}".format(len(query.repos["appstream"])))

    for addon in query.settings["addons"]:
        print("  {addon} : {num}".format(
            addon=addon,
            num=len(query.repos[addon])
        ))



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

def generate_pages(query, include_content_resolver_breadcrumb=False):
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

    view_id = "view-eln"
    for repo in query.settings["repos"]:
        template_data = {
            "query": query,
            "view_id": view_id,
            "page_repo": repo,
            "include_content_resolver_breadcrumb": include_content_resolver_breadcrumb
        }
        page_name = "repo-split--{view_id}--{repo}".format(
            view_id=view_id,
            repo=repo
        )
        _generate_html_page("repo-split", template_data, page_name, query.settings)
    
    page_name = "repo-split--{view_id}".format(
            view_id=view_id
        )
    template_data = {
            "view_id": view_id,
            "query": query,
            "include_content_resolver_breadcrumb": include_content_resolver_breadcrumb
        }
    _generate_html_page("repo-split-overview", template_data, page_name, query.settings)

    
    log("Outputting data files...")
    for repo in query.settings["repos"]:
        filename = "repo-split--view-eln--{repo}".format(
            repo=repo
        )
        _generate_a_flat_list_file(sorted(query.repos[repo]), filename, query.settings)
    log("  Done!")
    log("")



###############################################################################
### Main ######################################################################
###############################################################################


def main():
    settings = load_settings()

    configs = get_configs(settings)

    if settings["use_cache"]:
        data = load_data("cache_data.json")
    else:
        data = get_data_download_from_content_resolver(settings)
        dump_data("cache_data.json", data)
    
    query = Query(data, configs, settings)
    query.sort_out_pkgs()

    if query.settings["html"]:
        generate_pages(query)
    else:
        output_txt_files(query)
    
    print_summary(query)
    



if __name__ == "__main__":
    main()