#!/usr/bin/python3

# This file is part of dependency-visualiser hosted at:
#   https://pagure.io/minimization/dependency-visualiser
#
# Copyright (C) 2019 Adam Samalik <asamalik@redhat.com>
#
# This program is free software.
# For more information on the license, see LICENSE.
# For more information on free software, see <https://www.gnu.org/philosophy/free-sw.en.html>.

import dnf, json, subprocess, tempfile, argparse, jinja2


# === Data Structures ===
#
# data = {
#     "packages": {
#         "package1": Package,
#         "package2": Package
#     },
#     "groups": {
#         "group1": Group,
#         "group2": Group
#     },
#     "graph": {
#         "node1": Node,
#         "node2": Node
#     }
# }
# 
# 
# Package = {
#     "name": "package",
#     "epoch": "",
#     "version": "2.4",
#     "release": "3.fc36",
#     "arch": "arm",
#     "nevra": "package-2.4-3.fc36.arm"
#     "size": 432423,
#     "requires": [
#         "capability 1",
#         "capability 2"
#     ],
#     "requires_resolved": [
#         "package2",
#         "package3"
#     ],
#     "recommends": [
#         "capability 1",
#         "capability 2"
#     ],
#     "recommends_resolved": [
#         "package2",
#         "package3"
#     ],
#     "suggests": [
#         "capability 1",
#         "capability 2"
#     ],
#     "suggests_resolved": [
#         "package2",
#         "package3"
#     ],
# }
# 
# Group = {
#     "name": "group",
#     "size": 423432423,
#     "packages": [
#         "package1",
#         "package2"
#     ],
#     "requires": [
#         "capability 1",
#         "capability 2"
#     ],
#     "requires_resolved": [
#         "package2",
#         "package3"
#     ],
#     "recommends": [
#         "capability 1",
#         "capability 2"
#     ],
#     "recommends_resolved": [
#         "package2",
#         "package3"
#     ],
#     "suggests": [
#         "capability 1",
#         "capability 2"
#     ],
#     "suggests_resolved": [
#         "package2",
#         "package3"
#     ],
# }
# 
# Node = {
#     "name": "node",
#     "size": 323232,
#      "type": "TYPE",   # package, group
#     "dependencies": [
#         "node1",
#         "node2",
#     ],
#     "weak_dependencies": [
#         "node1",
#         "node2",
#     ],
# }


def _create_packages_structure(installed, query):
    # Make it into a list of my Package structures
    packages = {}
    for pkg in installed:
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

        deps = query.installed()
        deps = deps.filter(provides=pkg.requires)
        for dep in deps:
            package["requires_resolved"].append(dep.name)

        deps = query.installed()
        deps = deps.filter(provides=pkg.recommends)
        for dep in deps:
            package["recommends_resolved"].append(dep.name)

        deps = query.installed()
        deps = deps.filter(provides=pkg.suggests)
        for dep in deps:
            package["suggests_resolved"].append(dep.name)

        packages[package["name"]] = package

    return packages

def load_packages_from_path(root="/", releasever=None):

    # Look at the system and get a list of all installed RPM packages
    # in the as a list of DNF Package objects
    base = dnf.Base()
    if releasever:
        base.conf.substitutions['releasever'] = releasever
    base.conf.installroot = root
    base.fill_sack(load_available_repos=False)
    query = base.sack.query()
    installed = list(query.installed())

    return _create_packages_structure(installed, query)


def load_packages_from_container_image(image):
    base = dnf.Base()
    
    data = subprocess.check_output(['podman', 'inspect', image])
    #size_bytes = json.loads(data)[0]["Size"]

    # Extract DNF and RPM data
    with tempfile.TemporaryDirectory() as tmp:
        cmd = "mkdir -p /workdir/var/lib && cp -r /var/lib/dnf /workdir/var/lib/ && cp -r /var/lib/rpm /workdir/var/lib/"
        subprocess.run(['podman', 'run', '--rm', '-v', tmp+':/workdir:z', '-v', 'copy.sh:/copy.sh:z', image, '/bin/sh', '-c', cmd])
        base.conf.installroot = tmp
        base.fill_sack()

    query = base.sack.query()
    installed = list(query.installed())

    return _create_packages_structure(installed, query)



