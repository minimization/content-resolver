#!/usr/bin/python3

import os
import feedback_pipeline
import pprint

def ppr(stuff):
    pp = pprint.PrettyPrinter(indent=4)
    pp.pprint(stuff)



def log(msg):
    print(msg)
    
def pkg_id_to_name(pkg_id):
    pkg_name = pkg_id.rsplit("-",2)[0]
    return pkg_name

def get_query(data_file):
    data = feedback_pipeline.load_data(data_file)
    query = feedback_pipeline.Query(data["data"], data["configs"], data["settings"])
    return query

class OwnershipEngine:
    def __init__(self, query):
        self.query = query
        self.MAX_LEVEL = 9
        self.MAX_LAYER = 9
    
    # Levels:
    #
    #  
    # level0 == required
    # ---
    # level1 == 1st level runtime dep
    # ...
    # level9 == 9th level runtime dep
    #
    #  
    # level10 == build dep of something in the previous group
    # --- 
    # level11 == 1st level runtime dep 
    # ...
    # level19 == 9th level runtime dep
    #
    #  
    # level20 == build dep of something in the previous group
    # level21 == 1st level runtime dep 
    # ...
    # level29 == 9th level runtime dep
    #
    # etc. up to level99

    
    def process_view(self, view_conf_id):
        self._initiate_view(view_conf_id)

        self._process_layer_zero_entries()
        


        return self.component_maintainers

    
    def _initiate_view(self, view_conf_id):
        self.view_conf_id = view_conf_id
        self.all_arches = self.query.arches_in_view(view_conf_id)

        self.workload_ids = self.query.workloads_in_view(view_conf_id, None)

        self.pkg_entries = {}
        self.srpm_entries = {}
        self.component_maintainers = {}

        self.runtime_rpm_names = set()
        self.runtime_srpm_names = set()

        self.buildroot_rpm_names = set()
        self.buildroot_srpm_names = set()

        self.buildroot_only_rpm_names = set()
        self.buildroot_only_srpm_names = set()

        self.all_rpm_names = set()
        self.all_srpm_names = set()

        self.buildroot_pkgs = {}
        # {
        #   "RPM_NAME": {
        #       "source_name": "SRPM_NAME",
        #       "required_by": set(
        #           "RPM_NAME", 
        #           "RPM_NAME", 
        #       ),
        #       "required_by_srpms": set(
        #           "SRPM_NAME",
        #           "SRPM_NAME",
        #       ),
        #   } 
        # }


        ### Initiate: self.runtime_rpm_names
        for arch in self.all_arches:
            self.runtime_rpm_names.update(self.query.pkgs_in_view(view_conf_id, arch, output_change="binary_names"))


        ### Initiate: self.runtime_srpm_names
        for arch in self.all_arches:
            self.runtime_srpm_names.update(self.query.pkgs_in_view(view_conf_id, arch, output_change="source_names"))
        

        ### Initiate: self.buildroot_pkgs
        build_dependencies = {}
        for arch in self.all_arches:
            for pkg_name, pkg_data in self.query.view_buildroot_pkgs(view_conf_id, arch).items():
                if pkg_name not in build_dependencies:
                    build_dependencies[pkg_name] = {}
                    build_dependencies[pkg_name]["required_by"] = set()
                build_dependencies[pkg_name]["required_by"] = build_dependencies[pkg_name]["required_by"].union(pkg_data["required_by"])
        
        buildroot_pkg_relations = {}
        for buildroot_pkg_relations_conf_id, buildroot_pkg_relations_conf in self.query.configs["buildroot_pkg_relations"].items():
            if view_conf_id == buildroot_pkg_relations_conf["view_id"]:
                arch = buildroot_pkg_relations_conf["arch"]
                arch_buildroot_pkg_relations = buildroot_pkg_relations_conf["pkg_relations"]

                for pkg_id, pkg_data in arch_buildroot_pkg_relations.items():
                    pkg_name = pkg_id_to_name(pkg_id)
                    if pkg_name not in buildroot_pkg_relations:
                        buildroot_pkg_relations[pkg_name] = {}
                        buildroot_pkg_relations[pkg_name]["source_name"] = pkg_data["source_name"]
                        buildroot_pkg_relations[pkg_name]["required_by"] = set()
                    for required_by_pkg_id in pkg_data["required_by"]:
                        required_by_pkg_name = pkg_id_to_name(required_by_pkg_id)
                        buildroot_pkg_relations[pkg_name]["required_by"].add(required_by_pkg_name)

        for pkg_name, pkg in buildroot_pkg_relations.items():
            if pkg_name not in build_dependencies:
                continue
            
            self.buildroot_pkgs[pkg_name] = {}
            self.buildroot_pkgs[pkg_name]["source_name"] = pkg["source_name"]
            self.buildroot_pkgs[pkg_name]["required_by"] = pkg["required_by"]
            self.buildroot_pkgs[pkg_name]["required_by_srpms"] = build_dependencies[pkg_name]["required_by"]


        ### Initiate: self.buildroot_srpm_names
        for pkg_name, pkg in self.buildroot_pkgs.items():
            self.buildroot_srpm_names.add(pkg["source_name"])
        

        ### Initiate: self.buildroot_rpm_names
        self.buildroot_rpm_names = set(self.buildroot_pkgs.keys())
        

        ### Initiate: Other lists
        self.all_rpm_names = self.runtime_rpm_names.union(self.buildroot_rpm_names)
        self.all_srpm_names = self.runtime_srpm_names.union(self.buildroot_srpm_names)
        self.buildroot_only_rpm_names = self.buildroot_rpm_names.difference(self.runtime_rpm_names)
        self.buildroot_only_srpm_names = self.buildroot_srpm_names.difference(self.runtime_srpm_names)


        ### Initiate: self.pkg_entries
        for pkg_name in self.all_rpm_names:
            self.pkg_entries[pkg_name] = {}
            self.pkg_entries[pkg_name]["name"] = pkg_name
            for layer in range(0, self.MAX_LAYER + 1):
                for level in range(0, self.MAX_LEVEL + 1):
                    if layer == 0:
                        level_name = "level{}".format(level)
                        self.pkg_entries[pkg_name][level_name] = {}
                        self.pkg_entries[pkg_name][level_name]["workload_conf_ids"] = set()
                    else:
                        level_name = "level{}{}".format(layer, level)
                        self.pkg_entries[pkg_name][level_name] = {}
                        self.pkg_entries[pkg_name][level_name]["build_source_names"] = set()
        

        ### Initiate: self.srpm_entries
        for srpm_name in self.all_srpm_names:
            self.srpm_entries[srpm_name] = {}
            self.srpm_entries[srpm_name]["ownership"] = {}
            for layer in range(0, self.MAX_LAYER + 1):
                for level in range(0, self.MAX_LEVEL + 1):
                    if layer == 0:
                        level_name = "level{}".format(level)
                        self.srpm_entries[srpm_name]["ownership"][level_name] = {}
                    else:
                        level_name = "level{}{}".format(layer, level)
                        self.srpm_entries[srpm_name]["ownership"][level_name] = {}


        ### Initiate: self.component_maintainers
        for srpm_name in self.all_srpm_names:
            self.component_maintainers[srpm_name] = {}
            self.component_maintainers[srpm_name]["all"] = {}
            self.component_maintainers[srpm_name]["top"] = None



    def _pkg_relations_ids_to_names(self, pkg_relations):
        if not pkg_relations:
            return pkg_relations
        
        pkg_relations_names = {}

        for pkg_id, pkg in pkg_relations.items():
            pkg_name = pkg_id_to_name(pkg_id)

            pkg_relations_names[pkg_name] = {}
            pkg_relations_names[pkg_name]["required_by"] = set()

            for required_by_pkg_id in pkg["required_by"]:
                required_by_pkg_name = pkg_id_to_name(required_by_pkg_id)
                pkg_relations_names[pkg_name]["required_by"].add(required_by_pkg_name)
        
        return pkg_relations_names


    def _process_layer_zero_entries(self):
        # This is first done on an RPM level. Starts with level 0 == required,
        # assigns them based on who required them. Then moves on to level 1 == 1st
        # level depepdencies, and assigns them based on who pulled them in in
        # the above layer. And it goes deeper and deeper until MAX_LEVEL.
        # 
        # The second part of the function then takes this data from RPMs and
        # copies them over to their SRPMs. When multiple RPMs belong to a single
        # SRPM, it merges it.
        # 

        # 
        # Part 1: RPMs
        #  
        
        log("Generating ownership recommendation data...")

        workload_ids = self.query.workloads_in_view(self.view_conf_id, None)

        for workload_id in workload_ids:
            workload = self.query.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.query.configs["workloads"][workload_conf_id]
            workload_maintainer = workload_conf["maintainer"]
            
            pkgs = self.query.workload_pkgs_id(workload_id)
            pkg_relations_ids = workload["pkg_relations"]

            pkg_relations = self._pkg_relations_ids_to_names(pkg_relations_ids)


            # Packages on level 0 == required
            level0_pkg_names = set()

            # This will initially hold all packages.
            # When figuring out levels, I'll process each package just once.
            # And for that I'll be removing them from this set as I go.
            remaining_pkg_names = set()
            
            for pkg in pkgs:
                pkg_name = pkg["name"]

                remaining_pkg_names.add(pkg_name)

                if "source_name" not in self.pkg_entries[pkg_name]:
                    self.pkg_entries[pkg_name]["source_name"] = pkg["source_name"]
        
                # Is this package level 1?
                if workload_id in pkg["q_required_in"]:
                    self.pkg_entries[pkg_name]["level0"]["workload_conf_ids"].add(workload_conf_id)
                    level0_pkg_names.add(pkg_name)
                    remaining_pkg_names.remove(pkg_name)
            

            # Initialize sets for all levels
            pkg_names_level = []
            pkg_names_level.append(level0_pkg_names)

            # Starting at level 1, because level 0 is already done (that's required packages)
            for current_level in range(1, self.MAX_LEVEL + 1):

                #1..
                pkg_names_level.append(set())

                for pkg_name in remaining_pkg_names.copy():
                    pkg = self.pkg_entries[pkg_name]

                    # is pkg required by higher_pkg_name (which is level 1)?
                    # (== is higher_pkg_name in a list of packages that pkg is required by?)
                    # then pkg is level 2
                    for higher_pkg_name in pkg_names_level[current_level - 1]:
                        if higher_pkg_name in pkg_relations[pkg_name]["required_by"]:
                            pkg_names_level[current_level].add(pkg_name)
                            try:
                                remaining_pkg_names.remove(pkg_name)
                            except KeyError:
                                pass
            
            # Some might remain for weird reasons
            for pkg_name in remaining_pkg_names:
                pkg_names_level[self.MAX_LEVEL].add(pkg_name)


            for current_level in range(0, self.MAX_LEVEL + 1):
                level_name = "level{num}".format(num=str(current_level))

                for pkg_name in self.pkg_entries:
                    pkg = self.pkg_entries[pkg_name]

                    if pkg_name in pkg_names_level[current_level]:

                        self.pkg_entries[pkg_name][level_name]["workload_conf_ids"].add(workload_conf_id)

        # 
        # Part 2: SRPMs
        # 

        log("   Sorting pkgs per SRPM...")

        for pkg_name, pkg in self.pkg_entries.items():
            if "source_name" not in pkg:
                continue

            source_name = pkg["source_name"]

            for current_level in range(0, self.MAX_LEVEL + 1):
                level_name = "level{num}".format(num=str(current_level))
                
                for workload_conf_id in pkg[level_name]["workload_conf_ids"]:
                    maintainer = self.query.configs["workloads"][workload_conf_id]["maintainer"]
                    if maintainer not in self.srpm_entries[source_name]["ownership"][level_name]:
                        self.srpm_entries[source_name]["ownership"][level_name][maintainer] = {}
                        self.srpm_entries[source_name]["ownership"][level_name][maintainer]["workloads"] = {}
                        self.srpm_entries[source_name]["ownership"][level_name][maintainer]["pkg_count"] = 0
                    
                    self.srpm_entries[source_name]["ownership"][level_name][maintainer]["pkg_count"] += 1
                    self.srpm_entries[source_name]["ownership"][level_name][maintainer]["workloads"][workload_conf_id] = pkg_name

        #
        # Part 3: one owner
        # 

        #skipped_maintainers = self.configs["views"][view_conf_id]["maintainer_recommendation"]["skipped_maintainers"]
        skipped_maintainers = ["bakery", "jwboyer", "asamalik"]

        clear_components = set()
        unclear_components = set()

        for component_name, owner_data in self.srpm_entries.items():

            found = False
            maintainers = {}
            top_maintainer = None

            for level_name, level_data in owner_data["ownership"].items():
                if found:
                    break

                if not level_data:
                    continue

                for maintainer, maintainer_data in level_data.items():

                    if maintainer in skipped_maintainers:
                        continue

                    found = True
                    
                    maintainers[maintainer] = maintainer_data["pkg_count"]
            
            # Find a maintainer with the highest score
            maintainer_scores = {}
            for maintainer, score in maintainers.items():
                if score not in maintainer_scores:
                    maintainer_scores[score] = set()
                maintainer_scores[score].add(maintainer)
            for score in sorted(maintainer_scores, reverse=True):
                if len(maintainer_scores[score]) == 1:
                    for chosen_maintainer in maintainer_scores[score]:
                        top_maintainer = chosen_maintainer
                    break
                    
            
            self.component_maintainers[component_name] = {}
            self.component_maintainers[component_name]["all"] = maintainers
            self.component_maintainers[component_name]["top"] = top_maintainer



def main():

    print("Loading data...")
    query = get_query("/dnf_cachedir/data.json")

    view_conf_id = "view-eln"

    ownership_engine = OwnershipEngine(query)
    component_maintainers = ownership_engine.process_view(view_conf_id)

    for component_name, component in component_maintainers.items():
        print(component_name)
        print("  {}".format(component["top"]))
        print("")



if __name__ == "__main__":
    main()