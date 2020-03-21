#!/usr/bin/python3

import argparse, yaml, tempfile, os, subprocess, json, jinja2, datetime, copy, re, dnf
import rpm_showme as showme


# Data types:
#
#   Base = {
#       "id": "fedora-container-base",
#       "name": "Fedora Container Base",
#       "versions": {
#           "30": {
#               "packages": [
#                   "package-name",
#                   "package-name"
#               ],
#               "options": [
#                   "no-docs",
#                   "no-weak-deps"
#               ],
#               "source": {
#                   "releasever": 30
#               }
#           },
#           "31": {...}
#       }
#   }
#
#   UseCase = {
#       "id": "unique-id",
#       "name": "Human-friendly name",
#       "packages": [
#           "package-name",
#           "package-name"
#       ],
#       "options": [
#           "no-docs",
#           "no-weak-deps"
#       ],
#       "install_on": [
#           "fedora-container-base:30",
#           "fedora-container-base:31",
#           "empty:30",
#           "empty:31"
#       ]
#   }
#
#   BaseInstallation = {
#       "id": "fedora-container-base:30",
#       "base_id": "fedora-container-base",
#       "base_version": "30",
#       "packages": [
#           Package,    # Package from rpm-showme
#           Package     # Package from rpm-showme
#       ]
#   }
#
#   UseCaseInstallation = {
#       "id": "unique-id:fedora-container-base:30",
#       "use_case_id: "unique-id",
#       "base_id": "fedora-container-base",
#       "base_version": "30,
#       "packages": [
#           Package,    # Package from rpm-showme
#           Package     # Package from rpm-showme
#       ]
#   }

def log(msg):
    print(msg)

def err_log(msg):
    print("ERROR LOG:  {}".format(msg))

def get_configs(directory):
    configs = {}
    configs["bases"] = {}
    configs["use_cases"] = {}

    for yml_file in os.listdir(directory):
        if not yml_file.endswith(".yaml"):
            continue

        with open(os.path.join(directory, yml_file), "r") as file:
            try:
                document = yaml.safe_load(file)
            except yaml.YAMLError as err:
                print(err)

            if not ("document" in document and "version" in document):
                print("Error: {file} is invalid.".format(file=yml_file))

            if document["document"] == "feeback-pipeline-base":
    
                base = {}
                base["id"] = document["data"]["id"]
                base["name"] = document["data"]["name"]
                base["versions"] = {}
                for version,version_data in document["data"]["versions"].items():
                    base["versions"][version] = {}

                    packages = []
                    if "packages" in version_data:
                        packages = version_data["packages"]
                    base["versions"][version]["packages"] = packages

                    options = []
                    if "options" in version_data:
                        options = version_data["options"]
                    base["versions"][version]["options"] = options

                    releasever = version_data["source"]["releasever"]
                    base["versions"][version]["source"] = {}
                    base["versions"][version]["source"]["releasever"] = str(releasever)

                configs["bases"][base["id"]] = base

            if document["document"] == "feeback-pipeline-use-case":

                use_case = {}
                use_case["id"] = document["data"]["id"]
                use_case["name"] = document["data"]["name"]

                packages = []
                if "packages" in document["data"]:
                    packages = document["data"]["packages"]
                use_case["packages"] = packages

                options = []
                if "options" in document["data"]:
                    options = document["data"]["options"]
                use_case["options"] = options

                use_case["install_on"] = document["data"]["install_on"]

                configs["use_cases"][use_case["id"]] = use_case

    return configs