def compute_graph(packages, groups=None):

    graph = {}

    for _, package in packages.items():
        node = {}
        name = package["name"]

        # When groups are used,
        # and 'package' is in any of those,
        # add those groups to the graph instead of the package.
        if groups:
            for group in groups:
                if name in group["packages"]:
                    node["name"] = group["name"]
                    node["size"] = group["size"]
                    node["type"] = "group"
                    node["dependencies"] = group["requires_resolved"]
                    node["weak_dependencies"] = list(set(group["recommends_resolved"]) | set(group["suggests_resolved"]))

                    graph[node["name"]] = node

        # Otherwise (package is not in a group) just add it to the graph
        if not node:
            node["name"] = package["name"]
            node["size"] = package["size"]
            node["type"] = "package"

            # Package -> Group relations
            #
            # If groups are involved, this package might depend on a package that's in a group.
            # If that's the case, the "dependencies" field needs to contain the name of that
            # group instead of a name of the package, because the package is not on the graph. The group is.
            if groups:
                pkg_deps_requires = set(package["requires_resolved"])
                pkg_deps_recommends = set(package["recommends_resolved"])
                pkg_deps_suggests = set(package["suggests_resolved"])

                for group in groups:
                    group_pkgs = set(group["packages"])

                    requires_in_group = pkg_deps_requires & group_pkgs
                    recommends_in_group = pkg_deps_recommends & group_pkgs
                    suggests_in_group = pkg_deps_suggests & group_pkgs

                    if requires_in_group:
                        pkg_deps_requires -= requires_in_group
                        pkg_deps_requires.add(group["name"])

                    if recommends_in_group:
                        pkg_deps_recommends -= recommends_in_group
                        pkg_deps_recommends.add(group["name"])

                    if suggests_in_group:
                        pkg_deps_suggests -= suggests_in_group
                        pkg_deps_suggests.add(group["name"])

                node["dependencies"] = list(pkg_deps_requires)
                node["weak_dependencies"] = list(pkg_deps_recommends | pkg_deps_suggests)

            # Package -> Package relations
            else:
                node["dependencies"] = package["requires_resolved"]
                node["weak_dependencies"] = list(set(package["recommends_resolved"]) | set(package["suggests_resolved"]))
            

            graph[node["name"]] = node

    return graph

        

def size(num, suffix='B'):
    for unit in ['','k','M','G']:
        if abs(num) < 1024.0:
            return "%3.1f %s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f %s%s" % (num, 'T', suffix)


def graph_to_dot(graph, sizes=False, highlights=None):
    if not highlights:
        highlights = []

    
    # Start of the graph
    dot = 'digraph packages {\n'

    # Formatting
    for _, node in graph.items():

        formatting = ['shape=none']

        # Showing sizes
        if sizes:
            formatting.append('label="{name} ({size})"'.format(name=node["name"], size=size(node["size"])))

        # Highlighting certain nodes
        if node["name"] in highlights:
            formatting.append('fontsize=22')
            formatting.append('fontcolor="#cc0066"')

        # Highlight groups
        if node["type"] == "group":
            formatting.append('shape=ellipse')

        # Print it
        if formatting:
            dot += '"{node}" [{formatting}];\n'.format(node=node["name"], formatting=",".join(formatting))

    # Hard dependencies
    for _, node in graph.items():
        dot += '"{node}" -> {{\n'.format(node=node["name"])
        for dep in node["dependencies"]:
            dot += '    "{dep}"\n'.format(dep=dep)
        dot += "};\n"

    # Weak dependencies
    for _, node in graph.items():
        dot += '"{node}" -> {{\n'.format(node=node["name"])
        for dep in node["weak_dependencies"]:
            dot += '    "{dep}"\n'.format(dep=dep)
        dot += '}[style=dashed];\n'

    dot += '}'

    return dot



