#!/usr/bin/python3

import argparse, yaml, tempfile, os, subprocess, json
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
    cmd += ["install"]
    cmd += packages

    # Do the installation
    subprocess.run(cmd)
    #print(cmd)


def install_and_analyze(configs):
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
                installed_packages = showme.get_packages(installroot)

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

                # What and how to install
                packages = use_case["packages"]
                options = use_case["options"]
                releasever = bases[base_id]["versions"][base_version]["source"]["releasever"]

                # Do the installation
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


def generate_graphs(data, output):

    for _,base_install in data["base_installs"].items():
        graph = showme.compute_graph(base_install["packages"])

        base = data["base_definitions"][base_install["base_id"]]
        base_version = base_install["base_version"]

        # Highlight packages that were specified to be installed
        highlight = base["versions"][base_version]["packages"]

        dot = showme.graph_to_dot(graph, sizes=True, highlights=highlight)
        svg = showme.dot_to_graph_svg(dot)

        filename="graph--{base_id}--{base_version}.svg".format(
                base_id=base_install["base_id"],
                base_version=base_install["base_version"])

        with open(os.path.join(output, filename), "w") as file:
            file.write(svg)
    
    for _,use_case_install in data["use_case_installs"].items():
        base_install_id = "{base_id}:{base_version}".format(
                base_id=use_case_install["base_id"],
                base_version=use_case_install["base_version"])

        base_install = data["base_installs"][base_install_id]
        base_definition = data["base_definitions"][use_case_install["base_id"]]
        use_case_definition = data["use_case_definitions"][use_case_install["use_case_id"]]

        # Full graph shows all packages in the installation
        graph_full = showme.compute_graph(use_case_install["packages"])

        # Simple graph groups the base image into a single node
        group = showme.packages_to_group(base_definition["name"], base_install["packages"])
        graph_simple = showme.compute_graph(use_case_install["packages"], [group])

        # Highlight packages that were specified to be installed
        highlight = use_case_definition["packages"]

        dot_full = showme.graph_to_dot(graph_full, sizes=True, highlights=highlight)
        dot_simple = showme.graph_to_dot(graph_simple, sizes=True, highlights=highlight)

        svg_full = showme.dot_to_graph_svg(dot_full)
        svg_simple = showme.dot_to_graph_svg(dot_simple)

        base_filename = "{use_case_id}--{base_id}--{base_version}.svg".format(
                use_case_id=use_case_install["use_case_id"],
                base_id=use_case_install["base_id"],
                base_version=use_case_install["base_version"])

        filename_full = "graph-full--{base_filename}".format(
                base_filename=base_filename)

        filename_simple = "graph-simple--{base_filename}".format(
                base_filename=base_filename)

        with open(os.path.join(output, filename_full), "w") as file:
            file.write(svg_full)

        with open(os.path.join(output, filename_simple), "w") as file:
            file.write(svg_simple)


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
    #installs = install_and_analyze(configs)
    installs = load_data("installs-cache.json")
    #dump_data("installs-cache.json", installs)

    data = {}
    data["base_definitions"] = configs["bases"]
    data["base_installs"] = installs["bases"]

    data["use_case_definitions"] = configs["use_cases"]
    data["use_case_installs"] = installs["use_cases"]

    generate_graphs(data, args.output)
    
    

if __name__ == "__main__":
    main()