def _install_packages(installroot, pkgs_to_install, options, releasever, cachedir=None, installing_base_image=False, empty_base=False):

    # Explanation of installing_base_image:
    #  - There are two types of installation:
    #       1/ Base image
    #       2/ Use case
    #  - Base image installation needs to start fresh in an empty installrot,
    #    and the result needs to be written into RPMDB, so a use case installation
    #    can then consume it. It can't read it, though, because it doesn't exist.
    #  - Use case installation needs to then read it, but it won't write it again
    #    because for writing it, it would need to download all packages. A high
    #    and unneccessary price to pay. 

    # Some logging
    log("    installroot:     {}".format(installroot))
    log("    cachedir:        {}".format(cachedir))
    log("    number of pkgs:  {}".format(len(pkgs_to_install)))
    log("    empty base:      {}".format(empty_base))

    with dnf.Base() as base:

        if cachedir:
            base.conf.cachedir = cachedir

        base.conf.substitutions['releasever'] = releasever
        if "no-weak-deps" in options:
            base.conf.install_weak_deps = False

        base.conf.tsflags = []
        if "no-docs" in options:
            base.conf.tsflags.append('nodocs')
        # Base image? We're writing things down. This makes the transaction to only write
        # them down without installing to save time and resources.
        if installing_base_image:
            base.conf.tsflags.append('justdb')

        base.conf.installroot = installroot

        log("    Reading repos...")
        base.read_all_repos()

        # Base image? Starting fresh == don't read the local RPMBD.
        # Use case? Load it. But only if the base is not empty, otherwise there would be an OSError
        # about not being able to read the RPMDB (it doesn't get created for empty base images).
        if installing_base_image or empty_base:
            base.fill_sack(load_system_repo=False)
        else:
            base.fill_sack(load_system_repo=True)

        log("    Adding packages to the install list...")
        for pkg in pkgs_to_install:
            # the following can throw dnf.exceptions.MarkingError
            # when the requested package doesn't exist
            try:
                base.install(pkg)
            except dnf.exceptions.MarkingError:
                err_log("Package '{}' could not be found!".format(pkg))
                continue

        # the following can throw dnf.exceptions.DepsolveError
        # if dependencies can't be resolved     
        log("    Resolving dependnecies...")   
        base.resolve()

        if installing_base_image:
            log("    Base image dependency resolution.")
            # Write the result intoi RPMDB.
            # The transaction needs us to download all the packages. :(
            # So let's do that to make it happy.
            log("    Downloading packages...")
            base.download_packages(base.transaction.install_set)
            log("    Running transaction...")
            base.do_transaction()
            # The query in this case is just the resolve install set. Easy peasy!
            log("    Creating the query object...")
            query = base.sack.query().filterm(pkg=base.transaction.install_set)
        
        else:
            log("    Use case dependency resolution.")
            # The query here is the combinaton of the pre-installed base image
            # and the resolved install set. So let's get both of those,
            # merge them, and make a query out of that!
            log("    Creating the query object...")
            query_base = base.sack.query()
            base_installed = set(query_base.installed())
            use_case_installed = set(base.transaction.install_set)
            all_installed = set.union(base_installed, use_case_installed)
            query = base.sack.query().filterm(pkg=all_installed)

        # And finally, the reason we went through all that trouble, was to get
        # some interesting data! So let's finally get it.
        log("    Saving package data...")
        packages = {}
        for pkg in query:
            package = {}
            package["name"] = pkg.name
            package["epoch"] = pkg.epoch
            package["version"] = pkg.version
            package["release"] = pkg.release
            package["arch"] = pkg.arch
            package["nevra"] = str(pkg)
            package["size"] = pkg.installsize
            package["requires"] = []
            package["requires_resolved"] = []
            package["recommends"] = []
            package["recommends_resolved"] = []
            package["suggests"] = []
            package["suggests_resolved"] = []

            for req in pkg.requires:
                package["requires"].append(str(req))

            for req in pkg.recommends:
                package["recommends"].append(str(req))

            for req in pkg.suggests:
                package["suggests"].append(str(req))

            deps = query.filter(provides=pkg.requires)
            for dep in deps:
                package["requires_resolved"].append(dep.name)

            deps = query.filter(provides=pkg.recommends)
            for dep in deps:
                package["recommends_resolved"].append(dep.name)

            deps = query.filter(provides=pkg.suggests)
            for dep in deps:
                package["suggests_resolved"].append(dep.name)

            packages[package["name"]] = package

        log("    DONE!")
        log("")
    return packages