def graph_to_package_list(graph, sizes=False):

    groups = []
    packages = []
    
    for _, node in graph.items():

        if node["type"] == "group":
            if sizes:
                groups.append('[ {name} ({size}) ]'.format(name=node["name"], size=size(node["size"])))
            else:
                groups.append('[ {name} ]'.format(name=node["name"]))

        else:
            if sizes:
                packages.append('{name} ({size})'.format(name=node["name"], size=size(node["size"])))
            else:
                packages.append(node["name"])

    groups.sort()
    packages.sort()

    output = groups + packages

    return output


def packages_to_group(name, packages):

    group = {}
    group["name"] = name
    group["size"] = 0

    group_packages = set()
    requires = set()
    requires_resolved = set()
    recommends = set()
    recommends_resolved = set()
    suggests = set()
    suggests_resolved = set()

    for _, package in packages.items():
        group_packages.add(package["name"])

        for req in package["requires"]:
            requires.add(req)

        for req_pkg in package["requires_resolved"]:
            requires_resolved.add(req_pkg)

        for req in package["recommends"]:
            recommends.add(req)

        for req_pkg in package["recommends_resolved"]:
            recommends_resolved.add(req_pkg)

        for req in package["suggests"]:
            suggests.add(req)

        for req_pkg in package["suggests_resolved"]:
            suggests_resolved.add(req_pkg)

        group["size"] += package["size"]

    group["packages"] = list(group_packages)
    group["requires"] = list(requires)
    group["requires_resolved"] = list(requires_resolved - group_packages)
    group["recommends"] = list(recommends)
    group["recommends_resolved"] = list(recommends_resolved - group_packages)
    group["suggests"] = list(suggests)
    group["suggests_resolved"] = list(suggests_resolved - group_packages)

    return group



def dump_data(path, data):
    with open(path, 'w') as file:
        json.dump(data, file)



def load_data(path):
    with open(path, 'r') as file:
        data = json.load(file)

    return data


def _add_javascript_to_svg(svg):
    javascript = """
<script type="text/javascript"><![CDATA[

document.addEventListener('click', function(e) {
      e = e || window.event;
      var target = e.target || e.srcElement;
      text = target.parentElement.querySelector("title").textContent;

      console.log("Clicked on: " + text);
      // reset all strokes
      var nodes = document.getElementsByClassName("edge");

      for (index = 0, len = nodes.length; index < len; ++index) {
          //var title = nodes[index].querySelector("title").textContent.split("->");
          id = nodes[index].id;
          document.getElementById(id).querySelector("path").setAttribute("stroke-width", "1");
          document.getElementById(id).querySelector("path").setAttribute("stroke-opacity", "0.5");
          document.getElementById(id).querySelector("path").setAttribute("stroke", "#444444");
          document.getElementById(id).querySelector("polygon").setAttribute("stroke-width", "1");
          document.getElementById(id).querySelector("polygon").setAttribute("stroke-opacity", "0.5");
          document.getElementById(id).querySelector("polygon").setAttribute("stroke", "#444444");
      }
      // reset text highlight
      pkgs = document.getElementsByClassName("node");
      for (index2 = 0, len2 = pkgs.length; index2 < len2; ++index2) {
          target_id = pkgs[index2].id;
          document.getElementById(target_id).querySelector("text").setAttribute("font-weight", "normal");
      }

      // highlight deps
      var nodes = document.getElementsByClassName("edge");
      for (index = 0, len = nodes.length; index < len; ++index) {
        var title = nodes[index].querySelector("title").textContent.split("->");

        if (title[0] == text) {
          id = nodes[index].id;
          //console.log("ID:   " + id);
          document.getElementById(id).querySelector("path").setAttribute("stroke-width", "3");
          document.getElementById(id).querySelector("path").setAttribute("stroke-opacity", "1");
          document.getElementById(id).querySelector("path").setAttribute("stroke", "#aa3333");
          document.getElementById(id).querySelector("polygon").setAttribute("stroke-width", "5");
          document.getElementById(id).querySelector("polygon").setAttribute("stroke-opacity", "1");
          document.getElementById(id).querySelector("polygon").setAttribute("stroke", "#aa3333");

        }
        if (title[1] == text) {
          id = nodes[index].id;
          document.getElementById(id).querySelector("path").setAttribute("stroke-width", "3");
          document.getElementById(id).querySelector("path").setAttribute("stroke-opacity", "1");
          document.getElementById(id).querySelector("path").setAttribute("stroke", "#333377");
          document.getElementById(id).querySelector("polygon").setAttribute("stroke-width", "5");
          document.getElementById(id).querySelector("polygon").setAttribute("stroke-opacity", "1");
          document.getElementById(id).querySelector("polygon").setAttribute("stroke", "#333377");
        }

        if (title[0] == text || title[1] == text) {
          pkgs = document.getElementsByClassName("node");
          for (index2 = 0, len2 = pkgs.length; index2 < len2; ++index2) {
            var pkg_name = pkgs[index2].querySelector("title").textContent;
            if (pkg_name == title[0] || pkg_name == title[1]) {
              target_id = pkgs[index2].id;
              document.getElementById(target_id).querySelector("text").setAttribute("font-weight", "bold");
            }
          }
        }
      }


  }, false);

]]></script>
"""

    return svg.split("</svg>")[0] + javascript + "\n</svg>\n"



