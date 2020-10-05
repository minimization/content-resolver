#!/usr/bin/python3

import os
import feedback_pipeline

MAX_LEVEL = 10


def pkg_id_to_name(pkg_id):
    pkg_name = pkg_id.rsplit("-",2)[0]
    return pkg_name

def get_query(data_file):
    data = feedback_pipeline.load_data(data_file)
    query = feedback_pipeline.Query(data["data"], data["configs"], data["settings"])
    return query


def _init_pkg_structure(name):

    pkg = {}
    pkg["id"] = name

    # 0 is required
    # 1 is a direct dependency of a required package
    # 2 is being 2 hops from a required
    # ... 
    for level in range(0, MAX_LEVEL + 1):
        level_name = "level{num}".format(num=str(level))
        pkg[level_name] = {}
        pkg[level_name]["workload_conf_ids"] = set()
        pkg[level_name]["maintainers"] = set()
    
    return pkg


def process_data():
    print("Loading data...")
    query = get_query("/data/data.json")

    print("Doing things...")

    pkg_entries = {}

    workload_ids = query.workloads_in_view("view-eln", None)

    for workload_id in workload_ids:
        workload = query.data["workloads"][workload_id]
        workload_conf_id = workload["workload_conf_id"]
        workload_conf = query.configs["workloads"][workload_conf_id]
        workload_maintainer = workload_conf["maintainer"]
        
        pkgs = query.workload_pkgs_id(workload_id)
        pkg_relations = workload["pkg_relations"]

        # Packages on level 1 == required
        level1_pkg_ids = set()

        # This will initially hold all packages.
        # When figuring out levels, I'll process each package just once.
        # And for that I'll be removing them from this set as I go.
        remaining_pkg_ids = set()
        
        for pkg in pkgs:
            pkg_id = pkg["id"]

            remaining_pkg_ids.add(pkg_id)

            if pkg_id not in pkg_entries:
                pkg_entry = _init_pkg_structure(pkg_id)
                pkg_entries[pkg_id] = pkg_entry
    
            # Is this package level 1?
            if workload_id in pkg["q_required_in"]:
                pkg_entries[pkg_id]["level0"]["workload_conf_ids"].add(workload_conf_id)
                pkg_entries[pkg_id]["level0"]["maintainers"].add(workload_maintainer)
                level1_pkg_ids.add(pkg_id)
                remaining_pkg_ids.remove(pkg_id)
        

        # Initialize sets for all levels
        pkg_ids_level = []
        #0
        pkg_ids_level.append(level1_pkg_ids)

        #print("All:   : {}".format(len(pkgs)))
        #print("Level 1: {}".format(len(level1_pkg_ids)))
        #print("Remains: {}".format(len(remaining_pkg_ids)))
        #print("")

        # Starting at level 2, because level 1 is already done (that's required packages)
        for current_level in range(1, MAX_LEVEL + 1):

            #1..
            pkg_ids_level.append(set())

            for pkg_id in remaining_pkg_ids.copy():
                pkg = pkg_entries[pkg_id]

                # is pkg required by higher_pkg_id (which is level 1)?
                # (== is higher_pkg_id in a list of packages that pkg is required by?)
                # then pkg is level 2
                for higher_pkg_id in pkg_ids_level[current_level - 1]:
                    if higher_pkg_id in pkg_relations[pkg_id]["required_by"]:
                        pkg_ids_level[current_level].add(pkg_id)
                        try:
                            remaining_pkg_ids.remove(pkg_id)
                        except KeyError:
                            pass
            
            #print("Level {}: {}".format(current_level, len(pkg_ids_level[current_level])))
            #print("Remains: {}".format(len(remaining_pkg_ids)))
            #print("")
        
        # Some might remain for weird reasons
        for pkg_id in remaining_pkg_ids:
            pkg_ids_level[MAX_LEVEL].add(pkg_id)


        for current_level in range(0, MAX_LEVEL + 1):
            #print("Level {level}: {count}".format(
            #    level=current_level,
            #    count=len(pkg_ids_level[current_level])
            #))
            
            
            level_name = "level{num}".format(num=str(current_level))


            for pkg_id in pkg_entries:
                pkg = pkg_entries[pkg_id]

                if pkg_id in pkg_ids_level[current_level]:

                    pkg_entries[pkg_id][level_name]["workload_conf_ids"].add(workload_conf_id)
                    pkg_entries[pkg_id][level_name]["maintainers"].add(workload_maintainer)


    print("Done!")

    return pkg_entries

def main():

    pkg_entries = process_data()

    #feedback_pipeline.dump_data("pkg_entries.json", pkg_entries)
    #pkg_entries = feedback_pipeline.load_data("pkg_entries.json")


    #pkg = "json-c"
    #
    #for pkg_id in pkg_entries:
    #    pkg_name = pkg_id_to_name(pkg_id)
    #    if pkg_name == pkg:
    #        print(pkg_id)


    pkg = pkg_entries["json-c-0.14-6.eln102.x86_64"]

    print("")
    print("")

    for current_level in range(0, MAX_LEVEL + 1):
        level_name = "level{num}".format(num=str(current_level))

        print("")
        if current_level is 0:
            print(level_name + (" (required)"))
        else:
            print(level_name)
        print("==========================")
        for workload_conf_id in sorted(pkg[level_name]["workload_conf_ids"]):
            print(workload_conf_id)




    #print("Done!")




    


if __name__ == "__main__":
    main()