def install_and_load(configs):
    timestamp = str(datetime.datetime.now().strftime("%d/%m/%Y %H:%M"))

    installs = {}
    installs["bases"] = {}
    installs["use_cases"] = {}
    installs["timestamp"] = timestamp

    bases = configs["bases"]
    use_cases = configs["use_cases"]

    with tempfile.TemporaryDirectory() as root:
        # Step 1: Let's do all the base images!
        for base_id, base in bases.items():
            for base_version, base_version_data in base["versions"].items():
                base_install_id = "{id}:{version}".format(
                        id=base_id, version=base_version)

                # Where to install
                # We're writing data in the installroot, so each base image
                # needs its own space.
                dirname = "{id}--{version}".format(
                        id=base_id, version=base_version)
                installroot = os.path.join(root, dirname)

                # What and how to install
                packages = base_version_data["packages"]
                options = base_version_data["options"]
                releasever = base_version_data["source"]["releasever"]

                # DNF cache directory
                # Loading repodata only once speeds things up quite a bit
                cachedir = os.path.join(root, "dnf-cache-{releasever}".format(releasever=releasever))

                # Log!
                log("Analyzing a base image: {name} {release} ({number_pkgs} packages)".format(
                        name=base["name"],
                        release=base_version,
                        number_pkgs=len(packages)
                ))

                # Do the installation
                installed_packages = _install_packages(installroot=installroot,
                                                       pkgs_to_install=packages,
                                                       options=options,
                                                       releasever=releasever,
                                                       cachedir=cachedir,
                                                       installing_base_image=True)

                # Save
                base_installation = {}
                base_installation["id"] = base_install_id
                base_installation["base_id"] = base_id
                base_installation["base_version"] = base_version
                base_installation["packages"] = installed_packages
                installs["bases"][base_install_id] = base_installation

        # Step 2: Let's do all the use cases on top of the base images!
        for use_case_id, use_case in use_cases.items():
            for base_install_id in use_case["install_on"]:
                use_case_install_id = "{use_case_id}:{base_install_id}".format(
                        use_case_id=use_case_id,
                        base_install_id=base_install_id)

                # Where to install
                # Since we're not writing anything into the installroot,
                # let's just use the base image's installroot!
                base_id = installs["bases"][base_install_id]["base_id"]
                base_version = installs["bases"][base_install_id]["base_version"]
                base_dirname = "{base_id}--{base_version}".format(
                        base_id=base_id,
                        base_version=base_version)
                base_installroot = os.path.join(root, base_dirname)

                # What and how to install
                packages = use_case["packages"]
                options = use_case["options"]
                releasever = bases[base_id]["versions"][base_version]["source"]["releasever"]

                # DNF cache directory
                # Loading repodata only once speeds things up quite a bit
                cachedir = os.path.join(root, "dnf-cache-{releasever}".format(releasever=releasever))
                
                # Log!
                log("Analyzing a use case: {name} on {base_name} {release} ({number_pkgs} packages)".format(
                        name=use_case["name"],
                        base_name=bases[base_id]["name"],
                        release=base_version,
                        number_pkgs=len(packages)
                ))

                # Empty base?
                number_of_base_pkgs = len(installs["bases"][base_install_id]["packages"])
                empty_base = False
                if number_of_base_pkgs == 0:
                    empty_base = True

                # Do the installation
                installed_packages = _install_packages(installroot=base_installroot,
                                                       pkgs_to_install=packages,
                                                       options=options,
                                                       releasever=releasever,
                                                       cachedir=cachedir,
                                                       installing_base_image=False,
                                                       empty_base=empty_base)

                # Save
                use_case_installation = {}
                use_case_installation["id"] = use_case_install_id
                use_case_installation["use_case_id"] = use_case_id
                use_case_installation["base_id"] = base_id
                use_case_installation["base_version"] = base_version
                use_case_installation["packages"] = installed_packages
                installs["use_cases"][use_case_install_id] = use_case_installation
                
    return installs

def get_data(configs, installs):
    
    data = {}

    data["timestamp"] = installs["timestamp"]

    data["base_definitions"] = configs["bases"]
    data["base_installs"] = installs["bases"]

    data["use_case_definitions"] = configs["use_cases"]
    data["use_case_installs"] = installs["use_cases"]

    # Store base info in use_case_definitions
    for _, use_case_definition in data["use_case_definitions"].items():
        base_versions = []
        base_versions_set = set()
        base_ids = []
        base_ids_set = set()
        for base_id in use_case_definition["install_on"]:
            base_ids_set.add(base_id.split(":")[0])
            base_versions_set.add(base_id.split(":")[1])
        base_versions = list(base_versions_set)
        base_versions.sort()
        base_ids = list(base_ids_set)
        base_ids.sort()
        use_case_definition["base_versions"] = base_versions
        use_case_definition["base_ids"] = base_ids

        base_ids = use_case_definition["install_on"]
        base_names = {}
        for base_id in base_ids:
            base_name, base_version = base_id.split(":")
            if base_version not in base_names:
                base_names[base_version] = []            
            base_names[base_version].append(base_name)
        use_case_definition["base_names"] = base_names

    # Data focused on use cases
    use_cases = {}
    for _, use_case_install in data["use_case_installs"].items():
        use_case_definition_id = use_case_install["use_case_id"]
        use_case_definition = data["use_case_definitions"][use_case_definition_id]
    
        base_install_id = "{base_id}:{base_version}".format(
                base_id=use_case_install["base_id"],
                base_version=use_case_install["base_version"])
        base_install = data["base_installs"][base_install_id]
        base_id = use_case_install["base_id"]
        base_definition = data["base_definitions"][base_id]
        base_version = base_install["base_version"]

        # Define a new entity holding all info about use cases
        use_case = {}

        # Identity and names
        use_case["id"] = use_case_install["id"]
        use_case["name"] = use_case_definition["name"]
        use_case["file_id"] = "{use_case_id}--{base_id}--{base_version}".format(
                use_case_id=use_case_definition["id"],
                base_id=base_definition["id"],
                base_version=base_version)
        use_case["definition_id"] = use_case_definition["id"]

        # Related data
        use_case["base_name"] = base_definition["name"]
        use_case["base_version"] = base_version
        use_case["base_id"] = base_install_id
        use_case["base_definition_id"] = use_case_install["base_id"]

        # Total install size
        total_size = 0
        for _,package in use_case_install["packages"].items():
            total_size += package["size"]
        use_case["total_size"] = total_size

        # Total install size history TODO
        size_history = None
        use_case["size_history"] = size_history

        # Packages
        use_case["packages"] = use_case_install["packages"]
        package_names = []
        for _,package in use_case_install["packages"].items():
            package_names.append(package["name"])
        use_case["package_names"] = package_names
        use_case["required_package_names"] = use_case_definition["packages"]

        # Advanced packages
        base_package_names = []
        for _,package in base_install["packages"].items():
            base_package_names.append(package["name"])
        packages_in_base = list(set(base_package_names) & set(package_names))
        packages_not_in_base = list(set(package_names) - set(packages_in_base))
        packages_in_base.sort()
        packages_not_in_base.sort()
        use_case["packages_in_base"] = packages_in_base
        use_case["packages_not_in_base"] = packages_not_in_base

        use_cases[use_case["id"]] = use_case

    data["use_cases"] = use_cases

    # Data focued on bases
    bases = {}
    for _, base_install in data["base_installs"].items():
        base_definition = data["base_definitions"][base_install["base_id"]]
        base_version = base_install["base_version"]
        
        # Define a new entity holding all the info about bases
        base = {}
        base["id"] = base_install["id"]
        base["name"] = base_definition["name"]
        base["version"] = base_version
        base["file_id"] = "{id}--{version}".format(
                id=base_definition["id"],
                version=base_version)
        base["definition_id"] = base_definition["id"]

        # Related data
        use_case_ids = []
        for _,use_case in data["use_cases"].items():
            if use_case["base_id"] == base["id"]:
                use_case_ids.append(use_case["id"])
        base["use_case_ids"] = use_case_ids

        # Total install size
        total_size = 0
        for _,package in base_install["packages"].items():
            total_size += package["size"]
        base["total_size"] = total_size

        # Total install size history TODO
        size_history = None
        base["size_history"] = size_history

        # Packages
        base["packages"] = base_install["packages"]
        package_names = []
        for _,package in base_install["packages"].items():
            package_names.append(package["name"])
        base["package_names"] = package_names
        base["required_package_names"] = \
                base_definition["versions"][base_version]["packages"]

        bases[base["id"]] = base
        
    data["bases"] = bases

    return data