def dot_to_graph_svg(dot):

    stage1 = subprocess.run(["sfdp", "-Gstart=3", "-Goverlap=prism"], capture_output=True, input=dot, encoding="UTF-8")

    stage2 = subprocess.run(["gvmap", "-e", "-d", "3"], capture_output=True, input=stage1.stdout, encoding="UTF-8")

    stage3 = subprocess.run(["neato", "-Gstart=3", "-n", "-Ecolor=#44444455", "-Tsvg"], capture_output=True, input=stage2.stdout, encoding="UTF-8")

    svg = str(stage3.stdout)

    return _add_javascript_to_svg(svg)


def dot_to_directed_graph_svg(dot):

    stage1 = subprocess.run(["dot", "-Tsvg"], capture_output=True, input=dot, encoding="UTF-8")

    svg = str(stage1.stdout)

    return _add_javascript_to_svg(svg)


def get_template():

    template = """
<style>
.in-base td {
    background-color: #e3ece1;
}
thead td, thead th {
    background-color: #bbb;
    text-align: center;
}
</style>

<h1> RPM packages in {{base.name}}
{% if images %}
    {% if images | length > 1 %}
        and {{images | length}} other installations </h1>
    {% else %}
        and {{images[0].name}} </h1>
    {% endif %}
{% else %}
    </h1>
{% endif %}

<p> Generated by <i>rpm-showme</i> available at <a href="https://pagure.io/minimization/rpm-showme">pagure.io/minimization/rpm-showme</a>

<hr>

<table>
    <thead>
        <tr>
            <td rowspan="3">All packages in this report</td>
            <td>Base installation</td>
            <td colspan="{{images | length}}">Other installations</td>
        </tr>
        <tr>
            <th>{{base.name}}</th>
            {% for image in images %}
            <th>{{image.name}}</th>
            {% endfor %}
        </tr>
        <tr>
            <td>{{base.size}}</td>
            {% for image in images %}
            <td>{{image.size}}</td>
            {% endfor %}
        </tr>
    </thead>
    <tbody>
        {% for pkg in base.packages %}
        <tr class="in-base">
            <td>{{pkg}}</td>
            <td>{{pkg}}</td>
            {% for image in images %}
            {% if pkg in image.pkgs_in_base %}
            <td>{{pkg}}</td>
            {% else %}
            <td> - </td>
            {% endif %}
            {% endfor %}
        </tr>
        {% endfor %}
        {% for pkg in extra_pkgs %}
        <tr>
            <td>{{pkg}}</td>
            <td> - </td>
            {% for image in images %}
            {% if pkg in image.packages %}
            <td>{{pkg}}</td>
            {% else %}
            <td> - </td>
            {% endif %}
            {% endfor %}
        </tr>
        {% endfor %}
    </tbody>
</table>
"""
    return template


def get_packages(name):
    # Is it a container image or a path?
    if ":" in name:
        # A container image!
        packages = load_packages_from_container_image(name)
    else:
        # A file path!
        packages = load_packages_from_path(name)

    return packages



