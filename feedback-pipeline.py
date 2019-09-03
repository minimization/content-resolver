#!/usr/bin/python3

import argparse, yaml, tempfile, os, subprocess, json, jinja2
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


def _install_packages(installroot, packages, options, releasever):
    # DNF flags out of options
    additional_flags = []
    if "no-docs" in options:
        additional_flags.append("--nodocs")
    if "no-weak-deps" in options:
        additional_flags.append("--setopt=install_weak_deps=False")

    # Prepare the installation command
    cmd = []
    cmd += ["dnf", "-y", "--installroot", installroot,
                        "--releasever", releasever]
    cmd += additional_flags

    # If there are no packages, only create the DNF cache
    if packages:
        cmd += ["install"]
        cmd += packages
    else:
        cmd += ["makecache"]

    # Do the installation
    subprocess.run(cmd)
    #print(cmd)


def install_and_load(configs):
    installs = {}
    installs["bases"] = {}
    installs["use_cases"] = {}

    bases = configs["bases"]
    use_cases = configs["use_cases"]

    with tempfile.TemporaryDirectory() as root:
        for base_id, base in bases.items():
            for base_version, base_version_data in base["versions"].items():
                base_install_id = "{id}:{version}".format(
                        id=base_id, version=base_version)

                # Where to install
                dirname = "{id}--{version}".format(
                        id=base_id, version=base_version)
                installroot = os.path.join(root, dirname)

                # What and how to install
                packages = base_version_data["packages"]
                options = base_version_data["options"]
                releasever = base_version_data["source"]["releasever"]

                # Do the installation
                _install_packages(installroot=installroot,
                                  packages=packages,
                                  options=options,
                                  releasever=releasever)

                # Analysis
                # This command would fail on an empty installation
                # so this gets us the right result
                if packages:
                    installed_packages = showme.get_packages(installroot)
                else:
                    installed_packages = {}
                    

                # Save
                base_installation = {}
                base_installation["id"] = base_install_id
                base_installation["base_id"] = base_id
                base_installation["base_version"] = base_version
                base_installation["packages"] = installed_packages
                installs["bases"][base_install_id] = base_installation


        for use_case_id, use_case in use_cases.items():
            for base_install_id in use_case["install_on"]:
                use_case_install_id = "{use_case_id}:{base_install_id}".format(
                        use_case_id=use_case_id,
                        base_install_id=base_install_id)

                # Where to install
                base_id = installs["bases"][base_install_id]["base_id"]
                base_version = installs["bases"][base_install_id]["base_version"]
                dirname = "{use_case_id}--{base_id}--{base_version}".format(
                        use_case_id=use_case_id,
                        base_id=base_id,
                        base_version=base_version)
                installroot = os.path.join(root, dirname)
                base_dirname = "{base_id}--{base_version}".format(
                        base_id=base_id,
                        base_version=base_version)
                base_installroot = os.path.join(root, base_dirname)

                # What and how to install
                packages = use_case["packages"]
                options = use_case["options"]
                releasever = bases[base_id]["versions"][base_version]["source"]["releasever"]

                # First get the base
                subprocess.run(["cp", "-r", base_installroot, installroot])

                # And then do the final installation
                _install_packages(installroot=installroot,
                                  packages=packages,
                                  options=options,
                                  releasever=releasever)

                # Analysis
                installed_packages = showme.get_packages(installroot)

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

        # Related data
        use_case["base_name"] = base_definition["name"]
        use_case["base_version"] = base_version
        use_case["base_id"] = base_install_id

        # Total install size
        total_size = 0
        for _,package in use_case_install["packages"].items():
            total_size += package["size"]
        use_case["total_size"] = total_size

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


def generate_graphs(data, output):
    for _,base in data["bases"].items():
        graph = showme.compute_graph(base["packages"])
        highlights = base["required_package_names"]
        dot = showme.graph_to_dot(graph, sizes=True, highlights=highlights)
        svg = showme.dot_to_graph_svg(dot)

        filename = "graph--{file_id}.svg".format(file_id=base["file_id"])

        with open(os.path.join(output, filename), "w") as file:
            file.write(svg)

    for _,use_case in data["use_cases"].items():
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

        with open(os.path.join(output, filename_full), "w") as file:
            file.write(svg_full)

        with open(os.path.join(output, filename_simple), "w") as file:
            file.write(svg_simple)


        