def _add_title_to_use_case_svg(svg, title_data):

    try:
        title_code = """
  <text
     id="text7664"
     y="19.384439"
     x="9.9055691"
     style="font-style:normal;font-weight:normal;font-size:12.49485588px;line-height:125%;font-family:sans-serif;letter-spacing:0px;word-spacing:0px;fill:#000000;fill-opacity:1;stroke:none;stroke-width:0.3123714px;stroke-linecap:butt;stroke-linejoin:miter;stroke-opacity:1"
     xml:space="preserve"><tspan
       style="font-size:12.00258541px;stroke-width:0.3123714px"
       y="19.384439"
       x="9.9055691"
       id="tspan7666"><a href="{use_case_url}"><tspan
   style="font-weight:bold;font-size:12.00258541px;fill:#0066ff;stroke-width:0.3123714px"
   id="tspan7668">{use_case_name}</tspan></a> on <a href="{base_url}"><tspan
   style="font-weight:bold;font-size:12.00258541px;fill:#0066ff;stroke-width:0.3123714px"
   id="tspan7676">{base_name}</tspan></a> (Fedora {version})</tspan></text>
  <text
     xml:space="preserve"
     style="font-style:normal;font-weight:normal;font-size:11.85299969px;line-height:125%;font-family:sans-serif;letter-spacing:0px;word-spacing:0px;fill:#000000;fill-opacity:1;stroke:none;stroke-width:0.296325px;stroke-linecap:butt;stroke-linejoin:miter;stroke-opacity:1"
     x="9.9365959"
     y="58.520042"
     id="text8235"><tspan
       id="tspan8237"
       x="9.9365959"
       y="58.520042"
       style="stroke-width:0.296325px;font-size:10.80232704px;fill:#666666">Generated by <a href="./"><tspan
   style="fill:#3771c8"
   id="tspan8255">Feedback Pipeline</tspan></a></tspan></text>
  <text
     xml:space="preserve"
     style="font-style:normal;font-weight:normal;font-size:12.49485588px;line-height:125%;font-family:sans-serif;letter-spacing:0px;word-spacing:0px;fill:#000000;fill-opacity:1;stroke:none;stroke-width:0.3123714px;stroke-linecap:butt;stroke-linejoin:miter;stroke-opacity:1"
     x="9.9055691"
     y="38.974205"
     id="text8245"><a href="{expand_collapse_url}"><tspan
       id="tspan8247"
       x="9.9055691"
       y="38.974205"
       style="font-weight:normal;font-size:12.00258541px;fill:#0066ff;stroke-width:0.3123714px">{expand_collapse_text}</tspan></a></text>
""".format(
        use_case_url=title_data["use_case_url"],
        use_case_name=title_data["use_case_name"],
        base_url=title_data["base_url"],
        base_name=title_data["base_name"],
        version=title_data["version"],
        expand_collapse_url=title_data["expand_collapse_url"],
        expand_collapse_text=title_data["expand_collapse_text"]
    )

    except KeyError:
        return svg

    return svg.split("</svg>")[0] + title_code + "\n</svg>\n"