def main():

    # Usage:
    #
    # $ showme feora:30 graph
    # $ showme / graph

    parser = argparse.ArgumentParser(description="Dependency visualisation of an RPM-based installation (a system, an image, etc.)", formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument("what", metavar="WHAT",
        help="""What you want to see. Accepts:
  - a filesystem path
  - a container image such as 'fedora:30'
""")
    parser.add_argument("how", metavar="HOW", choices=["graph", "directed-graph", "list", "report", "size"],
        help="""How you want to see it. Choose from:
  graph — Dependency graph with clustering.
  directed-graph — Simple dependency graph organized top to bottom.
  list — Basic list of packages.
  size — Just the total size of all packages.
  report — An HTML page comparing multiple installations.
""")
    parser.add_argument("where", nargs="?", metavar="WHERE", help="Filename of the output (stdout when not specified).")

    parser.add_argument("--group-container", action="append", nargs=2, metavar=("GRPUP_NAME", "CONTAINER"), help="Group packages in the given container into a single node to simplify the graph.\nUseful, for example, when visualizing changes on top of a base image.")

    parser.add_argument("--sizes", action="store_true", help="Show package sizes on the graph.")
    parser.add_argument("-H", "--highlight", action="append", help="Highlight specified nodes in the graph.")
    parser.add_argument("--add", action="append", nargs=2, metavar=("NAME", "WHAT"), help="Add more installation to the output. Currently only supported by 'report'.")
    parser.add_argument("--name", help="Name of the installation, only useful for 'report'")

    args = parser.parse_args()

    packages = get_packages(args.what)

    if args.group_container:
        groups = []

        for container in args.group_container:
            grp_name = container[0]
            grp_pkgs = load_packages_from_container_image(container[1])

            group = packages_to_group(grp_name, grp_pkgs)

            groups.append(group)

        graph = compute_graph(packages, groups)

    else:
        graph = compute_graph(packages)


    if args.how == "graph":
        dot = graph_to_dot(graph, sizes=args.sizes, highlights=args.highlight)
        output = dot_to_graph_svg(dot)

    if args.how == "directed-graph":
        dot = graph_to_dot(graph, sizes=args.sizes, highlights=args.highlight)
        output = dot_to_directed_graph_svg(dot)

    if args.how == "list":
        pkg_list = graph_to_package_list(graph, sizes=args.sizes)
        output = "\n".join(pkg_list)

    if args.how == "size":
        base_size = 0
        for _, pkg in graph.items():
            base_size += pkg["size"]

        output = size(base_size)

    if args.how == "report":

        base_pkg_list = graph_to_package_list(graph, sizes=args.sizes)

        base_size = 0
        for _, pkg in graph.items():
            base_size += pkg["size"]

        base_name = "Base installation"
        if args.name:
            base_name = args.name
        
        base = {
            "name": base_name,
            "size": size(base_size),
            "packages": base_pkg_list,
        }

        images = []
        if args.add:
            for installation in args.add:

                install_name = installation[0]
                install_what = installation[1]

                packages = get_packages(install_what)

                if args.group_container:
                    graph = compute_graph(packages, groups)
                else:
                    graph = compute_graph(packages)

                pkg_list = graph_to_package_list(graph, sizes=args.sizes)

                pkgs_in_base = list(set(base_pkg_list) & set(pkg_list))
                pkgs_not_in_base = list(set(pkg_list) - set(pkgs_in_base))
                pkgs_in_base.sort()
                pkgs_not_in_base.sort()

                this_size = 0
                for _, pkg in graph.items():
                    this_size += pkg["size"]

                image = {
                    "name" : install_name,
                    "size" : size(this_size),
                    "pkgs_in_base": pkgs_in_base,
                    "pkgs_not_in_base": pkgs_not_in_base,
                    "packages": pkg_list
                    }
                images.append(image)


        extra_pkgs = []
        for image in images:
            extra_pkgs += image["pkgs_not_in_base"]
        extra_pkgs = list(set(extra_pkgs))
        extra_pkgs.sort()


        template = jinja2.Template(get_template())
        output = template.render(base=base, images=images, extra_pkgs=extra_pkgs)


    if args.where:
        with open(args.where, "w") as outfile:
            outfile.write(output)
    else:
        print (output)


if __name__ == "__main__":
    main()