def generate_reports_by_base(data, output):
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

        other_install_data = []
        for use_case_id in base["use_case_ids"]:
            use_case = data["use_cases"][use_case_id]

            use_case_report_data = {
                "name": use_case["name"],
                "size": showme.size(use_case["total_size"]),
                "pkgs_in_base": use_case["packages_in_base"],
                "pkgs_not_in_base": use_case["packages_not_in_base"],
                "required_pkgs": use_case["required_package_names"],
                "packages": use_case["packages"],
                "file_id": use_case["file_id"]
            } 

            other_install_data.append(use_case_report_data)

        extra_pkgs = []
        for install_data in other_install_data:
            extra_pkgs += install_data["pkgs_not_in_base"]
        extra_pkgs = list(set(extra_pkgs))
        extra_pkgs.sort()

        table_report_template = template_env.get_template("report_by_base.html")
        table_report = table_report_template.render(
                base=base_report_data,
                images=other_install_data,
                extra_pkgs=extra_pkgs)

        filename = "report-by-base--{file_id}.html".format(
                file_id=base["file_id"])

        with open(os.path.join(output, filename), "w") as file:
            file.write(table_report)


def generate_reports_by_use_case(data, output):
    template_loader = jinja2.FileSystemLoader(searchpath="./templates/")
    template_env = jinja2.Environment(loader=template_loader)

    for _,use_case_definition in data["use_case_definitions"].items():
        for base_version in use_case_definition["base_versions"]:
            use_case_report_data = {
                "name": use_case_definition["name"],
                "packages": use_case_definition["packages"],
                "version": base_version
            }

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


                required_package_names = use_case["required_package_names"]
                all_package_names = use_case["package_names"]
                dependencies = list(set(all_package_names) - set(required_package_names))
                dependencies.sort()

                base_report_data = {
                    "name": base["name"],
                    "size": showme.size(use_case["total_size"]),
                    "required_pkgs": required_package_names,
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
                    extra_pkgs=extra_pkgs)

            file_id = "{use_case}--{version}".format(
                    use_case=use_case_definition["id"],
                    version=base_version)
            filename = "report-by-use-case--{file_id}.html".format(
                    file_id=file_id)

            with open(os.path.join(output, filename), "w") as file:
                file.write(table_report)


def generate_reports_bases_releases(data, output):
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
                base_name=base_definition["name"])

        filename = "report-base-releases--{base_id}.html".format(
            base_id=base_definition["id"])

        with open(os.path.join(output, filename), "w") as file:
            file.write(table_report)


def generate_pages(data, output):
    template_loader = jinja2.FileSystemLoader(searchpath="./templates/")
    template_env = jinja2.Environment(loader=template_loader)
    
    # homepage
    homepage_template = template_env.get_template("homepage.html")
    homepage = homepage_template.render()
    with open(os.path.join(output, "index.html"), "w") as file:
        file.write(homepage)

    # views pages
    views_page_template = template_env.get_template("views.html")
    views_page = views_page_template.render(data=data)
    with open(os.path.join(output, "views.html"), "w") as file:
        file.write(views_page)

    views_page_template = template_env.get_template("view_bases_with_use_cases.html")
    views_page = views_page_template.render(data=data)
    with open(os.path.join(output, "view--bases-with-use-cases.html"), "w") as file:
        file.write(views_page)

    views_page_template = template_env.get_template("view_use_cases_on_bases.html")
    views_page = views_page_template.render(data=data)
    with open(os.path.join(output, "view--use-cases-on-bases.html"), "w") as file:
        file.write(views_page)

    views_page_template = template_env.get_template("view_bases_by_releases.html")
    views_page = views_page_template.render(data=data)
    with open(os.path.join(output, "view--bases-by-releases.html"), "w") as file:
        file.write(views_page)

    views_page_template = template_env.get_template("view_use_cases_by_releases.html")
    views_page = views_page_template.render(data=data)
    with open(os.path.join(output, "view--use-cases-by-releases.html"), "w") as file:
        file.write(views_page)


def dump_data(path, data):
    with open(path, 'w') as file:
        json.dump(data, file)


def load_data(path):
    with open(path, 'r') as file:
        data = json.load(file)

    return data


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

    generate_graphs(data, args.output)
    generate_pages(data, args.output)
    generate_reports_by_base(data, args.output)
    generate_reports_by_use_case(data, args.output)
    generate_reports_bases_releases(data, args.output)

    
    

if __name__ == "__main__":
    main()