def _add_title_to_base_svg(svg, title_data):

    try:
        title_code = """
  <text
     
     id="text7664"
     y="19.384439"
     x="9.9055691"
     style="font-style:normal;font-weight:normal;font-size:12.49485588px;line-height:125%;font-family:sans-serif;letter-spacing:0px;word-spacing:0px;fill:#000000;fill-opacity:1;stroke:none;stroke-width:0.3123714px;stroke-linecap:butt;stroke-linejoin:miter;stroke-opacity:1"
     xml:space="preserve"><tspan
       style="font-size:12.00258541px;stroke-width:0.3123714px"
       y="19.384439"
       x="9.9055691"
       id="tspan7666"
       ><a href="{base_url}"><tspan
   style="font-weight:bold;font-size:12.00258541px;fill:#0066ff;stroke-width:0.3123714px"
   id="tspan7668">{base_name}</tspan></a> (Fedora {version})</tspan></text>
  <text
     xml:space="preserve"
     style="font-style:normal;font-weight:normal;font-size:11.85299969px;line-height:125%;font-family:sans-serif;letter-spacing:0px;word-spacing:0px;fill:#000000;fill-opacity:1;stroke:none;stroke-width:0.296325px;stroke-linecap:butt;stroke-linejoin:miter;stroke-opacity:1"
     x="9.9365959"
     y="36.915382"
     id="text8235"><tspan
       
       id="tspan8237"
       x="9.9365959"
       y="36.915382"
       style="font-size:10.80232716px;fill:#666666;stroke-width:0.296325px">Generated by <a href="./"><tspan
   style="fill:#3771c8"
   id="tspan8255">Feedback Pipeline</tspan></a></tspan></text>
""".format(
        base_url=title_data["base_url"],
        base_name=title_data["base_name"],
        version=title_data["version"]
    )

    except KeyError:
        return svg

    return svg.split("</svg>")[0] + title_code + "\n</svg>\n"


def generate_graphs(data, output):
    log("Generating {} base graphs".format(len(data["bases"])))

    for _,base in data["bases"].items():
        log("  {} ({})".format(base["name"], base["version"]))

        graph = showme.compute_graph(base["packages"])
        highlights = base["required_package_names"]
        dot = showme.graph_to_dot(graph, sizes=True, highlights=highlights)
        svg = showme.dot_to_graph_svg(dot)

        filename = "graph--{file_id}.svg".format(file_id=base["file_id"])

        title_data = {}
        title_data["base_url"] = "report-base-definition--{file_id}.html".format(file_id=base["file_id"])
        title_data["base_name"] = base["name"]
        title_data["version"] = base["version"]

        svg = _add_title_to_base_svg(svg, title_data)

        with open(os.path.join(output, filename), "w") as file:
            file.write(svg)

    log("Generating {} use case graphs".format(len(data["use_cases"])))

    for _,use_case in data["use_cases"].items():
        log("  {} on {} ({})".format(
                use_case["name"], use_case["base_name"], use_case["base_version"]))

        # Full graph shows all packages in the installation
        graph_full = showme.compute_graph(use_case["packages"])
        
        # Simple graph groups the base image into a single node
        base_packages = data["bases"][use_case["base_id"]]["packages"]
        group = showme.packages_to_group(use_case["base_name"], base_packages)
        graph_simple = showme.compute_graph(use_case["packages"], [group])

        highlights = use_case["required_package_names"]
        dot_full = showme.graph_to_dot(graph_full, sizes=True, highlights=highlights)
        dot_simple = showme.graph_to_dot(graph_simple, sizes=True, highlights=highlights)

        svg_full = showme.dot_to_graph_svg(dot_full)
        svg_simple = showme.dot_to_graph_svg(dot_simple)

        filename_full = "graph--{file_id}.svg".format(file_id=use_case["file_id"])
        filename_simple = "graph-simple--{file_id}.svg".format(file_id=use_case["file_id"])

        title_data = {}
        title_data["use_case_url"] = "report-use-case-definition--{use_case_id}--{version}.html".format(
            use_case_id=use_case["definition_id"],
            version=use_case["base_version"])
        title_data["use_case_name"] = use_case["name"]
        title_data["base_url"] = "report-base-definition--{file_id}.html".format(file_id=data["bases"][use_case["base_id"]]["file_id"])
        title_data["base_name"] = use_case["base_name"]
        title_data["version"] = use_case["base_version"]
        title_data["expand_collapse_url"] = filename_simple
        title_data["expand_collapse_text"] = "Collapse base image"

        svg_full = _add_title_to_use_case_svg(svg_full, title_data)

        title_data["expand_collapse_url"] = filename_full
        title_data["expand_collapse_text"] = "Expand base image"

        svg_simple = _add_title_to_use_case_svg(svg_simple, title_data)

        

        with open(os.path.join(output, filename_full), "w") as file:
            file.write(svg_full)

        with open(os.path.join(output, filename_simple), "w") as file:
            file.write(svg_simple)


        

