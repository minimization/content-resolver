#!/usr/bin/python3

import json, tempfile, os, re, subprocess, jinja2, yaml, argparse, sys
import rpm_showme as showme


# data = {
#     "installations": {
#         "name": Installation,
#         "name": Installation
#     },
#     "releases": [
#         Release,
#         Release
#     ]
# }
# 
# Installation = {
#     "name" : "fedora-30-base",
#     "summary": "Fedora 30 Container Base",
#     "pkgs": [
#         "pkg1",
#         "pkg2"
#     ]
# }
# 
# Release = 31
#
# View = {
#     "name": "apps-fedora-base-containers",
#     "title": "Apps",
#     "subtitle": "on Fedora base container images",
#     "releases": [30, 31],
#     "base_installation": "fedora-30-base",
#     "installations": [
#       "httpd",
#       "nginx",
#       "dnf"
#     ],
# }

def dump_data(path, data):
    with open(path, 'w') as file:
        json.dump(data, file)



def load_data(path):
    with open(path, 'r') as file:
        data = json.load(file)

    return data


def install_and_analyze(data):
    installs = {}

    with tempfile.TemporaryDirectory() as root:
        for _,view in data["views"].items():
            for release in view["releases"]:
                base_name = view["base_installation"]
                base_installroot = "{root}/{release}--{name}".format(root=root, release=str(release), name=base_name)
                base_pkgs = data["installations"][base_name]["pkgs"]

                # Install base
                if not os.path.exists(base_installroot):
                    # Install base if not already
                    subprocess.run(["dnf", "-y", "--installroot", base_installroot, "--releasever", str(release), "install"] + base_pkgs)

                    # Reference in installs
                    install_name = "{release}--{name}".format(release=release,name=base_name)
                    installs[install_name] = {}
                    installs[install_name]["short_name"] = base_name

                # Install additional installations
                for installation in view["installations"]:
                    name = "{installation}-ON-{base}".format(installation=installation, base=base_name)
                    installroot = "{root}/{release}--{name}".format(root=root, release=str(release), name=name)
                    main_pkgs = data["installations"][installation]["pkgs"]

                    if not os.path.exists(installroot):
                        # Copy base into the target location
                        subprocess.run(["cp", "-r", base_installroot, installroot])
    
                        # Install additional packages on the base
                        subprocess.run(["dnf", "-y", "--installroot", installroot, "--releasever", str(release), "install"] + main_pkgs)

                        # Reference in installs
                        install_name = "{release}--{name}".format(release=release,name=name)
                        installs[install_name] = {}
                        installs[install_name]["short_name"] = installation

                # Analyze everything
                for name in installs:
                    installroot = "{root}/{name}".format(root=root, release=str(release), name=name)

                    # Analyze
                    packages = showme.get_packages(installroot)

                    # Save data
                    installs[name]["name"] = name
                    installs[name]["packages"] = packages
            
    return installs


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("where", nargs="?", metavar="WHERE", help="Filename of the output (stdout when not specified).")

    parser.add_argument("directory", help="Yaml file with definitions")

    args = parser.parse_args()


    data = {}
    data["installations"] = {}
    data["views"] = {}

    # === Load configs ===
    for yml_file in os.listdir(args.directory):
        with open(os.path.join(args.directory, yml_file), "r") as stream:
            try:
                document = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                print(exc)

            if "document" not in document or "version" not in document:
                print("Error: invalid data in {}".format(yml_file))
                sys.exit(1)

            if document["document"] == "feedback-pipeline-install":
                name = document["data"]["name"]
                data["installations"][name] = document["data"]

            if document["document"] == "feedback-pipeline-view":
                name = document["data"]["name"]
                data["views"][name] = document["data"]

    # === Analyze ===
    #installs = install_and_analyze(data)
    installs = load_data("data2.json")

    dump_data("data2.json", installs)

    # === Showme! ===
    template_loader = jinja2.FileSystemLoader(searchpath="./templates/")
    template_env = jinja2.Environment(loader=template_loader)

    # homepage
    homepage_template = template_env.get_template("homepage.html")
    homepage = homepage_template.render()
    with open("index.html", "w") as file:
        file.write(homepage)

    # views page
    results_template = template_env.get_template("results.html")
    results = results_template.render(installs=installs, views=data["views"])
    with open("results.html", "w") as file:
        file.write(results)

    # dependency graphs
    for _,install in installs.items():
        short_name = install["short_name"]
        graph = showme.compute_graph(install["packages"])
        dot = showme.graph_to_dot(graph, sizes=True, highlights=data["installations"][short_name]["pkgs"])
        output = showme.dot_to_graph_svg(dot)
        with open("{name}.svg".format(name=install["name"]), "w") as file:
            file.write(output)

    # report tables
    for _,view in data["views"].items():
        for release in view["releases"]:
            base_id = "{release}--{name}".format(release=release,name=view["base_installation"])
            graph = showme.compute_graph(installs[base_id]["packages"])
            
            base_pkg_list = showme.graph_to_package_list(graph, sizes=True)

            base_name = data["installations"][view["base_installation"]]["summary"]

            base_size = 0
            for _, pkg in graph.items():
                base_size += pkg["size"]

            base = {
                "name": base_name,
                "size": showme.size(base_size),
                "packages": base_pkg_list,
                "graph_name": "{name}.svg".format(name=base_id),
            }


            images = []
            for installation in view["installations"]:
                install_name = data["installations"][installation]["summary"]
                install_id = "{release}--{name}-ON-{base}".format(release=release,
                                                            name=data["installations"][installation]["name"],
                                                            base=view["base_installation"])

                packages = installs[install_id]["packages"]

                graph = showme.compute_graph(packages)
                pkg_list = showme.graph_to_package_list(graph, sizes=True)

                pkgs_in_base = list(set(base_pkg_list) & set(pkg_list))
                pkgs_not_in_base = list(set(pkg_list) - set(pkgs_in_base))
                pkgs_in_base.sort()
                pkgs_not_in_base.sort()

                this_size = 0
                for _, pkg in graph.items():
                    this_size += pkg["size"]

                image = {
                    "name" : install_name,
                    "size" : showme.size(this_size),
                    "pkgs_in_base": pkgs_in_base,
                    "pkgs_not_in_base": pkgs_not_in_base,
                    "packages": pkg_list,
                    "graph_name": "{name}.svg".format(name=install_id)
                    }
                images.append(image)

            extra_pkgs = []
            for image in images:
                extra_pkgs += image["pkgs_not_in_base"]
            extra_pkgs = list(set(extra_pkgs))
            extra_pkgs.sort()

            table_report_template = template_env.get_template("table_report.html")
            table_report = table_report_template.render(base=base, images=images, extra_pkgs=extra_pkgs)
            with open("view--{release}-{name}.html".format(release=release,name=view["name"]), "w") as file:
                file.write(table_report)




        
        
        

if __name__ == "__main__":
    main()


#
#data = {}
#data = ["installations"] = {}
#
#
#
#with open("install.yaml", "r") as stream:
#    try:
#        print(yaml.safe_load(stream))
#    except yaml.YAMLError as exc:
#        print(exc)
#


#installs = install_and_analyze(data)

#dump_data("data.json", installs)

#installs = load_data("data.json")
#
#homepage_template = jinja2.Template(get_homepage_template())
#homepage = homepage_template.render(releases=data["releases"])
#with open("index.html", "w") as file:
#    file.write(homepage)
#
#for _,install in installs.items():
#    print("")
#    print("=====================================")
#    print("Name: {}".format(install["name"]))
#    print("Packages:")
#    for _,pkg in install["packages"].items():
#        print("  {}".format(pkg["name"]))
#