def generate_reports_by_base(data, output):
    log("Generating reports: Bases with use cases")

    template_loader = jinja2.FileSystemLoader(searchpath="./templates/")
    template_env = jinja2.Environment(loader=template_loader)

    for _,base in data["bases"].items():
        base_report_data = {
            "name": base["name"],
            "version": base["version"],
            "size": showme.size(base["total_size"]),
            "packages": base["package_names"],
            "file_id": base["file_id"]
        }

        pkg_sizes = {}
        pkg_sizes_num = {}

        # Get sizes of all packags in the base
        for _,pkg in base["packages"].items():
            if pkg["name"] not in pkg_sizes:
                pkg_sizes[pkg["name"]] = showme.size(pkg["size"])
                pkg_sizes_num[pkg["name"]] = pkg["size"]
                

        use_case_data = []
        for use_case_id in base["use_case_ids"]:
            use_case = data["use_cases"][use_case_id]

            # Get sizes of all other packages
            for _,pkg in use_case["packages"].items():
                if pkg["name"] not in pkg_sizes:
                    pkg_sizes[pkg["name"]] = showme.size(pkg["size"])
                    pkg_sizes_num[pkg["name"]] = pkg["size"]

            use_case_report_data = {
                "name": use_case["name"],
                "definition_id": use_case["definition_id"],
                "size": showme.size(use_case["total_size"]),
                "pkgs_in_base": use_case["packages_in_base"],
                "pkgs_not_in_base": use_case["packages_not_in_base"],
                "required_pkgs": use_case["required_package_names"],
                "packages": use_case["packages"],
                "file_id": use_case["file_id"]
            } 

            use_case_data.append(use_case_report_data)
        
        # Sort use cases by name
        use_case_data_sorted = sorted(use_case_data, key=lambda k: k["name"])

        extra_pkgs = []
        for install_data in use_case_data_sorted:
            extra_pkgs += install_data["pkgs_not_in_base"]
        extra_pkgs = list(set(extra_pkgs))
        extra_pkgs.sort()

        table_report_template = template_env.get_template("report_by_base.html")
        table_report = table_report_template.render(
                base=base_report_data,
                images=use_case_data_sorted,
                extra_pkgs=extra_pkgs,
                pkg_sizes=pkg_sizes,
                pkg_sizes_num=pkg_sizes_num,
                timestamp=data["timestamp"])

        filename = "report-by-base--{file_id}.html".format(
                file_id=base["file_id"])

        with open(os.path.join(output, filename), "w") as file:
            file.write(table_report)


def generate_reports_by_use_case(data, output):
    log("Generating reports: Use cases on bases")

    template_loader = jinja2.FileSystemLoader(searchpath="./templates/")
    template_env = jinja2.Environment(loader=template_loader)

    for _,use_case_definition in data["use_case_definitions"].items():
        for base_version in use_case_definition["base_versions"]:
            use_case_report_data = {
                "name": use_case_definition["name"],
                "packages": use_case_definition["packages"],
                "version": base_version
            }

            pkg_sizes = {}
            pkg_sizes_num = {}


            other_install_data = []
            for base_definition_id in use_case_definition["base_ids"]:
                base_id = "{base_definition_id}:{base_version}".format(
                        base_definition_id=base_definition_id,
                        base_version=base_version)
                use_case_id = "{use_case_definition_id}:{base_id}".format(
                        use_case_definition_id=use_case_definition["id"],
                        base_id=base_id)

                base = data["bases"][base_id]
                use_case = data["use_cases"][use_case_id]

                # Get sizes of all packags in the base
                for _,pkg in use_case["packages"].items():
                    if pkg["name"] not in pkg_sizes:
                        pkg_sizes[pkg["name"]] = showme.size(pkg["size"])
                        pkg_sizes_num[pkg["name"]] = pkg["size"]

                required_package_names = use_case["required_package_names"]
                all_package_names = use_case["package_names"]
                dependencies = list(set(all_package_names) - set(required_package_names))
                dependencies.sort()

                base_report_data = {
                    "name": base["name"],
                    "definition_id": base["definition_id"],
                    "size": showme.size(use_case["total_size"]),
                    "required_pkgs": required_package_names,
                    "pkgs_in_base": use_case["packages_in_base"],
                    "dependencies": dependencies,
                    "packages": use_case["packages"],
                    "file_id": use_case["file_id"]
                }
                other_install_data.append(base_report_data)

            extra_pkgs = []
            for install_data in other_install_data:
                extra_pkgs += install_data["dependencies"]
            extra_pkgs = list(set(extra_pkgs))
            extra_pkgs.sort()

            table_report_template = template_env.get_template("report_by_use_case.html")
            table_report = table_report_template.render(
                    base=use_case_report_data,
                    images=other_install_data,
                    extra_pkgs=extra_pkgs,
                    pkg_sizes=pkg_sizes,
                    pkg_sizes_num=pkg_sizes_num,
                    timestamp=data["timestamp"])

            file_id = "{use_case}--{version}".format(
                    use_case=use_case_definition["id"],
                    version=base_version)
            filename = "report-by-use-case--{file_id}.html".format(
                    file_id=file_id)

            with open(os.path.join(output, filename), "w") as file:
                file.write(table_report)


def generate_reports_bases_releases(data, output):
    log("Generating reports: Bases by releases")

    template_loader = jinja2.FileSystemLoader(searchpath="./templates/")
    template_env = jinja2.Environment(loader=template_loader)

    for _,base_definition in data["base_definitions"].items():
        report_data = []
        for base_version in base_definition["versions"]:
            base_id = "{base_definition_id}:{base_version}".format(
                    base_definition_id=base_definition["id"],
                    base_version=base_version)
            base = data["bases"][base_id]
    
            base_report_data = {
                "version": base_version,
                "size": showme.size(base["total_size"]),
                "required_pkgs": base["required_package_names"],
                "packages": base["packages"],
                "file_id": base["file_id"]
            }
            report_data.append(base_report_data)

        all_packages = []
        for base_report_data in report_data:
            all_packages += base_report_data["packages"].keys()
        all_packages = list(set(all_packages))
        all_packages.sort()

        table_report_template = template_env.get_template("report_base_releases.html")
        table_report = table_report_template.render(
                report_data=report_data,
                all_packages=all_packages,
                base_name=base_definition["name"],
                size_function=showme.size,
                timestamp=data["timestamp"])

        filename = "report-base-releases--{base_id}.html".format(
            base_id=base_definition["id"])

        with open(os.path.join(output, filename), "w") as file:
            file.write(table_report)


def load_historic_data(data, output):

    log("Loading historic size data from disk")
    directory = os.path.join(output, "history")

    all_filenames = os.listdir(directory)
    all_filenames.sort()

    historic_data = {}

    #FIXME: This only needs to load a limited number of data.
    #       But for now it's ok, we don't have that much. (This is going to end up badly, right?)

    for filename in all_filenames:
        with open(os.path.join(directory, filename), "r") as file:
            document = json.load(file)

            date = datetime.datetime.strptime(document["timestamp"],"%d/%m/%Y %H:%M")
            key = datetime.datetime.strftime(date, "%Y-%m-%d-%H%M")

            historic_data[key] = document

    return historic_data


def get_historic_chart_data(historic_data, data):
    log("Generating historic size chart data")

    for base_id, base in data["bases"].items():
        size_history = {}
        for timestamp in sorted(historic_data):
            historic_data_instance = historic_data[timestamp]
            try:
                size = historic_data_instance["bases"][base_id]["total_size"]
            except KeyError:
                continue

            size_history[timestamp] = size
        
        base["size_history"] = size_history

    for use_case_id, use_case in data["use_cases"].items():
        size_history = {}
        for timestamp in sorted(historic_data):
            historic_data_instance = historic_data[timestamp]
            try:
                size = historic_data_instance["use_cases"][use_case_id]["total_size"]
            except KeyError:
                continue

            size_history[timestamp] = size

        use_case["size_history"] = size_history

def generate_reports_bases_definitions(data, output):
    log("Generating reports: Base definitions")

    template_loader = jinja2.FileSystemLoader(searchpath="./templates/")
    template_env = jinja2.Environment(loader=template_loader)

    for base_id, base_definition in data["base_definitions"].items():
        for base_version in base_definition["versions"]:
            report_template = template_env.get_template("report_base_definition.html")
            base = data["bases"][base_id+":"+base_version]

            chart_data_x = []
            chart_data_y = []
            for x, y in base["size_history"].items():
                chart_data_x.append(x)
                chart_data_y.append(round(y / 1024 / 1024,2))
            report = report_template.render(
                    base_definition=base_definition,
                    base_version=base_version,
                    base=base,
                    chart_data_x=chart_data_x,
                    chart_data_y=chart_data_y,
                    size_function=showme.size,
                    data=data)

            filename = "report-base-definition--{base_id}--{base_version}.html".format(
                base_id=base_definition["id"], base_version=base_version)

            with open(os.path.join(output, filename), "w") as file:
                file.write(report)


def generate_reports_use_cases_definitions(data, output):
    log("Generating reports: use_case definitions")

    template_loader = jinja2.FileSystemLoader(searchpath="./templates/")
    template_env = jinja2.Environment(loader=template_loader)

    for _,use_case_definition in data["use_case_definitions"].items():
        
        for base_version, base_ids in use_case_definition["base_names"].items():

            use_cases = []
            for base_id in base_ids:
                use_case = data["use_cases"]["{use_case}:{base}:{version}".format(
                    use_case=use_case_definition["id"],
                    base=base_id,
                    version=base_version
                )]
                use_cases.append(use_case)
            
            graph_timestamps_set = set()
            for use_case in use_cases:
                for timestamp, size in use_case["size_history"].items():
                    graph_timestamps_set.add(timestamp)
            
            graph_sizes = {}
            graph_timestamps = sorted(list(graph_timestamps_set))
            for timestamp in graph_timestamps:
                for use_case in use_cases:
                    base_id = use_case["base_definition_id"]

                    if base_id not in graph_sizes:
                        graph_sizes[base_id] = []

                    # The size is string because I need to be able to carry either
                    # the number or "null" that will be put into Javascript code
                    # in the jinja2 template. I know it's horrible.
                    if timestamp in use_case["size_history"]:
                        size_bytes = use_case["size_history"][timestamp]
                        size = str(round(size_bytes / 1024 / 1024,2))
                    else:
                        size = "null"

                    graph_sizes[base_id].append(size)
            
            report_template = template_env.get_template("report_use_case_definition.html")
            report = report_template.render(
                    use_case_definition=use_case_definition,
                    base_version=base_version,
                    graph_timestamps=graph_timestamps,
                    graph_sizes=graph_sizes,
                    data=data)

            filename = "report-use-case-definition--{use_case_id}--{base_version}.html".format(
                use_case_id=use_case_definition["id"],
                base_version=base_version
            )

            with open(os.path.join(output, filename), "w") as file:
                file.write(report)


def generate_pages(data, output):
    log("Generating common pages")

    data = sort_definitions(copy.deepcopy(data))
    template_loader = jinja2.FileSystemLoader(searchpath="./templates/")
    template_env = jinja2.Environment(loader=template_loader)
  
    pages = [
        # Template name                             # Output file
        ("homepage.html",                           "index.html"),
        ("view.html",                               "views.html"),
        ("view_bases_definitions.html",             "view--bases-definitions.html"),
        ("view_bases_compare_releases.html",        "view--bases-by-releases.html"),
        ("view_use_cases_definitions.html",         "view--use-cases-definitions.html"),
        ("view_use_cases_compare_bases.html",       "view--use-cases-on-bases.html"),
        ("view_use_cases_compare_releases.html",    "view--use-cases-by-releases.html"),
        ("view_use_cases_use_cases_by_base.html",   "view--bases-with-use-cases.html"),
    ]


    for template_name, output_file in pages:
        template = template_env.get_template(template_name)
        page = template.render(data=data)
        with open(os.path.join(output, output_file), "w") as file:
            file.write(page)
    

    src_static_dir = os.path.join("templates", "_static")
    output_static_dir = os.path.join(output)
    subprocess.run(["cp", "-R", src_static_dir, output_static_dir])




def dump_data(path, data):
    with open(path, 'w') as file:
        json.dump(data, file)


def load_data(path):
    with open(path, 'r') as file:
        data = json.load(file)

    return data


def sort_definitions(definitions):
    definitions["base_definitions"] = sort_data(definitions["base_definitions"])
    definitions["use_case_definitions"] = sort_data(definitions["use_case_definitions"])
    return definitions


def sort_data(data):
    names = {}
    for data_key, data_value in data.items():
        data_dict = {}
        data_dict[data_key] = data_value
        names[data_value["name"]] = data_dict
    sorted_list = sorted(names.items())
    soted_data = []
    for sorted_taple in sorted_list:
        for _, item_value in sorted_taple[1].items():
            soted_data.append(item_value)
    return soted_data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="Directory with YAML configuration files. Only files ending with '.yaml' are accepted.")
    parser.add_argument("output", help="Directory to contain the output.")
    args = parser.parse_args()

    configs = get_configs(args.input)
    #dump_data("installs-cache.json", installs)
    installs = install_and_load(configs)
    #installs = load_data("installs-cache.json")

    data = get_data(configs, installs)

    dump_data(os.path.join(args.output, "installs.json"), installs)
    dump_data(os.path.join(args.output, "data.json"), data)

    date = datetime.datetime.strptime(data["timestamp"],"%d/%m/%Y %H:%M")
    filedate = datetime.datetime.strftime(date, "%Y-%m-%d-%H%M")
    filename = "data-{}.json".format(filedate)
    dump_data(os.path.join(args.output, "history", filename), data)

    historic_data = load_historic_data(data, args.output)
    get_historic_chart_data(historic_data, data)

    generate_graphs(data, args.output)
    generate_pages(data, args.output)
    generate_reports_by_base(data, args.output)
    generate_reports_by_use_case(data, args.output)
    generate_reports_bases_releases(data, args.output)
    generate_reports_bases_definitions(data, args.output)
    generate_reports_use_cases_definitions(data, args.output)

    
    

if __name__ == "__main__":
    main()
